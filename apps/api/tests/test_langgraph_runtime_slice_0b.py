"""Slice 0B tests: budget reducers, budget gate, pause/approval contracts.

These tests NEVER use the parity comparator — REQUIRE_HUMAN_APPROVAL and
STOP_WITH_GAPS are new functionality with no legacy equivalent.
"""
from __future__ import annotations

import pytest

from app.services.agent.langgraph_runtime.events import SLICE_VERSION
from app.services.agent.langgraph_runtime.graph import (
    _budget_gate_router,
    build_research_graph,
    run_stub_graph,
)
from app.services.agent.langgraph_runtime.nodes import budget_gate
from app.services.agent.langgraph_runtime.state import (
    BudgetDecision,
    ResearchGraphState,
)
from app.services.agent.models import TurnRequest
from app.services.agent.research_profiles import research_budget_for

from test_agent_runtime import FakeTools, _patch_completion

_TEST_REQUEST = TurnRequest(message="test request")


# ---------------------------------------------------------------------------
# 0B.1  SLICE_VERSION constant is bumped to slice_0b
# ---------------------------------------------------------------------------

def test_slice_version_is_0b():
    assert SLICE_VERSION == "slice_0b"


# ---------------------------------------------------------------------------
# 0B.2  Budget reducer semantics: Annotated[float, operator.add]
# ---------------------------------------------------------------------------

def test_budget_reducers_accumulate_via_langgraph(monkeypatch):
    """When nodes emit budget deltas, LangGraph adds them together.

    Slice 3: synthesis/repair make real LLM calls — patch model_client so
    this structural test remains fast and sandbox-safe.
    """
    _patch_completion(monkeypatch)
    compiled = build_research_graph(
        run_id="test-run", request=_TEST_REQUEST, tools=FakeTools()
    )
    initial: ResearchGraphState = {
        "request_message": "budget reducer test",
        "cost_usd_spent": 0.0,
        "tool_calls_made": 0,
        "model_calls_made": 0,
        "visited_nodes": [],
        "artifacts": {},
    }
    result = compiled.invoke(initial)
    # brief(1) + synthesize(1) = 2 minimum.
    # contract/plan/judge are pure heuristic (0 LLM calls each).
    # repair adds 1 only when judge.status=="repair" (not "fail" or "pass").
    assert result["model_calls_made"] >= 2, (
        f"Expected model_calls_made>=2, got {result['model_calls_made']}"
    )


def test_accumulated_list_reducer_sources(monkeypatch):
    """Annotated[list[Source], operator.add] fields are lists after a full graph run."""
    _patch_completion(monkeypatch)
    result = run_stub_graph(
        {"request_message": "list reducer test", "visited_nodes": [], "artifacts": {}},
        run_id="test-list-reducer",
        request=_TEST_REQUEST,
        tools=FakeTools(),
    )
    assert isinstance(result.get("sources", []), list)
    assert isinstance(result.get("worker_reports", []), list)
    assert isinstance(result.get("tool_calls", []), list)


# ---------------------------------------------------------------------------
# 0B.3  BudgetDecision enum — all five values exist
# ---------------------------------------------------------------------------

def test_budget_decision_enum_has_five_values():
    values = {d.value for d in BudgetDecision}
    assert values == {
        "continue",
        "continue_with_reduced_search",
        "reserve_for_synthesis",
        "stop_with_gaps",
        "require_human_approval",
    }


# ---------------------------------------------------------------------------
# 0B.4  budget_gate node produces correct decisions from state
# ---------------------------------------------------------------------------

def _gate_state(**kwargs) -> ResearchGraphState:
    defaults: ResearchGraphState = {
        "request_message": "gate test",
        "cost_usd_spent": 0.0,
        "tool_calls_made": 0,
        "model_calls_made": 0,
        "visited_nodes": [],
        "artifacts": {},
    }
    defaults.update(kwargs)
    return defaults


def test_budget_gate_returns_continue_under_all_thresholds():
    # _TEST_REQUEST → regular tier. Threshold values come from research profiles.
    state = _gate_state(cost_usd_spent=0.01, tool_calls_made=3, model_calls_made=2)
    result = budget_gate(state, run_id="test-gate", request=_TEST_REQUEST)
    assert result["budget_decision"] == BudgetDecision.CONTINUE


def test_budget_gate_returns_continue_with_reduced_search_when_tool_calls_high():
    budget = research_budget_for(_TEST_REQUEST)
    # tool_calls_made > max_tool_calls but cost < max_cost_usd → CONTINUE_WITH_REDUCED_SEARCH
    state = _gate_state(cost_usd_spent=0.01, tool_calls_made=budget.max_tool_calls + 1, model_calls_made=2)
    result = budget_gate(state, run_id="test-gate", request=_TEST_REQUEST)
    assert result["budget_decision"] == BudgetDecision.CONTINUE_WITH_REDUCED_SEARCH


def test_budget_gate_returns_require_human_approval_when_cost_over_threshold():
    budget = research_budget_for(_TEST_REQUEST)
    # cost > max_cost_usd → REQUIRE_HUMAN_APPROVAL
    state = _gate_state(cost_usd_spent=budget.max_cost_usd + 0.01, tool_calls_made=5, model_calls_made=2)
    result = budget_gate(state, run_id="test-gate", request=_TEST_REQUEST)
    assert result["budget_decision"] == BudgetDecision.REQUIRE_HUMAN_APPROVAL


def test_budget_gate_cost_threshold_takes_precedence_over_tool_calls():
    budget = research_budget_for(_TEST_REQUEST)
    # Both cost AND tool_calls exceeded → REQUIRE_HUMAN_APPROVAL (cost takes priority)
    state = _gate_state(
        cost_usd_spent=budget.max_cost_usd + 0.01,
        tool_calls_made=budget.max_tool_calls + 1,
        model_calls_made=5,
    )
    result = budget_gate(state, run_id="test-gate", request=_TEST_REQUEST)
    assert result["budget_decision"] == BudgetDecision.REQUIRE_HUMAN_APPROVAL


# ---------------------------------------------------------------------------
# 0B.5  Pause contract fields — populated at REQUIRE_HUMAN_APPROVAL
# ---------------------------------------------------------------------------

def test_pause_contract_populated_when_approval_required():
    budget = research_budget_for(_TEST_REQUEST)
    # cost > max_cost_usd for regular tier
    state = _gate_state(cost_usd_spent=budget.max_cost_usd + 0.01)
    result = budget_gate(state, run_id="test-pause", request=_TEST_REQUEST)
    assert result["budget_decision"] == BudgetDecision.REQUIRE_HUMAN_APPROVAL
    pause = result.get("pause_contract")
    assert pause is not None, "pause_contract must be set when REQUIRE_HUMAN_APPROVAL"
    assert "pause_reason" in pause
    assert "audit_event_id" in pause
    assert "paused_at" in pause
    assert pause["audit_event_id"].startswith("lgpause")


def test_pause_contract_required_additional_budget_is_positive():
    """required_additional_budget_usd is the CONTINUATION budget (always positive).

    It must NOT be `threshold - cost` (which is 0 or negative at approval time).
    It must equal the request's budget ceiling so the user knows what to authorise.
    """
    budget = research_budget_for(_TEST_REQUEST)
    state = _gate_state(cost_usd_spent=budget.max_cost_usd + 0.01)
    result = budget_gate(state, run_id="test-pause-budget", request=_TEST_REQUEST)
    assert result["budget_decision"] == BudgetDecision.REQUIRE_HUMAN_APPROVAL
    pause = result.get("pause_contract")
    assert pause is not None
    continuation = pause["required_additional_budget_usd"]
    assert continuation > 0, (
        f"required_additional_budget_usd must be positive; got {continuation}"
    )
    # Equals one full budget ceiling.
    assert continuation == budget.max_cost_usd, (
        f"Expected continuation budget = {budget.max_cost_usd}; got {continuation}"
    )


def test_pause_contract_not_populated_for_continue():
    state = _gate_state(cost_usd_spent=0.01)
    result = budget_gate(state, run_id="test-no-pause", request=_TEST_REQUEST)
    assert result["budget_decision"] == BudgetDecision.CONTINUE
    assert "pause_contract" not in result


# ---------------------------------------------------------------------------
# 0B.5b  Budget gate respects request tier (not hard-coded thresholds)
# ---------------------------------------------------------------------------

def test_budget_gate_deep_tier_has_larger_cost_ceiling():
    """Deep tier allows spend that would pause a regular request."""
    deep_request = TurnRequest(message="test request", research_level="deep")
    regular_budget = research_budget_for(_TEST_REQUEST)
    deep_budget = research_budget_for(deep_request)
    assert deep_budget.max_cost_usd > regular_budget.max_cost_usd
    state = _gate_state(
        cost_usd_spent=(regular_budget.max_cost_usd + deep_budget.max_cost_usd) / 2,
        tool_calls_made=5,
        model_calls_made=2,
    )
    result = budget_gate(state, run_id="test-deep-gate", request=deep_request)
    assert result["budget_decision"] == BudgetDecision.CONTINUE


def test_budget_gate_easy_tier_pauses_earlier():
    """Easy tier pauses on spend that regular tier would allow."""
    easy_request = TurnRequest(message="test request", research_level="easy")
    easy_budget = research_budget_for(easy_request)
    state = _gate_state(cost_usd_spent=easy_budget.max_cost_usd + 0.01, tool_calls_made=1, model_calls_made=1)
    result = budget_gate(state, run_id="test-easy-gate", request=easy_request)
    assert result["budget_decision"] == BudgetDecision.REQUIRE_HUMAN_APPROVAL


# ---------------------------------------------------------------------------
# 0B.6  Approval contract — must NOT exist at pause time
# ---------------------------------------------------------------------------

def test_approval_contract_absent_at_pause_time():
    state = _gate_state(cost_usd_spent=9.99)
    result = budget_gate(state, run_id="test-approval-absent", request=_TEST_REQUEST)
    assert result["budget_decision"] == BudgetDecision.REQUIRE_HUMAN_APPROVAL
    assert "approval_contract" not in result


# ---------------------------------------------------------------------------
# 0B.7  _budget_gate_router conditional edge routing
# ---------------------------------------------------------------------------

def test_router_returns_continue_for_continue_decision():
    assert _budget_gate_router({"budget_decision": BudgetDecision.CONTINUE}) == "continue"


def test_router_returns_continue_for_reduced_search():
    assert _budget_gate_router({"budget_decision": BudgetDecision.CONTINUE_WITH_REDUCED_SEARCH}) == "continue"


def test_router_returns_requires_approval_for_human_approval():
    assert _budget_gate_router({"budget_decision": BudgetDecision.REQUIRE_HUMAN_APPROVAL}) == "requires_approval"


def test_router_returns_stop_with_gaps_for_stop():
    assert _budget_gate_router({"budget_decision": BudgetDecision.STOP_WITH_GAPS}) == "stop_with_gaps"


def test_router_defaults_to_continue_when_no_decision():
    assert _budget_gate_router({}) == "continue"


# ---------------------------------------------------------------------------
# 0B.8  Graph terminates early when approval required
# ---------------------------------------------------------------------------

def test_graph_terminates_at_approval_required(monkeypatch):
    import app.services.agent.langgraph_runtime.nodes as nodes_module

    original_bind = nodes_module.bind

    def inject_high_cost(state, *, run_id, request, tools=None, progress=None):
        result = original_bind(state, run_id=run_id, request=request, tools=tools, progress=progress)
        result["cost_usd_spent"] = 10.0
        return result

    monkeypatch.setattr(nodes_module, "bind", inject_high_cost)

    initial: ResearchGraphState = {
        "request_message": "approval required test",
        "cost_usd_spent": 0.0,
        "tool_calls_made": 0,
        "model_calls_made": 0,
        "visited_nodes": [],
        "artifacts": {},
    }
    result = run_stub_graph(initial, run_id="test-approval-graph", request=_TEST_REQUEST)

    assert result.get("budget_decision") == BudgetDecision.REQUIRE_HUMAN_APPROVAL
    visited = result.get("visited_nodes", [])
    assert "synthesize" not in visited
    assert result.get("pause_contract") is not None


# ---------------------------------------------------------------------------
# 0B.9  Event identity fields appear on every emitted event
# ---------------------------------------------------------------------------

def test_every_graph_event_has_full_identity_fields(monkeypatch):
    _patch_completion(monkeypatch)
    events = []

    def capture(stage, message, **data):
        events.append(data)

    run_stub_graph(
        {"request_message": "event identity test", "visited_nodes": [], "artifacts": {}},
        run_id="test-identity",
        request=_TEST_REQUEST,
        tools=FakeTools(),
        progress=capture,
    )

    assert events, "No events were emitted"
    for event in events:
        assert "event_id" in event, f"Missing event_id: {event}"
        assert "run_id" in event, f"Missing run_id: {event}"
        assert "node_name" in event, f"Missing node_name: {event}"
        assert "attempt" in event, f"Missing attempt: {event}"
        assert "state_version" in event, f"Missing state_version: {event}"
        assert "budget_snapshot" in event, f"Missing budget_snapshot: {event}"
        assert event["state_version"] == "slice_0b"


def test_run_id_is_consistent_across_all_events(monkeypatch):
    _patch_completion(monkeypatch)
    events = []

    def capture(stage, message, **data):
        events.append(data)

    run_stub_graph(
        {"request_message": "run id test", "visited_nodes": [], "artifacts": {}},
        run_id="consistent-run-id",
        request=_TEST_REQUEST,
        tools=FakeTools(),
        progress=capture,
    )

    for event in events:
        assert event["run_id"] == "consistent-run-id"
