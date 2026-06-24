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
    Artifact,
    Event,
    ToolCall,
    Turn,
    Workspace,
    DocumentTemplate,
    SessionLocal,
    User,
    UserAdminControl,
    get_global_monthly_spend,
    get_monthly_spend,
)
from app.services.agent import model_policy
from app.services.agent import persistence
from app.services.agent import prompt_library
from app.services.agent import routing_policy
from app.services.document_templates import template_path_for_row
from app.services.llm_gateway import (
    PROVIDER_TEST_MODELS,
    get_circuit_status,
    provider_for_model,
    test_provider_connection,
)
from app.services.rate_limit import check_rate_limit
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


class PrivacyDeleteRequest(BaseModel):
    document_templates: bool = False
    agent_data: bool = False
    confirm_user_id: str | None = None


class ProviderTestRequest(BaseModel):
    provider: str


class PromptUpsertRequest(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    system_prompt: str = Field(min_length=1)
    developer_prompt: str | None = None
    variables: list[str] = Field(default_factory=list)
    profile: str | None = Field(default=None, max_length=64)
    version: str = Field(default="1.0.0", max_length=32)
    status: Literal["draft", "active", "archived"] = "draft"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelPolicyUpdate(BaseModel):
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
    user = db.query(User).filter(User.clerk_id == user_id).first()
    has_preferences = bool(user and user.profile_json not in ("", "{}", None))
    return {
        "document_templates": db.query(DocumentTemplate).filter(DocumentTemplate.user_id == user_id).count(),
        "workspaces": db.query(Workspace).filter(Workspace.user_id == user_id).count(),
        "turns": db.query(Turn).filter(Turn.user_id == user_id).count(),
        "consolidated_preferences": 1 if has_preferences else 0,
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
    """An env-allowlisted admin is always 'admin' regardless of the DB role.

    Intentionally uses is_admin_user() (env-only) here, not is_admin_user_db(),
    because we specifically want to know if the user is protected by the static
    env allowlist — the DB role is passed in separately as `db_role`.
    """
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
        db.query(Workspace.user_id).distinct().all(),
        db.query(Turn.user_id).distinct().all(),
        db.query(UserAdminControl.user_id).distinct().all(),
        db.query(User.clerk_id).distinct().all(),
    ]
    for rows in sources:
        for (user_id,) in rows:
            if user_id:
                user_ids.add(user_id)
    return user_ids


@router.get("/me")
def admin_me(admin: AdminPrincipal = Depends(require_admin)) -> dict:
    return {"is_admin": True, "user_id": admin.user_id, "email": admin.email}


@router.get("/overview")
def overview(admin: AdminPrincipal = Depends(require_admin)) -> dict:
    db = SessionLocal()
    try:
        start = _start_for_range("1d")
        turns_today = db.query(Turn).filter(Turn.created_at >= start)
        requests_today = turns_today.count()
        spend_today = turns_today.with_entities(func.coalesce(func.sum(Turn.cost_usd), 0.0)).scalar() or 0.0
        errors_today = db.query(Turn).filter(
            Turn.status == "failed",
            Turn.created_at >= start,
        ).count()
        running_turns = db.query(Turn).filter(Turn.status.in_(["pending", "running"])).count()
        return {
            "users": len(_all_known_user_ids(db)),
            "requests_today": int(requests_today),
            "spend_today": round(float(spend_today), 6),
            "errors_today": errors_today,
            "running_research_runs": running_turns,
            "total_conversations": db.query(Workspace).count(),
            "total_memories": 0,
            "total_writing_samples": 0,
            "total_research_runs": db.query(Turn).filter(Turn.route == "research").count(),
        }
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

        workspace_counts = dict(db.query(Workspace.user_id, func.count(Workspace.id)).filter(Workspace.user_id.in_(page)).group_by(Workspace.user_id).all())
        turn_counts = dict(db.query(Turn.user_id, func.count(Turn.id)).filter(Turn.user_id.in_(page)).group_by(Turn.user_id).all())
        turn_costs = dict(db.query(Turn.user_id, func.coalesce(func.sum(Turn.cost_usd), 0.0)).filter(Turn.user_id.in_(page)).group_by(Turn.user_id).all())
        month_start = _today_start().replace(day=1)
        month_turn_costs = dict(
            db.query(Turn.user_id, func.coalesce(func.sum(Turn.cost_usd), 0.0))
            .filter(Turn.user_id.in_(page), Turn.created_at >= month_start)
            .group_by(Turn.user_id)
            .all()
        )
        turn_seen = dict(db.query(Turn.user_id, func.max(Turn.created_at)).filter(Turn.user_id.in_(page)).group_by(Turn.user_id).all())

        rows = []
        for user_id in page:
            control = controls.get(user_id)
            rows.append({
                "user_id": user_id,
                "email": emails.get(user_id),
                "name": names.get(user_id),
                "status": control.status if control else "active",
                "role": _effective_role(user_id, control.role if control else None, emails.get(user_id)),
                "monthly_budget_usd": control.monthly_budget_usd if control else None,
                "month_spend": round(float(month_turn_costs.get(user_id, 0) or 0), 6),
                "conversation_count": workspace_counts.get(user_id, 0),
                "request_count": turn_counts.get(user_id, 0),
                "total_spend": round(float(turn_costs.get(user_id, 0) or 0), 6),
                "memory_count": 0,
                "writing_sample_count": 0,
                "research_run_count": 0,
                "last_seen_at": _fmt(turn_seen.get(user_id)),
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
        workspaces = (
            db.query(Workspace)
            .filter(Workspace.user_id == user_id)
            .order_by(Workspace.updated_at.desc())
            .limit(10)
            .all()
        )
        recent_turns = (
            db.query(Turn)
            .filter(Turn.user_id == user_id)
            .order_by(Turn.created_at.desc())
            .limit(10)
            .all()
        )
        recent_errors = [t for t in recent_turns if t.status == "failed"][:10]
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
                "conversations": db.query(Workspace).filter(Workspace.user_id == user_id).count(),
                "messages": db.query(Turn).filter(Turn.user_id == user_id).count(),
                "memories": 0,
                "user_profiles": 0,
                "writing_samples": 0,
                "twin_profiles": 0,
                "research_runs": db.query(Turn).filter(Turn.user_id == user_id, Turn.route == "research").count(),
            },
            "recent_conversations": [
                {
                    "id": w.id,
                    "title": w.name,
                    "profile": None,
                    "message_count": db.query(Turn).filter(Turn.conversation_id == w.id).count(),
                    "updated_at": _fmt(w.updated_at),
                }
                for w in workspaces
            ],
            "recent_research_runs": [],
            "recent_errors": [
                {
                    "id": t.id,
                    "created_at": _fmt(t.created_at),
                    "task_type": t.route,
                    "selected_model": t.model_used,
                    "error": (t.error_message or "")[:500],
                }
                for t in recent_errors
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
        # Intentionally env-only: we're checking if this specific user is protected
        # by the static ADMIN_USER_IDS/ADMIN_EMAILS allowlist, not whether they're
        # an admin generally (which is_admin_user_db would answer).
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
        if body.agent_data:
            turn_ids = [tid for (tid,) in db.query(Turn.id).filter(Turn.user_id == user_id).all()]
            if turn_ids:
                db.query(Event).filter(Event.turn_id.in_(turn_ids)).delete(synchronize_session=False)
                db.query(ToolCall).filter(ToolCall.turn_id.in_(turn_ids)).delete(synchronize_session=False)
                db.query(Artifact).filter(Artifact.turn_id.in_(turn_ids)).delete(synchronize_session=False)
            deleted["turns"] = db.query(Turn).filter(Turn.user_id == user_id).delete(synchronize_session=False)
            # Per-workspace consolidated priorities (Workspace.priorities_json)
            # are deleted along with the workspace rows themselves below.
            deleted["workspaces"] = db.query(Workspace).filter(Workspace.user_id == user_id).delete(synchronize_session=False)
            # The user-level consolidated preferences are distilled from this
            # same turn history but live on User, not Workspace, so they
            # need an explicit clear -- otherwise a "delete my data" request
            # leaves behind a summary derived from the deleted data.
            target_user = db.query(User).filter(User.clerk_id == user_id).first()
            if target_user is not None and target_user.profile_json not in ("", "{}", None):
                target_user.profile_json = "{}"
                target_user.profile_consolidated_at = None
                deleted["consolidated_preferences"] = 1
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
        base = db.query(Turn)
        if start:
            base = base.filter(Turn.created_at >= start)

        by_day: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "requests": 0})
        for day, cost, count in (
            base.with_entities(
                func.date(Turn.created_at),
                func.coalesce(func.sum(Turn.cost_usd), 0.0),
                func.count(Turn.id),
            )
            .group_by(func.date(Turn.created_at))
            .all()
        ):
            by_day[str(day)]["cost"] += float(cost or 0)
            by_day[str(day)]["requests"] += int(count or 0)

        by_user: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "requests": 0})
        for user_id, cost, count in (
            base.with_entities(
                Turn.user_id,
                func.coalesce(func.sum(Turn.cost_usd), 0.0),
                func.count(Turn.id),
            )
            .group_by(Turn.user_id)
            .all()
        ):
            if user_id:
                by_user[user_id]["cost"] += float(cost or 0)
                by_user[user_id]["requests"] += int(count or 0)

        user_profiles = _user_profiles(db, by_user.keys())

        by_model: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "requests": 0, "latency_total": 0.0, "latency_count": 0})
        for model, cost, count, avg_latency in (
            base.with_entities(
                Turn.model_used,
                func.coalesce(func.sum(Turn.cost_usd), 0.0),
                func.count(Turn.id),
                func.avg(Turn.latency_ms),
            )
            .filter(Turn.model_used.isnot(None), Turn.model_used != "")
            .group_by(Turn.model_used)
            .all()
        ):
            if model:
                by_model[model]["cost"] += float(cost or 0)
                by_model[model]["requests"] += int(count or 0)
                if avg_latency is not None:
                    by_model[model]["latency_total"] += float(avg_latency) * int(count or 0)
                    by_model[model]["latency_count"] += int(count or 0)

        by_task: dict[str, int] = defaultdict(int)
        for task, count in base.with_entities(Turn.route, func.count(Turn.id)).filter(Turn.route.isnot(None)).group_by(Turn.route).all():
            if task:
                by_task[task] += int(count or 0)

        summary_row = base.with_entities(
            func.coalesce(func.sum(Turn.cost_usd), 0.0),
            func.count(Turn.id),
        ).one()
        total_cost = float(summary_row[0] or 0)
        total_requests = int(summary_row[1] or 0)
        return {
            "range": range,
            "summary": {
                "total_cost": round(total_cost, 6),
                "requests": total_requests,
                "tokens": 0,
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
            db.query(Turn)
            .filter(Turn.status == "failed", Turn.created_at >= _now().replace(tzinfo=None) - timedelta(days=7))
            .all()
        )
        provider_errors: dict[str, int] = defaultdict(int)
        for err in recent_errors:
            provider_errors[provider_for_model(err.model_used)] += 1
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


@router.get("/errors")
def errors(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    admin: AdminPrincipal = Depends(require_admin),
) -> dict:
    db = SessionLocal()
    try:
        q = db.query(Turn).filter(Turn.status == "failed")
        total = q.count()
        rows = q.order_by(Turn.created_at.desc()).offset(offset).limit(limit).all()
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
                    "task_type": r.route,
                    "complexity": None,
                    "selected_model": r.model_used,
                    "model_used": r.model_used,
                    "error": (r.error_message or "")[:1000],
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


@router.get("/workspaces")
def admin_workspaces_view(
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
                        for workspace in persistence.list_workspaces(user_id, ensure_default=False)
                    ],
                }
            ]
        }

    db = SessionLocal()
    try:
        user_ids = [
            row[0]
            for row in (
                db.query(Workspace.user_id)
                .distinct()
                .order_by(Workspace.user_id.asc())
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
                    for workspace in persistence.list_workspaces(uid, ensure_default=False)
                ],
            }
            for uid in user_ids
        ]
    }


@router.get("/prompts")
def admin_prompts_view(admin: AdminPrincipal = Depends(require_admin)) -> dict:
    return {"prompts": [item.model_dump(mode="json") for item in prompt_library.list_prompts()]}


@router.post("/prompts/seed")
def admin_prompt_seed(admin: AdminPrincipal = Depends(require_admin)) -> dict:
    counts = prompt_library.seed_defaults()
    db = SessionLocal()
    try:
        _audit(db, admin, "prompt.seed", details=counts)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return {"seeded": counts}


@router.post("/prompts")
def admin_prompt_upsert(
    body: PromptUpsertRequest,
    admin: AdminPrincipal = Depends(require_admin),
) -> dict:
    spec = prompt_library.PromptSpec(**body.model_dump())
    saved = prompt_library.upsert_prompt(spec)
    db = SessionLocal()
    try:
        _audit(
            db,
            admin,
            "prompt.upsert",
            details={"prompt_id": saved.id, "agent_id": saved.agent_id, "status": saved.status},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return {"prompt": saved.model_dump(mode="json")}


@router.post("/prompts/{prompt_id}/activate")
def admin_prompt_activate(prompt_id: str, admin: AdminPrincipal = Depends(require_admin)) -> dict:
    activated = prompt_library.activate_prompt(prompt_id)
    if activated is None:
        raise HTTPException(status_code=404, detail="Agent v3 prompt not found")
    db = SessionLocal()
    try:
        _audit(
            db,
            admin,
            "prompt.activate",
            details={"prompt_id": activated.id, "agent_id": activated.agent_id, "profile": activated.profile},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return {"activated": activated.id, "prompt": activated.model_dump(mode="json")}


@router.post("/prompts/{prompt_id}/rollback")
def admin_prompt_rollback(prompt_id: str, admin: AdminPrincipal = Depends(require_admin)) -> dict:
    rolled_back = prompt_library.rollback_prompt(prompt_id)
    if rolled_back is None:
        raise HTTPException(status_code=409, detail="No previous Agent v3 prompt version exists")
    db = SessionLocal()
    try:
        _audit(
            db,
            admin,
            "prompt.rollback",
            details={"prompt_id": prompt_id, "rolled_back_to": rolled_back.id},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return {"rolled_back_to": rolled_back.id, "prompt": rolled_back.model_dump(mode="json")}


@router.get("/model-policy")
def admin_model_policy_view(admin: AdminPrincipal = Depends(require_admin)) -> dict:
    """Effective Agent v3 model assignment: defaults merged with whatever an
    admin has overridden. This is the single source of truth — there is no
    .env fallback for model identity anymore."""
    db = SessionLocal()
    try:
        policy = model_policy.get_model_policy(db)
        return {
            "roles": policy["roles"],
            "fallback_models": policy["fallback_models"],
            "defaults": {
                "roles": model_policy.DEFAULT_MODEL_POLICY,
                "fallback_models": model_policy.DEFAULT_FALLBACK_MODELS,
            },
            "available_roles": list(model_policy.MODEL_ROLES),
        }
    finally:
        db.close()


@router.patch("/model-policy")
def admin_model_policy_update(
    body: ModelPolicyUpdate,
    admin: AdminPrincipal = Depends(require_admin),
) -> dict:
    db = SessionLocal()
    try:
        try:
            policy = model_policy.set_model_policy(
                db,
                role_overrides=body.roles or None,
                fallback_models=body.fallback_models,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        _audit(db, admin, "model_policy.update", details=body.model_dump())
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


@router.post("/model-policy/reset")
def admin_model_policy_reset(admin: AdminPrincipal = Depends(require_admin)) -> dict:
    db = SessionLocal()
    try:
        policy = model_policy.reset_model_policy(db)
        _audit(db, admin, "model_policy.reset")
        db.commit()
        return {"roles": policy["roles"], "fallback_models": policy["fallback_models"]}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.get("/routing-signals")
def admin_routing_signals_view(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    admin: AdminPrincipal = Depends(require_admin),
) -> dict:
    return routing_policy.list_signal_candidates(status=status, limit=limit)


@router.post("/routing-signals/{candidate_id}/approve")
def admin_routing_signal_approve(candidate_id: str, admin: AdminPrincipal = Depends(require_admin)) -> dict:
    candidate = routing_policy.set_signal_candidate_status(candidate_id, "approved")
    if candidate is None:
        raise HTTPException(status_code=404, detail="Agent v3 routing signal candidate not found")
    db = SessionLocal()
    try:
        _audit(db, admin, "routing_signal.approve", details={"candidate_id": candidate_id})
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return {"candidate": candidate}


@router.post("/routing-signals/{candidate_id}/reject")
def admin_routing_signal_reject(candidate_id: str, admin: AdminPrincipal = Depends(require_admin)) -> dict:
    candidate = routing_policy.set_signal_candidate_status(candidate_id, "rejected")
    if candidate is None:
        raise HTTPException(status_code=404, detail="Agent v3 routing signal candidate not found")
    db = SessionLocal()
    try:
        _audit(db, admin, "routing_signal.reject", details={"candidate_id": candidate_id})
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return {"candidate": candidate}


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
