"""Admin operational endpoints.

The frontend hides admin UI for non-admins, but these endpoints are the real
security boundary. Configure admins with ADMIN_USER_IDS and/or ADMIN_EMAILS.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_

from app.auth import get_claim_email, get_current_user_payload, is_admin_user, is_admin_user_db
from app.config import get_settings
from app.services.clerk import fetch_clerk_user
from app.db.models import (
    AdminAuditLog,
    AgentGoal,
    AgentRunLog,
    AgentStep,
    AgentV3Workspace,
    Conversation,
    ConversationMessage,
    ConversationTurn,
    DocumentTemplate,
    GuardrailEvent,
    RequestLog,
    ResearchClaim,
    ResearchFinding,
    ResearchQuestion,
    ResearchRun,
    ResearchSource,
    SessionLocal,
    TwinProfile,
    User,
    UserAdminControl,
    UserMemory,
    UserProfile,
    WritingSample,
    get_global_budget_config,
    get_global_monthly_spend,
    get_monthly_spend,
    get_turn_runtime_config,
    set_global_budget_config,
    set_turn_runtime_config,
)
from app.services.agent_v3 import model_policy as agent_v3_model_policy
from app.services.agent_v3 import persistence as agent_v3_persistence
from app.services.agent_v3 import prompt_library as agent_v3_prompt_library
from app.services.agent_v3 import routing_policy as agent_v3_routing_policy
from app.services.document_templates import template_path_for_row
from app.services.agent_runtime.db_models import DBPromptTemplate
from app.services.agent_runtime.fixtures import PromptFixtureRunner
from app.services.agent_runtime.registry import (
    RegistryNotSeeded,
    invalidate_registry_cache,
    load_default_registry,
    load_registry_from_db,
)
from app.services.agent_runtime.seeder import seed_registry_from_defaults
from app.services.llm_gateway import (
    PROVIDER_TEST_MODELS,
    get_circuit_status,
    provider_for_model,
    test_provider_connection,
)
from app.services.rate_limit import check_rate_limit
from app.services.router import choose_route, load_policy
from app.services.web_context import test_nimble_connection, test_tavily_connection, test_you_connection


router = APIRouter(prefix="/admin", tags=["admin"])
logger = logging.getLogger(__name__)


class AdminPrincipal(BaseModel):
    user_id: str
    email: str | None = None


class AdminControlUpdate(BaseModel):
    status: Literal["active", "suspended", "pending"] = "active"
    monthly_budget_usd: float | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=1000)


class UserRoleUpdate(BaseModel):
    role: Literal["user", "admin"]


class GlobalBudgetUpdate(BaseModel):
    monthly_budget_usd: float | None = Field(default=None, ge=0)
    admin_override_enabled: bool = True


class TurnRuntimeUpdate(BaseModel):
    quick_timeout_minutes: int = Field(default=30, ge=5, le=240)
    research_timeout_minutes: int = Field(default=180, ge=30, le=720)
    document_timeout_minutes: int = Field(default=120, ge=15, le=720)


class PrivacyDeleteRequest(BaseModel):
    conversations: bool = False
    memories: bool = False
    writing_samples: bool = False
    twin_profile: bool = False
    user_profile: bool = False
    document_templates: bool = False
    research_runs: bool = False
    confirm_user_id: str | None = None


class ProviderTestRequest(BaseModel):
    provider: str


class RouteTestRequest(BaseModel):
    message: str = Field(min_length=1, max_length=32000)
    profile: str | None = None
    force_model: str | None = None
    deep_research: bool = False
    web_search: bool = False
    task_override: str | None = None
    complexity_override: str | None = None
    preferred_model: str | None = None


class AdminTurnCancelRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class AgentV3PromptUpsertRequest(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    system_prompt: str = Field(min_length=1)
    developer_prompt: str | None = None
    variables: list[str] = Field(default_factory=list)
    profile: str | None = Field(default=None, max_length=64)
    version: str = Field(default="1.0.0", max_length=32)
    status: Literal["draft", "active", "archived"] = "draft"
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentV3ModelPolicyUpdate(BaseModel):
    roles: dict[str, str] = Field(default_factory=dict)
    fallback_models: list[str] | None = None


def require_admin(request: Request, payload: dict = Depends(get_current_user_payload)) -> AdminPrincipal:
    user_id = str(payload.get("sub") or "")
    email = get_claim_email(payload)
    if not is_admin_user_db(user_id, email):
        logger.warning(
            "Admin access denied: user_id=%s email=%s path=%s ua=%s",
            user_id,
            email,
            request.url.path,
            request.headers.get("user-agent", ""),
        )
        raise HTTPException(status_code=403, detail="Admin access required")
    return AdminPrincipal(user_id=user_id, email=email)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today_start() -> datetime:
    return datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=timezone.utc)


def _start_for_range(range_value: str) -> datetime | None:
    if range_value == "1d":
        return _today_start()
    if range_value == "7d":
        return _today_start() - timedelta(days=6)
    if range_value == "30d":
        return _today_start() - timedelta(days=29)
    return None


def _fmt(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _audit(db, admin: AdminPrincipal, action: str, target_user_id: str | None = None, details: dict | None = None) -> None:
    db.add(AdminAuditLog(
        admin_user_id=admin.user_id,
        action=action,
        target_user_id=target_user_id,
        details_json=json.dumps(details or {}),
        created_at=_now(),
    ))


def _key_hint(key: str | None) -> str | None:
    if not key:
        return None
    return f"...{key[-4:]}" if len(key) > 4 else "...."


def _ensure_target_user_id(user_id: str) -> None:
    if not user_id.strip():
        raise HTTPException(status_code=400, detail="Target user_id must not be empty.")


def _privacy_counts(db, user_id: str) -> dict[str, int]:
    conv_ids = [cid for (cid,) in db.query(Conversation.id).filter(Conversation.user_id == user_id).all()]
    run_ids = [rid for (rid,) in db.query(ResearchRun.id).filter(ResearchRun.user_id == user_id).all()]
    return {
        "conversations": len(conv_ids),
        "conversation_messages": (
            db.query(ConversationMessage).filter(ConversationMessage.conversation_id.in_(conv_ids)).count()
            if conv_ids else 0
        ),
        "memories": db.query(UserMemory).filter(UserMemory.user_id == user_id).count(),
        "user_profiles": db.query(UserProfile).filter(UserProfile.user_id == user_id).count(),
        "document_templates": db.query(DocumentTemplate).filter(DocumentTemplate.user_id == user_id).count(),
        "writing_samples": db.query(WritingSample).filter(WritingSample.user_id == user_id).count(),
        "twin_profiles": db.query(TwinProfile).filter(TwinProfile.user_id == user_id).count(),
        "research_runs": len(run_ids),
        "research_questions": (
            db.query(ResearchQuestion).filter(ResearchQuestion.run_id.in_(run_ids)).count()
            if run_ids else 0
        ),
        "research_sources": (
            db.query(ResearchSource).filter(ResearchSource.run_id.in_(run_ids)).count()
            if run_ids else 0
        ),
        "research_claims": (
            db.query(ResearchClaim).filter(ResearchClaim.run_id.in_(run_ids)).count()
            if run_ids else 0
        ),
        "research_findings": (
            db.query(ResearchFinding).filter(ResearchFinding.run_id.in_(run_ids)).count()
            if run_ids else 0
        ),
    }


def _control_out(control: UserAdminControl | None) -> dict:
    return {
        "status": control.status if control else "active",
        "role": (control.role if control and control.role else "user"),
        "monthly_budget_usd": control.monthly_budget_usd if control else None,
        "notes": control.notes if control else None,
        "updated_at": _fmt(control.updated_at) if control else None,
    }


def _effective_role(user_id: str, db_role: str | None, email: str | None = None) -> str:
    """An env-allowlisted admin is always 'admin' regardless of the DB role."""
    if is_admin_user(user_id, email):
        return "admin"
    return db_role or "user"


def _load_admin_registry(db):
    try:
        return load_registry_from_db(db)
    except RegistryNotSeeded:
        return load_default_registry()


def _user_profiles(db, user_ids) -> dict[str, dict[str, str | None]]:
    ids = sorted({str(user_id) for user_id in user_ids if user_id})
    if not ids:
        return {}
    return {
        clerk_id: {"email": email, "name": name}
        for clerk_id, email, name in (
            db.query(User.clerk_id, User.email, User.name)
            .filter(User.clerk_id.in_(ids))
            .all()
        )
    }


def _profile_fields(profiles: dict[str, dict[str, str | None]], user_id: str, prefix: str = "") -> dict[str, str | None]:
    profile = profiles.get(user_id) or {}
    return {
        f"{prefix}email": profile.get("email"),
        f"{prefix}name": profile.get("name"),
    }


def _all_known_user_ids(db) -> set[str]:
    user_ids: set[str] = set()
    sources = [
        db.query(Conversation.user_id).distinct().all(),
        db.query(RequestLog.user_id).distinct().all(),
        db.query(UserMemory.user_id).distinct().all(),
        db.query(UserProfile.user_id).distinct().all(),
        db.query(WritingSample.user_id).distinct().all(),
        db.query(TwinProfile.user_id).distinct().all(),
        db.query(ResearchRun.user_id).distinct().all(),
        db.query(UserAdminControl.user_id).distinct().all(),
        db.query(User.clerk_id).distinct().all(),
    ]
    for rows in sources:
        for (user_id,) in rows:
            if user_id:
                user_ids.add(user_id)
    return user_ids


def _assistant_rows(db, start: datetime | None = None):
    q = (
        db.query(
            Conversation.user_id.label("user_id"),
            ConversationMessage.created_at.label("created_at"),
            ConversationMessage.model_used.label("model_used"),
            ConversationMessage.task_type.label("task_type"),
            ConversationMessage.estimated_cost_usd.label("cost"),
            ConversationMessage.latency_ms.label("latency_ms"),
            ConversationMessage.prompt_tokens.label("prompt_tokens"),
            ConversationMessage.completion_tokens.label("completion_tokens"),
        )
        .join(Conversation, ConversationMessage.conversation_id == Conversation.id)
        .filter(ConversationMessage.role == "assistant")
    )
    if start:
        q = q.filter(ConversationMessage.created_at >= start)
    return q.all()


def _request_rows(db, start: datetime | None = None, include_errors: bool = False):
    q = db.query(
        RequestLog.user_id.label("user_id"),
        RequestLog.created_at.label("created_at"),
        RequestLog.model_used.label("model_used"),
        RequestLog.task_type.label("task_type"),
        RequestLog.estimated_cost_usd.label("cost"),
        RequestLog.latency_ms.label("latency_ms"),
        RequestLog.prompt_tokens.label("prompt_tokens"),
        RequestLog.completion_tokens.label("completion_tokens"),
        RequestLog.status.label("status"),
    )
    if not include_errors:
        q = q.filter(RequestLog.status == "success")
    if start:
        q = q.filter(RequestLog.created_at >= start)
    return q.all()


@router.get("/me")
def admin_me(admin: AdminPrincipal = Depends(require_admin)) -> dict:
    return {"is_admin": True, "user_id": admin.user_id, "email": admin.email}


@router.get("/overview")
def overview(admin: AdminPrincipal = Depends(require_admin)) -> dict:
    db = SessionLocal()
    try:
        start = _start_for_range("1d")
        msg_count = (
            db.query(func.count(ConversationMessage.id))
            .filter(ConversationMessage.role == "assistant", ConversationMessage.created_at >= start)
            .scalar() or 0
        )
        req_count = (
            db.query(func.count(RequestLog.id))
            .filter(RequestLog.status == "success", RequestLog.created_at >= start)
            .scalar() or 0
        )
        msg_cost = (
            db.query(func.coalesce(func.sum(ConversationMessage.estimated_cost_usd), 0.0))
            .filter(ConversationMessage.role == "assistant", ConversationMessage.created_at >= start)
            .scalar() or 0.0
        )
        req_cost = (
            db.query(func.coalesce(func.sum(RequestLog.estimated_cost_usd), 0.0))
            .filter(RequestLog.status == "success", RequestLog.created_at >= start)
            .scalar() or 0.0
        )
        errors_today = db.query(RequestLog).filter(RequestLog.status == "error", RequestLog.created_at >= start).count()
        running_research = db.query(ResearchRun).filter(ResearchRun.status == "running").count()
        return {
            "users": len(_all_known_user_ids(db)),
            "requests_today": int(msg_count) + int(req_count),
            "spend_today": round(float(msg_cost or 0) + float(req_cost or 0), 6),
            "errors_today": errors_today,
            "running_research_runs": running_research,
            "total_conversations": db.query(Conversation).count(),
            "total_memories": db.query(UserMemory).count(),
            "total_writing_samples": db.query(WritingSample).count(),
            "total_research_runs": db.query(ResearchRun).count(),
        }
    finally:
        db.close()


def _budget_status(db) -> dict:
    config = get_global_budget_config(db)
    spend = get_global_monthly_spend(db)
    cap = config["monthly_budget_usd"]
    percent = (spend / cap * 100.0) if cap and cap > 0 else None
    return {
        "monthly_budget_usd": cap,
        "month_spend": round(spend, 6),
        "percent_used": round(percent, 1) if percent is not None else None,
        "admin_override_enabled": config["admin_override_enabled"],
        "status": (
            "disabled" if cap is None else
            "exceeded" if spend >= cap else
            "warning" if percent is not None and percent >= 80 else
            "normal"
        ),
    }


def _ops_recommendations(db, budget: dict) -> list[dict]:
    recs: list[dict] = []
    pending_users = db.query(UserAdminControl).filter(UserAdminControl.status == "pending").count()
    if pending_users:
        recs.append({
            "severity": "medium",
            "title": "Review pending users",
            "detail": f"{pending_users} user{'s' if pending_users != 1 else ''} waiting for approval.",
            "action": "Open Users and activate or suspend the account.",
        })
    if budget["status"] == "exceeded":
        recs.append({
            "severity": "high",
            "title": "Global monthly budget exceeded",
            "detail": f"Spend is ${budget['month_spend']:.4f} this month.",
            "action": "Raise the cap, enable admin override, or pause expensive research usage.",
        })
    elif budget["status"] == "warning":
        recs.append({
            "severity": "medium",
            "title": "Global budget nearing cap",
            "detail": f"{budget['percent_used']}% of the monthly cap has been used.",
            "action": "Review top users and model spend before the cap is hit.",
        })
    start = _today_start()
    errors_today = db.query(RequestLog).filter(RequestLog.status == "error", RequestLog.created_at >= start).count()
    if errors_today:
        recs.append({
            "severity": "medium",
            "title": "Request errors today",
            "detail": f"{errors_today} backend request error{'s' if errors_today != 1 else ''} logged today.",
            "action": "Open Errors and inspect provider/auth/routing failures.",
        })
    running_research = db.query(ResearchRun).filter(ResearchRun.status == "running").count()
    if running_research:
        recs.append({
            "severity": "low",
            "title": "Research runs in progress",
            "detail": f"{running_research} research run{'s' if running_research != 1 else ''} currently marked running.",
            "action": "Open Research and check for long-running or stuck jobs.",
        })
    stale_cutoff = _now().replace(tzinfo=None) - timedelta(minutes=10)
    idle_turns = (
        db.query(ConversationTurn)
        .filter(ConversationTurn.status.in_(["pending", "running"]), ConversationTurn.updated_at < stale_cutoff)
        .count()
    )
    if idle_turns:
        recs.append({
            "severity": "medium",
            "title": "Chat turns may be stuck",
            "detail": f"{idle_turns} active turn{'s' if idle_turns != 1 else ''} idle for more than 10 minutes.",
            "action": "Open Turns and cancel or inspect long-running work.",
        })
    return recs


def _percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percentile)))
    return int(ordered[idx])


def _stage_latency_summary(db, *, since: datetime, limit: int = 500) -> list[dict]:
    rows = (
        db.query(ConversationMessage.execution_log_json)
        .filter(
            ConversationMessage.role == "assistant",
            ConversationMessage.created_at >= since,
            ConversationMessage.execution_log_json.isnot(None),
        )
        .order_by(ConversationMessage.created_at.desc())
        .limit(limit)
        .all()
    )
    by_stage: dict[str, list[int]] = defaultdict(list)
    for (raw,) in rows:
        try:
            payload = json.loads(raw or "{}")
        except (TypeError, ValueError):
            continue
        for timing in payload.get("stage_timings") or []:
            if not isinstance(timing, dict):
                continue
            stage = str(timing.get("stage") or "").strip()
            latency = timing.get("latency_ms")
            if not stage or not isinstance(latency, (int, float)):
                continue
            by_stage[stage].append(max(0, int(latency)))

    return sorted(
        [
            {
                "stage": stage,
                "count": len(values),
                "avg_ms": round(sum(values) / len(values), 1) if values else 0,
                "p50_ms": _percentile(values, 0.50),
                "p95_ms": _percentile(values, 0.95),
            }
            for stage, values in by_stage.items()
        ],
        key=lambda item: item["p95_ms"],
        reverse=True,
    )


def _parse_json_object(raw: str | None) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_json_list(raw: str | None) -> list[Any]:
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _ms(value: Any) -> int:
    return max(0, int(value)) if isinstance(value, (int, float)) else 0


def _stage_rows_from_exec_log(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for timing in payload.get("stage_timings") or []:
        if not isinstance(timing, dict):
            continue
        stage = str(timing.get("stage") or "").strip()
        latency = timing.get("latency_ms")
        if not stage or not isinstance(latency, (int, float)):
            continue
        rows.append({
            "stage": stage,
            "latency_ms": _ms(latency),
            "meta": timing.get("meta") if isinstance(timing.get("meta"), dict) else {},
        })
    return rows


def _turn_profiler_payload(db, *, since: datetime | None, limit: int) -> dict:
    query = (
        db.query(ConversationMessage, Conversation, ConversationTurn)
        .join(Conversation, ConversationMessage.conversation_id == Conversation.id)
        .outerjoin(ConversationTurn, ConversationTurn.assistant_message_id == ConversationMessage.id)
        .filter(
            ConversationMessage.role == "assistant",
            ConversationMessage.execution_log_json.isnot(None),
        )
    )
    if since is not None:
        query = query.filter(ConversationMessage.created_at >= since)
    rows = query.order_by(ConversationMessage.created_at.desc()).limit(limit).all()

    stage_values: dict[str, list[int]] = defaultdict(list)
    stage_total_ms: dict[str, int] = defaultdict(int)
    model_values: dict[str, list[int]] = defaultdict(list)
    model_costs: dict[str, float] = defaultdict(float)
    turn_kind_counts: dict[str, int] = defaultdict(int)
    turn_status_counts: dict[str, int] = defaultdict(int)
    profiled_turns: list[dict[str, Any]] = []
    total_latency_values: list[int] = []
    total_cost = 0.0
    total_tokens = 0

    for msg, conv, turn in rows:
        payload = _parse_json_object(msg.execution_log_json)
        stages = _stage_rows_from_exec_log(payload)
        planner = payload.get("planner") if isinstance(payload.get("planner"), dict) else {}
        worker = payload.get("worker") if isinstance(payload.get("worker"), dict) else {}
        total_latency_ms = _ms(payload.get("total_latency_ms") or msg.latency_ms)
        if total_latency_ms:
            total_latency_values.append(total_latency_ms)
        cost = float(payload.get("total_cost_usd") or msg.estimated_cost_usd or 0.0)
        total_cost += cost
        prompt_tokens = int(msg.prompt_tokens or worker.get("prompt_tokens") or 0)
        completion_tokens = int(msg.completion_tokens or worker.get("completion_tokens") or 0)
        total_tokens += prompt_tokens + completion_tokens

        stage_sum = 0
        for stage in stages:
            latency = _ms(stage.get("latency_ms"))
            stage_name = str(stage.get("stage") or "")
            stage_values[stage_name].append(latency)
            stage_total_ms[stage_name] += latency
            stage_sum += latency
        bottleneck = max(stages, key=lambda s: _ms(s.get("latency_ms")), default=None)
        model = str(msg.model_used or worker.get("model") or planner.get("model") or "unknown")
        if total_latency_ms:
            model_values[model].append(total_latency_ms)
        model_costs[model] += cost
        kind = str((turn.turn_kind if turn else None) or planner.get("turn_type") or msg.task_type or "unknown")
        status = str((turn.status if turn else None) or "completed")
        turn_kind_counts[kind] += 1
        turn_status_counts[status] += 1

        progress = _parse_json_list(turn.progress_json if turn else None)
        lifecycle = _parse_json_list(turn.lifecycle_json if turn else None)
        profiled_turns.append({
            "message_id": msg.id,
            "turn_id": turn.public_id if turn else None,
            "conversation_id": conv.public_id,
            "user_id": conv.user_id,
            "created_at": _fmt(msg.created_at),
            "completed_at": _fmt(turn.completed_at) if turn else None,
            "status": status,
            "turn_kind": kind,
            "task_type": msg.task_type,
            "complexity": msg.complexity,
            "action": planner.get("action"),
            "turn_type": planner.get("turn_type"),
            "model": model,
            "planner_model": planner.get("model"),
            "worker_model": worker.get("model"),
            "latency_ms": total_latency_ms,
            "cost_usd": round(cost, 6),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "stage_sum_ms": stage_sum,
            "unattributed_ms": max(0, total_latency_ms - stage_sum),
            "bottleneck_stage": bottleneck.get("stage") if bottleneck else None,
            "bottleneck_ms": _ms(bottleneck.get("latency_ms")) if bottleneck else 0,
            "stage_timings": stages,
            "progress_count": len(progress),
            "last_progress": progress[-1] if progress else None,
            "lifecycle_last_event": lifecycle[-1] if lifecycle else None,
        })

    stage_summary = sorted(
        [
            {
                "stage": stage,
                "count": len(values),
                "total_ms": stage_total_ms[stage],
                "avg_ms": round(sum(values) / len(values), 1) if values else 0,
                "p50_ms": _percentile(values, 0.50),
                "p95_ms": _percentile(values, 0.95),
                "max_ms": max(values) if values else 0,
            }
            for stage, values in stage_values.items()
        ],
        key=lambda item: (item["total_ms"], item["p95_ms"]),
        reverse=True,
    )
    model_summary = sorted(
        [
            {
                "model": model,
                "count": len(values),
                "avg_ms": round(sum(values) / len(values), 1) if values else 0,
                "p95_ms": _percentile(values, 0.95),
                "cost_usd": round(model_costs[model], 6),
            }
            for model, values in model_values.items()
        ],
        key=lambda item: (item["cost_usd"], item["p95_ms"]),
        reverse=True,
    )
    slow_turns = sorted(profiled_turns, key=lambda item: item["latency_ms"], reverse=True)[:25]
    conversations: dict[str, dict[str, Any]] = {}
    for turn in profiled_turns:
        conversation_id = turn.get("conversation_id") or "unknown"
        bucket = conversations.setdefault(conversation_id, {
            "conversation_id": conversation_id,
            "user_id": turn.get("user_id"),
            "turn_count": 0,
            "latency_values": [],
            "total_latency_ms": 0,
            "total_cost_usd": 0.0,
            "total_tokens": 0,
            "stage_totals": defaultdict(int),
            "status_counts": defaultdict(int),
            "turn_kind_counts": defaultdict(int),
            "slowest_turn_id": None,
            "slowest_turn_ms": 0,
            "first_seen_at": turn.get("created_at"),
            "last_seen_at": turn.get("created_at"),
        })
        latency = _ms(turn.get("latency_ms"))
        bucket["turn_count"] += 1
        bucket["latency_values"].append(latency)
        bucket["total_latency_ms"] += latency
        bucket["total_cost_usd"] += float(turn.get("cost_usd") or 0.0)
        bucket["total_tokens"] += int(turn.get("prompt_tokens") or 0) + int(turn.get("completion_tokens") or 0)
        bucket["status_counts"][turn.get("status") or "unknown"] += 1
        bucket["turn_kind_counts"][turn.get("turn_kind") or turn.get("task_type") or "unknown"] += 1
        for stage in turn.get("stage_timings") or []:
            if not isinstance(stage, dict):
                continue
            stage_name = str(stage.get("stage") or "").strip()
            if stage_name:
                bucket["stage_totals"][stage_name] += _ms(stage.get("latency_ms"))
        if latency >= bucket["slowest_turn_ms"]:
            bucket["slowest_turn_ms"] = latency
            bucket["slowest_turn_id"] = turn.get("turn_id") or f"msg:{turn.get('message_id')}"
        created_at = turn.get("created_at")
        if created_at:
            if not bucket["first_seen_at"] or created_at < bucket["first_seen_at"]:
                bucket["first_seen_at"] = created_at
            if not bucket["last_seen_at"] or created_at > bucket["last_seen_at"]:
                bucket["last_seen_at"] = created_at

    conversation_rollups = []
    for bucket in conversations.values():
        values = bucket["latency_values"]
        bottleneck_stage = None
        bottleneck_ms = 0
        if bucket["stage_totals"]:
            bottleneck_stage, bottleneck_ms = max(bucket["stage_totals"].items(), key=lambda item: item[1])
        conversation_rollups.append({
            "conversation_id": bucket["conversation_id"],
            "user_id": bucket["user_id"],
            "turn_count": bucket["turn_count"],
            "total_latency_ms": bucket["total_latency_ms"],
            "avg_latency_ms": round(sum(values) / len(values), 1) if values else 0,
            "p95_latency_ms": _percentile(values, 0.95),
            "total_cost_usd": round(bucket["total_cost_usd"], 6),
            "total_tokens": bucket["total_tokens"],
            "slowest_turn_id": bucket["slowest_turn_id"],
            "slowest_turn_ms": bucket["slowest_turn_ms"],
            "bottleneck_stage": bottleneck_stage,
            "bottleneck_ms": bottleneck_ms,
            "first_seen_at": bucket["first_seen_at"],
            "last_seen_at": bucket["last_seen_at"],
            "status_counts": dict(sorted(bucket["status_counts"].items())),
            "turn_kind_counts": dict(sorted(bucket["turn_kind_counts"].items())),
        })
    conversation_rollups.sort(key=lambda item: (item["total_latency_ms"], item["turn_count"]), reverse=True)

    recommendations: list[dict[str, str]] = []
    if stage_summary:
        top = stage_summary[0]
        recommendations.append({
            "severity": "medium" if top["p95_ms"] < 30000 else "high",
            "title": f"Top latency stage: {top['stage']}",
            "detail": f"p95 {top['p95_ms']} ms across {top['count']} turn(s); total observed time {top['total_ms']} ms.",
            "action": "Prioritize this stage for caching, parallelism, timeout tuning, or async/background execution.",
        })
    high_unattributed = [t for t in profiled_turns if t["unattributed_ms"] > 2000 and t["unattributed_ms"] > t["latency_ms"] * 0.25]
    if high_unattributed:
        recommendations.append({
            "severity": "medium",
            "title": "Unattributed latency is visible",
            "detail": f"{len(high_unattributed)} turn(s) spent >25% of latency outside named stage timings.",
            "action": "Add finer spans around DB, serialization, subprocess, polling, and background handoff boundaries.",
        })
    return {
        "summary": {
            "turns": len(profiled_turns),
            "avg_latency_ms": round(sum(total_latency_values) / len(total_latency_values), 1) if total_latency_values else 0,
            "p50_latency_ms": _percentile(total_latency_values, 0.50),
            "p95_latency_ms": _percentile(total_latency_values, 0.95),
            "total_cost_usd": round(total_cost, 6),
            "total_tokens": total_tokens,
        },
        "stage_summary": stage_summary,
        "model_summary": model_summary,
        "turn_kind_counts": dict(sorted(turn_kind_counts.items())),
        "turn_status_counts": dict(sorted(turn_status_counts.items())),
        "conversation_rollups": conversation_rollups[:25],
        "slow_turns": slow_turns,
        "recent_turns": profiled_turns[:limit],
        "recommendations": recommendations,
    }


@router.get("/ops-summary")
def ops_summary(admin: AdminPrincipal = Depends(require_admin)) -> dict:
    db = SessionLocal()
    try:
        budget = _budget_status(db)
        month_start = _today_start().replace(day=1)
        users_near_budget = []
        controls = {
            c.user_id: c
            for c in db.query(UserAdminControl).filter(UserAdminControl.monthly_budget_usd.isnot(None)).all()
        }
        for user_id, control in controls.items():
            if not control.monthly_budget_usd or control.monthly_budget_usd <= 0:
                continue
            spend = get_monthly_spend(db, user_id)
            percent = spend / control.monthly_budget_usd * 100.0
            if percent >= 80:
                users_near_budget.append({
                    "user_id": user_id,
                    "month_spend": round(spend, 6),
                    "monthly_budget_usd": control.monthly_budget_usd,
                    "percent_used": round(percent, 1),
                })
        top_models = (
            db.query(
                RequestLog.model_used,
                func.coalesce(func.sum(RequestLog.estimated_cost_usd), 0.0),
                func.count(RequestLog.id),
            )
            .filter(RequestLog.status == "success", RequestLog.created_at >= month_start, RequestLog.model_used.isnot(None))
            .group_by(RequestLog.model_used)
            .order_by(func.coalesce(func.sum(RequestLog.estimated_cost_usd), 0.0).desc())
            .limit(5)
            .all()
        )
        pending_users = db.query(UserAdminControl).filter(UserAdminControl.status == "pending").count()
        failed_research = db.query(ResearchRun).filter(ResearchRun.status == "failed").count()
        active_turns = db.query(ConversationTurn).filter(ConversationTurn.status.in_(["pending", "running"])).count()
        failed_turns_today = db.query(ConversationTurn).filter(
            ConversationTurn.status == "failed",
            ConversationTurn.completed_at >= _today_start(),
        ).count()
        recent_errors = db.query(RequestLog).filter(RequestLog.status == "error").order_by(RequestLog.created_at.desc()).limit(5).all()
        return {
            "budget": budget,
            "pending": {
                "user_approvals": pending_users,
                "failed_research_runs": failed_research,
                "active_turns": active_turns,
                "failed_turns_today": failed_turns_today,
                "users_near_budget": sorted(users_near_budget, key=lambda x: -x["percent_used"])[:10],
            },
            "top_models_month": [
                {"model": model, "cost": round(float(cost or 0), 6), "requests": int(count or 0)}
                for model, cost, count in top_models
            ],
            "recent_errors": [
                {
                    "id": row.id,
                    "created_at": _fmt(row.created_at),
                    "user_id": row.user_id,
                    "model": row.model_used or row.selected_model,
                    "error": row.error,
                }
                for row in recent_errors
            ],
            "stage_latency": _stage_latency_summary(db, since=_now().replace(tzinfo=None) - timedelta(days=7)),
            "recommendations": _ops_recommendations(db, budget),
        }
    finally:
        db.close()


@router.get("/turn-profiler")
def turn_profiler(
    range: str = Query(default="7d", pattern="^(1d|7d|30d|all)$"),
    limit: int = Query(default=100, ge=1, le=500),
    admin: AdminPrincipal = Depends(require_admin),
) -> dict:
    db = SessionLocal()
    try:
        return {
            "range": range,
            "limit": limit,
            **_turn_profiler_payload(db, since=_start_for_range(range), limit=limit),
        }
    finally:
        db.close()


def _turn_row(turn: ConversationTurn, conv_public_id: str | None = None) -> dict:
    try:
        progress = json.loads(turn.progress_json or "[]")
    except (TypeError, ValueError):
        progress = []
    try:
        lifecycle = json.loads(turn.lifecycle_json or "[]")
    except (TypeError, ValueError):
        lifecycle = []
    graph_trace = None
    graph_summary = None
    graph_canary = None
    if isinstance(lifecycle, list):
        for row in reversed(lifecycle):
            if isinstance(row, dict) and row.get("event") == "turn_graph_canary" and graph_canary is None:
                graph_canary = {
                    "mode": row.get("mode"),
                    "planner_model": row.get("planner_model"),
                    "action": row.get("action"),
                    "plan_confidence": row.get("plan_confidence"),
                }
            if isinstance(row, dict) and row.get("event") == "turn_graph_shadow" and graph_trace is None:
                graph_trace = {
                    "status": row.get("status"),
                    "error": row.get("error"),
                    "selected_tools": row.get("selected_tools") or [],
                    "accepted_tools": row.get("accepted_tools") or [],
                    "triage_decision": row.get("triage_decision"),
                    "gate": row.get("gate"),
                    "events": row.get("events") or [],
                    "node_timings": row.get("node_timings") or [],
                }
            if graph_trace is not None and graph_canary is not None:
                break
    if graph_trace:
        timings = graph_trace.get("node_timings") or []
        graph_summary = {
            "status": graph_trace.get("status"),
            "path": [t.get("node") for t in timings if isinstance(t, dict) and t.get("node")],
            "total_node_latency_ms": sum(int(t.get("latency_ms") or 0) for t in timings if isinstance(t, dict)),
            "selected_tools": [t.get("name") for t in (graph_trace.get("selected_tools") or []) if isinstance(t, dict)],
            "canary": graph_canary,
        }
    age_seconds = max(0, int((_now().replace(tzinfo=None) - turn.created_at).total_seconds()))
    idle_seconds = max(0, int((_now().replace(tzinfo=None) - turn.updated_at).total_seconds()))
    return {
        "id": turn.public_id,
        "user_id": turn.user_id,
        "conversation_id": conv_public_id,
        "turn_kind": turn.turn_kind or "quick",
        "status": turn.status,
        "client_request_id": turn.client_request_id,
        "user_message_id": turn.user_message_id,
        "assistant_message_id": turn.assistant_message_id,
        "last_progress": progress[-1] if isinstance(progress, list) and progress else None,
        "progress_count": len(progress) if isinstance(progress, list) else 0,
        "lifecycle": lifecycle[-20:] if isinstance(lifecycle, list) else [],
        "graph_trace": graph_trace,
        "graph_summary": graph_summary,
        "error_message": turn.error_message,
        "created_at": _fmt(turn.created_at),
        "updated_at": _fmt(turn.updated_at),
        "completed_at": _fmt(turn.completed_at),
        "age_seconds": age_seconds,
        "idle_seconds": idle_seconds,
    }


def _append_turn_lifecycle(turn: ConversationTurn, event: str, data: dict | None = None) -> None:
    try:
        rows = json.loads(turn.lifecycle_json or "[]")
    except (TypeError, ValueError):
        rows = []
    rows.append({
        "event": event,
        "ts": _now().isoformat(),
        **(data or {}),
    })
    turn.lifecycle_json = json.dumps(rows[-120:])


@router.get("/turns")
def turns(
    status: str = Query(default="active"),
    user_id: str | None = Query(default=None),
    min_idle_seconds: int | None = Query(default=None, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    admin: AdminPrincipal = Depends(require_admin),
) -> dict:
    db = SessionLocal()
    try:
        q = db.query(ConversationTurn, Conversation.public_id).join(
            Conversation, ConversationTurn.conversation_id == Conversation.id
        )
        if status == "active":
            q = q.filter(ConversationTurn.status.in_(["pending", "running"]))
        elif status != "all":
            q = q.filter(ConversationTurn.status == status)
        if user_id:
            q = q.filter(ConversationTurn.user_id == user_id)
        if min_idle_seconds is not None:
            cutoff = _now().replace(tzinfo=None) - timedelta(seconds=min_idle_seconds)
            q = q.filter(ConversationTurn.updated_at <= cutoff)
        rows = q.order_by(ConversationTurn.updated_at.desc()).limit(limit).all()
        return {"items": [_turn_row(turn, conv_public_id) for turn, conv_public_id in rows]}
    finally:
        db.close()


@router.get("/turns/{turn_id}/guardrail-events")
def turn_guardrail_events(
    turn_id: str,
    admin: AdminPrincipal = Depends(require_admin),
) -> dict:
    db = SessionLocal()
    try:
        turn = db.query(ConversationTurn).filter(ConversationTurn.public_id == turn_id).first()
        if not turn:
            raise HTTPException(status_code=404, detail="Turn not found")
        rows = (
            db.query(GuardrailEvent)
            .filter(GuardrailEvent.turn_id == turn.public_id)
            .order_by(GuardrailEvent.created_at.asc())
            .all()
        )
        return {
            "turn_id": turn.public_id,
            "events": [_guardrail_event_row(row) for row in rows],
        }
    finally:
        db.close()


@router.get("/turns/{turn_id}/trace")
def turn_trace(
    turn_id: str,
    admin: AdminPrincipal = Depends(require_admin),
) -> dict:
    db = SessionLocal()
    try:
        goal = db.query(AgentGoal).filter(AgentGoal.turn_id == turn_id).first()
        runs = (
            db.query(AgentRunLog)
            .filter(AgentRunLog.goal_id == goal.id)
            .order_by(AgentRunLog.started_at.asc())
            .all()
            if goal else []
        )
        run_ids = [run.id for run in runs]
        steps = (
            db.query(AgentStep)
            .filter(AgentStep.run_id.in_(run_ids))
            .order_by(AgentStep.created_at.asc())
            .all()
            if run_ids else []
        )
        events = (
            db.query(GuardrailEvent)
            .filter(GuardrailEvent.turn_id == turn_id)
            .order_by(GuardrailEvent.created_at.asc())
            .all()
        )

        runs_by_id = {run.id: run for run in runs}
        prompt_versions: dict[str, str] = {}
        for step in steps:
            meta = _parse_json_object(step.metadata_json)
            prompt_id = meta.get("prompt_id")
            run = runs_by_id.get(step.run_id)
            if run and isinstance(prompt_id, str):
                prompt_versions[run.agent_id] = prompt_id

        return {
            "turn_id": turn_id,
            "goal": {
                "id": goal.id,
                "objective": goal.objective,
                "quality_mode": goal.quality_mode,
                "status": goal.status,
            } if goal else None,
            "agent_runs": [
                {
                    "id": run.id,
                    "agent_id": run.agent_id,
                    "parent_run_id": run.parent_run_id,
                    "status": run.status,
                    "latency_ms": run.latency_ms,
                    "cost_usd": run.total_cost_usd,
                }
                for run in runs
            ],
            "agent_steps": [
                {
                    "id": step.id,
                    "run_id": step.run_id,
                    "step_type": step.step_type,
                    "model_used": step.model_used,
                    "latency_ms": step.latency_ms,
                    "cost_usd": step.cost_usd,
                }
                for step in steps
            ],
            "guardrail_events": [
                {
                    "policy_id": event.policy_id,
                    "boundary": event.boundary,
                    "action": event.action,
                    "tool_name": event.tool_name,
                }
                for event in events
            ],
            "prompt_versions": prompt_versions,
            "total_cost_usd": sum(float(run.total_cost_usd or 0.0) for run in runs),
        }
    finally:
        db.close()


def _guardrail_event_row(row: GuardrailEvent) -> dict:
    try:
        triggered_checks = json.loads(row.triggered_checks_json or "[]")
    except (TypeError, ValueError):
        triggered_checks = []
    return {
        "policy_id": row.policy_id,
        "boundary": row.boundary,
        "action": row.action,
        "triggered_checks": triggered_checks if isinstance(triggered_checks, list) else [],
        "reason": row.reason,
        "tool_name": row.tool_name,
        "created_at": _fmt(row.created_at),
    }


@router.get("/agent-v3/workspaces")
def admin_agent_v3_workspaces(
    user_id: str | None = Query(default=None),
    admin: AdminPrincipal = Depends(require_admin),
) -> dict:
    """Read Agent v3 workspace summaries across users.

    User-facing Agent v3 endpoints always filter by the authenticated user.
    This admin-only view intentionally rolls up all users unless user_id is
    provided.
    """
    if user_id:
        return {
            "users": [
                {
                    "user_id": user_id,
                    "workspaces": [
                        workspace.model_dump(mode="json")
                        for workspace in agent_v3_persistence.list_workspaces(user_id, ensure_default=False)
                    ],
                }
            ]
        }

    db = SessionLocal()
    try:
        user_ids = [
            row[0]
            for row in (
                db.query(AgentV3Workspace.user_id)
                .distinct()
                .order_by(AgentV3Workspace.user_id.asc())
                .all()
            )
        ]
    finally:
        db.close()
    return {
        "users": [
            {
                "user_id": uid,
                "workspaces": [
                    workspace.model_dump(mode="json")
                    for workspace in agent_v3_persistence.list_workspaces(uid, ensure_default=False)
                ],
            }
            for uid in user_ids
        ]
    }


@router.get("/agent-v3/prompts")
def admin_agent_v3_prompts(admin: AdminPrincipal = Depends(require_admin)) -> dict:
    return {"prompts": [item.model_dump(mode="json") for item in agent_v3_prompt_library.list_prompts()]}


@router.post("/agent-v3/prompts/seed")
def admin_agent_v3_prompt_seed(admin: AdminPrincipal = Depends(require_admin)) -> dict:
    counts = agent_v3_prompt_library.seed_defaults()
    db = SessionLocal()
    try:
        _audit(db, admin, "agent_v3.prompt.seed", details=counts)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return {"seeded": counts}


@router.post("/agent-v3/prompts")
def admin_agent_v3_prompt_upsert(
    body: AgentV3PromptUpsertRequest,
    admin: AdminPrincipal = Depends(require_admin),
) -> dict:
    spec = agent_v3_prompt_library.AgentV3PromptSpec(**body.model_dump())
    saved = agent_v3_prompt_library.upsert_prompt(spec)
    db = SessionLocal()
    try:
        _audit(
            db,
            admin,
            "agent_v3.prompt.upsert",
            details={"prompt_id": saved.id, "agent_id": saved.agent_id, "status": saved.status},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return {"prompt": saved.model_dump(mode="json")}


@router.post("/agent-v3/prompts/{prompt_id}/activate")
def admin_agent_v3_prompt_activate(prompt_id: str, admin: AdminPrincipal = Depends(require_admin)) -> dict:
    activated = agent_v3_prompt_library.activate_prompt(prompt_id)
    if activated is None:
        raise HTTPException(status_code=404, detail="Agent v3 prompt not found")
    db = SessionLocal()
    try:
        _audit(
            db,
            admin,
            "agent_v3.prompt.activate",
            details={"prompt_id": activated.id, "agent_id": activated.agent_id, "profile": activated.profile},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return {"activated": activated.id, "prompt": activated.model_dump(mode="json")}


@router.post("/agent-v3/prompts/{prompt_id}/rollback")
def admin_agent_v3_prompt_rollback(prompt_id: str, admin: AdminPrincipal = Depends(require_admin)) -> dict:
    rolled_back = agent_v3_prompt_library.rollback_prompt(prompt_id)
    if rolled_back is None:
        raise HTTPException(status_code=409, detail="No previous Agent v3 prompt version exists")
    db = SessionLocal()
    try:
        _audit(
            db,
            admin,
            "agent_v3.prompt.rollback",
            details={"prompt_id": prompt_id, "rolled_back_to": rolled_back.id},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return {"rolled_back_to": rolled_back.id, "prompt": rolled_back.model_dump(mode="json")}


@router.get("/agent-v3/model-policy")
def admin_agent_v3_model_policy(admin: AdminPrincipal = Depends(require_admin)) -> dict:
    """Effective Agent v3 model assignment: defaults merged with whatever an
    admin has overridden. This is the single source of truth — there is no
    .env fallback for model identity anymore."""
    db = SessionLocal()
    try:
        policy = agent_v3_model_policy.get_model_policy(db)
        return {
            "roles": policy["roles"],
            "fallback_models": policy["fallback_models"],
            "defaults": {
                "roles": agent_v3_model_policy.DEFAULT_MODEL_POLICY,
                "fallback_models": agent_v3_model_policy.DEFAULT_FALLBACK_MODELS,
            },
            "available_roles": list(agent_v3_model_policy.MODEL_ROLES),
        }
    finally:
        db.close()


@router.patch("/agent-v3/model-policy")
def admin_agent_v3_model_policy_update(
    body: AgentV3ModelPolicyUpdate,
    admin: AdminPrincipal = Depends(require_admin),
) -> dict:
    db = SessionLocal()
    try:
        try:
            policy = agent_v3_model_policy.set_model_policy(
                db,
                role_overrides=body.roles or None,
                fallback_models=body.fallback_models,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        _audit(db, admin, "agent_v3.model_policy.update", details=body.model_dump())
        db.commit()
        return {"roles": policy["roles"], "fallback_models": policy["fallback_models"]}
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.post("/agent-v3/model-policy/reset")
def admin_agent_v3_model_policy_reset(admin: AdminPrincipal = Depends(require_admin)) -> dict:
    db = SessionLocal()
    try:
        policy = agent_v3_model_policy.reset_model_policy(db)
        _audit(db, admin, "agent_v3.model_policy.reset")
        db.commit()
        return {"roles": policy["roles"], "fallback_models": policy["fallback_models"]}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.get("/agent-v3/routing-signals")
def admin_agent_v3_routing_signals(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    admin: AdminPrincipal = Depends(require_admin),
) -> dict:
    return agent_v3_routing_policy.list_signal_candidates(status=status, limit=limit)


@router.post("/agent-v3/routing-signals/{candidate_id}/approve")
def admin_agent_v3_routing_signal_approve(candidate_id: str, admin: AdminPrincipal = Depends(require_admin)) -> dict:
    candidate = agent_v3_routing_policy.set_signal_candidate_status(candidate_id, "approved")
    if candidate is None:
        raise HTTPException(status_code=404, detail="Agent v3 routing signal candidate not found")
    db = SessionLocal()
    try:
        _audit(db, admin, "agent_v3.routing_signal.approve", details={"candidate_id": candidate_id})
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return {"candidate": candidate}


@router.post("/agent-v3/routing-signals/{candidate_id}/reject")
def admin_agent_v3_routing_signal_reject(candidate_id: str, admin: AdminPrincipal = Depends(require_admin)) -> dict:
    candidate = agent_v3_routing_policy.set_signal_candidate_status(candidate_id, "rejected")
    if candidate is None:
        raise HTTPException(status_code=404, detail="Agent v3 routing signal candidate not found")
    db = SessionLocal()
    try:
        _audit(db, admin, "agent_v3.routing_signal.reject", details={"candidate_id": candidate_id})
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return {"candidate": candidate}


@router.post("/turns/{turn_id}/cancel")
def cancel_turn(
    turn_id: str,
    body: AdminTurnCancelRequest | None = None,
    admin: AdminPrincipal = Depends(require_admin),
) -> dict:
    db = SessionLocal()
    try:
        turn = db.query(ConversationTurn).filter(ConversationTurn.public_id == turn_id).first()
        if not turn:
            raise HTTPException(status_code=404, detail="Turn not found")
        if turn.status in {"pending", "running"}:
            from app.routers.conversations import _mark_turn_cancel_requested
            _mark_turn_cancel_requested(turn.public_id)
            now = _now()
            turn.status = "cancelled"
            turn.completed_at = now
            turn.updated_at = now
            turn.error_message = (body.reason if body and body.reason else "Cancelled by admin.")
            _append_turn_lifecycle(turn, "cancelled_by_admin", {"admin_user_id": admin.user_id, "reason": turn.error_message})
            _audit(db, admin, "turn.cancel", turn.user_id, {"turn_id": turn.public_id, "reason": turn.error_message})
            db.commit()
        conv_public_id = db.query(Conversation.public_id).filter(Conversation.id == turn.conversation_id).scalar()
        return _turn_row(turn, conv_public_id)
    finally:
        db.close()


@router.get("/turn-runtime")
def turn_runtime(admin: AdminPrincipal = Depends(require_admin)) -> dict:
    db = SessionLocal()
    try:
        return get_turn_runtime_config(db)
    finally:
        db.close()


@router.patch("/turn-runtime")
def update_turn_runtime(body: TurnRuntimeUpdate, admin: AdminPrincipal = Depends(require_admin)) -> dict:
    db = SessionLocal()
    try:
        set_turn_runtime_config(
            db,
            body.quick_timeout_minutes,
            body.research_timeout_minutes,
            body.document_timeout_minutes,
        )
        _audit(db, admin, "turn_runtime.update", None, body.model_dump())
        db.commit()
        return get_turn_runtime_config(db)
    finally:
        db.close()


@router.patch("/global-budget")
def update_global_budget(body: GlobalBudgetUpdate, admin: AdminPrincipal = Depends(require_admin)) -> dict:
    db = SessionLocal()
    try:
        set_global_budget_config(db, body.monthly_budget_usd, body.admin_override_enabled)
        _audit(db, admin, "global_budget.update", None, body.model_dump())
        budget = _budget_status(db)
        db.commit()
        return budget
    finally:
        db.close()


@router.get("/users")
def users(
    query: str = "",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    admin: AdminPrincipal = Depends(require_admin),
) -> dict:
    db = SessionLocal()
    try:
        all_ids = sorted(_all_known_user_ids(db))
        if query.strip():
            needle = query.strip().lower()
            matched_by_profile = {
                clerk_id
                for (clerk_id,) in db.query(User.clerk_id).filter(
                    or_(
                        func.lower(User.email).contains(needle),
                        func.lower(User.name).contains(needle),
                    )
                ).all()
            }
            all_ids = [
                u for u in all_ids
                if needle in u.lower() or u in matched_by_profile
            ]
        page = all_ids[offset:offset + limit]
        if not page:
            return {"items": [], "total": len(all_ids), "limit": limit, "offset": offset}
        controls = {
            c.user_id: c
            for c in db.query(UserAdminControl).filter(UserAdminControl.user_id.in_(page)).all()
        }
        emails = dict(db.query(User.clerk_id, User.email).filter(User.clerk_id.in_(page)).all())
        names = dict(db.query(User.clerk_id, User.name).filter(User.clerk_id.in_(page)).all())

        # Backfill missing email/name from Clerk for users we haven't resolved yet
        # (e.g. accounts created before the User profile table was populated, or
        # whose JWT didn't carry email/name claims).
        if get_settings().clerk_secret_key:
            for user_id in page:
                if emails.get(user_id) or names.get(user_id):
                    continue
                info = fetch_clerk_user(user_id)
                if not info:
                    continue
                user_row = db.query(User).filter(User.clerk_id == user_id).first()
                if not user_row:
                    user_row = User(clerk_id=user_id)
                    db.add(user_row)
                if info.get("email"):
                    user_row.email = info["email"]
                    emails[user_id] = info["email"]
                if info.get("name"):
                    user_row.name = info["name"]
                    names[user_id] = info["name"]
            db.commit()

        conv_counts = dict(db.query(Conversation.user_id, func.count(Conversation.id)).filter(Conversation.user_id.in_(page)).group_by(Conversation.user_id).all())
        req_counts = dict(db.query(RequestLog.user_id, func.count(RequestLog.id)).filter(RequestLog.user_id.in_(page), RequestLog.status == "success").group_by(RequestLog.user_id).all())
        asst_counts = dict(
            db.query(Conversation.user_id, func.count(ConversationMessage.id))
            .join(ConversationMessage, ConversationMessage.conversation_id == Conversation.id)
            .filter(Conversation.user_id.in_(page), ConversationMessage.role == "assistant")
            .group_by(Conversation.user_id)
            .all()
        )
        msg_costs = dict(
            db.query(Conversation.user_id, func.coalesce(func.sum(ConversationMessage.estimated_cost_usd), 0.0))
            .join(ConversationMessage, ConversationMessage.conversation_id == Conversation.id)
            .filter(Conversation.user_id.in_(page), ConversationMessage.role == "assistant")
            .group_by(Conversation.user_id)
            .all()
        )
        req_costs = dict(
            db.query(RequestLog.user_id, func.coalesce(func.sum(RequestLog.estimated_cost_usd), 0.0))
            .filter(RequestLog.user_id.in_(page), RequestLog.status == "success")
            .group_by(RequestLog.user_id)
            .all()
        )
        month_start = _today_start().replace(day=1)
        month_msg_costs = dict(
            db.query(Conversation.user_id, func.coalesce(func.sum(ConversationMessage.estimated_cost_usd), 0.0))
            .join(ConversationMessage, ConversationMessage.conversation_id == Conversation.id)
            .filter(Conversation.user_id.in_(page), ConversationMessage.role == "assistant", ConversationMessage.created_at >= month_start)
            .group_by(Conversation.user_id)
            .all()
        )
        month_req_costs = dict(
            db.query(RequestLog.user_id, func.coalesce(func.sum(RequestLog.estimated_cost_usd), 0.0))
            .filter(RequestLog.user_id.in_(page), RequestLog.status == "success", RequestLog.created_at >= month_start)
            .group_by(RequestLog.user_id)
            .all()
        )
        memory_counts = dict(db.query(UserMemory.user_id, func.count(UserMemory.id)).filter(UserMemory.user_id.in_(page)).group_by(UserMemory.user_id).all())
        sample_counts = dict(db.query(WritingSample.user_id, func.count(WritingSample.id)).filter(WritingSample.user_id.in_(page)).group_by(WritingSample.user_id).all())
        research_counts = dict(db.query(ResearchRun.user_id, func.count(ResearchRun.id)).filter(ResearchRun.user_id.in_(page)).group_by(ResearchRun.user_id).all())
        conv_seen = dict(db.query(Conversation.user_id, func.max(Conversation.updated_at)).filter(Conversation.user_id.in_(page)).group_by(Conversation.user_id).all())
        req_seen = dict(db.query(RequestLog.user_id, func.max(RequestLog.created_at)).filter(RequestLog.user_id.in_(page)).group_by(RequestLog.user_id).all())
        research_seen = dict(db.query(ResearchRun.user_id, func.max(ResearchRun.updated_at)).filter(ResearchRun.user_id.in_(page)).group_by(ResearchRun.user_id).all())

        rows = []
        for user_id in page:
            last_seen = max(
                (v for v in [conv_seen.get(user_id), req_seen.get(user_id), research_seen.get(user_id)] if v is not None),
                default=None,
            )
            control = controls.get(user_id)
            rows.append({
                "user_id": user_id,
                "email": emails.get(user_id),
                "name": names.get(user_id),
                "status": control.status if control else "active",
                "role": _effective_role(user_id, control.role if control else None, emails.get(user_id)),
                "monthly_budget_usd": control.monthly_budget_usd if control else None,
                "month_spend": round(float(month_msg_costs.get(user_id, 0) or 0) + float(month_req_costs.get(user_id, 0) or 0), 6),
                "conversation_count": conv_counts.get(user_id, 0),
                "request_count": req_counts.get(user_id, 0) + asst_counts.get(user_id, 0),
                "total_spend": round(float(msg_costs.get(user_id, 0) or 0) + float(req_costs.get(user_id, 0) or 0), 6),
                "memory_count": memory_counts.get(user_id, 0),
                "writing_sample_count": sample_counts.get(user_id, 0),
                "research_run_count": research_counts.get(user_id, 0),
                "last_seen_at": _fmt(last_seen),
            })
        return {"items": rows, "total": len(all_ids), "limit": limit, "offset": offset}
    finally:
        db.close()


@router.get("/users/{user_id}")
def user_detail(user_id: str, admin: AdminPrincipal = Depends(require_admin)) -> dict:
    db = SessionLocal()
    try:
        _ensure_target_user_id(user_id)
        control = db.query(UserAdminControl).filter(UserAdminControl.user_id == user_id).first()
        conversations = (
            db.query(Conversation)
            .filter(Conversation.user_id == user_id)
            .order_by(Conversation.updated_at.desc())
            .limit(10)
            .all()
        )
        recent_errors = (
            db.query(RequestLog)
            .filter(RequestLog.user_id == user_id, RequestLog.status == "error")
            .order_by(RequestLog.created_at.desc())
            .limit(10)
            .all()
        )
        research_runs = (
            db.query(ResearchRun)
            .filter(ResearchRun.user_id == user_id)
            .order_by(ResearchRun.updated_at.desc())
            .limit(10)
            .all()
        )
        user_row = db.query(User).filter(User.clerk_id == user_id).first()
        user_email = user_row.email if user_row else None
        user_name = user_row.name if user_row else None
        if not (user_email or user_name) and get_settings().clerk_secret_key:
            info = fetch_clerk_user(user_id)
            if info:
                if not user_row:
                    user_row = User(clerk_id=user_id)
                    db.add(user_row)
                if info.get("email"):
                    user_row.email = info["email"]
                if info.get("name"):
                    user_row.name = info["name"]
                db.commit()
                user_email = user_row.email
                user_name = user_row.name
        control_out = _control_out(control)
        control_out["role"] = _effective_role(user_id, control.role if control else None, user_email)
        result = {
            "user_id": user_id,
            "email": user_email,
            "name": user_name,
            "control": control_out,
            "month_spend": round(get_monthly_spend(db, user_id), 6),
            "counts": {
                "conversations": db.query(Conversation).filter(Conversation.user_id == user_id).count(),
                "messages": (
                    db.query(ConversationMessage)
                    .join(Conversation, ConversationMessage.conversation_id == Conversation.id)
                    .filter(Conversation.user_id == user_id)
                    .count()
                ),
                "memories": db.query(UserMemory).filter(UserMemory.user_id == user_id).count(),
                "user_profiles": db.query(UserProfile).filter(UserProfile.user_id == user_id).count(),
                "writing_samples": db.query(WritingSample).filter(WritingSample.user_id == user_id).count(),
                "twin_profiles": db.query(TwinProfile).filter(TwinProfile.user_id == user_id).count(),
                "research_runs": db.query(ResearchRun).filter(ResearchRun.user_id == user_id).count(),
            },
            "recent_conversations": [
                {
                    "id": c.id,
                    "title": c.title,
                    "profile": c.profile,
                    "message_count": c.message_count,
                    "updated_at": _fmt(c.updated_at),
                }
                for c in conversations
            ],
            "recent_research_runs": [
                {
                    "id": r.id,
                    "query": r.query[:240],
                    "mode": r.mode,
                    "status": r.status,
                    "source_count": r.source_count,
                    "claim_count": r.claim_count,
                    "confidence": r.confidence,
                    "updated_at": _fmt(r.updated_at),
                }
                for r in research_runs
            ],
            "recent_errors": [
                {
                    "id": e.id,
                    "created_at": _fmt(e.created_at),
                    "task_type": e.task_type,
                    "selected_model": e.selected_model,
                    "error": (e.error or "")[:500],
                }
                for e in recent_errors
            ],
        }
        _audit(db, admin, "user_detail.view", user_id, {"counts": result["counts"]})
        db.commit()
        return result
    finally:
        db.close()


@router.patch("/users/{user_id}/control")
def update_user_control(
    user_id: str,
    body: AdminControlUpdate,
    admin: AdminPrincipal = Depends(require_admin),
) -> dict:
    db = SessionLocal()
    try:
        _ensure_target_user_id(user_id)
        if user_id == admin.user_id and body.status == "suspended":
            raise HTTPException(status_code=400, detail="Admins cannot suspend their own account.")
        control = db.query(UserAdminControl).filter(UserAdminControl.user_id == user_id).first()
        if not control:
            control = UserAdminControl(user_id=user_id, created_at=_now())
            db.add(control)
        control.status = body.status
        control.monthly_budget_usd = body.monthly_budget_usd
        control.notes = body.notes
        control.updated_at = _now()
        _audit(db, admin, "user_control.update", user_id, body.model_dump())
        db.commit()
        db.refresh(control)
        return {"user_id": user_id, "control": _control_out(control)}
    finally:
        db.close()


@router.patch("/users/{user_id}/role")
def update_user_role(
    user_id: str,
    body: UserRoleUpdate,
    admin: AdminPrincipal = Depends(require_admin),
) -> dict:
    """Grant or revoke admin access for a user. Backed by `user_admin_controls.role`,
    layered on top of the static ADMIN_USER_IDS/ADMIN_EMAILS env allowlist."""
    db = SessionLocal()
    try:
        _ensure_target_user_id(user_id)
        if user_id == admin.user_id and body.role != "admin":
            raise HTTPException(status_code=400, detail="Admins cannot remove their own admin role.")
        if is_admin_user(user_id, None) and body.role != "admin":
            raise HTTPException(
                status_code=400,
                detail="This user is an admin via ADMIN_USER_IDS/ADMIN_EMAILS env config; "
                       "remove them from that allowlist to revoke admin access.",
            )
        control = db.query(UserAdminControl).filter(UserAdminControl.user_id == user_id).first()
        if not control:
            control = UserAdminControl(user_id=user_id, created_at=_now())
            db.add(control)
        control.role = body.role
        control.updated_at = _now()
        _audit(db, admin, "user_role.update", user_id, {"role": body.role})
        db.commit()
        db.refresh(control)
        user_email = db.query(User.email).filter(User.clerk_id == user_id).scalar()
        return {"user_id": user_id, "role": _effective_role(user_id, control.role, user_email)}
    finally:
        db.close()


@router.post("/users/{user_id}/privacy-delete")
def privacy_delete(
    user_id: str,
    body: PrivacyDeleteRequest,
    dry_run: bool = Query(default=False),
    admin: AdminPrincipal = Depends(require_admin),
) -> dict:
    db = SessionLocal()
    try:
        _ensure_target_user_id(user_id)
        requested = body.model_dump(exclude={"confirm_user_id"})
        counts = _privacy_counts(db, user_id)
        _audit(db, admin, "user.privacy_delete.request", user_id, {"dry_run": dry_run, "requested": requested, "counts": counts})
        if dry_run or not any(requested.values()):
            db.commit()
            return {"user_id": user_id, "dry_run": True, "counts": counts}
        if user_id == admin.user_id:
            db.commit()
            raise HTTPException(status_code=400, detail="Admins cannot privacy-delete their own account.")
        if body.confirm_user_id != user_id:
            db.commit()
            raise HTTPException(status_code=400, detail="confirm_user_id must match target user_id.")
        deleted: dict[str, int] = {}
        if body.memories:
            deleted["memories"] = db.query(UserMemory).filter(UserMemory.user_id == user_id).delete()
        if body.user_profile:
            deleted["user_profiles"] = db.query(UserProfile).filter(UserProfile.user_id == user_id).delete()
        if body.document_templates:
            rows = db.query(DocumentTemplate).filter(DocumentTemplate.user_id == user_id).all()
            deleted["document_templates"] = len(rows)
            for row in rows:
                try:
                    path = template_path_for_row(row)
                    if path.exists():
                        path.unlink()
                except Exception:
                    logger.warning("Failed to delete template file for %s", row.public_id, exc_info=True)
                db.delete(row)
        if body.writing_samples:
            deleted["writing_samples"] = db.query(WritingSample).filter(WritingSample.user_id == user_id).delete()
        if body.twin_profile:
            deleted["twin_profiles"] = db.query(TwinProfile).filter(TwinProfile.user_id == user_id).delete()
        if body.conversations:
            conv_ids = [cid for (cid,) in db.query(Conversation.id).filter(Conversation.user_id == user_id).all()]
            if conv_ids:
                db.query(ConversationMessage).filter(ConversationMessage.conversation_id.in_(conv_ids)).delete(synchronize_session=False)
            deleted["conversations"] = db.query(Conversation).filter(Conversation.user_id == user_id).delete()
        if body.research_runs:
            run_ids = [rid for (rid,) in db.query(ResearchRun.id).filter(ResearchRun.user_id == user_id).all()]
            if run_ids:
                db.query(ResearchFinding).filter(ResearchFinding.run_id.in_(run_ids)).delete(synchronize_session=False)
                db.query(ResearchClaim).filter(ResearchClaim.run_id.in_(run_ids)).delete(synchronize_session=False)
                db.query(ResearchSource).filter(ResearchSource.run_id.in_(run_ids)).delete(synchronize_session=False)
                db.query(ResearchQuestion).filter(ResearchQuestion.run_id.in_(run_ids)).delete(synchronize_session=False)
            deleted["research_runs"] = db.query(ResearchRun).filter(ResearchRun.user_id == user_id).delete()
        _audit(db, admin, "user.privacy_delete", user_id, {"deleted": deleted})
        db.commit()
        return {"user_id": user_id, "deleted": deleted}
    finally:
        db.close()


@router.get("/usage")
def usage(
    range: str = Query(default="7d", pattern="^(1d|7d|30d|all)$"),
    admin: AdminPrincipal = Depends(require_admin),
) -> dict:
    db = SessionLocal()
    try:
        start = _start_for_range(range)
        msg_base = (
            db.query(ConversationMessage)
            .join(Conversation, ConversationMessage.conversation_id == Conversation.id)
            .filter(ConversationMessage.role == "assistant")
        )
        req_base = db.query(RequestLog).filter(RequestLog.status == "success")
        if start:
            msg_base = msg_base.filter(ConversationMessage.created_at >= start)
            req_base = req_base.filter(RequestLog.created_at >= start)

        by_day: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "requests": 0})
        for day, cost, count in (
            msg_base.with_entities(
                func.date(ConversationMessage.created_at),
                func.coalesce(func.sum(ConversationMessage.estimated_cost_usd), 0.0),
                func.count(ConversationMessage.id),
            )
            .group_by(func.date(ConversationMessage.created_at))
            .all()
        ):
            by_day[str(day)]["cost"] += float(cost or 0)
            by_day[str(day)]["requests"] += int(count or 0)
        for day, cost, count in (
            req_base.with_entities(
                func.date(RequestLog.created_at),
                func.coalesce(func.sum(RequestLog.estimated_cost_usd), 0.0),
                func.count(RequestLog.id),
            )
            .group_by(func.date(RequestLog.created_at))
            .all()
        ):
            by_day[str(day)]["cost"] += float(cost or 0)
            by_day[str(day)]["requests"] += int(count or 0)

        by_user: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "requests": 0})
        for user_id, cost, count in (
            msg_base.with_entities(
                Conversation.user_id,
                func.coalesce(func.sum(ConversationMessage.estimated_cost_usd), 0.0),
                func.count(ConversationMessage.id),
            )
            .group_by(Conversation.user_id)
            .all()
        ):
            if user_id:
                by_user[user_id]["cost"] += float(cost or 0)
                by_user[user_id]["requests"] += int(count or 0)
        for user_id, cost, count in (
            req_base.with_entities(
                RequestLog.user_id,
                func.coalesce(func.sum(RequestLog.estimated_cost_usd), 0.0),
                func.count(RequestLog.id),
            )
            .group_by(RequestLog.user_id)
            .all()
        ):
            if user_id:
                by_user[user_id]["cost"] += float(cost or 0)
                by_user[user_id]["requests"] += int(count or 0)

        user_profiles = _user_profiles(db, by_user.keys())

        by_model: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "requests": 0, "latency_total": 0.0, "latency_count": 0})
        for model, cost, count, avg_latency in (
            msg_base.with_entities(
                ConversationMessage.model_used,
                func.coalesce(func.sum(ConversationMessage.estimated_cost_usd), 0.0),
                func.count(ConversationMessage.id),
                func.avg(ConversationMessage.latency_ms),
            )
            .filter(ConversationMessage.model_used.isnot(None))
            .group_by(ConversationMessage.model_used)
            .all()
        ):
            if model:
                by_model[model]["cost"] += float(cost or 0)
                by_model[model]["requests"] += int(count or 0)
                if avg_latency is not None:
                    by_model[model]["latency_total"] += float(avg_latency) * int(count or 0)
                    by_model[model]["latency_count"] += int(count or 0)
        for model, cost, count, avg_latency in (
            req_base.with_entities(
                RequestLog.model_used,
                func.coalesce(func.sum(RequestLog.estimated_cost_usd), 0.0),
                func.count(RequestLog.id),
                func.avg(RequestLog.latency_ms),
            )
            .filter(RequestLog.model_used.isnot(None))
            .group_by(RequestLog.model_used)
            .all()
        ):
            if model:
                by_model[model]["cost"] += float(cost or 0)
                by_model[model]["requests"] += int(count or 0)
                if avg_latency is not None:
                    by_model[model]["latency_total"] += float(avg_latency) * int(count or 0)
                    by_model[model]["latency_count"] += int(count or 0)

        by_task: dict[str, int] = defaultdict(int)
        for task, count in msg_base.with_entities(ConversationMessage.task_type, func.count(ConversationMessage.id)).filter(ConversationMessage.task_type.isnot(None)).group_by(ConversationMessage.task_type).all():
            if task:
                by_task[task] += int(count or 0)
        for task, count in req_base.with_entities(RequestLog.task_type, func.count(RequestLog.id)).filter(RequestLog.task_type.isnot(None)).group_by(RequestLog.task_type).all():
            if task:
                by_task[task] += int(count or 0)

        msg_summary = msg_base.with_entities(
            func.coalesce(func.sum(ConversationMessage.estimated_cost_usd), 0.0),
            func.count(ConversationMessage.id),
            func.coalesce(func.sum(ConversationMessage.prompt_tokens), 0),
            func.coalesce(func.sum(ConversationMessage.completion_tokens), 0),
        ).one()
        req_summary = req_base.with_entities(
            func.coalesce(func.sum(RequestLog.estimated_cost_usd), 0.0),
            func.count(RequestLog.id),
            func.coalesce(func.sum(RequestLog.prompt_tokens), 0),
            func.coalesce(func.sum(RequestLog.completion_tokens), 0),
        ).one()
        total_cost = float(msg_summary[0] or 0) + float(req_summary[0] or 0)
        total_requests = int(msg_summary[1] or 0) + int(req_summary[1] or 0)
        total_tokens = int(msg_summary[2] or 0) + int(msg_summary[3] or 0) + int(req_summary[2] or 0) + int(req_summary[3] or 0)
        return {
            "range": range,
            "summary": {
                "total_cost": round(total_cost, 6),
                "requests": total_requests,
                "tokens": total_tokens,
                "users": len(by_user),
            },
            "cost_by_day": [
                {"date": d, "cost": round(v["cost"], 6), "requests": v["requests"]}
                for d, v in sorted(by_day.items())
            ],
            "top_users": sorted(
                [
                    {
                        "user_id": u,
                        **_profile_fields(user_profiles, u),
                        "cost": round(v["cost"], 6),
                        "requests": v["requests"],
                    }
                    for u, v in by_user.items()
                ],
                key=lambda x: -x["cost"],
            )[:20],
            "model_usage": sorted(
                [
                    {
                        "model": m,
                        "cost": round(v["cost"], 6),
                        "requests": v["requests"],
                        "avg_latency_ms": round(v["latency_total"] / v["latency_count"], 1) if v["latency_count"] else 0,
                    }
                    for m, v in by_model.items()
                ],
                key=lambda x: -x["requests"],
            ),
            "task_distribution": sorted(
                [{"task_type": t, "count": c} for t, c in by_task.items()],
                key=lambda x: -x["count"],
            ),
        }
    finally:
        db.close()


@router.get("/providers")
def providers(admin: AdminPrincipal = Depends(require_admin)) -> dict:
    settings = get_settings()
    db = SessionLocal()
    try:
        recent_errors = (
            db.query(RequestLog)
            .filter(RequestLog.status == "error", RequestLog.created_at >= _now() - timedelta(days=7))
            .all()
        )
        provider_errors: dict[str, int] = defaultdict(int)
        for err in recent_errors:
            provider_errors[provider_for_model(err.selected_model or err.model_used)] += 1
        circuit_status = get_circuit_status()
        default_circuit = {"consecutive_failures": 0, "open": False, "cooldown_remaining_s": 0}
        return {
            "providers": [
                {
                    "name": "OpenAI", "key": "OPENAI_API_KEY",
                    "configured": bool(settings.openai_api_key),
                    "key_hint": _key_hint(settings.openai_api_key),
                    "testable": True,
                    "circuit": circuit_status.get("OpenAI", default_circuit),
                },
                {
                    "name": "Anthropic", "key": "ANTHROPIC_API_KEY",
                    "configured": bool(settings.anthropic_api_key),
                    "key_hint": _key_hint(settings.anthropic_api_key),
                    "testable": True,
                    "circuit": circuit_status.get("Anthropic", default_circuit),
                },
                {
                    "name": "Gemini", "key": "GEMINI_API_KEY",
                    "configured": bool(settings.gemini_api_key),
                    "key_hint": _key_hint(settings.gemini_api_key),
                    "testable": True,
                    "circuit": circuit_status.get("Gemini", default_circuit),
                },
                {
                    "name": "OpenRouter", "key": "OPENROUTER_API_KEY",
                    "configured": bool(settings.openrouter_api_key),
                    "key_hint": _key_hint(settings.openrouter_api_key),
                    "testable": True,
                    "circuit": circuit_status.get("OpenRouter", default_circuit),
                },
                {
                    "name": "You.com", "key": "YOU_API_KEY",
                    "configured": bool(settings.you_api_key),
                    "key_hint": _key_hint(settings.you_api_key),
                    "testable": True,
                },
                {
                    "name": "Tavily", "key": "TAVILY_API_KEY",
                    "configured": bool(settings.tavily_api_key),
                    "key_hint": _key_hint(settings.tavily_api_key),
                    "testable": True,
                },
                {
                    "name": "Nimble", "key": "NIMBLE_API_KEY",
                    "configured": bool(settings.nimble_api_key),
                    "key_hint": _key_hint(settings.nimble_api_key),
                    "testable": True,
                },
            ],
            "recent_error_counts": dict(provider_errors),
        }
    finally:
        db.close()


@router.post("/providers/test")
def providers_test(body: ProviderTestRequest, admin: AdminPrincipal = Depends(require_admin)) -> dict:
    provider = body.provider
    # Live provider/search calls cost money and quota — throttle repeated clicks
    # per admin per provider, independent of the per-user rate limits.
    check_rate_limit(f"admin-provider-test:{admin.user_id}:{provider}", 6, 60)
    if provider in PROVIDER_TEST_MODELS:
        result = test_provider_connection(provider)
    elif provider == "You.com":
        result = test_you_connection()
    elif provider == "Tavily":
        result = test_tavily_connection()
    elif provider == "Nimble":
        result = test_nimble_connection()
    else:
        raise HTTPException(status_code=400, detail=f"Unknown or non-testable provider '{provider}'.")

    db = SessionLocal()
    try:
        _audit(db, admin, "provider.test", None, {
            "provider": provider,
            "success": result.get("success"),
            "latency_ms": result.get("latency_ms"),
            "error": result.get("error"),
        })
        db.commit()
    finally:
        db.close()
    return {"provider": provider, **result}


@router.get("/routing/policy")
def routing_policy(admin: AdminPrincipal = Depends(require_admin)) -> dict:
    return load_policy()


@router.post("/routing/test")
def routing_test(body: RouteTestRequest, admin: AdminPrincipal = Depends(require_admin)) -> dict:
    route = choose_route(
        body.message,
        profile=body.profile,  # type: ignore[arg-type]
        force_model=body.force_model,
        deep_research=body.deep_research,
        web_search=body.web_search,
        task_override=body.task_override,  # type: ignore[arg-type]
        complexity_override=body.complexity_override,  # type: ignore[arg-type]
        preferred_model=body.preferred_model,
    )
    return route.model_dump()


@router.get("/research-runs")
def research_runs(
    status: str = "",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    admin: AdminPrincipal = Depends(require_admin),
) -> dict:
    db = SessionLocal()
    try:
        q = db.query(ResearchRun)
        if status:
            q = q.filter(ResearchRun.status == status)
        total = q.count()
        runs = q.order_by(ResearchRun.updated_at.desc()).offset(offset).limit(limit).all()
        user_profiles = _user_profiles(db, [r.user_id for r in runs])
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [
                {
                    "id": r.id,
                    "user_id": r.user_id,
                    **_profile_fields(user_profiles, r.user_id),
                    "conversation_id": r.conversation_id,
                    "query": r.query[:300],
                    "mode": r.mode,
                    "status": r.status,
                    "iterations": r.iterations,
                    "source_count": r.source_count,
                    "claim_count": r.claim_count,
                    "confidence": r.confidence,
                    "created_at": _fmt(r.created_at),
                    "updated_at": _fmt(r.updated_at),
                }
                for r in runs
            ]
        }
    finally:
        db.close()


@router.get("/errors")
def errors(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    admin: AdminPrincipal = Depends(require_admin),
) -> dict:
    db = SessionLocal()
    try:
        q = db.query(RequestLog).filter(or_(RequestLog.status == "error", RequestLog.error.isnot(None)))
        total = q.count()
        rows = q.order_by(RequestLog.created_at.desc()).offset(offset).limit(limit).all()
        user_profiles = _user_profiles(db, [r.user_id for r in rows])
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [
                {
                    "id": r.id,
                    "user_id": r.user_id,
                    **_profile_fields(user_profiles, r.user_id),
                    "created_at": _fmt(r.created_at),
                    "task_type": r.task_type,
                    "complexity": r.complexity,
                    "selected_model": r.selected_model,
                    "model_used": r.model_used,
                    "error": (r.error or "")[:1000],
                }
                for r in rows
            ]
        }
    finally:
        db.close()


@router.get("/audit")
def audit(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    admin: AdminPrincipal = Depends(require_admin),
) -> dict:
    db = SessionLocal()
    try:
        q = db.query(AdminAuditLog)
        total = q.count()
        rows = q.order_by(AdminAuditLog.created_at.desc()).offset(offset).limit(limit).all()
        user_profiles = _user_profiles(
            db,
            [r.admin_user_id for r in rows] + [r.target_user_id for r in rows if r.target_user_id],
        )
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [
                {
                    "id": r.id,
                    "admin_user_id": r.admin_user_id,
                    **_profile_fields(user_profiles, r.admin_user_id, "admin_"),
                    "action": r.action,
                    "target_user_id": r.target_user_id,
                    **_profile_fields(user_profiles, r.target_user_id or "", "target_"),
                    "details": json.loads(r.details_json or "{}"),
                    "created_at": _fmt(r.created_at),
                }
                for r in rows
            ]
        }
    finally:
        db.close()


@router.get("/registry/agents")
def registry_agents(admin: AdminPrincipal = Depends(require_admin)) -> list[dict]:
    db = SessionLocal()
    try:
        registry = _load_admin_registry(db)
        return [item.model_dump(mode="json") for item in registry.agents.values()]
    finally:
        db.close()


@router.get("/registry/agents/{agent_id}")
def registry_agent(agent_id: str, admin: AdminPrincipal = Depends(require_admin)) -> dict:
    db = SessionLocal()
    try:
        registry = _load_admin_registry(db)
        try:
            return registry.agent(agent_id).model_dump(mode="json")
        except KeyError:
            raise HTTPException(status_code=404, detail="Agent not found") from None
    finally:
        db.close()


@router.get("/registry/prompts")
def registry_prompts(admin: AdminPrincipal = Depends(require_admin)) -> list[dict]:
    db = SessionLocal()
    try:
        registry = _load_admin_registry(db)
        return [item.model_dump(mode="json") for item in registry.prompts.values()]
    finally:
        db.close()


@router.get("/registry/prompts/{prompt_id}")
def registry_prompt(prompt_id: str, admin: AdminPrincipal = Depends(require_admin)) -> dict:
    db = SessionLocal()
    try:
        registry = _load_admin_registry(db)
        try:
            return registry.prompt(prompt_id).model_dump(mode="json")
        except KeyError:
            raise HTTPException(status_code=404, detail="Prompt not found") from None
    finally:
        db.close()


@router.get("/registry/model-policies")
def registry_model_policies(admin: AdminPrincipal = Depends(require_admin)) -> list[dict]:
    db = SessionLocal()
    try:
        registry = _load_admin_registry(db)
        return [item.model_dump(mode="json") for item in registry.model_policies.values()]
    finally:
        db.close()


@router.get("/registry/tools")
def registry_tools(admin: AdminPrincipal = Depends(require_admin)) -> list[dict]:
    db = SessionLocal()
    try:
        registry = _load_admin_registry(db)
        return [item.model_dump(mode="json") for item in registry.tools.values()]
    finally:
        db.close()


@router.get("/registry/guardrails")
def registry_guardrails(admin: AdminPrincipal = Depends(require_admin)) -> list[dict]:
    db = SessionLocal()
    try:
        registry = _load_admin_registry(db)
        return [item.model_dump(mode="json") for item in registry.guardrails.values()]
    finally:
        db.close()


@router.post("/registry/seed")
def registry_seed(admin: AdminPrincipal = Depends(require_admin)) -> dict:
    db = SessionLocal()
    try:
        counts = seed_registry_from_defaults(db)
        _audit(db, admin, "registry.seed", details=counts)
        db.commit()
        return {"seeded": counts}
    finally:
        db.close()


@router.post("/registry/prompts/{prompt_id}/activate")
def registry_prompt_activate(prompt_id: str, admin: AdminPrincipal = Depends(require_admin)) -> dict:
    db = SessionLocal()
    try:
        prompt = db.get(DBPromptTemplate, prompt_id)
        if not prompt:
            raise HTTPException(status_code=404, detail="Prompt not found")
        if prompt.status == "active":
            raise HTTPException(status_code=409, detail="Prompt is already active")
        if prompt.status not in {"draft", "archived"}:
            raise HTTPException(status_code=409, detail="Prompt is not activatable")

        try:
            registry = load_registry_from_db(db)
        except RegistryNotSeeded:
            registry = load_default_registry()
        summary = PromptFixtureRunner(registry).run(prompt_id, live=True)
        if not summary.all_passed:
            raise HTTPException(
                status_code=422,
                detail={"fixture_failures": summary.model_dump()},
            )

        (
            db.query(DBPromptTemplate)
            .filter(DBPromptTemplate.agent_id == prompt.agent_id, DBPromptTemplate.status == "active")
            .update({"status": "archived"}, synchronize_session=False)
        )
        prompt.status = "active"
        prompt.updated_at = _now()
        _audit(db, admin, "registry.prompt.activate", details={"prompt_id": prompt_id})
        db.commit()
        invalidate_registry_cache()
        return {"activated": prompt_id, "fixture_summary": summary.model_dump()}
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.post("/registry/prompts/{prompt_id}/rollback")
def registry_prompt_rollback(prompt_id: str, admin: AdminPrincipal = Depends(require_admin)) -> dict:
    """Roll back the active prompt for the prompt_id's agent.

    The path prompt_id selects the agent via prompt.agent_id. The endpoint
    always archives that agent's currently active prompt and restores its most
    recently archived version, regardless of which prompt version ID is passed.
    """

    db = SessionLocal()
    try:
        prompt = db.get(DBPromptTemplate, prompt_id)
        if not prompt:
            raise HTTPException(status_code=404, detail="Prompt not found")

        current = (
            db.query(DBPromptTemplate)
            .filter(DBPromptTemplate.agent_id == prompt.agent_id, DBPromptTemplate.status == "active")
            .first()
        )
        if current:
            current.status = "archived"
            current.updated_at = _now()

        previous = (
            db.query(DBPromptTemplate)
            .filter(
                DBPromptTemplate.agent_id == prompt.agent_id,
                DBPromptTemplate.status == "archived",
                DBPromptTemplate.id != (current.id if current else ""),
            )
            .order_by(DBPromptTemplate.updated_at.desc(), DBPromptTemplate.created_at.desc())
            .first()
        )
        if not previous:
            raise HTTPException(status_code=409, detail="No previous prompt version exists")

        previous.status = "active"
        previous.updated_at = _now()
        _audit(db, admin, "registry.prompt.rollback", details={"from": current.id if current else None, "to": previous.id})
        db.commit()
        invalidate_registry_cache()
        return {"rolled_back_to": previous.id}
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.get("/system")
def system(admin: AdminPrincipal = Depends(require_admin)) -> dict:
    settings = get_settings()
    return {
        "app_env": settings.app_env,
        "database": "sqlite" if settings.database_url.startswith("sqlite") else "postgres",
        "allowed_origins": settings.origins,
        "default_profile": settings.default_profile,
        "monthly_budget_usd": settings.monthly_budget_usd,
        "planner_model": settings.planner_model,
        "planner_fallback_models": settings.planner_fallback_model_list,
        "clerk_issuer_configured": bool(settings.clerk_issuer),
        "clerk_audience_configured": bool(settings.clerk_audience),
        "admin_user_ids_configured": len(settings.admin_id_set),
        "admin_emails_configured": len(settings.admin_email_set),
    }
