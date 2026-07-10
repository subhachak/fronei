"""Fix for a live failure: a bare confirmation reply ("Yes") to a prior
deep-research confirmation offer produced a research plan and search entirely
about an unrelated same-word entity (the band "Yes"), not the Corebridge
Financial/JAVAH topic the user was actually confirming.

Root cause (confirmed by code/prompt inspection, not guesswork):
  - BRIEF_PROMPT told the model to "convert the user request into a...
    research brief" but never instructed it to use conversation_context to
    resolve the objective when the current message is itself just a short
    confirmation/continuation reply -- a literal "yes" is a perfectly
    reasonable "user request" to build a (wrong) brief from, absent that
    instruction.
  - Every downstream node that builds an LLM prompt referencing "the user's
    request" (subject_derivation, contract, plan, synthesize, repair) read
    request.message directly, discarding research_brief.objective entirely --
    so even a correctly-resolved brief couldn't help. Fixed centrally: brief()
    now writes state["resolved_message"], and every one of those nodes reads
    it via the shared _effective_request() helper instead of using
    request.message directly. Scanned across the whole graph (read/
    classify_claims/expand_source_graph/bind/verify/judge don't reference
    request.message at all -- confirmed by direct inspection, no fix needed
    there).

Covers:
  - research_profiles._is_confirmation_reply() -- the deterministic signal
    (reused from orchestrator.py's existing pending-intent carry-forward
    heuristic: last_turn_route == "clarify" + a short reply)
  - generate_research_brief() sends that signal explicitly in the payload
  - BRIEF_PROMPT's new Continuation rule
  - nodes.brief() computing state["resolved_message"]
  - nodes._effective_request() -- the shared helper every downstream node
    uses instead of request.message directly
  - nodes.subject_derivation()'s use of _effective_request() -- proven
    deterministic (not model-luck-dependent) by reproducing it with a
    correctly-resolved brief.objective and a message that only says "Yes"
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from app.services.agent import model_client
from app.services.agent.langgraph_runtime.nodes import subject_derivation
from app.services.agent.langgraph_runtime.state import ResearchGraphState
from app.services.agent.models import TurnRequest
from app.services.agent.research_models import CoverageContract, ResearchBrief, ResearchPlan
from app.services.agent.research_contracts import _extract_named_comparison_subjects
from app.services.agent.research_profiles import (
    BRIEF_PROMPT,
    _is_confirmation_reply,
    generate_research_brief,
)

CONFIRMATION_CONTEXT = (
    "User asked: Assess whether to challenge the proposed C++ to C#/React re-platforming "
    "of Corebridge Financial's JAVAH derivatives risk simulation platform. "
    "Fronei responded via clarify: This looks like deep research. Continue with deep "
    "research, use regular research instead, or answer directly?"
)


# ---------------------------------------------------------------------------
# _is_confirmation_reply()
# ---------------------------------------------------------------------------

def test_is_confirmation_reply_true_for_short_reply_after_clarify():
    request = TurnRequest(message="Yes", last_turn_route="clarify")
    assert _is_confirmation_reply(request) is True


def test_is_confirmation_reply_false_when_last_turn_was_not_clarify():
    request = TurnRequest(message="Yes", last_turn_route="research")
    assert _is_confirmation_reply(request) is False


def test_is_confirmation_reply_false_for_a_long_substantive_message():
    long_message = " ".join(["word"] * 30)
    request = TurnRequest(message=long_message, last_turn_route="clarify")
    assert _is_confirmation_reply(request) is False


def test_is_confirmation_reply_false_when_no_prior_turn():
    request = TurnRequest(message="Yes", last_turn_route=None)
    assert _is_confirmation_reply(request) is False


def test_extract_named_comparison_subjects_treats_bare_yes_as_a_proper_noun():
    """Documents the underlying extractor quirk this fix routes around rather
    than patches: a lone capitalized word with no comparison structure at all
    still passes the has_capital check, so a not-empty-result check alone
    cannot detect "this raw message has no real subject" for a bare
    confirmation reply. subject_derivation's fix keys off _is_confirmation_
    reply() instead of an emptiness check for exactly this reason."""
    assert _extract_named_comparison_subjects("Yes") == ["Yes"]


# ---------------------------------------------------------------------------
# BRIEF_PROMPT's Continuation rule
# ---------------------------------------------------------------------------

def test_brief_prompt_contains_continuation_rule():
    assert "Continuation rule" in BRIEF_PROMPT
    assert "is_confirmation_reply" in BRIEF_PROMPT


# ---------------------------------------------------------------------------
# generate_research_brief() sends is_confirmation_reply explicitly
# ---------------------------------------------------------------------------

def test_generate_research_brief_sends_confirmation_reply_flag_when_true(monkeypatch):
    def _fake_complete(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        assert payload["is_confirmation_reply"] is True
        assert payload["conversation_context"] == CONFIRMATION_CONTEXT
        return json_response({
            "objective": "Assess the JAVAH re-platforming decision for Corebridge Financial",
            "research_profile": "strategy_brief",
            "scope_in": ["Corebridge Financial", "JAVAH platform"],
        })

    monkeypatch.setattr(model_client, "complete", _fake_complete)

    brief = generate_research_brief(
        TurnRequest(message="Yes", last_turn_route="clarify", conversation_context=CONFIRMATION_CONTEXT)
    )

    assert "Corebridge" in brief.objective


def test_generate_research_brief_sends_confirmation_reply_flag_false_for_fresh_request(monkeypatch):
    def _fake_complete(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        assert payload["is_confirmation_reply"] is False
        return json_response({"objective": "Compare API pricing tiers", "research_profile": "vendor_comparison"})

    monkeypatch.setattr(model_client, "complete", _fake_complete)

    generate_research_brief(TurnRequest(message="Compare API pricing tiers across providers"))


def json_response(payload: dict):
    from types import SimpleNamespace
    return SimpleNamespace(text=json.dumps(payload), model_used="test", latency_ms=1, cost_usd=0.001)


# ---------------------------------------------------------------------------
# brief() node: computes state["resolved_message"] -- swaps in
# research_brief.objective for a bare confirmation reply, otherwise a no-op
# ---------------------------------------------------------------------------

def _state() -> ResearchGraphState:
    return {
        "visited_nodes": [],
        "artifacts": {},
        "cost_usd_spent": 0.0,
        "tool_calls_made": 0,
        "model_calls_made": 0,
    }


def test_brief_node_resolves_message_for_confirmation_reply(monkeypatch):
    monkeypatch.setattr(model_client, "complete", lambda *a, **k: json_response({
        "objective": "Assess the JAVAH re-platforming decision for Corebridge Financial",
        "research_profile": "strategy_brief",
    }))
    from app.services.agent.langgraph_runtime.nodes import brief as brief_node

    request = TurnRequest(message="Yes", last_turn_route="clarify", conversation_context=CONFIRMATION_CONTEXT)

    result = brief_node(_state(), run_id="test", request=request)

    assert result["resolved_message"] == "Assess the JAVAH re-platforming decision for Corebridge Financial"


def test_brief_node_does_not_resolve_message_for_a_fresh_request(monkeypatch):
    monkeypatch.setattr(model_client, "complete", lambda *a, **k: json_response({
        "objective": "Compare API pricing tiers across providers", "research_profile": "vendor_comparison",
    }))
    from app.services.agent.langgraph_runtime.nodes import brief as brief_node

    request = TurnRequest(message="Compare API pricing tiers across providers")

    result = brief_node(_state(), run_id="test", request=request)

    assert result["resolved_message"] == "Compare API pricing tiers across providers"


# ---------------------------------------------------------------------------
# subject_derivation(): consumes state["resolved_message"] via
# _effective_request() -- set directly here to simulate brief() having
# already run, matching how these nodes actually chain in the graph
# ---------------------------------------------------------------------------

def _state_with_brief(brief: ResearchBrief | None, *, resolved_message: str | None = None) -> ResearchGraphState:
    state: ResearchGraphState = {
        "visited_nodes": [],
        "artifacts": {},
        "brief": brief,
        "cost_usd_spent": 0.0,
        "tool_calls_made": 0,
        "model_calls_made": 0,
    }
    if resolved_message is not None:
        state["resolved_message"] = resolved_message
    return state


def test_subject_derivation_falls_back_to_brief_objective_for_confirmation_reply():
    """Diagnostic + regression: even a correctly-resolved brief.objective
    (simulating a competent model that used conversation_context, e.g. after
    the BRIEF_PROMPT/is_confirmation_reply fix) previously got silently
    discarded here, because named_subjects was derived from request.message
    ("Yes") alone."""
    request = TurnRequest(message="Yes", last_turn_route="clarify")
    objective = "Compare AWS S3, Azure Blob Storage, and Google Cloud Storage covering durability and pricing"
    brief = ResearchBrief(objective=objective, research_level="regular")
    state = _state_with_brief(brief, resolved_message=objective)

    result = subject_derivation(state, run_id="test", request=request)

    assert set(result["named_subjects"]) >= {"AWS S3", "Azure Blob Storage", "Google Cloud Storage"}, (
        f"expected brief.objective's named entities, got {result['named_subjects']!r}"
    )


def test_subject_derivation_still_uses_message_when_it_has_named_subjects():
    """No regression: when the raw message itself has extractable entities
    (the normal, non-continuation case), it's still used directly and the
    brief fallback never fires."""
    request = TurnRequest(
        message="Compare Epic, Oracle Cerner, and Meditech Expanse as EHR platforms",
        research_level="regular",
    )
    brief = ResearchBrief(objective="Unrelated objective mentioning Salesforce and HubSpot", research_level="regular")
    state = _state_with_brief(brief, resolved_message=request.message)

    result = subject_derivation(state, run_id="test", request=request)

    assert set(result["named_subjects"]) == {"Epic", "Oracle Cerner", "Meditech Expanse"}


def test_subject_derivation_returns_empty_when_neither_message_nor_brief_has_subjects():
    request = TurnRequest(message="Yes", last_turn_route="clarify")
    objective = "Provide background information about recent developments"
    brief = ResearchBrief(objective=objective, research_level="regular")
    state = _state_with_brief(brief, resolved_message=objective)

    result = subject_derivation(state, run_id="test", request=request)

    assert result["named_subjects"] == []


def test_subject_derivation_does_not_fall_back_when_brief_is_absent():
    """Degraded case: no brief to fall back to at all, so the raw message is
    used as a last resort -- even though _extract_named_comparison_subjects
    treats a bare capitalized "Yes" as a proper noun (a separate, narrower
    quirk of that extractor's capitalization heuristic, not something this
    fix touches)."""
    request = TurnRequest(message="Yes", last_turn_route="clarify")
    state = _state_with_brief(None)

    result = subject_derivation(state, run_id="test", request=request)

    assert result["named_subjects"] == ["Yes"]


# ---------------------------------------------------------------------------
# _effective_request() -- the shared helper contract/plan/synthesize/repair
# all use instead of request.message directly
# ---------------------------------------------------------------------------

def test_effective_request_swaps_in_resolved_message_when_different():
    from app.services.agent.langgraph_runtime.nodes import _effective_request

    request = TurnRequest(message="Yes")
    state = _state()
    state["resolved_message"] = "Corebridge Financial JAVAH modernization assessment"

    effective = _effective_request(state, request)

    assert effective.message == "Corebridge Financial JAVAH modernization assessment"
    assert request.message == "Yes"  # original untouched


def test_effective_request_is_a_noop_when_resolved_message_matches():
    from app.services.agent.langgraph_runtime.nodes import _effective_request

    request = TurnRequest(message="Compare API pricing tiers")
    state = _state()
    state["resolved_message"] = "Compare API pricing tiers"

    assert _effective_request(state, request) is request


def test_effective_request_is_a_noop_when_resolved_message_absent():
    from app.services.agent.langgraph_runtime.nodes import _effective_request

    request = TurnRequest(message="Yes")

    assert _effective_request(_state(), request) is request


# ---------------------------------------------------------------------------
# contract()/plan()/synthesize() node integration: each passes the resolved
# request into its domain-function call, not the raw one
# ---------------------------------------------------------------------------

def test_contract_node_passes_resolved_request_to_generate_coverage_contract(monkeypatch):
    from app.services.agent.langgraph_runtime.nodes import contract as contract_node
    from app.services.agent import research_contracts

    captured = {}

    def _fake_generate(request, brief, **kwargs):
        captured["message"] = request.message
        return CoverageContract(subjects=["Corebridge Financial"], dimensions=["status"], cells=[], source="test")

    monkeypatch.setattr(research_contracts, "generate_coverage_contract", _fake_generate)

    request = TurnRequest(message="Yes", last_turn_route="clarify")
    state = _state()
    state["brief"] = ResearchBrief(objective="Corebridge Financial JAVAH modernization assessment", research_level="regular")
    state["resolved_message"] = "Corebridge Financial JAVAH modernization assessment"

    contract_node(state, run_id="test", request=request)

    assert captured["message"] == "Corebridge Financial JAVAH modernization assessment"


def test_plan_node_passes_resolved_request_to_plan_from_contract(monkeypatch):
    from app.services.agent.langgraph_runtime.nodes import plan as plan_node
    from app.services.agent import research_planner

    captured = {}

    def _fake_plan_from_contract(request, contract, **kwargs):
        captured["message"] = request.message
        return ResearchPlan(workers=[])

    monkeypatch.setattr(research_planner, "plan_from_contract", _fake_plan_from_contract)
    monkeypatch.setattr(research_planner, "flag_untargeted_worker_queries", lambda *a, **k: None)

    request = TurnRequest(message="Yes", last_turn_route="clarify")
    state = _state()
    state["contract"] = CoverageContract(subjects=["Corebridge Financial"], dimensions=["status"], cells=[], source="test")
    state["resolved_message"] = "Corebridge Financial JAVAH modernization assessment"

    plan_node(state, run_id="test", request=request)

    assert captured["message"] == "Corebridge Financial JAVAH modernization assessment"


def test_synthesize_node_passes_resolved_request_to_synthesize_answer_stream(monkeypatch):
    from app.services.agent.langgraph_runtime.nodes import synthesize as synthesize_node
    from app.services.agent import research_synthesis
    from app.services.agent.research_models import EvidencePack

    captured = {}

    def _fake_stream(request, plan, evidence, on_delta=None):
        captured["message"] = request.message
        return SimpleNamespace(text="answer", model_used="test", latency_ms=1, cost_usd=0.0)

    monkeypatch.setattr(research_synthesis, "synthesize_answer_stream", _fake_stream)

    request = TurnRequest(message="Yes", last_turn_route="clarify")
    state = _state()
    state["plan"] = ResearchPlan(workers=[])
    state["evidence"] = EvidencePack(items=[])
    state["resolved_message"] = "Corebridge Financial JAVAH modernization assessment"

    synthesize_node(state, run_id="test", request=request)

    assert captured["message"] == "Corebridge Financial JAVAH modernization assessment"
