"""Admin operational endpoints.

The frontend hides admin UI for non-admins, but these endpoints are the real
security boundary. Configure admins with ADMIN_USER_IDS and/or ADMIN_EMAILS.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_

from app.auth import get_claim_email, get_current_user_payload, is_admin_user, is_admin_user_db
from app.config import get_settings
from app.services.clerk import fetch_clerk_user
from app.db.models import (
    AdminAuditLog,
    Conversation,
    ConversationMessage,
    ConversationTurn,
    DocumentTemplate,
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
from app.services.document_templates import template_path_for_row
from app.services.llm_gateway import (
    PROVIDER_TEST_MODELS,
    get_circuit_status,
    provider_for_model,
    test_provider_connection,
)
from app.services.rate_limit import check_rate_limit
from app.services.router import choose_route, load_policy
from app.services.web_context import test_brave_connection, test_tavily_connection


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


def _turn_row(turn: ConversationTurn, conv_public_id: str | None = None) -> dict:
    try:
        progress = json.loads(turn.progress_json or "[]")
    except (TypeError, ValueError):
        progress = []
    try:
        lifecycle = json.loads(turn.lifecycle_json or "[]")
    except (TypeError, ValueError):
        lifecycle = []
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
                    "name": "Tavily", "key": "TAVILY_API_KEY",
                    "configured": bool(settings.tavily_api_key),
                    "key_hint": _key_hint(settings.tavily_api_key),
                    "testable": True,
                },
                {
                    "name": "Brave", "key": "BRAVE_API_KEY",
                    "configured": bool(settings.brave_api_key),
                    "key_hint": _key_hint(settings.brave_api_key),
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
    elif provider == "Tavily":
        result = test_tavily_connection()
    elif provider == "Brave":
        result = test_brave_connection()
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
