from __future__ import annotations

from typing import Any

from app.services.agent.langgraph_runtime.events import ProgressCallback, emit_graph_event
from app.services.agent.langgraph_runtime.state import GraphNodeName, ResearchGraphState


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
    )
    return {**state, "visited_nodes": visited, "artifacts": artifacts}


def brief(state: ResearchGraphState, *, run_id: str, progress: ProgressCallback | None = None) -> ResearchGraphState:
    return _stub_node(state, run_id=run_id, node_name="brief", progress=progress)


def subject_derivation(
    state: ResearchGraphState, *, run_id: str, progress: ProgressCallback | None = None
) -> ResearchGraphState:
    return _stub_node(state, run_id=run_id, node_name="subject_derivation", progress=progress)


def contract(state: ResearchGraphState, *, run_id: str, progress: ProgressCallback | None = None) -> ResearchGraphState:
    return _stub_node(state, run_id=run_id, node_name="contract", progress=progress)


def plan(state: ResearchGraphState, *, run_id: str, progress: ProgressCallback | None = None) -> ResearchGraphState:
    return _stub_node(state, run_id=run_id, node_name="plan", progress=progress)


def search(state: ResearchGraphState, *, run_id: str, progress: ProgressCallback | None = None) -> ResearchGraphState:
    return _stub_node(state, run_id=run_id, node_name="search", progress=progress)


def rank(state: ResearchGraphState, *, run_id: str, progress: ProgressCallback | None = None) -> ResearchGraphState:
    return _stub_node(state, run_id=run_id, node_name="rank", progress=progress)


def read(state: ResearchGraphState, *, run_id: str, progress: ProgressCallback | None = None) -> ResearchGraphState:
    return _stub_node(state, run_id=run_id, node_name="read", progress=progress)


def classify_claims(
    state: ResearchGraphState, *, run_id: str, progress: ProgressCallback | None = None
) -> ResearchGraphState:
    return _stub_node(state, run_id=run_id, node_name="classify_claims", progress=progress)


def expand_source_graph(
    state: ResearchGraphState, *, run_id: str, progress: ProgressCallback | None = None
) -> ResearchGraphState:
    return _stub_node(state, run_id=run_id, node_name="expand_source_graph", progress=progress)


def bind(state: ResearchGraphState, *, run_id: str, progress: ProgressCallback | None = None) -> ResearchGraphState:
    return _stub_node(state, run_id=run_id, node_name="bind", progress=progress)


def synthesize(
    state: ResearchGraphState, *, run_id: str, progress: ProgressCallback | None = None
) -> ResearchGraphState:
    next_state = _stub_node(state, run_id=run_id, node_name="synthesize", progress=progress)
    return {**next_state, "answer": "", "model_used": "langgraph-slice-0a-stub", "cost_usd": 0.0, "latency_ms": 0}


def verify(state: ResearchGraphState, *, run_id: str, progress: ProgressCallback | None = None) -> ResearchGraphState:
    return _stub_node(state, run_id=run_id, node_name="verify", progress=progress)


def judge(state: ResearchGraphState, *, run_id: str, progress: ProgressCallback | None = None) -> ResearchGraphState:
    return _stub_node(state, run_id=run_id, node_name="judge", progress=progress)


def repair(state: ResearchGraphState, *, run_id: str, progress: ProgressCallback | None = None) -> ResearchGraphState:
    return _stub_node(state, run_id=run_id, node_name="repair", progress=progress)
