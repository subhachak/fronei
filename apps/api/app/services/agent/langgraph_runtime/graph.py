from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any

from langgraph.graph import END, StateGraph

from app.services.agent.langgraph_runtime import nodes
from app.services.agent.langgraph_runtime.events import ProgressCallback
from app.services.agent.langgraph_runtime.state import BudgetDecision, ResearchGraphState

if TYPE_CHECKING:
    from app.services.agent.models import TurnRequest
    from app.services.agent.tools import Tools


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
    request: TurnRequest,
    progress: ProgressCallback | None = None,
    tools: Tools | None = None,
) -> Any:  # langgraph.graph.compiled.CompiledStateGraph
    """Build and compile the Slice 2 StateGraph.

    Pipeline shape:
      brief → subject_derivation → contract → plan →
      dispatch_search ──(Send×N)──► search_worker (parallel)
                                         │
                                         ▼
      rank → read → classify_claims → expand_source_graph →
      bind → [budget_gate] → synthesize → verify →
      judge → [budget_gate] → repair → END

    Slice 2: search fan-out (dispatch_search/search_worker), rank, read,
             classify_claims, expand_source_graph, bind nodes are real.
    synthesize/verify/judge/repair remain stubs until Slice 3.
    """
    graph: StateGraph = StateGraph(ResearchGraphState)

    # --- Pipeline nodes: each bound with run_id, request, tools, progress ----
    for node_name in nodes.NODE_ORDER:
        node_fn = getattr(nodes, node_name)
        bound = functools.partial(
            node_fn,
            run_id=run_id,
            request=request,
            tools=tools,
            progress=progress,
        )
        graph.add_node(node_name, bound)

    # --- Budget gate (fires at two points in the pipeline) ------------------
    gate_kwargs = dict(run_id=run_id, request=request, tools=tools, progress=progress)
    graph.add_node(
        "budget_gate_pre_synthesis",
        functools.partial(nodes.budget_gate, **gate_kwargs),
    )
    graph.add_node(
        "budget_gate_pre_repair",
        functools.partial(nodes.budget_gate, **gate_kwargs),
    )

    # --- Linear pre-search edges --------------------------------------------
    pre_search_sequence = ["brief", "subject_derivation", "contract", "plan"]
    for i, name in enumerate(pre_search_sequence[:-1]):
        graph.add_edge(name, pre_search_sequence[i + 1])
    graph.add_edge("plan", "dispatch_search")

    # dispatch_search → search_worker fan-out via Send routing function.
    # dispatch_search_router returns either [Send("search_worker", ...)] or ["rank"].
    # After all search_worker invocations complete, execution continues at rank.
    graph.add_conditional_edges(
        "dispatch_search",
        functools.partial(
            nodes.dispatch_search_router,
            run_id=run_id,
            request=request,
            tools=tools,
            progress=progress,
        ),
    )
    graph.add_edge("search_worker", "rank")

    # --- Linear post-search edges through bind ------------------------------
    post_search_sequence = [
        "rank", "read", "classify_claims", "expand_source_graph", "bind",
    ]
    for i, name in enumerate(post_search_sequence[:-1]):
        graph.add_edge(name, post_search_sequence[i + 1])
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
    request: TurnRequest,
    progress: ProgressCallback | None = None,
    tools: Tools | None = None,
) -> ResearchGraphState:
    """Execute the graph synchronously via LangGraph invoke."""
    compiled = build_research_graph(
        run_id=run_id, request=request, progress=progress, tools=tools
    )
    return compiled.invoke(initial_state)
