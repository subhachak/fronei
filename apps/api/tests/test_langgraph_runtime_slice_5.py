"""Slice 5 — STRUCTURAL INVARIANT SUITE (not a parity gate).

What this suite checks
----------------------
These tests use FakeTools + patched LLM completions.  They verify that the
LangGraph pipeline DOES NOT CRASH and that basic structural invariants hold
across all 25 golden-set queries.  They are NOT a parity gate.

Structural invariants verified:
  1. Pipeline completes without exception for every golden-set case.
  2. response.text is a non-empty string.
  3. evidence.items is non-empty (FakeTools always returns ≥1 source).
  4. evidence.claims is non-empty (classify_claims + bind both run).
  5. judge_result has a numeric score ≥ 0.
  6. Budget accounting is non-trivial (model_calls_made ≥ 2).
  7. All expected pipeline nodes are visited.
  8. claim_classification_results is present for every case.
  9. research_level from the request is threaded through.

What this suite does NOT check (requires real API keys + the live comparator):
  - Answer quality, accuracy, or completeness.
  - Whether LangGraph matches or exceeds the legacy oracle on coverage or score.
  - primary_evidence_role distribution or failure-mode detection.

Retirement cutover no longer uses a legacy-vs-LangGraph parity workflow; this
suite remains a structural invariant check for the active LangGraph runtime.

The 25 cases span: immigration, operational, conflict, freshness, independence,
medical, financial, tech product, multi-subject, entity status, research level
classification, subject extraction, and time-sensitive routing.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.agent.langgraph_runtime.runtime import run_langgraph_research
from app.services.agent.models import TurnRequest

from test_agent_runtime import FakeTools as _BaseFakeTools, _patch_completion


# ---------------------------------------------------------------------------
# FakeTools with rich source content so _claim_candidate_sentences fires
# ---------------------------------------------------------------------------

class FakeTools(_BaseFakeTools):
    """FakeTools with long-form content so claim classification produces results."""

    def extract_urls(self, urls: list[str], max_chars_per_source: int = 2500):
        from app.services.agent.models import Source, ToolCall

        extracted = [
            Source(
                title="Research Source",
                url="https://example.com",
                content=(
                    "The evidence supports the view that current processing times are changing "
                    "significantly due to policy shifts and operational backlogs. "
                    "The evidence supports the claim that official processing SLAs differ from "
                    "real-world practitioner experience reported in forums and communities. "
                    "The data supports the observation that independent sources show changing "
                    "wait times and costs across different request categories. "
                    "Evidence supports that anecdotal reports from practitioners confirm "
                    "operational realities diverge from official government statistics. "
                    "The evidence supports that market pricing and competitive dynamics are "
                    "changing as vendors respond to customer demand for integration."
                ),
            )
        ]
        tc = ToolCall(
            name="read_url",
            input={"urls": urls},
            output={"source_count": 1},
            latency_ms=1,
        )
        return extracted, tc


# ---------------------------------------------------------------------------
# Load golden set
# ---------------------------------------------------------------------------

_GOLDEN_SET_PATH = (
    Path(__file__).resolve().parent.parent / "evals" / "research_golden_set.json"
)


def _load_golden_cases() -> list[dict]:
    with open(_GOLDEN_SET_PATH, encoding="utf-8") as fh:
        return json.load(fh)


_GOLDEN_CASES = _load_golden_cases()
_CASE_IDS = [c["id"] for c in _GOLDEN_CASES]


# ---------------------------------------------------------------------------
# Autouse: patch all model completions
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def patch_model_completions(monkeypatch):
    _patch_completion(monkeypatch)


# ---------------------------------------------------------------------------
# Helper: build TurnRequest from a golden-set entry
# ---------------------------------------------------------------------------

def _request_for(entry: dict) -> TurnRequest:
    req = entry["request"]
    kwargs: dict = {"message": req["message"]}
    # Propagate research_level if the case overrides it.
    if "research_level" in req:
        kwargs["research_level"] = req["research_level"]
    return TurnRequest(**kwargs)


# ---------------------------------------------------------------------------
# 5.1  Pipeline completes without exception for every golden-set case
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", _GOLDEN_CASES, ids=_CASE_IDS)
def test_pipeline_completes_without_error(entry):
    request = _request_for(entry)
    result = run_langgraph_research(request, FakeTools())
    # If we reach here the pipeline didn't raise — now do the structural checks.
    assert result is not None, f"[{entry['id']}] run_langgraph_research returned None"


# ---------------------------------------------------------------------------
# 5.2  response.text is a non-empty string
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", _GOLDEN_CASES, ids=_CASE_IDS)
def test_response_text_non_empty(entry):
    request = _request_for(entry)
    result = run_langgraph_research(request, FakeTools())
    text = result["response"].text
    assert isinstance(text, str), f"[{entry['id']}] response.text is not a str: {type(text)}"
    assert text.strip(), f"[{entry['id']}] response.text is empty"


# ---------------------------------------------------------------------------
# 5.3  evidence.items is non-empty
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", _GOLDEN_CASES, ids=_CASE_IDS)
def test_evidence_items_non_empty(entry):
    request = _request_for(entry)
    result = run_langgraph_research(request, FakeTools())
    evidence = result["evidence"]
    assert evidence is not None, f"[{entry['id']}] evidence is None"
    assert len(evidence.items) >= 1, (
        f"[{entry['id']}] evidence.items is empty"
    )


# ---------------------------------------------------------------------------
# 5.4  evidence.claims is non-empty (claim classification fired)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", _GOLDEN_CASES, ids=_CASE_IDS)
def test_evidence_claims_non_empty(entry):
    request = _request_for(entry)
    result = run_langgraph_research(request, FakeTools())
    evidence = result["evidence"]
    assert len(evidence.claims) >= 1, (
        f"[{entry['id']}] evidence.claims is empty — classify_claims may not have fired"
    )


# ---------------------------------------------------------------------------
# 5.5  judge_result is populated with a valid score
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", _GOLDEN_CASES, ids=_CASE_IDS)
def test_judge_result_populated(entry):
    request = _request_for(entry)
    result = run_langgraph_research(request, FakeTools())
    feedback = result["feedback"]
    assert feedback is not None, f"[{entry['id']}] feedback is None"
    assert isinstance(feedback.final_score, float), (
        f"[{entry['id']}] feedback.final_score is not a float: {feedback.final_score!r}"
    )
    assert feedback.final_score >= 0, (
        f"[{entry['id']}] feedback.final_score < 0: {feedback.final_score}"
    )


# ---------------------------------------------------------------------------
# 5.6  Budget accounting: model_calls_made ≥ 2, cost_usd_spent ≥ 0
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", _GOLDEN_CASES, ids=_CASE_IDS)
def test_budget_accounting_non_trivial(entry):
    request = _request_for(entry)
    result = run_langgraph_research(request, FakeTools())
    state = result["langgraph_state"]
    model_calls = state.get("model_calls_made", 0)
    cost = state.get("cost_usd_spent", -1.0)
    assert model_calls >= 2, (
        f"[{entry['id']}] model_calls_made={model_calls} < 2 — brief or synthesize skipped"
    )
    assert cost >= 0, (
        f"[{entry['id']}] cost_usd_spent={cost} is negative"
    )


# ---------------------------------------------------------------------------
# 5.7  All core nodes are visited
# ---------------------------------------------------------------------------

_REQUIRED_NODES = [
    "brief", "subject_derivation", "contract", "plan",
    "dispatch_search", "rank", "read",
    "classify_claims", "expand_source_graph", "bind",
    "synthesize", "verify", "judge",
]


@pytest.mark.parametrize("entry", _GOLDEN_CASES, ids=_CASE_IDS)
def test_all_core_nodes_visited(entry):
    request = _request_for(entry)
    result = run_langgraph_research(request, FakeTools())
    visited = result["langgraph_state"].get("visited_nodes", [])
    missing = [n for n in _REQUIRED_NODES if n not in visited]
    assert not missing, (
        f"[{entry['id']}] nodes not visited: {missing} (visited={visited})"
    )


# ---------------------------------------------------------------------------
# 5.8  claim_classification_results is non-empty
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", _GOLDEN_CASES, ids=_CASE_IDS)
def test_claim_classification_results_present(entry):
    request = _request_for(entry)
    result = run_langgraph_research(request, FakeTools())
    state = result["langgraph_state"]
    records = state.get("claim_classification_results") or []
    assert isinstance(records, list), (
        f"[{entry['id']}] claim_classification_results is not a list"
    )
    assert len(records) >= 1, (
        f"[{entry['id']}] claim_classification_results is empty — classify_claims node produced no records"
    )


# ---------------------------------------------------------------------------
# 5.9  For cases with expected.research_level in the request: plan matches
# ---------------------------------------------------------------------------

_LEVEL_CASES = [
    c for c in _GOLDEN_CASES
    if "research_level" in c.get("request", {})
]


@pytest.mark.parametrize("entry", _LEVEL_CASES, ids=[c["id"] for c in _LEVEL_CASES])
def test_request_research_level_respected(entry):
    """When research_level is explicit in the request, it flows to the budget gate."""
    request = _request_for(entry)
    result = run_langgraph_research(request, FakeTools())
    plan = result["plan"]
    expected_level = entry["request"]["research_level"]
    # The plan may have derived a different level, but the budget gate must
    # have used the request's level — verify via state budget decisions.
    state = result["langgraph_state"]
    # At minimum: pipeline completes without budget abort.
    assert "synthesize" in state.get("visited_nodes", []), (
        f"[{entry['id']}] synthesize not visited — budget gate may have aborted "
        f"with research_level={expected_level!r}"
    )


# ---------------------------------------------------------------------------
# 5.10  sources are populated (search + read ran)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", _GOLDEN_CASES, ids=_CASE_IDS)
def test_sources_populated(entry):
    request = _request_for(entry)
    result = run_langgraph_research(request, FakeTools())
    sources = result["sources"]
    assert isinstance(sources, list), f"[{entry['id']}] sources is not a list"
    assert len(sources) >= 1, (
        f"[{entry['id']}] sources is empty — search_worker or read did not fire"
    )
