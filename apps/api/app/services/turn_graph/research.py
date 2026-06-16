from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.services.turn_graph.state import TurnGraphState
from app.services.turn_graph.tools import TurnToolOutput


ResearchStage = Literal[
    "decompose",
    "search",
    "crawl",
    "extract",
    "sufficiency",
    "synthesize",
    "verify",
    "complete",
]


_PROGRESS_STAGE_MAP: dict[str, ResearchStage] = {
    "planning": "decompose",
    "searching": "search",
    "reading": "crawl",
    "source_read": "crawl",
    "extracting": "extract",
    "checking": "sufficiency",
    "synthesising": "synthesize",
    "synthesizing": "synthesize",
    "verifying": "verify",
    "complete": "complete",
}


class ResearchSubgraphEvent(BaseModel):
    stage: ResearchStage
    message: str
    elapsed_ms: int = 0
    data: dict[str, Any] = Field(default_factory=dict)


class ResearchToolInput(BaseModel):
    user_id: str
    conversation_id: int | None = None
    query: str
    profile: str | None = None
    force_model: str | None = None
    mode: str = "deep"


ResearchRunner = Callable[..., Any]
ProgressSink = Callable[[str, str, dict[str, Any]], None]
ResearchStageFn = Callable[[TurnGraphState], dict[str, Any] | None]


def research_stage_for_progress(stage: str) -> ResearchStage:
    return _PROGRESS_STAGE_MAP.get(stage, "search")


def research_stage_node(
    state: TurnGraphState,
    stage: ResearchStage,
    *,
    message: str = "",
    fn: ResearchStageFn | None = None,
) -> TurnGraphState:
    """Run or record one callable research subgraph stage.

    The current live path still delegates to `run_research()`, but these nodes
    provide the stable split points for gradually moving the monolith into
    proper graph stages without changing the stage vocabulary or trace shape.
    """

    node = f"research.{stage}"
    started = time.perf_counter()
    state.add_event(node, "started", message)
    try:
        data = fn(state) if fn else None
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if data:
            state.research_progress.append(
                ResearchSubgraphEvent(
                    stage=stage,
                    message=message or f"{stage} complete",
                    elapsed_ms=elapsed_ms,
                    data=data,
                ).model_dump()
            )
        state.add_timing(node, "completed", elapsed_ms)
        state.add_event(node, "completed", message, **(data or {}))
    except Exception as exc:
        state.add_timing(node, "failed", int((time.perf_counter() - started) * 1000), error=str(exc))
        state.add_event(node, "failed", str(exc))
        raise
    return state


def decompose_research_node(state: TurnGraphState, *, fn: ResearchStageFn | None = None) -> TurnGraphState:
    return research_stage_node(state, "decompose", message="Planning research questions", fn=fn)


def search_research_node(state: TurnGraphState, *, fn: ResearchStageFn | None = None) -> TurnGraphState:
    return research_stage_node(state, "search", message="Searching for sources", fn=fn)


def crawl_research_node(state: TurnGraphState, *, fn: ResearchStageFn | None = None) -> TurnGraphState:
    return research_stage_node(state, "crawl", message="Reading sources", fn=fn)


def extract_research_node(state: TurnGraphState, *, fn: ResearchStageFn | None = None) -> TurnGraphState:
    return research_stage_node(state, "extract", message="Extracting claims", fn=fn)


def sufficiency_research_node(state: TurnGraphState, *, fn: ResearchStageFn | None = None) -> TurnGraphState:
    return research_stage_node(state, "sufficiency", message="Checking sufficiency", fn=fn)


def synthesize_research_node(state: TurnGraphState, *, fn: ResearchStageFn | None = None) -> TurnGraphState:
    return research_stage_node(state, "synthesize", message="Synthesizing findings", fn=fn)


def verify_research_node(state: TurnGraphState, *, fn: ResearchStageFn | None = None) -> TurnGraphState:
    return research_stage_node(state, "verify", message="Verifying answer", fn=fn)


def _research_result_payload(result: Any, events: list[ResearchSubgraphEvent]) -> dict[str, Any]:
    run = getattr(result, "run", None)
    llm_result = getattr(result, "result", None)
    route = getattr(result, "route", None)
    return {
        "run_id": getattr(run, "id", None),
        "confidence": getattr(run, "confidence", None),
        "mode": getattr(run, "mode", None),
        "answer": getattr(llm_result, "answer", None),
        "model_used": getattr(llm_result, "model_used", None),
        "latency_ms": getattr(llm_result, "latency_ms", None),
        "estimated_cost_usd": getattr(llm_result, "estimated_cost_usd", None),
        "route_model": getattr(route, "primary_model", None),
        "sources_count": len(getattr(result, "source_logs", []) or []),
        "claims_count": len(getattr(result, "claim_logs", []) or []),
        "questions": getattr(result, "questions", []) or [],
        "gaps": getattr(result, "gaps", []) or [],
        "contradictions": getattr(result, "contradictions", []) or [],
        "verifier_notes": getattr(result, "verifier_notes", None),
        "events": [event.model_dump() for event in events],
    }


def execute_deep_research_tool(
    state: TurnGraphState,
    *,
    db: Any,
    tool_input: ResearchToolInput,
    runner: ResearchRunner,
    progress_sink: ProgressSink | None = None,
) -> TurnToolOutput:
    """Run the current durable research engine behind the graph tool contract."""

    started = time.perf_counter()
    events: list[ResearchSubgraphEvent] = []

    def progress(stage: str, message: str, extra: dict | None = None) -> None:
        event = ResearchSubgraphEvent(
            stage=research_stage_for_progress(stage),
            message=message,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            data=extra or {},
        )
        events.append(event)
        state.research_progress.append(event.model_dump())
        state.add_event(f"research.{event.stage}", "progress", message, **event.data)
        if progress_sink:
            progress_sink(stage, message, extra or {})

    try:
        state.add_event("deep_research", "started", tool_input.query, mode=tool_input.mode)
        result = runner(
            db,
            user_id=tool_input.user_id,
            conversation_id=tool_input.conversation_id,
            query=tool_input.query,
            profile=tool_input.profile,
            force_model=tool_input.force_model,
            mode=tool_input.mode,
            progress=progress,
        )
        state.research_raw_result = result
        payload = _research_result_payload(result, events)
        state.research_result = payload
        state.add_timing("deep_research", "completed", int((time.perf_counter() - started) * 1000))
        state.add_event("deep_research", "completed", "Research complete", run_id=payload.get("run_id"))
        return TurnToolOutput(status="ok", result=payload, user_message=payload.get("answer") or "")
    except Exception as exc:
        state.add_timing("deep_research", "failed", int((time.perf_counter() - started) * 1000), error=str(exc))
        state.add_event("deep_research", "failed", str(exc))
        return TurnToolOutput(status="failed", error=str(exc))
