from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


GraphNodeStatus = Literal["pending", "running", "completed", "failed", "skipped", "interrupted"]


class TurnGraphNodeTiming(BaseModel):
    """Timing/trace record for one graph node."""

    node: str
    status: GraphNodeStatus
    latency_ms: int = 0
    meta: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class TurnGraphEvent(BaseModel):
    """Append-only graph event suitable for admin/debug views."""

    node: str
    event: str
    message: str = ""
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    data: dict[str, Any] = Field(default_factory=dict)


class TurnGraphState(BaseModel):
    """Canonical state object for the future LangGraph turn runtime.

    This intentionally mirrors the concepts already spread across
    ConversationTurn, Plan, plan_gate, research/document outputs, and execution
    logs. Fields are optional during the shell phase so the state can wrap the
    current pipeline without forcing a big-bang migration.
    """

    conversation_id: str | None = None
    turn_id: str | None = None
    user_id: str | None = None
    user_message: str
    profile: str = "balanced"
    quality_mode: str = "standard"

    history: list[dict[str, Any]] = Field(default_factory=list)
    user_memory: str = ""
    running_summary: str = ""
    active_task: dict[str, Any] | None = None
    doc_context: str = ""
    artifact_context: str = ""

    triage_decision: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    gate: dict[str, Any] | None = None
    selected_tools: list[dict[str, Any]] = Field(default_factory=list)
    accepted_tools: list[dict[str, Any]] = Field(default_factory=list)
    declined_tools: list[dict[str, Any]] = Field(default_factory=list)

    web_context: dict[str, Any] | None = None
    research_progress: list[dict[str, Any]] = Field(default_factory=list)
    research_queries: list[str] = Field(default_factory=list)
    research_sources: list[dict[str, Any]] = Field(default_factory=list)
    research_claims: list[dict[str, Any]] = Field(default_factory=list)
    research_result: dict[str, Any] | None = None
    research_raw_result: Any = Field(default=None, exclude=True)
    document_brief: dict[str, Any] | None = None
    document_content: str | None = None
    document_result: dict[str, Any] | None = None
    document_raw_result: Any = Field(default=None, exclude=True)
    artifact_result: dict[str, Any] | None = None

    final_answer: str | None = None
    error: str | None = None
    status: Literal["pending", "running", "completed", "failed", "interrupted"] = "pending"

    node_timings: list[TurnGraphNodeTiming] = Field(default_factory=list)
    events: list[TurnGraphEvent] = Field(default_factory=list)

    def add_event(self, node: str, event: str, message: str = "", **data: Any) -> None:
        self.events.append(TurnGraphEvent(node=node, event=event, message=message, data=data))

    def add_timing(
        self,
        node: str,
        status: GraphNodeStatus,
        latency_ms: int = 0,
        **meta: Any,
    ) -> None:
        self.node_timings.append(TurnGraphNodeTiming(
            node=node,
            status=status,
            latency_ms=latency_ms,
            meta={k: v for k, v in meta.items() if v is not None},
        ))
