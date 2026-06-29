from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.services.agent.langgraph_runtime.events import ProgressCallback, emit_graph_event
from app.services.agent.langgraph_runtime.state import (
    BudgetDecision,
    GraphNodeName,
    ResearchGraphState,
)

if TYPE_CHECKING:
    from app.services.agent.models import TurnRequest

logger = logging.getLogger(__name__)


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
    return {
        **state,
        "visited_nodes": visited,
        "artifacts": artifacts,
        "cost_usd_spent": 0.0,
        "tool_calls_made": 0,
        "model_calls_made": 0,
    }


# ---------------------------------------------------------------------------
# Slice 1 real nodes
# ---------------------------------------------------------------------------

def brief(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    progress: ProgressCallback | None = None,
) -> ResearchGraphState:
    """Call generate_research_brief; write ResearchBrief to state."""
    from app.services.agent.research_profiles import generate_research_brief

    visited = [*state.get("visited_nodes", []), "brief"]
    try:
        research_brief = generate_research_brief(request)
    except Exception as exc:
        logger.warning("brief node: generate_research_brief failed: %s", exc)
        from app.services.agent.research_models import ResearchBrief
        research_brief = ResearchBrief(
            objective=request.message,
            research_level=getattr(request, "research_level", "regular"),
        )

    cost = getattr(research_brief, "cost_usd", 0.0) or 0.0
    emit_graph_event(
        progress,
        run_id=run_id,
        node_name="brief",
        message=f"Research brief generated (profile={research_brief.research_profile}).",
        research_profile=research_brief.research_profile,
        cost_usd_spent=cost,
        tool_calls_made=0,
        model_calls_made=1,
    )
    return {
        **state,
        "visited_nodes": visited,
        "artifacts": {**state.get("artifacts", {}), "brief": {"status": "done"}},
        "brief": research_brief,
        "cost_usd_spent": cost,
        "tool_calls_made": 0,
        "model_calls_made": 1,
    }


def subject_derivation(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    progress: ProgressCallback | None = None,
) -> ResearchGraphState:
    """Derive named_subjects from request text + brief.

    Uses _extract_named_comparison_subjects(request.message) as the primary
    source. Validates against brief.scope_in as a sanity check (not a gating
    condition). Fallback to [] when no named entities are found.

    Spec correction from v1: the generic-template fallback in the contract node
    is gated on named_subjects being empty — NOT on scope_in being empty.
    scope_in is almost always populated with dimension labels, so that check
    would essentially never fire.
    """
    from app.services.agent.research_contracts import _extract_named_comparison_subjects

    visited = [*state.get("visited_nodes", []), "subject_derivation"]
    research_brief = state.get("brief")

    named_subjects = _extract_named_comparison_subjects(request.message)

    # Sanity check: if brief.scope_in contains candidate subjects that look
    # like proper nouns (not dimension labels), surface them as a warning but
    # do not override — the message-level extraction is authoritative.
    if research_brief and research_brief.scope_in:
        brief_scope = [s.strip() for s in research_brief.scope_in if len(s.strip()) > 1]
        if brief_scope and not named_subjects:
            # scope_in terms are usually dimension labels ("regulatory compliance",
            # "total cost of ownership") not entity names. Log them for visibility
            # but don't promote them to named_subjects automatically.
            logger.debug(
                "subject_derivation: no named subjects from message; brief.scope_in=%r (not promoted)",
                brief_scope[:6],
            )

    emit_graph_event(
        progress,
        run_id=run_id,
        node_name="subject_derivation",
        message=f"Derived {len(named_subjects)} named subject(s): {named_subjects}.",
        named_subjects=named_subjects,
        cost_usd_spent=0.0,
        tool_calls_made=0,
        model_calls_made=0,
    )
    return {
        **state,
        "visited_nodes": visited,
        "artifacts": {**state.get("artifacts", {}), "subject_derivation": {"status": "done"}},
        "named_subjects": named_subjects,
        "cost_usd_spent": 0.0,
        "tool_calls_made": 0,
        "model_calls_made": 0,
    }


def contract(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    progress: ProgressCallback | None = None,
) -> ResearchGraphState:
    """Call generate_coverage_contract with pre-derived named_subjects."""
    from app.services.agent.research_contracts import generate_coverage_contract

    visited = [*state.get("visited_nodes", []), "contract"]
    research_brief = state.get("brief")
    named_subjects = state.get("named_subjects") or []

    if research_brief is None:
        from app.services.agent.research_models import ResearchBrief
        research_brief = ResearchBrief(
            objective=request.message,
            research_level=getattr(request, "research_level", "regular"),
        )

    coverage_contract = generate_coverage_contract(
        request, research_brief, named_subjects=named_subjects or None
    )

    emit_graph_event(
        progress,
        run_id=run_id,
        node_name="contract",
        message=f"Coverage contract built: {len(coverage_contract.subjects)} subject(s), {len(coverage_contract.cells)} cell(s).",
        subjects=coverage_contract.subjects,
        cell_count=len(coverage_contract.cells),
        cost_usd_spent=0.0,
        tool_calls_made=0,
        model_calls_made=0,
    )
    return {
        **state,
        "visited_nodes": visited,
        "artifacts": {**state.get("artifacts", {}), "contract": {"status": "done"}},
        "contract": coverage_contract,
        "named_subjects": coverage_contract.subjects,
        "cost_usd_spent": 0.0,
        "tool_calls_made": 0,
        "model_calls_made": 0,
    }


def plan(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    progress: ProgressCallback | None = None,
) -> ResearchGraphState:
    """Call plan_from_contract to build the ResearchPlan."""
    from app.services.agent.research_planner import plan_from_contract

    visited = [*state.get("visited_nodes", []), "plan"]
    coverage_contract = state.get("contract")

    if coverage_contract is None:
        # Fallback: use plan_research for a pure LLM plan
        from app.services.agent.research_planner import plan_research
        research_plan = plan_research(request)
    else:
        research_plan = plan_from_contract(request, coverage_contract)

    emit_graph_event(
        progress,
        run_id=run_id,
        node_name="plan",
        message=f"Research plan built: {len(research_plan.workers)} worker(s).",
        worker_count=len(research_plan.workers),
        research_profile=research_plan.research_profile,
        cost_usd_spent=0.0,
        tool_calls_made=0,
        model_calls_made=0,
    )
    return {
        **state,
        "visited_nodes": visited,
        "artifacts": {**state.get("artifacts", {}), "plan": {"status": "done"}},
        "plan": research_plan,
        "cost_usd_spent": 0.0,
        "tool_calls_made": 0,
        "model_calls_made": 0,
    }


# ---------------------------------------------------------------------------
# Remaining stub nodes (Slice 2+)
# ---------------------------------------------------------------------------

def search(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    progress: ProgressCallback | None = None,
) -> ResearchGraphState:
    result = _stub_node(state, run_id=run_id, node_name="search", progress=progress)
    return {**result, "sources": [], "worker_reports": [], "tool_calls": [], "provider_attempts": []}


def rank(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    progress: ProgressCallback | None = None,
) -> ResearchGraphState:
    return _stub_node(state, run_id=run_id, node_name="rank", progress=progress)


def read(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    progress: ProgressCallback | None = None,
) -> ResearchGraphState:
    result = _stub_node(state, run_id=run_id, node_name="read", progress=progress)
    return {**result, "tool_calls": []}


def classify_claims(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    progress: ProgressCallback | None = None,
) -> ResearchGraphState:
    result = _stub_node(state, run_id=run_id, node_name="classify_claims", progress=progress)
    return {**result, "claim_classification_results": []}


def expand_source_graph(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    progress: ProgressCallback | None = None,
) -> ResearchGraphState:
    result = _stub_node(state, run_id=run_id, node_name="expand_source_graph", progress=progress)
    return {**result, "source_graph_expansion_results": []}


def bind(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    progress: ProgressCallback | None = None,
) -> ResearchGraphState:
    return _stub_node(state, run_id=run_id, node_name="bind", progress=progress)


def synthesize(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    progress: ProgressCallback | None = None,
) -> ResearchGraphState:
    result = _stub_node(state, run_id=run_id, node_name="synthesize", progress=progress)
    return {
        **result,
        "answer": "",
        "model_used": "langgraph-slice-1-stub",
        "latency_ms": 0,
        "model_calls_made": 1,
    }


def verify(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    progress: ProgressCallback | None = None,
) -> ResearchGraphState:
    return _stub_node(state, run_id=run_id, node_name="verify", progress=progress)


def judge(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    progress: ProgressCallback | None = None,
) -> ResearchGraphState:
    result = _stub_node(state, run_id=run_id, node_name="judge", progress=progress)
    return {**result, "next_action": "publish", "model_calls_made": 1}


def repair(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    progress: ProgressCallback | None = None,
) -> ResearchGraphState:
    result = _stub_node(state, run_id=run_id, node_name="repair", progress=progress)
    return {**result, "model_calls_made": 1}


# ---------------------------------------------------------------------------
# Budget gate node
# ---------------------------------------------------------------------------

_SYNTHESIS_RESERVE_MODEL_CALLS = 2
_APPROVAL_THRESHOLD_USD = 5.0


def budget_gate(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
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
            "resume_checkpoint_id": "",
            "audit_event_id": new_id("lgpause"),
            "paused_at": datetime.datetime.utcnow().isoformat() + "Z",
        }
    return updates
