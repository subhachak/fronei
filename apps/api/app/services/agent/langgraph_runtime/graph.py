from __future__ import annotations

import functools
from typing import Any

from langgraph.graph import END, StateGraph

from app.services.agent.langgraph_runtime import nodes
from app.services.agent.langgraph_runtime.events import ProgressCallback
from app.services.agent.langgraph_runtime.state import BudgetDecision, ResearchGraphState


def _budget_gate_router(state: ResearchGraphState) -> str:
    """Conditional edge: route after the budget gate fires."""
    decision = state.get("budget_decision")
    if decision == BudgetDecision.REQUIRE_HUMAN_APPROVAL:
        return "requires_approval"
    if decision == BudgetDecision.STOP_WITH_GAPS:
        return "stop_with_gaps"
    return "continue"


def build_research_graph(
    run_id: str,
    progress: ProgressCallback | None = None,
) -> Any:  # langgraph.graph.compiled.CompiledStateGraph
    """Build and compile the Slice 0B StateGraph.

    Pipeline shape:
      brief → subject_derivation → contract → plan →
      search → rank → read → classify_claims → expand_source_graph →
      bind → [budget_gate] → synthesize → verify →
      judge → [budget_gate] → repair → END

    The budget gate fires after `bind` (pre-synthesis) and after `judge`
    (pre-repair).  In 0B the nodes are still stubs; real domain logic
    is wired in Slices 1–3.
    """
    graph: StateGraph = StateGraph(ResearchGraphState)

    # --- Stub pipeline nodes ------------------------------------------------
    for node_name in nodes.NODE_ORDER:
        node_fn = getattr(nodes, node_name)
        bound = functools.partial(node_fn, run_id=run_id, progress=progress)
        graph.add_node(node_name, bound)

    # --- Budget gate (fires at two points in the pipeline) ------------------
    # Use distinct node names so each gate instance has its own graph position.
    pre_synthesis_gate = functools.partial(nodes.budget_gate, run_id=run_id, progress=progress)
    pre_repair_gate = functools.partial(nodes.budget_gate, run_id=run_id, progress=progress)
    graph.add_node("budget_gate_pre_synthesis", pre_synthesis_gate)
    graph.add_node("budget_gate_pre_repair", pre_repair_gate)

    # --- Linear edges through the search/read/bind phase --------------------
    pre_gate_sequence = [
        "brief", "subject_derivation", "contract", "plan",
        "search", "rank", "read", "classify_claims", "expand_source_graph", "bind",
    ]
    for i, name in enumerate(pre_gate_sequence[:-1]):
        graph.add_edge(name, pre_gate_sequence[i + 1])
    graph.add_edge("bind", "budget_gate_pre_synthesis")

    # --- Conditional routing after pre-synthesis gate -----------------------
    graph.add_conditional_edges(
        "budget_gate_pre_synthesis",
        _budget_gate_router,
        {
            "continue": "synthesize",
            "stop_with_gaps": END,
            "requires_approval": END,
        },
    )

    # --- Synthesis → verify → judge -----------------------------------------
    graph.add_edge("synthesize", "verify")
    graph.add_edge("verify", "judge")
    graph.add_edge("judge", "budget_gate_pre_repair")

    # --- Conditional routing after pre-repair gate --------------------------
    graph.add_conditional_edges(
        "budget_gate_pre_repair",
        _budget_gate_router,
        {
            "continue": "repair",
            "stop_with_gaps": END,
            "requires_approval": END,
        },
    )

    graph.add_edge("repair", END)
    graph.set_entry_point("brief")
    return graph.compile()


def run_stub_graph(
    initial_state: ResearchGraphState,
    *,
    run_id: str,
    progress: ProgressCallback | None = None,
) -> ResearchGraphState:
    """Execute the Slice 0B stub graph synchronously via LangGraph invoke."""
    compiled = build_research_graph(run_id=run_id, progress=progress)
    return compiled.invoke(initial_state)
