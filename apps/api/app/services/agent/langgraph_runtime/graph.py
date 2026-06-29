from __future__ import annotations

import functools
from typing import Any

from langgraph.graph import END, StateGraph

from app.services.agent.langgraph_runtime import nodes
from app.services.agent.langgraph_runtime.events import ProgressCallback
from app.services.agent.langgraph_runtime.state import ResearchGraphState


def build_research_graph(
    run_id: str,
    progress: ProgressCallback | None = None,
) -> Any:  # langgraph.graph.compiled.CompiledStateGraph
    """Build and compile the Slice 0A stub StateGraph.

    All nodes are stubs that return placeholder state. The graph executes
    linearly through the full pipeline. Non-linear edges (judge → repair
    loop, budget gates) are introduced in later slices.

    Node functions accept (state, *, run_id, progress); we bind the
    non-state parameters via functools.partial so each LangGraph node
    receives only the state dict it expects.
    """
    graph: StateGraph = StateGraph(ResearchGraphState)

    for node_name in nodes.NODE_ORDER:
        node_fn = getattr(nodes, node_name)
        bound = functools.partial(node_fn, run_id=run_id, progress=progress)
        graph.add_node(node_name, bound)

    # Linear edges through the pipeline — Slice 0A has no branching.
    for i, node_name in enumerate(nodes.NODE_ORDER[:-1]):
        graph.add_edge(node_name, nodes.NODE_ORDER[i + 1])
    graph.add_edge(nodes.NODE_ORDER[-1], END)

    graph.set_entry_point(nodes.NODE_ORDER[0])
    return graph.compile()


def run_stub_graph(
    initial_state: ResearchGraphState,
    *,
    run_id: str,
    progress: ProgressCallback | None = None,
) -> ResearchGraphState:
    """Execute the Slice 0A stub graph synchronously via LangGraph invoke."""
    compiled = build_research_graph(run_id=run_id, progress=progress)
    return compiled.invoke(initial_state)
