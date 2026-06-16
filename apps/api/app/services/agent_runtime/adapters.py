from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from app.services.agent_runtime.models import AgentRun, Goal, RuntimeBudget, RuntimeTrace


class RuntimeContext(BaseModel):
    """Compatibility envelope from the existing app shell into agent_runtime."""

    user_id: str
    conversation_id: str
    turn_id: str
    user_message: str
    tenant_id: str | None = None
    profile: str = "balanced"
    history: list[dict[str, Any]] = Field(default_factory=list)
    memory_context: str = ""
    active_task: dict[str, Any] | None = None
    document_context: str = ""
    artifact_context: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def build_goal_from_context(
    context: RuntimeContext,
    *,
    objective: str | None = None,
    quality_mode: str = "standard",
    budget: RuntimeBudget | None = None,
) -> Goal:
    """Create an inert Phase-A goal from existing conversation/turn context."""

    return Goal(
        id=f"goal_{context.turn_id}",
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        conversation_id=context.conversation_id,
        turn_id=context.turn_id,
        objective=objective or context.user_message,
        success_criteria=["Produce the most useful response for the user's request."],
        constraints=[],
        output_contract={"mode": "chat"},
        quality_mode=quality_mode,  # type: ignore[arg-type]
        budget=budget or RuntimeBudget(quality_mode=quality_mode),  # type: ignore[arg-type]
        status="created",
    )


def empty_runtime_trace(context: RuntimeContext) -> RuntimeTrace:
    return RuntimeTrace(goal=build_goal_from_context(context))


def runtime_trace_payload(trace: RuntimeTrace) -> dict[str, Any]:
    return trace.model_dump(mode="json", exclude_none=True)


def model_policy_to_route(policy: "ModelPolicy") -> "RouteDecision":
    """Convert a registry ModelPolicy into a gateway RouteDecision."""

    from app.schemas import RouteDecision

    return RouteDecision(
        task_type="planning",
        complexity="low",
        profile="balanced",
        primary_model=policy.primary_model,
        fallbacks=policy.fallback_models,
        reason="agent routing",
    )


def goal_from_conversation_turn(
    turn,
    conversation,
) -> Goal:
    """Bridge existing turn/conversation ORM objects into a Goal for shadow tracing."""

    conversation_public_id = getattr(conversation, "public_id", None) or str(getattr(conversation, "id", ""))
    turn_public_id = getattr(turn, "public_id", None) or str(getattr(turn, "id", ""))
    user_id = str(getattr(turn, "user_id", None) or getattr(conversation, "user_id", "") or "")
    return Goal(
        id=f"goal_{turn_public_id}",
        user_id=user_id,
        conversation_id=conversation_public_id,
        turn_id=turn_public_id,
        objective=getattr(turn, "error_message", None) or "Conversation turn",
        success_criteria=["Complete the conversation turn without changing existing response behavior."],
        output_contract={"mode": getattr(turn, "turn_kind", "quick")},
        quality_mode="standard",
        budget=RuntimeBudget(),
        status=_goal_status_from_turn(getattr(turn, "status", None)),
        created_at=getattr(turn, "created_at", None) or datetime.now(timezone.utc),
        updated_at=getattr(turn, "updated_at", None) or datetime.now(timezone.utc),
    )


def agent_run_from_research_run(
    research_run,
    goal_id: str,
) -> AgentRun:
    """Bridge an existing ResearchRun ORM object into an AgentRun for shadow tracing."""

    status = _agent_status_from_research_status(getattr(research_run, "status", None))
    created_at = getattr(research_run, "created_at", None) or datetime.now(timezone.utc)
    updated_at = getattr(research_run, "updated_at", None)
    return AgentRun(
        id=f"research_run_{getattr(research_run, 'id')}",
        goal_id=goal_id,
        agent_id="research_lead",
        status=status,
        failure_code="research.failed" if status == "failed" else None,
        started_at=created_at,
        completed_at=updated_at if status in {"completed", "failed", "cancelled"} else None,
    )


def _goal_status_from_turn(status: str | None) -> str:
    if status in {"completed", "failed", "cancelled", "waiting_for_user"}:
        return status
    if status == "running":
        return "running"
    return "created"


def _agent_status_from_research_status(status: str | None) -> str:
    if status in {"completed", "failed", "cancelled", "waiting_for_user", "running"}:
        return status
    if status in {"success", "succeeded"}:
        return "completed"
    return "created"
