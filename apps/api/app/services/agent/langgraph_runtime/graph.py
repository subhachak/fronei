from __future__ import annotations

import functools
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph

from app.services.agent.langgraph_runtime.checkpointer import get_checkpointer
from app.services.agent.langgraph_runtime import nodes
from app.services.agent.langgraph_runtime.events import ProgressCallback
from app.services.agent.langgraph_runtime.state import BudgetDecision, ResearchGraphState

if TYPE_CHECKING:
    from app.services.agent.models import TurnRequest
    from app.services.agent.tools import Tools


def _run_config(
    *,
    run_id: str,
    request: TurnRequest,
    tools: Tools | None = None,
    progress: ProgressCallback | None = None,
) -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": run_id,
            "run_id": run_id,
            "request": request,
            "tools": tools,
            "progress": progress,
            "preserve_tools_none": tools is None,
        },
        "metadata": {
            "run_id": run_id,
            "research_level": getattr(request, "research_level", None),
            "conversation_id": getattr(request, "conversation_id", None),
            "orchestrator": "langgraph",
        },
        "tags": ["fronei", "research", "langgraph"],
    }


class _BoundResearchGraph:
    def __init__(self, compiled: Any, config: RunnableConfig, *, reset_thread: bool = False) -> None:
        self._compiled = compiled
        self._config = config
        self._reset_thread = reset_thread

    def __getattr__(self, name: str) -> Any:
        return getattr(self._compiled, name)

    def invoke(self, input: Any, config: RunnableConfig | None = None, **kwargs: Any) -> Any:
        effective_config = config or self._config
        if self._reset_thread:
            _delete_thread(self._compiled, effective_config)
        return self._compiled.invoke(input, config=effective_config, **kwargs)

    def stream(self, input: Any, config: RunnableConfig | None = None, **kwargs: Any) -> Any:
        effective_config = config or self._config
        if self._reset_thread:
            _delete_thread(self._compiled, effective_config)
        return self._compiled.stream(input, config=effective_config, **kwargs)


def _delete_thread(compiled: Any, config: RunnableConfig) -> None:
    thread_id = ((config or {}).get("configurable") or {}).get("thread_id")
    checkpointer = getattr(compiled, "checkpointer", None)
    if thread_id and hasattr(checkpointer, "delete_thread"):
        checkpointer.delete_thread(thread_id)


def _budget_gate_router(state: ResearchGraphState) -> str:
    """Conditional edge: route after the budget gate fires."""
    decision = state.get("budget_decision")
    if decision == BudgetDecision.REQUIRE_HUMAN_APPROVAL:
        return "requires_approval"
    if decision == BudgetDecision.STOP_WITH_GAPS:
        return "stop_with_gaps"
    return "continue"


def _relevance_gate_router(state: ResearchGraphState) -> str:
    """Conditional edge: route after relevance_gate scores aggregated search
    results.

    "insufficient" → budget_gate_pre_synthesis → synthesize (skips rank/read/
        classify_claims/expand_source_graph/bind; relevance_gate already set
        state["evidence"] to a gap-only EvidencePack for this path, and
        routing through the existing budget gate rather than straight to
        synthesize preserves its cost/approval checks instead of bypassing them)
    "continue"      → rank (normal path)
    """
    if state.get("insufficient_relevant_evidence"):
        return "insufficient"
    return "continue"


def _judge_router(state: ResearchGraphState) -> str:
    """Conditional edge: route after the judge fires, keyed on next_action.

    "publish"        → END (answer is ready; skip budget gate and repair)
    "stop_with_gaps" → END (judge failed; repair cannot recover)
    "requires_approval" → END (budget approval needed before repair)
    "research_more"  → budget_gate_pre_repair → repair (judge or verifier requested repair)
    default          → END (treat unknown values as publish)
    """
    next_action = state.get("next_action", "publish")
    if next_action == "research_more":
        return "repair_gate"
    return "publish_end"


def _runtime_context(state: ResearchGraphState, config: RunnableConfig | None) -> dict[str, Any]:
    configurable = (config or {}).get("configurable") or {}
    request = configurable.get("request")
    if request is None:
        request_payload = state.get("request_payload")
        if isinstance(request_payload, dict):
            from app.services.agent.models import TurnRequest

            request = TurnRequest.model_validate(request_payload)
    tools = configurable.get("tools")
    if tools is None and not configurable.get("preserve_tools_none"):
        from app.services.agent.tools import Tools

        tools = Tools.from_settings()
    run_id = configurable.get("run_id") or configurable.get("thread_id") or "langgraph-run"
    return {
        "run_id": run_id,
        "request": request,
        "tools": tools,
        "progress": configurable.get("progress"),
    }


def _node_adapter(node_name: str) -> Callable[..., Any]:
    def wrapped(state: ResearchGraphState, config: RunnableConfig = None) -> Any:
        ctx = _runtime_context(state, config)
        node_fn = getattr(nodes, node_name)
        return node_fn(state, **ctx)

    return wrapped


def _router_adapter(router_name: str) -> Callable[..., Any]:
    def wrapped(state: ResearchGraphState, config: RunnableConfig = None) -> Any:
        ctx = _runtime_context(state, config)
        router_fn = getattr(nodes, router_name)
        return router_fn(state, **ctx)

    return wrapped


@functools.lru_cache(maxsize=1)
def get_compiled_research_graph() -> Any:  # langgraph.graph.compiled.CompiledStateGraph
    """Build and compile the Slice 3 StateGraph.

    Pipeline shape:
      brief → subject_derivation → contract → plan →
      dispatch_search ──(Send×N)──► search_worker (parallel)
                                         │
                                         ▼
      relevance_gate → [_relevance_gate_router] →
          "continue"     → rank → read → classify_claims → expand_source_graph →
                            bind → [budget_gate_pre_synthesis] → synthesize → ...
          "insufficient" → budget_gate_pre_synthesis → synthesize → ...
              (one retry already attempted inside relevance_gate itself before
              this routing decision; skips rank/read/classify_claims/
              expand_source_graph/bind on evidence that scored below
              the configured relevance threshold against the research target even after the
              retry -- relevance_gate sets state["evidence"] to a gap-only
              EvidencePack itself for this path, since bind never runs)
      synthesize → verify →
      judge → [_judge_router] →
          "publish_end"  → END           (judge approved or unrecoverable fail)
          "repair_gate"  → budget_gate_pre_repair →
              "continue"         → repair → END
              "stop_with_gaps"   → END
              human approval pauses via LangGraph interrupt() inside the gate

    All nodes are real domain-function calls (Slice 4 complete).
    classify_claims pre-classifies sources and stores results in state;
    bind reads them via pre_classified_by_url to skip duplicate LLM calls.
    """
    graph: StateGraph = StateGraph(ResearchGraphState)

    # --- Pipeline nodes: request-scoped data is read from RunnableConfig. ----
    for node_name in nodes.NODE_ORDER:
        graph.add_node(node_name, _node_adapter(node_name))

    # --- Budget gate (fires at two points in the pipeline) ------------------
    graph.add_node(
        "budget_gate_pre_synthesis",
        _node_adapter("budget_gate"),
    )
    graph.add_node(
        "budget_gate_pre_repair",
        _node_adapter("budget_gate"),
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
        _router_adapter("dispatch_search_router"),
    )
    # search_worker → relevance_gate is the fan-in join point (all Send
    # invocations complete before relevance_gate runs once with the full
    # aggregated state["sources"]).
    graph.add_edge("search_worker", "relevance_gate")
    graph.add_conditional_edges(
        "relevance_gate",
        _relevance_gate_router,
        {
            "continue": "rank",
            "insufficient": "budget_gate_pre_synthesis",
        },
    )

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

    # Judge sets next_action; _judge_router reads it and decides whether
    # repair is warranted.  "publish" and unrecoverable cases go straight to END.
    # Only "research_more" (judge or citation verifier requested repair) passes
    # through the budget gate before repair.
    graph.add_conditional_edges(
        "judge",
        _judge_router,
        {
            "repair_gate": "budget_gate_pre_repair",
            "publish_end": END,
        },
    )

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
    return graph.compile(checkpointer=get_checkpointer())


def build_research_graph(
    run_id: str,
    request: TurnRequest,
    progress: ProgressCallback | None = None,
    tools: Tools | None = None,
) -> Any:  # langgraph.graph.compiled.CompiledStateGraph
    """Backward-compatible graph builder API.

    The graph is now compiled once. Per-run values are supplied at invoke/stream
    time through RunnableConfig so no request data is captured in the graph.
    """
    return _BoundResearchGraph(
        get_compiled_research_graph(),
        _run_config(run_id=run_id, request=request, progress=progress, tools=tools),
        reset_thread=True,
    )


def run_stub_graph(
    initial_state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    progress: ProgressCallback | None = None,
    tools: Tools | None = None,
) -> ResearchGraphState:
    """Execute the graph synchronously via LangGraph invoke."""
    compiled = get_compiled_research_graph()
    config = _run_config(run_id=run_id, request=request, progress=progress, tools=tools)
    _delete_thread(compiled, config)
    seeded_state = dict(initial_state)
    seeded_state.setdefault("run_id", run_id)
    if hasattr(request, "model_dump"):
        seeded_state.setdefault("request_payload", request.model_dump(mode="json"))
    result = compiled.invoke(seeded_state, config=config)
    if isinstance(result, dict) and "__interrupt__" in result:
        snapshot = compiled.get_state(config)
        values = dict(getattr(snapshot, "values", None) or {})
        interrupts = result.get("__interrupt__") or ()
        if interrupts:
            interrupt = interrupts[-1]
            payload = getattr(interrupt, "value", interrupt)
            if isinstance(payload, dict):
                values["pause_contract"] = payload
        values["budget_decision"] = BudgetDecision.REQUIRE_HUMAN_APPROVAL
        values["interrupted"] = True
        return values
    return result
