from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


RouteName = Literal["direct", "clarify", "research", "document", "research_document"]
ResearchLevel = Literal["auto", "easy", "regular", "deep"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:24]}"


class TurnRequest(BaseModel):
    message: str = Field(min_length=1)
    conversation_id: str | None = None
    conversation_context: str = ""
    # Prior completed-turn context only. Unlike conversation_context, this
    # should not include attachment text or generic workspace/profile context.
    # It lets routers distinguish real conversational grounding from other
    # useful context sources. EPIC-03 should materialize this as an L1
    # ContextItem with scope="conversation" and source_type="prior_turn"; this
    # field remains the compatibility bridge for current router/orchestrator
    # call sites.
    prior_turn_context: str = ""
    quality_mode: Literal["draft", "standard", "executive"] = "standard"
    research_level: ResearchLevel = "auto"
    confirm_deep_research: bool = False
    comparison_mode: bool = False
    force_route: RouteName | None = None
    output_format: Literal["chat", "markdown", "docx", "pptx"] = "chat"
    template_id: str | None = None
    # Admin-only per-turn model override (role -> litellm model string). The
    # org-wide default lives in the DB-backed model policy
    # (app/services/agent/model_policy.py, admin-editable at
    # /admin/model-policy); this field lets an admin test a
    # different model for one turn without changing that default. Silently
    # stripped server-side for non-admins -- see routers/agent.py.
    model_overrides: dict[str, str] | None = None
    # Text extracted client-side (via /documents/extract) from a file or
    # photo the user attached to this message for context -- not a document
    # the user is asking us to generate, just grounding material. Folded
    # into conversation_context server-side (see routers/agent.py) so
    # every existing prompt site that already reads conversation_context
    # picks it up for free. Length-capped server-side; see
    # ATTACHMENT_CONTEXT_MAX_CHARS in routers/agent.py.
    attachment_context: str = ""
    # Route of the immediately preceding completed turn in this conversation.
    # Populated server-side (agent.py) from context_json; never sent by clients.
    # Used by the orchestrator to detect pending-intent patterns — specifically
    # "last turn was clarify, user is now answering the clarification" → inherit
    # the original research/document intent rather than routing to direct.
    last_turn_route: str | None = None


class Goal(BaseModel):
    id: str = Field(default_factory=lambda: new_id("goal"))
    user_id: str
    conversation_id: str | None = None
    objective: str
    route: RouteName
    quality_mode: str = "standard"
    created_at: datetime = Field(default_factory=utc_now)


class ProgressEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: new_id("evt"))
    turn_id: str
    stage: str
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class Source(BaseModel):
    title: str = ""
    url: str = ""
    snippet: str = ""
    content: str = ""
    query: str = ""
    provider: str = ""


class ToolCall(BaseModel):
    id: str = Field(default_factory=lambda: new_id("tool"))
    name: str
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    error: str | None = None
    latency_ms: int = 0


class ToolDefinition(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    route_tags: list[RouteName] = Field(default_factory=list)
    enabled: bool = True


class Artifact(BaseModel):
    id: str = Field(default_factory=lambda: new_id("artifact"))
    kind: Literal["markdown", "docx", "pptx"]
    filename: str
    mime_type: str
    base64_data: str = ""
    download_url: str | None = None
    size_bytes: int = 0


class TurnResult(BaseModel):
    turn_id: str
    goal: Goal
    answer: str
    route: RouteName
    turn_status: str = "completed"
    langgraph_run_id: str | None = None
    pause_reason: str | None = None
    required_additional_budget_usd: float | None = None
    model_used: str = ""
    sources: list[Source] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    events: list[ProgressEvent] = Field(default_factory=list)
    latency_ms: int = 0
    cost_usd: float = 0.0
    follow_up_options: list[dict[str, Any]] = Field(default_factory=list)
    research_plan_preview: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utc_now)


class StreamEnvelope(BaseModel):
    type: Literal["start", "progress", "result", "error", "done"]
    data: dict[str, Any] = Field(default_factory=dict)


class ConversationSummary(BaseModel):
    id: str
    workspace_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    turn_count: int = 0
    artifact_count: int = 0
    source_count: int = 0
    total_latency_ms: int = 0
    total_cost_usd: float = 0.0


class WorkspaceSummary(BaseModel):
    id: str
    name: str
    created_at: datetime
    updated_at: datetime
    conversations: list[ConversationSummary] = Field(default_factory=list)


class WorkspaceCreate(BaseModel):
    name: str = Field(default="New workspace", min_length=1, max_length=160)


class WorkspaceUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=160)


class ConversationCreate(BaseModel):
    title: str = Field(default="New conversation", min_length=1, max_length=180)
