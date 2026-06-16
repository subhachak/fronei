from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.services.turn_graph.state import TurnGraphState
from app.services.turn_graph.tools import TurnToolOutput


DocumentStage = Literal[
    "content_plan",
    "design_plan",
    "render",
    "qa_polish",
    "final_preview",
]


class DocumentSubgraphEvent(BaseModel):
    stage: DocumentStage
    message: str
    elapsed_ms: int = 0
    data: dict[str, Any] = Field(default_factory=dict)


class DocumentGenerationToolInput(BaseModel):
    title: str = "Fronei document"
    doc_type: str = "document"
    format: str = "markdown"
    quality_mode: str = "standard"
    template_id: str | None = None
    template_path: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class ArtifactRenderToolInput(BaseModel):
    title: str
    body: str
    doc_type: str
    format: str = "markdown"
    template_id: str | None = None
    template_path: str | None = None
    quality_mode: str = "standard"
    defer_render_qa: bool = False


DocumentStageFn = Callable[[TurnGraphState], dict[str, Any] | None]
DocumentGenerator = Callable[..., tuple[Any, str, str, str]]
ArtifactRenderer = Callable[..., dict[str, Any]]
QAPolisher = Callable[..., dict[str, Any] | None]


def document_stage_node(
    state: TurnGraphState,
    stage: DocumentStage,
    *,
    message: str = "",
    fn: DocumentStageFn | None = None,
) -> TurnGraphState:
    node = f"document.{stage}"
    started = time.perf_counter()
    state.add_event(node, "started", message)
    try:
        data = fn(state) if fn else None
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        state.add_timing(node, "completed", elapsed_ms)
        event = DocumentSubgraphEvent(
            stage=stage,
            message=message or f"{stage} complete",
            elapsed_ms=elapsed_ms,
            data=data or {},
        )
        state.add_event(node, "completed", event.message, **event.data)
        if state.document_result is None:
            state.document_result = {"events": []}
        state.document_result.setdefault("events", []).append(event.model_dump())
    except Exception as exc:
        state.add_timing(node, "failed", int((time.perf_counter() - started) * 1000), error=str(exc))
        state.add_event(node, "failed", str(exc))
        raise
    return state


def content_plan_node(state: TurnGraphState, *, fn: DocumentStageFn | None = None) -> TurnGraphState:
    return document_stage_node(state, "content_plan", message="Planning document content", fn=fn)


def design_plan_node(state: TurnGraphState, *, fn: DocumentStageFn | None = None) -> TurnGraphState:
    return document_stage_node(state, "design_plan", message="Planning document design", fn=fn)


def render_artifact_node(state: TurnGraphState, *, fn: DocumentStageFn | None = None) -> TurnGraphState:
    return document_stage_node(state, "render", message="Rendering artifact", fn=fn)


def qa_polish_node(state: TurnGraphState, *, fn: DocumentStageFn | None = None) -> TurnGraphState:
    return document_stage_node(state, "qa_polish", message="Quality checking artifact", fn=fn)


def final_preview_node(state: TurnGraphState, *, fn: DocumentStageFn | None = None) -> TurnGraphState:
    return document_stage_node(state, "final_preview", message="Preparing final preview", fn=fn)


def execute_generate_document_tool(
    state: TurnGraphState,
    *,
    tool_input: DocumentGenerationToolInput,
    generator: DocumentGenerator,
    **kwargs: Any,
) -> TurnToolOutput:
    started = time.perf_counter()
    state.add_event("generate_document", "started", tool_input.title, format=tool_input.format)
    try:
        llm_result, body, summary, doc_type = generator(**kwargs)
        payload = {
            "title": tool_input.title,
            "doc_type": doc_type,
            "format": tool_input.format,
            "quality_mode": tool_input.quality_mode,
            "body": body,
            "summary": summary,
            "model_used": getattr(llm_result, "model_used", None),
            "latency_ms": getattr(llm_result, "latency_ms", None),
            "estimated_cost_usd": getattr(llm_result, "estimated_cost_usd", None),
        }
        state.document_result = payload
        state.document_raw_result = (llm_result, body, summary, doc_type)
        state.add_timing("generate_document", "completed", int((time.perf_counter() - started) * 1000))
        state.add_event("generate_document", "completed", "Document content ready", doc_type=doc_type)
        return TurnToolOutput(status="ok", result=payload, user_message=summary)
    except Exception as exc:
        state.add_timing("generate_document", "failed", int((time.perf_counter() - started) * 1000), error=str(exc))
        state.add_event("generate_document", "failed", str(exc))
        return TurnToolOutput(status="failed", error=str(exc))


def execute_render_artifact_tool(
    state: TurnGraphState,
    *,
    tool_input: ArtifactRenderToolInput,
    renderer: ArtifactRenderer,
) -> TurnToolOutput:
    started = time.perf_counter()
    state.add_event("render_artifact", "started", tool_input.title, format=tool_input.format)
    try:
        preview = renderer(
            tool_input.title,
            tool_input.body,
            tool_input.doc_type,
            tool_input.format,
            template_id=tool_input.template_id,
            template_path=tool_input.template_path,
            quality_mode=tool_input.quality_mode,
            defer_render_qa=tool_input.defer_render_qa,
        )
        state.artifact_result = preview
        state.add_timing("render_artifact", "completed", int((time.perf_counter() - started) * 1000))
        state.add_event("render_artifact", "completed", "Artifact preview ready", format=preview.get("format"))
        return TurnToolOutput(status="ok", result=preview, user_message=preview.get("title") or tool_input.title)
    except Exception as exc:
        state.add_timing("render_artifact", "failed", int((time.perf_counter() - started) * 1000), error=str(exc))
        state.add_event("render_artifact", "failed", str(exc))
        return TurnToolOutput(status="failed", error=str(exc))


def execute_quality_check_tool(
    state: TurnGraphState,
    *,
    preview: dict[str, Any],
    checker: QAPolisher,
    **kwargs: Any,
) -> TurnToolOutput:
    started = time.perf_counter()
    state.add_event("quality_check", "started", preview.get("title", "artifact"))
    try:
        qa = checker(preview, **kwargs)
        result = {"render_qa": qa, "available": bool(qa)}
        if state.artifact_result is None:
            state.artifact_result = dict(preview)
        if qa is not None:
            state.artifact_result["render_qa"] = qa
        state.add_timing("quality_check", "completed", int((time.perf_counter() - started) * 1000))
        state.add_event("quality_check", "completed", "Quality check complete", available=bool(qa))
        return TurnToolOutput(status="ok", result=result)
    except Exception as exc:
        state.add_timing("quality_check", "failed", int((time.perf_counter() - started) * 1000), error=str(exc))
        state.add_event("quality_check", "failed", str(exc))
        return TurnToolOutput(status="failed", error=str(exc))
