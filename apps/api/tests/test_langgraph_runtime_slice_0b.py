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


# ---------------------------------------------------------------------------
# 0B.1  SLICE_VERSION constant is bumped to slice_0b
# ---------------------------------------------------------------------------

def test_slice_version_is_0b():
    assert SLICE_VERSION == "slice_0b"


# ---------------------------------------------------------------------------
# 0B.2  Budget reducer semantics: Annotated[float, operator.add]
# ---------------------------------------------------------------------------

def test_budget_reducers_accumulate_via_langgraph():
    """When two nodes each emit cost_usd_spent, LangGraph adds them together."""
    compiled = build_research_graph(run_id="test-run")
    initial: ResearchGraphState = {
        "request_message": "budget reducer test",
        "cost_usd_spent": 0.0,
        "tool_calls_made": 0,
        "model_calls_made": 0,
        "visited_nodes": [],
        "artifacts": {},
    }
    result = compiled.invoke(initial)
    # All stub nodes emit 0 budget deltas, but synthesize, judge, and repair
    # each add 1 model_calls_made.  The accumulated total is exactly 3.
    assert result["model_calls_made"] == 3, (
        f"Expected model_calls_made=3 (synthesize + judge + repair), got {result['model_calls_made']}"
    )


def test_accumulated_list_reducer_sources():
    """Annotated[list[Source], operator.add] fields start empty and stay empty for stubs."""
    result = run_stub_graph(
        {"request_message": "list reducer test", "visited_nodes": [], "artifacts": {}},
        run_id="test-list-reducer",
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
    state = _gate_state(cost_usd_spent=0.50, tool_calls_made=5, model_calls_made=2)
    result = budget_gate(state, run_id="test-gate")
    assert result["budget_decision"] == BudgetDecision.CONTINUE


def test_budget_gate_returns_continue_with_reduced_search_when_tool_calls_high():
    state = _gate_state(cost_usd_spent=0.10, tool_calls_made=25, model_calls_made=2)
    result = budget_gate(state, run_id="test-gate")
    assert result["budget_decision"] == BudgetDecision.CONTINUE_WITH_REDUCED_SEARCH


def test_budget_gate_returns_require_human_approval_when_cost_over_threshold():
    state = _gate_state(cost_usd_spent=6.00, tool_calls_made=5, model_calls_made=2)
    result = budget_gate(state, run_id="test-gate")
    assert result["budget_decision"] == BudgetDecision.REQUIRE_HUMAN_APPROVAL


def test_budget_gate_cost_threshold_takes_precedence_over_tool_calls():
    """Cost ceiling fires before tool-call ceiling."""
    state = _gate_state(cost_usd_spent=10.0, tool_calls_made=30, model_calls_made=5)
    result = budget_gate(state, run_id="test-gate")
    assert result["budget_decision"] == BudgetDecision.REQUIRE_HUMAN_APPROVAL


# ---------------------------------------------------------------------------
# 0B.5  Pause contract fields — populated at REQUIRE_HUMAN_APPROVAL
# ---------------------------------------------------------------------------

def test_pause_contract_populated_when_approval_required():
    state = _gate_state(cost_usd_spent=7.50)
    result = budget_gate(state, run_id="test-pause")
    assert result["budget_decision"] == BudgetDecision.REQUIRE_HUMAN_APPROVAL
    pause = result.get("pause_contract")
    assert pause is not None, "pause_contract must be set when REQUIRE_HUMAN_APPROVAL"
    assert "pause_reason" in pause
    assert "audit_event_id" in pause
    assert "paused_at" in pause
    assert pause["audit_event_id"].startswith("lgpause")


def test_pause_contract_not_populated_for_continue():
    state = _gate_state(cost_usd_spent=0.10)
    result = budget_gate(state, run_id="test-no-pause")
    assert result["budget_decision"] == BudgetDecision.CONTINUE
    assert "pause_contract" not in result, (
        "pause_contract must NOT be set when decision is CONTINUE"
    )


# ---------------------------------------------------------------------------
# 0B.6  Approval contract — must NOT exist at pause time
# ---------------------------------------------------------------------------

def test_approval_contract_absent_at_pause_time():
    """The approval contract is only populated after a human approves.

    At the moment the budget gate fires REQUIRE_HUMAN_APPROVAL, the
    approval_contract field must not be set — that would be asserting a
    future state before the human has acted.
    """
    state = _gate_state(cost_usd_spent=9.99)
    result = budget_gate(state, run_id="test-approval-absent")
    assert result["budget_decision"] == BudgetDecision.REQUIRE_HUMAN_APPROVAL
    assert "approval_contract" not in result, (
        "approval_contract must not be set before human approval has occurred"
    )


# ---------------------------------------------------------------------------
# 0B.7  _budget_gate_router conditional edge routing
# ---------------------------------------------------------------------------

def test_router_returns_continue_for_continue_decision():
    state: ResearchGraphState = {"budget_decision": BudgetDecision.CONTINUE}
    assert _budget_gate_router(state) == "continue"


def test_router_returns_continue_for_reduced_search():
    state: ResearchGraphState = {"budget_decision": BudgetDecision.CONTINUE_WITH_REDUCED_SEARCH}
    assert _budget_gate_router(state) == "continue"


def test_router_returns_requires_approval_for_human_approval():
    state: ResearchGraphState = {"budget_decision": BudgetDecision.REQUIRE_HUMAN_APPROVAL}
    assert _budget_gate_router(state) == "requires_approval"


def test_router_returns_stop_with_gaps_for_stop():
    state: ResearchGraphState = {"budget_decision": BudgetDecision.STOP_WITH_GAPS}
    assert _budget_gate_router(state) == "stop_with_gaps"


def test_router_defaults_to_continue_when_no_decision():
    """If budget_decision is None (initial state), the router defaults to continue."""
    state: ResearchGraphState = {}
    assert _budget_gate_router(state) == "continue"


# ---------------------------------------------------------------------------
# 0B.8  Graph terminates early when approval required
# ---------------------------------------------------------------------------

def test_graph_terminates_at_approval_required(monkeypatch):
    """When cost_usd_spent >= threshold at budget_gate_pre_synthesis, graph ends before synthesize."""
    import app.services.agent.langgraph_runtime.nodes as nodes_module

    original_bind = nodes_module.bind

    def inject_high_cost(state, *, run_id, progress=None):
        result = original_bind(state, run_id=run_id, progress=progress)
        result["cost_usd_spent"] = 10.0   # exceed approval threshold
        return result

    monkeypatch.setattr(nodes_module, "bind", inject_high_cost)

    # Must rebuild after monkeypatching
    initial: ResearchGraphState = {
        "request_message": "approval required test",
        "cost_usd_spent": 0.0,
        "tool_calls_made": 0,
        "model_calls_made": 0,
        "visited_nodes": [],
        "artifacts": {},
    }
    result = run_stub_graph(initial, run_id="test-approval-graph")

    assert result.get("budget_decision") == BudgetDecision.REQUIRE_HUMAN_APPROVAL
    # synthesize was NOT reached
    visited = result.get("visited_nodes", [])
    assert "synthesize" not in visited, (
        f"synthesize should not have run after REQUIRE_HUMAN_APPROVAL; visited={visited}"
    )
    # pause_contract must be present
    assert result.get("pause_contract") is not None


# ---------------------------------------------------------------------------
# 0B.9  Event identity fields appear on every emitted event
# ---------------------------------------------------------------------------

def test_every_graph_event_has_full_identity_fields():
    events = []

    def capture(stage, message, **data):
        events.append(data)

    run_stub_graph(
        {"request_message": "event identity test", "visited_nodes": [], "artifacts": {}},
        run_id="test-identity",
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
        assert event["state_version"] == "slice_0b", (
            f"Wrong state_version '{event['state_version']}'; expected 'slice_0b'"
        )


def test_run_id_is_consistent_across_all_events():
    events = []

    def capture(stage, message, **data):
        events.append(data)

    run_stub_graph(
        {"request_message": "run id test", "visited_nodes": [], "artifacts": {}},
        run_id="consistent-run-id",
        progress=capture,
    )

    for event in events:
        assert event["run_id"] == "consistent-run-id", (
            f"Unexpected run_id '{event['run_id']}' in event for node {event.get('node_name')}"
        )
