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


class AgentV3Request(BaseModel):
    message: str = Field(min_length=1)
    conversation_id: str | None = None
    conversation_context: str = ""
    quality_mode: Literal["draft", "standard", "executive"] = "standard"
    research_level: ResearchLevel = "auto"
    confirm_deep_research: bool = False
    force_route: RouteName | None = None
    output_format: Literal["chat", "markdown", "docx"] = "chat"


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
    kind: Literal["markdown", "docx"]
    filename: str
    mime_type: str
    base64_data: str = ""
    download_url: str | None = None
    size_bytes: int = 0


class AgentV3Result(BaseModel):
    turn_id: str
    goal: Goal
    answer: str
    route: RouteName
    model_used: str = ""
    sources: list[Source] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    events: list[ProgressEvent] = Field(default_factory=list)
    latency_ms: int = 0
    cost_usd: float = 0.0
    follow_up_options: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class StreamEnvelope(BaseModel):
    type: Literal["start", "progress", "result", "error", "done"]
    data: dict[str, Any] = Field(default_factory=dict)


class AgentV3ConversationSummary(BaseModel):
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


class AgentV3WorkspaceSummary(BaseModel):
    id: str
    name: str
    created_at: datetime
    updated_at: datetime
    conversations: list[AgentV3ConversationSummary] = Field(default_factory=list)


class AgentV3WorkspaceCreate(BaseModel):
    name: str = Field(default="New workspace", min_length=1, max_length=160)


class AgentV3WorkspaceUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=160)


class AgentV3ConversationCreate(BaseModel):
    title: str = Field(default="New conversation", min_length=1, max_length=180)
