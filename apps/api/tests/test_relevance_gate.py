"""Tests for the relevance_gate LangGraph node: scores aggregated search_worker
results against the research target before paying for read/classify_claims/
expand_source_graph/bind, retries once with a narrower query if below
threshold, and skips straight toward synthesis with a gap-only EvidencePack
if still insufficient after the retry.

Covers:
  - research_relevance.score_search_relevance() -- the LLM relevance judgment,
    including its fail-open behavior on any error
  - research_relevance.reformulate_queries_for_exact_match() -- the one-retry
    query-narrowing step
  - langgraph_runtime.nodes.relevance_gate() -- node-level: sufficient (no
    retry), insufficient-then-recovers (retry succeeds), still-insufficient
    (retry doesn't help -> gap EvidencePack + insufficient_relevant_evidence),
    no-tools (scoring only, no retry attempted)
  - langgraph_runtime.graph._relevance_gate_router() -- the conditional edge
  - End-to-end: a full run where relevance stays low routes around
    rank/read/classify_claims/bind entirely
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.services.agent import model_client
from app.services.agent.langgraph_runtime.graph import _relevance_gate_router, run_stub_graph
from app.services.agent.langgraph_runtime.nodes import NODE_ORDER, relevance_gate
from app.services.agent.langgraph_runtime.state import ResearchGraphState
from app.services.agent.models import Source, TurnRequest
from app.services.agent.research_models import (
    CoverageContract,
    EvidencePack,
    ResearchPlan,
    SearchWorkerPlan,
)
from app.services.agent.research_relevance import (
    RELEVANCE_GATE_PROMPT,
    RELEVANCE_THRESHOLD,
    RelevanceAssessment,
    reformulate_queries_for_exact_match,
    score_search_relevance,
)

from test_agent_runtime import FakeTools

TZ = "America/New_York"
REQUEST = TurnRequest(message="Research the JAVAH modernization project", user_timezone=TZ)


def _sources(*, on_topic: int = 0, off_topic: int = 0) -> list[Source]:
    result = [
        Source(title=f"JAVAH modernization update {i}", url=f"https://example.com/javah-{i}", snippet="Details about the JAVAH project.")
        for i in range(on_topic)
    ]
    result += [
        Source(title=f"javah JDK tool reference {i}", url=f"https://docs.oracle.com/javah-{i}", snippet="Unrelated JDK tool documentation.")
        for i in range(off_topic)
    ]
    return result


# ---------------------------------------------------------------------------
# score_search_relevance()
# ---------------------------------------------------------------------------

def test_score_search_relevance_no_sources_returns_zero_without_model_call(monkeypatch):
    called = False

    def _fake_complete(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("should not be called")

    monkeypatch.setattr(model_client, "complete", _fake_complete)

    assessment = score_search_relevance([], "JAVAH", REQUEST)

    assert assessment.relevance_fraction == 0.0
    assert called is False


def test_score_search_relevance_no_target_returns_one_without_model_call(monkeypatch):
    called = False

    def _fake_complete(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("should not be called")

    monkeypatch.setattr(model_client, "complete", _fake_complete)

    assessment = score_search_relevance(_sources(on_topic=2), "", REQUEST)

    assert assessment.relevance_fraction == 1.0
    assert called is False


def test_score_search_relevance_uses_model_output(monkeypatch):
    def _fake_complete(messages, **kwargs):
        assert kwargs.get("role") == "relevance_gate"
        payload = json.loads(messages[1]["content"])
        assert payload["target"] == "JAVAH"
        assert len(payload["results"]) == 3
        return SimpleNamespace(text=json.dumps({"relevance_fraction": 0.2, "reasoning": "mostly unrelated JDK tool hits"}), model_used="test", latency_ms=1, cost_usd=0.002)

    monkeypatch.setattr(model_client, "complete", _fake_complete)

    assessment = score_search_relevance(_sources(on_topic=1, off_topic=2), "JAVAH", REQUEST)

    assert assessment.relevance_fraction == 0.2
    assert assessment.reasoning == "mostly unrelated JDK tool hits"
    assert assessment.model_calls_made == 1
    assert assessment.cost_usd == 0.002
    assert assessment.sufficient is False


def test_score_search_relevance_clamps_out_of_range_fraction(monkeypatch):
    def _fake_complete(messages, **kwargs):
        return SimpleNamespace(text=json.dumps({"relevance_fraction": 1.7}), model_used="test", latency_ms=1, cost_usd=0.0)

    monkeypatch.setattr(model_client, "complete", _fake_complete)

    assessment = score_search_relevance(_sources(on_topic=1), "JAVAH", REQUEST)

    assert assessment.relevance_fraction == 1.0


def test_score_search_relevance_fails_open_on_malformed_json(monkeypatch):
    def _fake_complete(messages, **kwargs):
        return SimpleNamespace(text="not json", model_used="test", latency_ms=1, cost_usd=0.0)

    monkeypatch.setattr(model_client, "complete", _fake_complete)

    assessment = score_search_relevance(_sources(on_topic=1), "JAVAH", REQUEST)

    assert assessment.sufficient is True
    assert assessment.model_calls_made == 0


def test_score_search_relevance_fails_open_on_model_error(monkeypatch):
    def _fake_complete(messages, **kwargs):
        raise RuntimeError("provider timeout")

    monkeypatch.setattr(model_client, "complete", _fake_complete)

    assessment = score_search_relevance(_sources(on_topic=1), "JAVAH", REQUEST)

    assert assessment.sufficient is True
    assert assessment.model_calls_made == 0
    assert assessment.cost_usd == 0.0


def test_relevance_gate_prompt_asks_for_a_fraction_and_reasoning():
    assert "relevance_fraction" in RELEVANCE_GATE_PROMPT


# ---------------------------------------------------------------------------
# reformulate_queries_for_exact_match()
# ---------------------------------------------------------------------------

def test_reformulate_queries_adds_quoted_target():
    workers = [SearchWorkerPlan(question="q", query="javah modernization status")]

    reformulated = reformulate_queries_for_exact_match(workers, "JAVAH")

    assert reformulated[0].query == '"JAVAH" javah modernization status'


def test_reformulate_queries_does_not_duplicate_existing_quoted_target():
    workers = [SearchWorkerPlan(question="q", query='"JAVAH" modernization status')]

    reformulated = reformulate_queries_for_exact_match(workers, "JAVAH")

    assert reformulated[0].query == '"JAVAH" modernization status'


def test_reformulate_queries_is_noop_without_target():
    workers = [SearchWorkerPlan(question="q", query="javah modernization status")]

    assert reformulate_queries_for_exact_match(workers, "") == workers


# ---------------------------------------------------------------------------
# nodes.relevance_gate() -- node-level, direct calls
# ---------------------------------------------------------------------------

def _state(**overrides) -> ResearchGraphState:
    base: ResearchGraphState = {
        "visited_nodes": ["brief", "subject_derivation", "contract", "plan", "dispatch_search"],
        "artifacts": {},
        "sources": _sources(on_topic=2),
        "worker_reports": [],
        "contract": CoverageContract(subjects=["JAVAH"], dimensions=["status"], cells=[], source="test"),
        "plan": ResearchPlan(workers=[SearchWorkerPlan(question="q", query="javah status")]),
    }
    base.update(overrides)
    return base


def test_relevance_gate_sufficient_score_does_not_retry(monkeypatch):
    monkeypatch.setattr(model_client, "complete", lambda *a, **k: SimpleNamespace(text=json.dumps({"relevance_fraction": 0.9}), model_used="t", latency_ms=1, cost_usd=0.001))

    result = relevance_gate(_state(), run_id="rg-test", request=REQUEST, tools=FakeTools())

    assert result["insufficient_relevant_evidence"] is False
    assert "evidence" not in result
    assert result["sources"] == []  # no retry -> no extra sources delta
    assert "relevance_gate" in result["visited_nodes"]
    assert result["artifacts"]["relevance_gate"]["retried"] is False


def test_relevance_gate_low_score_retries_and_recovers(monkeypatch):
    calls = {"n": 0}

    def _fake_complete(messages, **kwargs):
        calls["n"] += 1
        fraction = 0.1 if calls["n"] == 1 else 0.9
        return SimpleNamespace(text=json.dumps({"relevance_fraction": fraction}), model_used="t", latency_ms=1, cost_usd=0.001)

    monkeypatch.setattr(model_client, "complete", _fake_complete)

    result = relevance_gate(_state(), run_id="rg-test", request=REQUEST, tools=FakeTools())

    assert calls["n"] == 2
    assert result["insufficient_relevant_evidence"] is False
    assert "evidence" not in result
    assert result["sources"], "expected the retry's sources as the returned delta"
    assert result["artifacts"]["relevance_gate"]["retried"] is True


def test_relevance_gate_still_insufficient_after_retry_sets_gap_evidence(monkeypatch):
    monkeypatch.setattr(model_client, "complete", lambda *a, **k: SimpleNamespace(text=json.dumps({"relevance_fraction": 0.1}), model_used="t", latency_ms=1, cost_usd=0.001))

    result = relevance_gate(_state(), run_id="rg-test", request=REQUEST, tools=FakeTools())

    assert result["insufficient_relevant_evidence"] is True
    assert isinstance(result["evidence"], EvidencePack)
    assert result["evidence"].items == []
    assert result["evidence"].gaps
    assert "JAVAH" in result["evidence"].gaps[0]
    assert result["artifacts"]["relevance_gate"]["retried"] is True


def test_relevance_gate_no_tools_skips_retry_but_still_gates(monkeypatch):
    monkeypatch.setattr(model_client, "complete", lambda *a, **k: SimpleNamespace(text=json.dumps({"relevance_fraction": 0.1}), model_used="t", latency_ms=1, cost_usd=0.001))

    result = relevance_gate(_state(), run_id="rg-test", request=REQUEST, tools=None)

    assert result["insufficient_relevant_evidence"] is True
    assert result["artifacts"]["relevance_gate"]["retried"] is False
    assert result["tool_calls_made"] == 0


def test_relevance_gate_emits_progress_event_with_score_and_retry_flag(monkeypatch):
    monkeypatch.setattr(model_client, "complete", lambda *a, **k: SimpleNamespace(text=json.dumps({"relevance_fraction": 0.1}), model_used="t", latency_ms=1, cost_usd=0.001))
    events = []

    def _progress(stage, message, **data):
        events.append((stage, message, data))

    relevance_gate(_state(), run_id="rg-test", request=REQUEST, tools=FakeTools(), progress=_progress)

    relevance_events = [e for e in events if e[0] == "relevance_gate"]
    assert relevance_events, "expected at least one relevance_gate progress event"
    assert any("relevance_score" in e[2] for e in relevance_events)
    assert any(e[2].get("retried") is True for e in relevance_events)


def test_relevance_gate_target_prefers_contract_subjects():
    from app.services.agent.langgraph_runtime.nodes import _relevance_gate_target

    state = _state(
        contract=CoverageContract(subjects=["Corebridge Financial", "JAVAH"], dimensions=["status"], cells=[], source="test"),
        named_subjects=["something else"],
    )

    assert _relevance_gate_target(state) == "Corebridge Financial, JAVAH"


def test_relevance_gate_target_falls_back_to_named_subjects_then_brief():
    from app.services.agent.langgraph_runtime.nodes import _relevance_gate_target
    from app.services.agent.research_models import ResearchBrief

    no_contract = _state(contract=None, named_subjects=["Corebridge Financial"])
    assert _relevance_gate_target(no_contract) == "Corebridge Financial"

    only_brief = _state(contract=None, named_subjects=[], brief=ResearchBrief(objective="Assess JAVAH modernization"))
    assert _relevance_gate_target(only_brief) == "Assess JAVAH modernization"

    nothing = _state(contract=None, named_subjects=[])
    assert _relevance_gate_target(nothing) == ""


# ---------------------------------------------------------------------------
# graph._relevance_gate_router()
# ---------------------------------------------------------------------------

def test_relevance_gate_router_continue_when_sufficient():
    assert _relevance_gate_router({"insufficient_relevant_evidence": False}) == "continue"


def test_relevance_gate_router_insufficient_when_flagged():
    assert _relevance_gate_router({"insufficient_relevant_evidence": True}) == "insufficient"


def test_relevance_gate_is_registered_between_search_worker_and_rank():
    assert list(NODE_ORDER).index("search_worker") + 1 == list(NODE_ORDER).index("relevance_gate")
    assert list(NODE_ORDER).index("relevance_gate") + 1 == list(NODE_ORDER).index("rank")


# ---------------------------------------------------------------------------
# End-to-end: insufficient relevance skips rank/read/classify_claims/bind
# ---------------------------------------------------------------------------

def test_relevance_gate_insufficient_evidence_skips_expensive_stages_end_to_end(monkeypatch):
    def _fake_complete(messages, *, role=None, **kwargs):
        if role == "relevance_gate":
            return SimpleNamespace(text=json.dumps({"relevance_fraction": 0.05, "reasoning": "off-topic"}), model_used="test", latency_ms=1, cost_usd=0.0)
        return SimpleNamespace(text="# Answer\n\nInsufficient relevant evidence was found.", model_used="test", latency_ms=1, cost_usd=0.0)

    monkeypatch.setattr(model_client, "complete", _fake_complete)

    result = run_stub_graph(
        {"request_message": "Research the JAVAH project", "visited_nodes": [], "artifacts": {}},
        run_id="relevance-gate-e2e-test",
        request=TurnRequest(message="Research the JAVAH project"),
        tools=FakeTools(),
    )

    visited = result.get("visited_nodes", [])
    assert "relevance_gate" in visited
    assert "rank" not in visited
    assert "read" not in visited
    assert "classify_claims" not in visited
    assert "bind" not in visited
    assert "synthesize" in visited

    evidence = result.get("evidence")
    assert evidence is not None
    assert evidence.items == []
    assert evidence.gaps
    assert result.get("answer")
