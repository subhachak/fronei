from __future__ import annotations

from typing import Any

from app.services.agent.langgraph_runtime.events import ProgressCallback, emit_graph_event
from app.services.agent.langgraph_runtime.state import (
    BudgetDecision,
    GraphNodeName,
    ResearchGraphState,
)


NODE_ORDER: tuple[GraphNodeName, ...] = (
    "brief",
    "subject_derivation",
    "contract",
    "plan",
    "search",
    "rank",
    "read",
    "classify_claims",
    "expand_source_graph",
    "bind",
    "synthesize",
    "verify",
    "judge",
    "repair",
)

# Nodes after which the budget gate fires.
BUDGET_GATE_AFTER: frozenset[str] = frozenset({"bind", "judge"})


def _stub_node(
    state: ResearchGraphState,
    *,
    run_id: str,
    node_name: GraphNodeName,
    progress: ProgressCallback | None,
) -> ResearchGraphState:
    visited = [*state.get("visited_nodes", []), node_name]
    artifacts: dict[str, Any] = {**state.get("artifacts", {})}
    artifacts[node_name] = {"status": "stubbed"}
    emit_graph_event(
        progress,
        run_id=run_id,
        node_name=node_name,
        message=f"LangGraph {node_name} node stubbed.",
        placeholder=True,
        cost_usd_delta=0.0,
        tool_calls_delta=0,
        model_calls_delta=0,
    )
    # Emit zero budget deltas so reducers accumulate correctly from the start.
    return {
        **state,
        "visited_nodes": visited,
        "artifacts": artifacts,
        "cost_usd_spent": 0.0,
        "tool_calls_made": 0,
        "model_calls_made": 0,
    }


def brief(state: ResearchGraphState, *, run_id: str, progress: ProgressCallback | None = None) -> ResearchGraphState:
    return _stub_node(state, run_id=run_id, node_name="brief", progress=progress)


def subject_derivation(
    state: ResearchGraphState, *, run_id: str, progress: ProgressCallback | None = None
) -> ResearchGraphState:
    result = _stub_node(state, run_id=run_id, node_name="subject_derivation", progress=progress)
    return {**result, "named_subjects": []}


def contract(state: ResearchGraphState, *, run_id: str, progress: ProgressCallback | None = None) -> ResearchGraphState:
    return _stub_node(state, run_id=run_id, node_name="contract", progress=progress)


def plan(state: ResearchGraphState, *, run_id: str, progress: ProgressCallback | None = None) -> ResearchGraphState:
    return _stub_node(state, run_id=run_id, node_name="plan", progress=progress)


def search(state: ResearchGraphState, *, run_id: str, progress: ProgressCallback | None = None) -> ResearchGraphState:
    result = _stub_node(state, run_id=run_id, node_name="search", progress=progress)
    return {**result, "sources": [], "worker_reports": [], "tool_calls": [], "provider_attempts": []}


def rank(state: ResearchGraphState, *, run_id: str, progress: ProgressCallback | None = None) -> ResearchGraphState:
    return _stub_node(state, run_id=run_id, node_name="rank", progress=progress)


def read(state: ResearchGraphState, *, run_id: str, progress: ProgressCallback | None = None) -> ResearchGraphState:
    result = _stub_node(state, run_id=run_id, node_name="read", progress=progress)
    return {**result, "tool_calls": []}


def classify_claims(
    state: ResearchGraphState, *, run_id: str, progress: ProgressCallback | None = None
) -> ResearchGraphState:
    result = _stub_node(state, run_id=run_id, node_name="classify_claims", progress=progress)
    return {**result, "claim_classification_results": []}


def expand_source_graph(
    state: ResearchGraphState, *, run_id: str, progress: ProgressCallback | None = None
) -> ResearchGraphState:
    result = _stub_node(state, run_id=run_id, node_name="expand_source_graph", progress=progress)
    return {**result, "source_graph_expansion_results": []}


def bind(state: ResearchGraphState, *, run_id: str, progress: ProgressCallback | None = None) -> ResearchGraphState:
    return _stub_node(state, run_id=run_id, node_name="bind", progress=progress)


def synthesize(
    state: ResearchGraphState, *, run_id: str, progress: ProgressCallback | None = None
) -> ResearchGraphState:
    result = _stub_node(state, run_id=run_id, node_name="synthesize", progress=progress)
    return {
        **result,
        "answer": "",
        "model_used": "langgraph-slice-0b-stub",
        "latency_ms": 0,
        "model_calls_made": 1,
    }


def verify(state: ResearchGraphState, *, run_id: str, progress: ProgressCallback | None = None) -> ResearchGraphState:
    return _stub_node(state, run_id=run_id, node_name="verify", progress=progress)


def judge(state: ResearchGraphState, *, run_id: str, progress: ProgressCallback | None = None) -> ResearchGraphState:
    result = _stub_node(state, run_id=run_id, node_name="judge", progress=progress)
    return {**result, "next_action": "publish", "model_calls_made": 1}


def repair(state: ResearchGraphState, *, run_id: str, progress: ProgressCallback | None = None) -> ResearchGraphState:
    result = _stub_node(state, run_id=run_id, node_name="repair", progress=progress)
    return {**result, "model_calls_made": 1}


# ---------------------------------------------------------------------------
# Budget gate node (Slice 0B)
#
# Reads accumulated counters and produces a typed BudgetDecision.
# Thresholds here are placeholders; real values come from the request's
# ResearchBudget in Slice 1.
# ---------------------------------------------------------------------------
_SYNTHESIS_RESERVE_MODEL_CALLS = 2
_APPROVAL_THRESHOLD_USD = 5.0


def budget_gate(
    state: ResearchGraphState,
    *,
    run_id: str,
    progress: ProgressCallback | None = None,
) -> ResearchGraphState:
    import datetime

    from app.services.agent.models import new_id

    cost = state.get("cost_usd_spent", 0.0)
    tool_calls = state.get("tool_calls_made", 0)
    model_calls = state.get("model_calls_made", 0)

    if cost >= _APPROVAL_THRESHOLD_USD:
        decision = BudgetDecision.REQUIRE_HUMAN_APPROVAL
    elif model_calls + _SYNTHESIS_RESERVE_MODEL_CALLS > 36:
        decision = BudgetDecision.RESERVE_FOR_SYNTHESIS
    elif tool_calls > 20:
        decision = BudgetDecision.CONTINUE_WITH_REDUCED_SEARCH
    else:
        decision = BudgetDecision.CONTINUE

    emit_graph_event(
        progress,
        run_id=run_id,
        node_name="budget_gate",
        message=f"Budget gate: {decision.value}.",
        cost_usd_spent=cost,
        tool_calls_made=tool_calls,
        model_calls_made=model_calls,
        decision=decision.value,
    )

    updates: dict = {"budget_decision": decision}
    if decision == BudgetDecision.REQUIRE_HUMAN_APPROVAL:
        updates["pause_contract"] = {
            "pause_reason": f"Budget ceiling reached: ${cost:.4f} spent.",
            "required_additional_budget_usd": max(0.0, _APPROVAL_THRESHOLD_USD - cost),
            "resume_checkpoint_id": "",  # populated by checkpointer in Slice 6
            "audit_event_id": new_id("lgpause"),
            "paused_at": datetime.datetime.utcnow().isoformat() + "Z",
        }
        # approval_contract intentionally NOT set here — it does not yet exist.
    return updates
