"""User-facing profile endpoints: preferences, workspace priorities,
persistent defaults, personal usage/BI report, and self-service privacy
controls (export / delete).

Distinct from /admin/* -- everything here is scoped to the authenticated
user themselves (CurrentActiveUser), never another user_id. There is no
admin override; a user can only ever see or change their own data.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func

from app.auth import CurrentActiveUser
from app.db.models import (
    Artifact,
    Conversation,
    DocumentTemplate,
    Event,
    SessionLocal,
    ToolCall,
    Turn,
    User,
    Workspace,
)
from app.services.document_templates import list_document_templates, template_path_for_row

router = APIRouter(prefix="/profile", tags=["profile"])
logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _loads(value: str | None, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _fmt(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * pct)))
    return int(ordered[idx])


def _start_for_range(range_value: str) -> datetime | None:
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=timezone.utc)
    if range_value == "1d":
        return today_start
    if range_value == "7d":
        return today_start - timedelta(days=6)
    if range_value == "30d":
        return today_start - timedelta(days=29)
    if range_value == "90d":
        return today_start - timedelta(days=89)
    return None


class PreferencesUpdate(BaseModel):
    preferences: list[str] = Field(default_factory=list)


class WorkspacePrioritiesUpdate(BaseModel):
    priorities: list[str] = Field(default_factory=list)


class SettingsUpdate(BaseModel):
    quality_mode: Literal["draft", "standard", "executive"] | None = None
    output_format: Literal["chat", "markdown", "docx", "pptx"] | None = None
    research_level: Literal["auto", "easy", "regular", "deep"] | None = None
    default_template_id: str | None = Field(default=None, max_length=128)


class PrivacyDeleteConfirm(BaseModel):
    confirm: bool = False


def _get_or_create_user_row(db, user_id: str) -> User:
    user = db.query(User).filter(User.clerk_id == user_id).first()
    if user is None:
        user = User(clerk_id=user_id, created_at=_now())
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


@router.get("/me")
def get_profile(user_id: str = CurrentActiveUser) -> dict:
    db = SessionLocal()
    try:
        user = _get_or_create_user_row(db, user_id)
        profile = _loads(user.profile_json, {})
        settings = _loads(user.settings_json, {})
        preferences = profile.get("preferences") if isinstance(profile, dict) else None
        return {
            "user_id": user_id,
            "email": user.email,
            "name": user.name,
            "preferences": preferences if isinstance(preferences, list) else [],
            "preferences_updated_at": _fmt(user.profile_consolidated_at),
            "settings": settings if isinstance(settings, dict) else {},
        }
    finally:
        db.close()


@router.patch("/preferences")
def update_preferences(body: PreferencesUpdate, user_id: str = CurrentActiveUser) -> dict:
    """Replace the user's stored preferences -- e.g. to remove an item the
    consolidator inferred incorrectly. The consolidator may add to this list
    again on its next run if the same signal is still present in recent
    turns, but a one-off bad inference won't survive a deliberate removal
    unless it keeps recurring."""
    db = SessionLocal()
    try:
        user = _get_or_create_user_row(db, user_id)
        cleaned = [str(item).strip()[:200] for item in body.preferences if str(item).strip()][:10]
        user.profile_json = json.dumps({"preferences": cleaned})
        db.commit()
        return {"preferences": cleaned}
    finally:
        db.close()


@router.get("/settings")
def get_user_settings(user_id: str = CurrentActiveUser) -> dict:
    db = SessionLocal()
    try:
        user = _get_or_create_user_row(db, user_id)
        settings = _loads(user.settings_json, {})
        return settings if isinstance(settings, dict) else {}
    finally:
        db.close()


@router.patch("/settings")
def update_user_settings(body: SettingsUpdate, user_id: str = CurrentActiveUser) -> dict:
    db = SessionLocal()
    try:
        user = _get_or_create_user_row(db, user_id)
        existing = _loads(user.settings_json, {})
        if not isinstance(existing, dict):
            existing = {}
        updates = body.model_dump(exclude_none=True)
        existing.update(updates)
        user.settings_json = json.dumps(existing)
        db.commit()
        return existing
    finally:
        db.close()


@router.get("/workspaces")
def list_workspace_profiles(user_id: str = CurrentActiveUser) -> dict:
    db = SessionLocal()
    try:
        workspaces = (
            db.query(Workspace)
            .filter(Workspace.user_id == user_id)
            .order_by(Workspace.updated_at.desc())
            .all()
        )
        if not workspaces:
            return {"workspaces": []}
        workspace_ids = [w.id for w in workspaces]
        conversation_counts = dict(
            db.query(Conversation.workspace_id, func.count(Conversation.id))
            .filter(Conversation.workspace_id.in_(workspace_ids))
            .group_by(Conversation.workspace_id)
            .all()
        )
        turn_stats_map = {
            workspace_id: {"turn_count": count, "total_cost_usd": float(cost or 0), "last_active_at": last}
            for workspace_id, count, cost, last in (
                db.query(
                    Conversation.workspace_id,
                    func.count(Turn.id),
                    func.coalesce(func.sum(Turn.cost_usd), 0.0),
                    func.max(Turn.created_at),
                )
                .join(Turn, Turn.conversation_id == Conversation.id)
                .filter(Conversation.workspace_id.in_(workspace_ids))
                .group_by(Conversation.workspace_id)
                .all()
            )
        }
        return {
            "workspaces": [
                {
                    "id": w.id,
                    "name": w.name,
                    "priorities": _loads(w.priorities_json, []) if isinstance(_loads(w.priorities_json, []), list) else [],
                    "priorities_updated_at": _fmt(w.priorities_consolidated_at),
                    "conversation_count": conversation_counts.get(w.id, 0),
                    "turn_count": turn_stats_map.get(w.id, {}).get("turn_count", 0),
                    "total_cost_usd": round(turn_stats_map.get(w.id, {}).get("total_cost_usd", 0.0), 6),
                    "last_active_at": _fmt(turn_stats_map.get(w.id, {}).get("last_active_at")),
                    "created_at": _fmt(w.created_at),
                }
                for w in workspaces
            ]
        }
    finally:
        db.close()


@router.patch("/workspaces/{workspace_id}/priorities")
def update_workspace_priorities(
    workspace_id: str,
    body: WorkspacePrioritiesUpdate,
    user_id: str = CurrentActiveUser,
) -> dict:
    db = SessionLocal()
    try:
        workspace = db.query(Workspace).filter(Workspace.id == workspace_id, Workspace.user_id == user_id).first()
        if workspace is None:
            raise HTTPException(status_code=404, detail="Workspace not found")
        cleaned = [str(item).strip()[:200] for item in body.priorities if str(item).strip()][:8]
        workspace.priorities_json = json.dumps(cleaned)
        db.commit()
        return {"workspace_id": workspace_id, "priorities": cleaned}
    finally:
        db.close()


@router.get("/usage")
def get_usage(
    range: str = Query(default="30d", pattern="^(1d|7d|30d|90d|all)$"),
    user_id: str = CurrentActiveUser,
) -> dict:
    """Personal BI report: cost/spend trend, activity/engagement, and
    model+route performance, all scoped to this user's own turns."""
    db = SessionLocal()
    try:
        start = _start_for_range(range)
        base = db.query(Turn).filter(Turn.user_id == user_id)
        if start:
            base = base.filter(Turn.created_at >= start)

        rows = base.all()
        total_requests = len(rows)
        failed = [r for r in rows if r.status == "failed"]
        completed = [r for r in rows if r.status == "completed"]
        total_cost = sum(float(r.cost_usd or 0) for r in rows)
        latencies = [int(r.latency_ms or 0) for r in completed if r.latency_ms]
        active_days = len({r.created_at.date() for r in rows if r.created_at})

        by_day: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "requests": 0})
        for r in rows:
            if not r.created_at:
                continue
            key = r.created_at.date().isoformat()
            by_day[key]["cost"] += float(r.cost_usd or 0)
            by_day[key]["requests"] += 1

        by_route: dict[str, int] = defaultdict(int)
        for r in rows:
            if r.route:
                by_route[r.route] += 1

        by_model: dict[str, dict] = defaultdict(lambda: {"requests": 0, "cost": 0.0, "latencies": [], "failures": 0})
        for r in rows:
            model = r.model_used or "unknown"
            bucket = by_model[model]
            bucket["requests"] += 1
            bucket["cost"] += float(r.cost_usd or 0)
            if r.status == "failed":
                bucket["failures"] += 1
            elif r.latency_ms:
                bucket["latencies"].append(int(r.latency_ms))

        return {
            "range": range,
            "summary": {
                "total_cost": round(total_cost, 6),
                "requests": total_requests,
                "failed_requests": len(failed),
                "failure_rate": round(len(failed) / total_requests, 4) if total_requests else 0.0,
                "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0,
                "p95_latency_ms": _percentile(latencies, 0.95),
                "active_days": active_days,
            },
            "cost_by_day": [
                {"date": d, "cost": round(v["cost"], 6), "requests": v["requests"]}
                for d, v in sorted(by_day.items())
            ],
            "route_distribution": sorted(
                [{"route": route, "count": count} for route, count in by_route.items()],
                key=lambda x: -x["count"],
            ),
            "model_performance": sorted(
                [
                    {
                        "model": model,
                        "requests": v["requests"],
                        "cost": round(v["cost"], 6),
                        "avg_latency_ms": round(sum(v["latencies"]) / len(v["latencies"]), 1) if v["latencies"] else 0,
                        "p95_latency_ms": _percentile(v["latencies"], 0.95),
                        "failure_count": v["failures"],
                    }
                    for model, v in by_model.items()
                ],
                key=lambda x: -x["requests"],
            ),
        }
    finally:
        db.close()


@router.get("/export")
def export_my_data(user_id: str = CurrentActiveUser) -> dict:
    """A snapshot of the user's own data for download -- preferences,
    workspaces (with their consolidated priorities), turns (objective,
    answer, and basic telemetry; not internal tool-call/event detail), and
    template metadata (not the binary file contents)."""
    db = SessionLocal()
    try:
        user = _get_or_create_user_row(db, user_id)
        profile = _loads(user.profile_json, {})
        settings = _loads(user.settings_json, {})
        workspaces = db.query(Workspace).filter(Workspace.user_id == user_id).all()
        turns = (
            db.query(Turn)
            .filter(Turn.user_id == user_id)
            .order_by(Turn.created_at.asc())
            .all()
        )
        templates = list_document_templates("presentation", db=db, user_id=user_id)

        return {
            "exported_at": _fmt(_now()),
            "user": {"user_id": user_id, "email": user.email, "name": user.name},
            "preferences": profile.get("preferences") if isinstance(profile, dict) else [],
            "settings": settings if isinstance(settings, dict) else {},
            "workspaces": [
                {
                    "id": w.id,
                    "name": w.name,
                    "priorities": _loads(w.priorities_json, []),
                    "created_at": _fmt(w.created_at),
                }
                for w in workspaces
            ],
            "turns": [
                {
                    "id": t.id,
                    "conversation_id": t.conversation_id,
                    "objective": t.objective,
                    "answer": t.answer,
                    "route": t.route,
                    "status": t.status,
                    "model_used": t.model_used,
                    "cost_usd": t.cost_usd,
                    "latency_ms": t.latency_ms,
                    "created_at": _fmt(t.created_at),
                }
                for t in turns
            ],
            "document_templates": templates,
        }
    finally:
        db.close()


@router.post("/privacy-delete")
def delete_my_data(body: PrivacyDeleteConfirm, user_id: str = CurrentActiveUser) -> dict:
    """Self-service "delete my data": every workspace/conversation/turn/
    artifact, consolidated preferences, and uploaded document templates for
    the authenticated user. Irreversible -- requires an explicit confirm
    flag rather than acting on a bare POST."""
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Set confirm=true to proceed. This cannot be undone.")
    db = SessionLocal()
    try:
        deleted: dict[str, int] = {}

        turn_ids = [tid for (tid,) in db.query(Turn.id).filter(Turn.user_id == user_id).all()]
        if turn_ids:
            db.query(Event).filter(Event.turn_id.in_(turn_ids)).delete(synchronize_session=False)
            db.query(ToolCall).filter(ToolCall.turn_id.in_(turn_ids)).delete(synchronize_session=False)
            db.query(Artifact).filter(Artifact.turn_id.in_(turn_ids)).delete(synchronize_session=False)
        deleted["turns"] = db.query(Turn).filter(Turn.user_id == user_id).delete(synchronize_session=False)
        # Workspace deletion cascades to Conversation rows (ondelete="CASCADE")
        # and removes Workspace.priorities_json along with the row.
        deleted["workspaces"] = db.query(Workspace).filter(Workspace.user_id == user_id).delete(synchronize_session=False)

        template_rows = db.query(DocumentTemplate).filter(DocumentTemplate.user_id == user_id).all()
        for row in template_rows:
            try:
                path = template_path_for_row(row)
                if path.exists():
                    path.unlink()
            except Exception:
                logger.warning("Failed to delete template file for %s", row.public_id, exc_info=True)
            db.delete(row)
        deleted["document_templates"] = len(template_rows)

        user = db.query(User).filter(User.clerk_id == user_id).first()
        if user is not None:
            user.profile_json = "{}"
            user.profile_consolidated_at = None
            user.settings_json = "{}"
            deleted["consolidated_preferences"] = 1
            deleted["settings"] = 1

        db.commit()
        return {"status": "ok", "deleted": deleted}
    finally:
        db.close()
