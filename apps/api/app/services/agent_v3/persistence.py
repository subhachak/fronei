from __future__ import annotations

import base64
import hashlib
import json
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db.models import (
    AgentV3Artifact,
    AgentV3Conversation,
    AgentV3Event,
    AgentV3ToolCall,
    AgentV3Turn,
    AgentV3Workspace,
    SessionLocal,
    User,
)
from app.services.agent_v3.models import (
    AgentV3ConversationSummary,
    AgentV3Result,
    AgentV3WorkspaceSummary,
    Artifact,
    Goal,
    ProgressEvent,
    Source,
    ToolCall,
    new_id,
)

logger = logging.getLogger(__name__)

_CONTEXT_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent-v3-context")
_PENDING_CONTEXT_FUTURES: set[Future] = set()


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str)


def _loads(value: str | None, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _title_from_message(message: str) -> str:
    cleaned = " ".join((message or "").replace("\n", " ").split())
    if not cleaned:
        return "New conversation"
    return cleaned[:90].strip(" .") or "New conversation"


def _compact_text(value: str, limit: int) -> str:
    cleaned = " ".join((value or "").split())
    return cleaned[:limit].rstrip()


def _user_profile_context(db, user_id: str) -> dict:
    user = db.query(User).filter(User.clerk_id == user_id).first()
    profile = {"user_id": user_id}
    if user:
        if user.name:
            profile["name"] = user.name
        if user.email:
            profile["email"] = user.email
    return profile


def _initial_workspace_context(db, user_id: str, workspace: AgentV3Workspace) -> dict:
    return {
        "version": 1,
        "max_chars": 4000,
        "user_profile": _user_profile_context(db, user_id),
        "workspace": {"id": workspace.id, "name": workspace.name},
        "conversation": {},
        "running_summary": "",
        "key_facts": [],
        "recent_turns": [],
    }


def _initial_context(db, user_id: str, workspace: AgentV3Workspace, conversation: AgentV3Conversation | None = None) -> dict:
    return {
        "version": 1,
        "max_chars": 6000,
        "user_profile": _user_profile_context(db, user_id),
        "workspace": {"id": workspace.id, "name": workspace.name},
        "conversation": {"id": conversation.id, "title": conversation.title} if conversation else {},
        "running_summary": "",
        "key_facts": [],
        "recent_turns": [],
    }


def _normalize_context(ctx: dict, *, max_chars: int = 6000) -> dict:
    ctx = dict(ctx or {})
    ctx["version"] = int(ctx.get("version") or 1)
    ctx["max_chars"] = max(1200, min(12000, int(ctx.get("max_chars") or max_chars)))
    ctx["user_profile"] = ctx.get("user_profile") if isinstance(ctx.get("user_profile"), dict) else {}
    ctx["workspace"] = ctx.get("workspace") if isinstance(ctx.get("workspace"), dict) else {}
    ctx["conversation"] = ctx.get("conversation") if isinstance(ctx.get("conversation"), dict) else {}
    ctx["running_summary"] = _compact_text(str(ctx.get("running_summary") or ""), 2200)
    facts = ctx.get("key_facts") if isinstance(ctx.get("key_facts"), list) else []
    ctx["key_facts"] = [_compact_text(str(item), 220) for item in facts if str(item).strip()][:10]
    turns = ctx.get("recent_turns") if isinstance(ctx.get("recent_turns"), list) else []
    normalized_turns = []
    for turn in turns[-8:]:
        if not isinstance(turn, dict):
            continue
        normalized_turns.append(
            {
                "turn_id": str(turn.get("turn_id") or ""),
                "route": str(turn.get("route") or ""),
                "conversation": _compact_text(str(turn.get("conversation") or ""), 180),
                "user": _compact_text(str(turn.get("user") or ""), 360),
                "assistant": _compact_text(str(turn.get("assistant") or ""), 520),
                "artifacts": turn.get("artifacts") if isinstance(turn.get("artifacts"), list) else [],
                "source_count": int(turn.get("source_count") or 0),
            }
        )
    ctx["recent_turns"] = normalized_turns
    return _trim_context(ctx)


def _render_context(ctx: dict, *, max_chars: int = 6000, label: str = "Conversation context") -> str:
    ctx = _normalize_context(ctx, max_chars=max_chars)
    lines = [f"{label}:"]
    profile = ctx.get("user_profile") or {}
    if profile:
        display = profile.get("name") or profile.get("email") or profile.get("user_id")
        lines.append(f"- User: {display}")
    workspace = ctx.get("workspace") or {}
    if workspace.get("name"):
        lines.append(f"- Workspace: {workspace['name']}")
    conversation = ctx.get("conversation") or {}
    if conversation.get("title"):
        lines.append(f"- Conversation: {conversation['title']}")
    if ctx.get("running_summary"):
        lines.append(f"- Running summary: {ctx['running_summary']}")
    if ctx.get("key_facts"):
        lines.append("- Key facts:")
        lines.extend(f"  - {fact}" for fact in ctx["key_facts"])
    if ctx.get("recent_turns"):
        lines.append("- Recent turns:")
        for turn in ctx["recent_turns"][-6:]:
            if turn.get("conversation"):
                lines.append(f"  - Conversation: {turn['conversation']}")
            lines.append(f"  - User: {turn['user']}")
            lines.append(f"    Fronei: {turn['assistant']}")
            if turn.get("artifacts"):
                lines.append(f"    Artifacts: {', '.join(str(item) for item in turn['artifacts'][:3])}")
    rendered = "\n".join(lines)
    return rendered[-max_chars:]


def _trim_context(ctx: dict) -> dict:
    max_chars = int(ctx.get("max_chars") or 6000)
    while len(_render_context_no_trim(ctx)) > max_chars and ctx.get("recent_turns"):
        ctx["recent_turns"] = ctx["recent_turns"][1:]
    if len(_render_context_no_trim(ctx)) > max_chars and ctx.get("running_summary"):
        ctx["running_summary"] = _compact_text(ctx["running_summary"], max(500, max_chars // 3))
    return ctx


def _render_context_no_trim(ctx: dict) -> str:
    clone = dict(ctx or {})
    lines = [
        str(clone.get("running_summary") or ""),
        json.dumps(clone.get("key_facts") or [], default=str),
        json.dumps(clone.get("recent_turns") or [], default=str),
    ]
    return "\n".join(lines)


def _update_context_with_result(ctx: dict, result: AgentV3Result, *, conversation_title: str | None = None) -> dict:
    ctx = _normalize_context(ctx)
    artifact_names = [artifact.filename for artifact in result.artifacts]
    turn_entry = {
        "turn_id": result.turn_id,
        "route": result.route,
        "user": result.goal.objective,
        "assistant": result.answer,
        "artifacts": artifact_names,
        "source_count": len(result.sources),
    }
    if conversation_title:
        turn_entry["conversation"] = conversation_title
    ctx["recent_turns"] = [*ctx.get("recent_turns", []), turn_entry][-8:]
    summary_parts = [ctx.get("running_summary") or ""]
    summary_parts.append(f"User asked: {_compact_text(result.goal.objective, 220)}")
    summary_parts.append(f"Fronei responded via {result.route}: {_compact_text(result.answer, 260)}")
    if conversation_title:
        summary_parts.append(f"Conversation: {conversation_title}")
    if artifact_names:
        summary_parts.append(f"Artifacts created: {', '.join(artifact_names[:3])}")
    if result.sources:
        summary_parts.append(f"Sources used: {len(result.sources)}")
    ctx["running_summary"] = _compact_text(" ".join(part for part in summary_parts if part), 2200)
    facts = list(ctx.get("key_facts") or [])
    if artifact_names:
        facts.append(f"Latest artifact: {artifact_names[0]}")
    if result.route in {"research", "research_document"} and result.sources:
        facts.append(f"Recent research used {len(result.sources)} source(s).")
    ctx["key_facts"] = facts[-10:]
    return _trim_context(ctx)


def _update_context_with_snapshot(ctx: dict, snapshot: dict, *, conversation_title: str | None = None) -> dict:
    ctx = _normalize_context(ctx)
    artifact_names = [str(item) for item in snapshot.get("artifact_filenames") or []]
    turn_entry = {
        "turn_id": str(snapshot.get("turn_id") or ""),
        "route": str(snapshot.get("route") or ""),
        "user": _compact_text(str(snapshot.get("objective") or ""), 360),
        "assistant": _compact_text(str(snapshot.get("answer") or ""), 520),
        "artifacts": artifact_names,
        "source_count": int(snapshot.get("source_count") or 0),
    }
    if conversation_title:
        turn_entry["conversation"] = conversation_title
    ctx["recent_turns"] = [*ctx.get("recent_turns", []), turn_entry][-8:]
    summary_parts = [ctx.get("running_summary") or ""]
    summary_parts.append(f"User asked: {_compact_text(str(snapshot.get('objective') or ''), 220)}")
    summary_parts.append(
        f"Fronei responded via {snapshot.get('route')}: {_compact_text(str(snapshot.get('answer') or ''), 260)}"
    )
    if conversation_title:
        summary_parts.append(f"Conversation: {conversation_title}")
    if artifact_names:
        summary_parts.append(f"Artifacts created: {', '.join(artifact_names[:3])}")
    if int(snapshot.get("source_count") or 0):
        summary_parts.append(f"Sources used: {int(snapshot.get('source_count') or 0)}")
    ctx["running_summary"] = _compact_text(" ".join(part for part in summary_parts if part), 2200)
    facts = list(ctx.get("key_facts") or [])
    if artifact_names:
        facts.append(f"Latest artifact: {artifact_names[0]}")
    if snapshot.get("route") in {"research", "research_document"} and int(snapshot.get("source_count") or 0):
        facts.append(f"Recent research used {int(snapshot.get('source_count') or 0)} source(s).")
    ctx["key_facts"] = facts[-10:]
    return _trim_context(ctx)


def _context_snapshot_from_result(result: AgentV3Result) -> dict:
    return {
        "turn_id": result.turn_id,
        "user_id": result.goal.user_id,
        "conversation_id": result.goal.conversation_id,
        "objective": result.goal.objective,
        "route": result.route,
        "answer": result.answer,
        "artifact_filenames": [artifact.filename for artifact in result.artifacts],
        "source_count": len(result.sources),
        "completed_at": _now().isoformat(),
    }


def _update_context_for_completed_turn(snapshot: dict) -> None:
    conversation_id = str(snapshot.get("conversation_id") or "")
    user_id = str(snapshot.get("user_id") or "")
    if not conversation_id or not user_id:
        return
    db = SessionLocal()
    try:
        conversation = db.get(AgentV3Conversation, conversation_id)
        if conversation is None or conversation.user_id != user_id:
            return
        workspace = db.get(AgentV3Workspace, conversation.workspace_id)
        completed_at = _now()
        conversation_ctx = _loads(conversation.context_json, {})
        if workspace and not conversation_ctx:
            conversation_ctx = _initial_context(db, user_id, workspace, conversation)
        if isinstance(conversation_ctx, dict):
            conversation_ctx.setdefault("conversation", {})
            conversation_ctx["conversation"]["title"] = conversation.title
            conversation.context_json = _dumps(_update_context_with_snapshot(conversation_ctx, snapshot))
            conversation.context_updated_at = completed_at
        if workspace and workspace.user_id == user_id:
            workspace_ctx = _loads(workspace.context_json, {}) or _initial_workspace_context(db, user_id, workspace)
            workspace.context_json = _dumps(
                _update_context_with_snapshot(workspace_ctx, snapshot, conversation_title=conversation.title)
            )
            workspace.context_updated_at = completed_at
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Agent v3 context update failed for turn %s", snapshot.get("turn_id"))
    finally:
        db.close()


def _submit_context_update(snapshot: dict) -> None:
    future = _CONTEXT_EXECUTOR.submit(_update_context_for_completed_turn, snapshot)
    _PENDING_CONTEXT_FUTURES.add(future)

    def _cleanup(done: Future) -> None:
        _PENDING_CONTEXT_FUTURES.discard(done)
        try:
            done.result()
        except Exception:
            logger.exception("Agent v3 context update worker failed")

    future.add_done_callback(_cleanup)


def wait_for_context_updates(timeout_s: float = 5.0) -> None:
    """Drain pending best-effort context updates. Intended for tests/admin checks."""
    for future in list(_PENDING_CONTEXT_FUTURES):
        future.result(timeout=timeout_s)


def conversation_context_text(user_id: str, conversation_id: str | None, *, max_chars: int = 6000) -> str:
    if not conversation_id:
        return ""
    db = SessionLocal()
    try:
        conversation = db.get(AgentV3Conversation, conversation_id)
        if conversation is None or conversation.user_id != user_id:
            return ""
        workspace = db.get(AgentV3Workspace, conversation.workspace_id)
        workspace_text = ""
        if workspace and workspace.user_id == user_id:
            workspace_ctx = _loads(workspace.context_json, {})
            if not workspace_ctx:
                workspace_ctx = _initial_workspace_context(db, user_id, workspace)
                workspace.context_json = _dumps(workspace_ctx)
                workspace.context_updated_at = _now()
                db.commit()
            workspace_text = _render_context(workspace_ctx, max_chars=max(1200, max_chars // 3), label="Workspace context")
        ctx = _loads(conversation.context_json, {})
        if not ctx:
            if workspace:
                ctx = _initial_context(db, user_id, workspace, conversation)
                conversation.context_json = _dumps(ctx)
                conversation.context_updated_at = _now()
                db.commit()
        conversation_text = _render_context(ctx, max_chars=max(1200, max_chars - len(workspace_text)), label="Conversation context")
        return "\n\n".join(part for part in [workspace_text, conversation_text] if part)[-max_chars:]
    finally:
        db.close()


def _unique_workspace_name(db, user_id: str, requested_name: str, *, exclude_workspace_id: str | None = None) -> str:
    base = " ".join((requested_name or "").split())[:160].strip() or "New workspace"
    existing = {
        row.name.lower()
        for row in db.query(AgentV3Workspace.id, AgentV3Workspace.name)
        .filter(AgentV3Workspace.user_id == user_id)
        .all()
        if row.id != exclude_workspace_id
    }
    if base.lower() not in existing:
        return base
    for index in range(2, 1000):
        candidate = f"{base} {index}"
        if candidate.lower() not in existing:
            return candidate
    return f"{base} {new_id('ws')[-6:]}"


def _safe_path_segment(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)
    return cleaned[:140] or "unknown"


def _artifact_root() -> Path:
    return Path(get_settings().agent_v3_artifact_storage_dir).expanduser().resolve()


def _artifact_download_url(artifact_id: str) -> str:
    return f"/agent-v3/artifacts/{artifact_id}/download"


def _artifact_path(user_id: str, turn_id: str, artifact: Artifact) -> Path:
    filename = _safe_path_segment(artifact.filename) or f"{artifact.id}.bin"
    return _artifact_root() / _safe_path_segment(user_id) / _safe_path_segment(turn_id) / f"{_safe_path_segment(artifact.id)}_{filename}"


def _write_artifact_file(user_id: str, turn_id: str, artifact: Artifact) -> tuple[str | None, int, str | None]:
    if not artifact.base64_data:
        return None, 0, None
    try:
        payload = base64.b64decode(artifact.base64_data)
    except Exception:
        return None, 0, None
    path = _artifact_path(user_id, turn_id, artifact)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    return str(path), len(payload), digest


def _artifact_base64(row: AgentV3Artifact) -> str:
    if row.storage_path:
        try:
            path = Path(row.storage_path).expanduser().resolve()
            root = _artifact_root()
            path.relative_to(root)
            if path.exists() and path.is_file():
                return base64.b64encode(path.read_bytes()).decode("ascii")
        except Exception:
            pass
    return row.base64_data or ""


def _conversation_stats(db, conversation_id: str) -> tuple[int, int, int, int, float]:
    turns = db.query(AgentV3Turn).filter(AgentV3Turn.conversation_id == conversation_id).all()
    if not turns:
        return 0, 0, 0, 0, 0.0
    turn_ids = [turn.id for turn in turns]
    artifact_count = db.query(AgentV3Artifact).filter(AgentV3Artifact.turn_id.in_(turn_ids)).count()
    source_count = 0
    total_latency_ms = 0
    total_cost_usd = 0.0
    for turn in turns:
        source_count += len(_loads(turn.sources_json, []))
        total_latency_ms += int(turn.latency_ms or 0)
        total_cost_usd += float(turn.cost_usd or 0.0)
    return len(turns), int(artifact_count), source_count, total_latency_ms, total_cost_usd


def _conversation_summary(db, row: AgentV3Conversation) -> AgentV3ConversationSummary:
    turn_count, artifact_count, source_count, total_latency_ms, total_cost_usd = _conversation_stats(db, row.id)
    return AgentV3ConversationSummary(
        id=row.id,
        workspace_id=row.workspace_id,
        title=row.title,
        created_at=row.created_at,
        updated_at=row.updated_at,
        turn_count=turn_count,
        artifact_count=artifact_count,
        source_count=source_count,
        total_latency_ms=total_latency_ms,
        total_cost_usd=total_cost_usd,
    )


def _ensure_default_workspace(db, user_id: str) -> AgentV3Workspace:
    workspace = (
        db.query(AgentV3Workspace)
        .filter(AgentV3Workspace.user_id == user_id)
        .order_by(AgentV3Workspace.updated_at.desc())
        .first()
    )
    if workspace:
        if not _loads(workspace.context_json, {}):
            workspace.context_json = _dumps(_initial_workspace_context(db, user_id, workspace))
            workspace.context_updated_at = _now()
        return workspace
    workspace = AgentV3Workspace(id=new_id("ws"), user_id=user_id, name="Personal workspace")
    db.add(workspace)
    db.flush()
    workspace.context_json = _dumps(_initial_workspace_context(db, user_id, workspace))
    workspace.context_updated_at = _now()
    return workspace


def ensure_conversation(user_id: str, conversation_id: str | None, seed_message: str) -> AgentV3Conversation:
    db = SessionLocal()
    try:
        if conversation_id:
            conversation = db.get(AgentV3Conversation, conversation_id)
            if conversation and conversation.user_id == user_id:
                return conversation
        workspace = _ensure_default_workspace(db, user_id)
        conversation = AgentV3Conversation(
            id=new_id("conv"),
            user_id=user_id,
            workspace_id=workspace.id,
            title=_title_from_message(seed_message),
        )
        conversation.context_json = _dumps(_initial_context(db, user_id, workspace, conversation))
        conversation.context_updated_at = _now()
        workspace.updated_at = _now()
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
        return conversation
    finally:
        db.close()


def list_workspaces(user_id: str, *, ensure_default: bool = True) -> list[AgentV3WorkspaceSummary]:
    db = SessionLocal()
    try:
        if ensure_default:
            _ensure_default_workspace(db, user_id)
            db.commit()
        workspaces = (
            db.query(AgentV3Workspace)
            .filter(AgentV3Workspace.user_id == user_id)
            .order_by(AgentV3Workspace.updated_at.desc(), AgentV3Workspace.created_at.desc())
            .all()
        )
        result: list[AgentV3WorkspaceSummary] = []
        for workspace in workspaces:
            conversations = (
                db.query(AgentV3Conversation)
                .filter(
                    AgentV3Conversation.workspace_id == workspace.id,
                    AgentV3Conversation.user_id == user_id,
                )
                .order_by(AgentV3Conversation.updated_at.desc(), AgentV3Conversation.created_at.desc())
                .all()
            )
            result.append(
                AgentV3WorkspaceSummary(
                    id=workspace.id,
                    name=workspace.name,
                    created_at=workspace.created_at,
                    updated_at=workspace.updated_at,
                    conversations=[_conversation_summary(db, row) for row in conversations],
                )
            )
        return result
    finally:
        db.close()


def create_workspace(user_id: str, name: str) -> AgentV3WorkspaceSummary:
    db = SessionLocal()
    try:
        workspace = AgentV3Workspace(
            id=new_id("ws"),
            user_id=user_id,
            name=_unique_workspace_name(db, user_id, name),
        )
        workspace.context_json = _dumps(_initial_workspace_context(db, user_id, workspace))
        workspace.context_updated_at = _now()
        db.add(workspace)
        db.commit()
        db.refresh(workspace)
        return AgentV3WorkspaceSummary(
            id=workspace.id,
            name=workspace.name,
            created_at=workspace.created_at,
            updated_at=workspace.updated_at,
            conversations=[],
        )
    finally:
        db.close()


def update_workspace(user_id: str, workspace_id: str, name: str) -> AgentV3WorkspaceSummary | None:
    db = SessionLocal()
    try:
        workspace = db.get(AgentV3Workspace, workspace_id)
        if workspace is None or workspace.user_id != user_id:
            return None
        workspace.name = _unique_workspace_name(db, user_id, name, exclude_workspace_id=workspace_id)
        workspace.updated_at = _now()
        ctx = _loads(workspace.context_json, {}) or _initial_workspace_context(db, user_id, workspace)
        ctx.setdefault("workspace", {})
        ctx["workspace"]["name"] = workspace.name
        workspace.context_json = _dumps(ctx)
        workspace.context_updated_at = workspace.updated_at
        db.commit()
        db.refresh(workspace)
        conversations = (
            db.query(AgentV3Conversation)
            .filter(AgentV3Conversation.workspace_id == workspace.id, AgentV3Conversation.user_id == user_id)
            .order_by(AgentV3Conversation.updated_at.desc(), AgentV3Conversation.created_at.desc())
            .all()
        )
        return AgentV3WorkspaceSummary(
            id=workspace.id,
            name=workspace.name,
            created_at=workspace.created_at,
            updated_at=workspace.updated_at,
            conversations=[_conversation_summary(db, row) for row in conversations],
        )
    finally:
        db.close()


def delete_workspace(user_id: str, workspace_id: str) -> bool:
    db = SessionLocal()
    try:
        workspace = db.get(AgentV3Workspace, workspace_id)
        if workspace is None or workspace.user_id != user_id:
            return False
        conversations = (
            db.query(AgentV3Conversation)
            .filter(AgentV3Conversation.workspace_id == workspace_id, AgentV3Conversation.user_id == user_id)
            .all()
        )
        for conversation in conversations:
            _delete_conversation_rows(db, user_id, conversation.id)
        db.delete(workspace)
        db.commit()
        return True
    finally:
        db.close()


def create_conversation(user_id: str, workspace_id: str, title: str) -> AgentV3ConversationSummary | None:
    db = SessionLocal()
    try:
        workspace = db.get(AgentV3Workspace, workspace_id)
        if workspace is None or workspace.user_id != user_id:
            return None
        now = _now()
        conversation = AgentV3Conversation(
            id=new_id("conv"),
            user_id=user_id,
            workspace_id=workspace_id,
            title=title.strip()[:180] or "New conversation",
            created_at=now,
            updated_at=now,
        )
        conversation.context_json = _dumps(_initial_context(db, user_id, workspace, conversation))
        conversation.context_updated_at = now
        workspace.updated_at = now
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
        return _conversation_summary(db, conversation)
    finally:
        db.close()


def _delete_conversation_rows(db, user_id: str, conversation_id: str) -> None:
    turns = (
        db.query(AgentV3Turn)
        .filter(AgentV3Turn.user_id == user_id, AgentV3Turn.conversation_id == conversation_id)
        .all()
    )
    for turn in turns:
        db.query(AgentV3Event).filter(AgentV3Event.turn_id == turn.id).delete()
        db.query(AgentV3ToolCall).filter(AgentV3ToolCall.turn_id == turn.id).delete()
        artifacts = db.query(AgentV3Artifact).filter(AgentV3Artifact.turn_id == turn.id).all()
        for artifact in artifacts:
            if artifact.storage_path:
                try:
                    Path(artifact.storage_path).unlink(missing_ok=True)
                except Exception:
                    pass
            db.delete(artifact)
        db.delete(turn)


def delete_conversation(user_id: str, conversation_id: str) -> bool:
    db = SessionLocal()
    try:
        conversation = db.get(AgentV3Conversation, conversation_id)
        if conversation is None or conversation.user_id != user_id:
            return False
        workspace = db.get(AgentV3Workspace, conversation.workspace_id)
        _delete_conversation_rows(db, user_id, conversation_id)
        db.delete(conversation)
        if workspace:
            workspace.updated_at = _now()
        db.commit()
        return True
    finally:
        db.close()


def create_turn(goal: Goal, turn_id: str) -> None:
    db = SessionLocal()
    try:
        existing = db.get(AgentV3Turn, turn_id)
        if existing:
            existing.user_id = goal.user_id
            existing.conversation_id = goal.conversation_id
            existing.objective = goal.objective
            existing.route = goal.route
            existing.quality_mode = goal.quality_mode
            existing.status = "running"
            existing.updated_at = _now()
            db.commit()
            return
        conversation = None
        if goal.conversation_id:
            conversation = db.get(AgentV3Conversation, goal.conversation_id)
            if conversation and conversation.user_id == goal.user_id:
                conversation.updated_at = _now()
                workspace = db.get(AgentV3Workspace, conversation.workspace_id)
                if workspace:
                    workspace.updated_at = conversation.updated_at
        db.add(
            AgentV3Turn(
                id=turn_id,
                user_id=goal.user_id,
                conversation_id=goal.conversation_id,
                objective=goal.objective,
                route=goal.route,
                quality_mode=goal.quality_mode,
                status="running",
            )
        )
        db.commit()
    finally:
        db.close()


def append_event(event: ProgressEvent) -> None:
    db = SessionLocal()
    try:
        if db.get(AgentV3Event, event.event_id):
            return
        db.add(
            AgentV3Event(
                id=event.event_id,
                turn_id=event.turn_id,
                stage=event.stage,
                message=event.message,
                data_json=_dumps(event.data),
                created_at=event.created_at,
            )
        )
        db.commit()
    finally:
        db.close()


def complete_turn(result: AgentV3Result) -> None:
    context_snapshot = _context_snapshot_from_result(result)
    should_update_context = bool(result.goal.conversation_id)
    db = SessionLocal()
    try:
        turn = db.get(AgentV3Turn, result.turn_id)
        if turn is None:
            turn = AgentV3Turn(
                id=result.turn_id,
                user_id=result.goal.user_id,
                conversation_id=result.goal.conversation_id,
                objective=result.goal.objective,
                route=result.route,
                quality_mode=result.goal.quality_mode,
            )
            db.add(turn)
        turn.user_id = result.goal.user_id
        turn.conversation_id = result.goal.conversation_id
        turn.objective = result.goal.objective
        turn.route = result.route
        turn.quality_mode = result.goal.quality_mode
        turn.status = "completed"
        turn.answer = result.answer
        turn.model_used = result.model_used
        turn.sources_json = _dumps([source.model_dump(mode="json") for source in result.sources])
        turn.latency_ms = result.latency_ms
        turn.cost_usd = result.cost_usd
        turn.completed_at = datetime.now(timezone.utc)
        turn.updated_at = turn.completed_at

        if turn.conversation_id:
            conversation = db.get(AgentV3Conversation, turn.conversation_id)
            if conversation and conversation.user_id == turn.user_id:
                existing_turns = (
                    db.query(AgentV3Turn)
                    .filter(AgentV3Turn.conversation_id == conversation.id, AgentV3Turn.id != result.turn_id)
                    .count()
                )
                if existing_turns == 0:
                    conversation.title = _title_from_message(result.goal.objective)
                    ctx = _loads(conversation.context_json, {})
                    if isinstance(ctx, dict):
                        ctx.setdefault("conversation", {})
                        ctx["conversation"]["title"] = conversation.title
                        conversation.context_json = _dumps(ctx)
                conversation.updated_at = turn.completed_at
                workspace = db.get(AgentV3Workspace, conversation.workspace_id)
                if workspace:
                    workspace.updated_at = turn.completed_at

        for tool in result.tool_calls:
            if db.get(AgentV3ToolCall, tool.id):
                continue
            db.add(
                AgentV3ToolCall(
                    id=tool.id,
                    turn_id=result.turn_id,
                    name=tool.name,
                    input_json=_dumps(tool.input),
                    output_json=_dumps(tool.output),
                    ok=tool.ok,
                    error=tool.error,
                    latency_ms=tool.latency_ms,
                )
            )

        for artifact in result.artifacts:
            if db.get(AgentV3Artifact, artifact.id):
                continue
            storage_path, size_bytes, digest = _write_artifact_file(turn.user_id, result.turn_id, artifact)
            db.add(
                AgentV3Artifact(
                    id=artifact.id,
                    turn_id=result.turn_id,
                    kind=artifact.kind,
                    filename=artifact.filename,
                    mime_type=artifact.mime_type,
                    base64_data="" if storage_path else artifact.base64_data,
                    storage_path=storage_path,
                    size_bytes=size_bytes,
                    sha256=digest,
                )
            )
        db.commit()
    finally:
        db.close()
    if should_update_context:
        _submit_context_update(context_snapshot)


def fail_turn(turn_id: str, message: str) -> None:
    db = SessionLocal()
    try:
        turn = db.get(AgentV3Turn, turn_id)
        if turn:
            turn.status = "failed"
            turn.error_message = message
            turn.completed_at = datetime.now(timezone.utc)
            turn.updated_at = turn.completed_at
            db.commit()
    finally:
        db.close()


def load_turn_status(turn_id: str, user_id: str) -> dict | None:
    db = SessionLocal()
    try:
        turn = db.get(AgentV3Turn, turn_id)
        if turn is None or turn.user_id != user_id:
            return None
        status = turn.status
        error_message = turn.error_message
    finally:
        db.close()
    result = load_turn(turn_id, user_id)
    if result is None:
        return None
    return {
        "turn_id": turn_id,
        "status": status,
        "error_message": error_message,
        "turn": result.model_dump(mode="json"),
    }


def load_turn(turn_id: str, user_id: str) -> AgentV3Result | None:
    db = SessionLocal()
    try:
        turn = db.get(AgentV3Turn, turn_id)
        if turn is None or turn.user_id != user_id:
            return None
        events = (
            db.query(AgentV3Event)
            .filter(AgentV3Event.turn_id == turn_id)
            .order_by(AgentV3Event.created_at.asc())
            .all()
        )
        tool_rows = (
            db.query(AgentV3ToolCall)
            .filter(AgentV3ToolCall.turn_id == turn_id)
            .order_by(AgentV3ToolCall.created_at.asc())
            .all()
        )
        artifact_rows = (
            db.query(AgentV3Artifact)
            .filter(AgentV3Artifact.turn_id == turn_id)
            .order_by(AgentV3Artifact.created_at.asc())
            .all()
        )
        goal = Goal(
            id=f"goal_for_{turn.id}",
            user_id=turn.user_id,
            conversation_id=turn.conversation_id,
            objective=turn.objective,
            route=turn.route,  # type: ignore[arg-type]
            quality_mode=turn.quality_mode,
            created_at=turn.created_at,
        )
        progress_events = [
            ProgressEvent(
                event_id=row.id,
                turn_id=row.turn_id,
                stage=row.stage,
                message=row.message,
                data=_loads(row.data_json, {}),
                created_at=row.created_at,
            )
            for row in events
        ]
        research_plan_preview = _research_plan_preview_from_events(progress_events)
        return AgentV3Result(
            turn_id=turn.id,
            goal=goal,
            answer=turn.answer,
            route=turn.route,  # type: ignore[arg-type]
            model_used=turn.model_used,
            sources=[Source.model_validate(item) for item in _loads(turn.sources_json, [])],
            tool_calls=[
                ToolCall(
                    id=row.id,
                    name=row.name,
                    input=_loads(row.input_json, {}),
                    output=_loads(row.output_json, {}),
                    ok=row.ok,
                    error=row.error,
                    latency_ms=row.latency_ms,
                )
                for row in tool_rows
            ],
            artifacts=[
                Artifact(
                    id=row.id,
                    kind=row.kind,  # type: ignore[arg-type]
                    filename=row.filename,
                    mime_type=row.mime_type,
                    base64_data=_artifact_base64(row),
                    download_url=_artifact_download_url(row.id),
                    size_bytes=int(row.size_bytes or 0),
                )
                for row in artifact_rows
            ],
            events=progress_events,
            latency_ms=turn.latency_ms,
            cost_usd=turn.cost_usd,
            follow_up_options=_deep_research_followups(turn.objective, turn.route, research_plan_preview),
            research_plan_preview=research_plan_preview,
            created_at=turn.created_at,
        )
    finally:
        db.close()


def _research_plan_preview_from_events(events: list[ProgressEvent]) -> dict | None:
    for event in reversed(events):
        if event.stage == "research_plan_preview":
            preview = event.data.get("research_plan_preview")
            if isinstance(preview, dict):
                return preview
    return None


def _deep_research_followups(objective: str, route: str, preview: dict | None) -> list[dict]:
    if not preview:
        return []
    output_format = str(preview.get("output_format") or "chat")
    target_route = route if route in {"research", "research_document"} else ("research_document" if output_format != "chat" else "research")
    return [
        {
            "label": "Start research",
            "message": objective,
            "force_route": target_route,
            "research_level": "deep",
            "confirm_deep_research": True,
            "output_format": output_format,
        },
        {
            "label": "Use regular research",
            "message": objective,
            "force_route": target_route,
            "research_level": "regular",
            "confirm_deep_research": False,
            "output_format": output_format,
        },
        {
            "label": "Answer directly",
            "message": objective,
            "force_route": "direct",
            "research_level": "easy",
            "confirm_deep_research": False,
            "output_format": "chat",
        },
    ]


def list_conversation_turns(user_id: str, conversation_id: str, *, limit: int = 20, before: str | None = None) -> list[AgentV3Result]:
    db = SessionLocal()
    try:
        conversation = db.get(AgentV3Conversation, conversation_id)
        if conversation is None or conversation.user_id != user_id:
            return []
        query = db.query(AgentV3Turn).filter(
            AgentV3Turn.user_id == user_id,
            AgentV3Turn.conversation_id == conversation_id,
        )
        if before:
            before_turn = db.get(AgentV3Turn, before)
            if before_turn and before_turn.user_id == user_id:
                query = query.filter(AgentV3Turn.created_at < before_turn.created_at)
        rows = query.order_by(AgentV3Turn.created_at.desc()).limit(max(1, min(limit, 50))).all()
        ids = [row.id for row in reversed(rows)]
    finally:
        db.close()
    loaded = [load_turn(turn_id, user_id) for turn_id in ids]
    return [turn for turn in loaded if turn is not None]


def get_artifact_for_user(artifact_id: str, user_id: str) -> tuple[AgentV3Artifact, bytes] | None:
    db = SessionLocal()
    try:
        artifact = db.get(AgentV3Artifact, artifact_id)
        if artifact is None:
            return None
        turn = db.get(AgentV3Turn, artifact.turn_id)
        if turn is None or turn.user_id != user_id:
            return None
        if artifact.storage_path:
            try:
                path = Path(artifact.storage_path).expanduser().resolve()
                path.relative_to(_artifact_root())
                if path.exists() and path.is_file():
                    return artifact, path.read_bytes()
            except Exception:
                return None
        if artifact.base64_data:
            try:
                return artifact, base64.b64decode(artifact.base64_data)
            except Exception:
                return None
        return None
    finally:
        db.close()
