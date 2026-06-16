from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


QualityMode = Literal["draft", "standard", "executive"]
GoalStatus = Literal["created", "running", "waiting_for_user", "completed", "failed", "cancelled"]
ActiveGoalPolicy = Literal["append", "interrupt", "fork", "cancel", "ignore_for_goal"]
AgentRunStatus = Literal[
    "created",
    "running",
    "completed",
    "failed",
    "cancelled",
    "timed_out",
    "budget_exhausted",
    "tool_failed",
    "model_refused",
    "guardrail_blocked",
    "waiting_for_user",
]
StepType = Literal["model", "tool", "guardrail", "judge", "repair"]
GuardrailAction = Literal[
    "allow",
    "allow_with_constraints",
    "transform",
    "ask_user",
    "require_research",
    "require_judge",
    "redact",
    "block",
    "stop_with_caveat",
    "escalate_to_admin",
]
ToolBackend = Literal["native", "mcp", "external_api"]
JobType = Literal["research", "document", "artifact_render", "qa_polish"]
DurableJobStatus = Literal["queued", "running", "waiting_for_user", "completed", "failed", "cancelled", "stale"]
JudgeTargetType = Literal["answer", "research", "slide", "deck", "document", "tool_output"]
JudgeStatus = Literal["pass", "repair", "fail"]


class RuntimeBudget(BaseModel):
    """Runtime budget shared by goals, agents, tools, and jobs."""

    max_turn_cost_usd: float = 0.25
    max_goal_cost_usd: float = 1.00
    max_latency_ms: int = 30_000
    max_agent_runs: int = 8
    max_model_calls: int = 10
    max_tool_calls: int = 12
    max_research_rounds: int = 2
    max_sources: int = 12
    max_judge_iterations: int = 1
    max_repair_iterations: int = 1
    allow_paid_search: bool = True
    allow_parallel_fallback: bool = False
    quality_mode: QualityMode = "standard"

    @field_validator(
        "max_turn_cost_usd",
        "max_goal_cost_usd",
        mode="after",
    )
    @classmethod
    def _positive_cost(cls, value: float) -> float:
        if value < 0:
            raise ValueError("budget costs must be non-negative")
        return value

    @field_validator(
        "max_latency_ms",
        "max_agent_runs",
        "max_model_calls",
        "max_tool_calls",
        "max_research_rounds",
        "max_sources",
        "max_judge_iterations",
        "max_repair_iterations",
        mode="after",
    )
    @classmethod
    def _positive_limit(cls, value: int) -> int:
        if value < 0:
            raise ValueError("budget limits must be non-negative")
        return value


class Goal(BaseModel):
    id: str
    user_id: str
    tenant_id: str | None = None
    conversation_id: str
    turn_id: str
    parent_goal_id: str | None = None
    supersedes_goal_id: str | None = None
    superseded_by_goal_id: str | None = None
    objective: str
    success_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    output_contract: dict[str, Any] = Field(default_factory=dict)
    sensitivity: str = "normal"
    criticality: str = "normal"
    ambiguity: str = "low"
    quality_mode: QualityMode = "standard"
    budget: RuntimeBudget = Field(default_factory=RuntimeBudget)
    active_policy: ActiveGoalPolicy | None = None
    revision: int = 1
    lock_owner: str | None = None
    lock_expires_at: datetime | None = None
    status: GoalStatus = "created"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AgentDefinition(BaseModel):
    id: str
    name: str
    role: str
    prompt_template_id: str
    allowed_tools: list[str] = Field(default_factory=list)
    model_policy_id: str
    guardrail_policy_ids: list[str] = Field(default_factory=list)
    judge_policy_id: str | None = None
    max_iterations: int = 1
    max_tool_calls: int = 4
    enabled: bool = True
    version: str = "1.0.0"


class ModelPolicy(BaseModel):
    id: str
    name: str
    allowed_models: list[str]
    primary_model: str
    fallback_models: list[str] = Field(default_factory=list)
    max_input_tokens: int = 16_000
    max_output_tokens: int = 2_000
    max_cost_usd_per_call: float = 0.10
    timeout_ms: int = 30_000
    parallel_fallback_enabled: bool = False
    quality_modes: list[QualityMode] = Field(default_factory=lambda: ["draft", "standard"])
    sensitive_domain_allowed: bool = True
    enabled: bool = True

    @field_validator("primary_model", mode="after")
    @classmethod
    def _primary_model_must_be_allowed(cls, value: str, info) -> str:
        allowed = info.data.get("allowed_models") or []
        if allowed and value not in allowed:
            raise ValueError("primary_model must be in allowed_models")
        return value


class PromptTemplate(BaseModel):
    id: str
    agent_id: str
    version: str = "1.0.0"
    system_prompt: str
    developer_prompt: str | None = None
    output_schema: dict[str, Any] | None = None
    variables: list[str] = Field(default_factory=list)
    status: Literal["draft", "active", "archived"] = "draft"


class GuardrailPolicy(BaseModel):
    id: str
    name: str
    applies_to: list[str]
    checks: list[dict[str, Any]] = Field(default_factory=list)
    action_map: dict[str, GuardrailAction] = Field(default_factory=dict)
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    enabled: bool = True
    version: str = "1.0.0"


class ToolDefinition(BaseModel):
    id: str
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    allowed_agent_ids: list[str] = Field(default_factory=list)
    required_user_roles: list[str] = Field(default_factory=list)
    guardrail_policy_ids: list[str] = Field(default_factory=list)
    budget_policy: RuntimeBudget | None = None
    timeout_ms: int = 15_000
    retry_policy: dict[str, Any] = Field(default_factory=lambda: {"max_attempts": 1})
    idempotent: bool = True
    backend: ToolBackend = "native"
    backend_ref: str
    enabled: bool = True
    version: str = "1.0.0"


class AgentRun(BaseModel):
    id: str
    goal_id: str
    agent_id: str
    parent_run_id: str | None = None
    status: AgentRunStatus = "created"
    failure_code: str | None = None
    failure_message: str | None = None
    retry_count: int = 0
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    total_cost_usd: float = 0.0
    latency_ms: int = 0


class AgentStep(BaseModel):
    id: str
    run_id: str
    step_type: StepType
    input_summary: str = ""
    output_summary: str = ""
    model_used: str | None = None
    tool_name: str | None = None
    latency_ms: int = 0
    cost_usd: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class JudgeResult(BaseModel):
    id: str
    target_type: JudgeTargetType
    target_id: str
    judge_agent_id: str
    score: float = Field(ge=0.0, le=1.0)
    status: JudgeStatus
    issues: list[dict[str, Any]] = Field(default_factory=list)
    required_repairs: list[dict[str, Any]] = Field(default_factory=list)
    can_publish: bool


class DurableJob(BaseModel):
    id: str
    goal_id: str
    user_id: str
    tenant_id: str | None = None
    conversation_id: str
    turn_id: str
    job_type: JobType
    status: DurableJobStatus = "queued"
    progress_stage: str = "queued"
    progress_message: str = ""
    percent_complete: int | None = Field(default=None, ge=0, le=100)
    result_ref: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    idempotency_key: str
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RuntimeTrace(BaseModel):
    """Inert Phase-A trace payload for admin and shadow-mode adapters."""

    goal: Goal | None = None
    agent_runs: list[AgentRun] = Field(default_factory=list)
    prompt_versions: dict[str, str] = Field(
        default_factory=dict,
        description="agent_id -> prompt_template_id used for this trace",
    )
    agent_steps: list[AgentStep] = Field(default_factory=list)
    guardrail_decisions: list[dict[str, Any]] = Field(default_factory=list)
    judge_results: list[JudgeResult] = Field(default_factory=list)
    jobs: list[DurableJob] = Field(default_factory=list)
