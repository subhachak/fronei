from __future__ import annotations

import base64
import json
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, func, or_

from app.db.models import (
    Artifact as ArtifactRow,
)
from app.db.models import (
    Conversation,
    Event,
    SessionLocal,
    Turn,
    User,
    Workspace,
)
from app.db.models import (
    ToolCall as ToolCallRow,
)
from app.services.agent import routing_policy
from app.services.agent.models import (
    Artifact,
    ConversationSummary,
    Goal,
    ProgressEvent,
    Source,
    StreamEnvelope,
    ToolCall,
    TurnRequest,
    TurnResult,
    WorkspaceSummary,
    new_id,
)
from app.services.blob_store import (
    delete_blob_location,
    get_blob_store,
    read_legacy_local_path,
    store_for_location,
)

logger = logging.getLogger(__name__)

_CONTEXT_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent-context")
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
    """Always re-read live, not cached in context_json -- unlike the rolling
    conversation/workspace context (which is intentionally a frozen,
    incrementally-updated snapshot), the consolidated profile should reflect
    the latest nightly consolidation immediately, not just for newly-created
    workspaces/conversations.

    Only durable, workspace-agnostic preferences live here. "Current
    priorities" are workspace-scoped (see _workspace_priorities) so that an
    active project in one of a user's workspaces doesn't bleed into an
    unrelated workspace's context."""
    user = db.query(User).filter(User.clerk_id == user_id).first()
    profile: dict = {"user_id": user_id}
    if user:
        if user.name:
            profile["name"] = user.name
        if user.email:
            profile["email"] = user.email
        consolidated = _loads(user.profile_json, {})
        if isinstance(consolidated, dict):
            preferences = consolidated.get("preferences")
            if isinstance(preferences, list) and preferences:
                profile["preferences"] = preferences
    return profile


def _workspace_priorities(workspace: Workspace | None) -> list[str]:
    if workspace is None:
        return []
    priorities = _loads(workspace.priorities_json, [])
    if not isinstance(priorities, list):
        return []
    return [str(item) for item in priorities if str(item).strip()][:4]


def _initial_workspace_context(db, user_id: str, workspace: Workspace) -> dict:
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


def _initial_context(db, user_id: str, workspace: Workspace, conversation: Conversation | None = None) -> dict:
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


def _render_context(
    ctx: dict,
    *,
    max_chars: int = 6000,
    label: str = "Conversation context",
    include_running_summary: bool = True,
    include_key_facts: bool = True,
    include_recent_turns: bool = True,
) -> str:
    ctx = _normalize_context(ctx, max_chars=max_chars)
    lines = [f"{label}:"]
    profile = ctx.get("user_profile") or {}
    if profile:
        display = profile.get("name") or profile.get("email") or profile.get("user_id")
        lines.append(f"- User: {display}")
        if profile.get("preferences"):
            lines.append("- User preferences:")
            lines.extend(f"  - {item}" for item in profile["preferences"])
    workspace = ctx.get("workspace") or {}
    if workspace.get("name"):
        lines.append(f"- Workspace: {workspace['name']}")
    if ctx.get("workspace_priorities"):
        lines.append("- Active priorities in this workspace:")
        lines.extend(f"  - {item}" for item in ctx["workspace_priorities"])
    conversation = ctx.get("conversation") or {}
    if conversation.get("title"):
        lines.append(f"- Conversation: {conversation['title']}")
    if include_running_summary and ctx.get("running_summary"):
        lines.append(f"- Running summary: {ctx['running_summary']}")
    if include_key_facts and ctx.get("key_facts"):
        lines.append("- Key facts:")
        lines.extend(f"  - {fact}" for fact in ctx["key_facts"])
    if include_recent_turns and ctx.get("recent_turns"):
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


def _explicit_workspace_context_request(message: str | None) -> bool:
    text = " ".join((message or "").lower().split())
    if not text:
        return False
    signals = [
        "workspace",
        "across conversations",
        "other conversations",
        "shared context",
        "what we know",
        "what you know",
    ]
    return any(signal in text for signal in signals)


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


def _update_context_with_result(ctx: dict, result: TurnResult, *, conversation_title: str | None = None) -> dict:
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


def _context_snapshot_from_result(result: TurnResult) -> dict:
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
        conversation = db.get(Conversation, conversation_id)
        if conversation is None or conversation.user_id != user_id:
            return
        workspace = db.get(Workspace, conversation.workspace_id)
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
        logger.exception("Fronei context update failed for turn %s", snapshot.get("turn_id"))
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
            logger.exception("Fronei context update worker failed")

    future.add_done_callback(_cleanup)


def wait_for_context_updates(timeout_s: float = 5.0) -> None:
    """Drain pending best-effort context updates. Intended for tests/admin checks."""
    for future in list(_PENDING_CONTEXT_FUTURES):
        future.result(timeout=timeout_s)


def last_turn_route_for_conversation(user_id: str, conversation_id: str | None) -> str | None:
    """Return the route of the most recently completed turn in this conversation,
    or None if no turns exist yet or the conversation doesn't belong to user_id.
    Reads context_json.recent_turns (same rolling buffer that conversation_context_text
    renders) rather than doing a separate DB query."""
    if not conversation_id:
        return None
    db = SessionLocal()
    try:
        conversation = db.get(Conversation, conversation_id)
        if conversation is None or conversation.user_id != user_id:
            return None
        ctx = _loads(conversation.context_json, {})
        recent_turns = ctx.get("recent_turns") or []
        if not recent_turns:
            return None
        return str(recent_turns[-1].get("route") or "") or None
    finally:
        db.close()


def conversation_context_text(
    user_id: str,
    conversation_id: str | None,
    *,
    max_chars: int = 6000,
    current_message: str | None = None,
) -> str:
    if not conversation_id:
        return ""
    db = SessionLocal()
    try:
        conversation = db.get(Conversation, conversation_id)
        if conversation is None or conversation.user_id != user_id:
            return ""
        workspace = db.get(Workspace, conversation.workspace_id)
        ctx = _loads(conversation.context_json, {})
        if not ctx:
            if workspace:
                ctx = _initial_context(db, user_id, workspace, conversation)
                conversation.context_json = _dumps(ctx)
                conversation.context_updated_at = _now()
                db.commit()
        # The rolling running_summary/key_facts/recent_turns are an
        # intentionally frozen, incrementally-updated snapshot, but the
        # consolidated preferences/priorities should always reflect the
        # latest nightly run -- re-fetch rather than use whatever was
        # baked into context_json when this conversation/workspace was
        # created.
        live_profile = _user_profile_context(db, user_id)
        ctx["user_profile"] = live_profile
        include_workspace_history = _explicit_workspace_context_request(current_message)
        workspace_text = ""
        if workspace and workspace.user_id == user_id:
            workspace_ctx = _loads(workspace.context_json, {})
            if not workspace_ctx:
                workspace_ctx = _initial_workspace_context(db, user_id, workspace)
                workspace.context_json = _dumps(workspace_ctx)
                workspace.context_updated_at = _now()
                db.commit()
            # Priorities are scoped to this workspace, not the user, and are
            # rendered once here rather than duplicated onto the
            # conversation-level ctx below (which carries the global
            # preferences instead).
            workspace_ctx["workspace_priorities"] = _workspace_priorities(workspace)
            workspace_text = _render_context(
                workspace_ctx,
                max_chars=max(800, max_chars // 3),
                label=(
                    "Workspace context requested by user"
                    if include_workspace_history
                    else "Workspace background only; do not use this to resolve vague follow-ups"
                ),
                include_running_summary=include_workspace_history,
                include_key_facts=include_workspace_history,
                include_recent_turns=include_workspace_history,
            )
        conversation_text = _render_context(
            ctx,
            max_chars=max(1200, max_chars - len(workspace_text)),
            label="Current conversation context; use this for pronouns and follow-ups",
        )
        return "\n\n".join(part for part in [conversation_text, workspace_text] if part)[-max_chars:]
    finally:
        db.close()


def _unique_workspace_name(db, user_id: str, requested_name: str, *, exclude_workspace_id: str | None = None) -> str:
    base = " ".join((requested_name or "").split())[:160].strip() or "New workspace"
    existing = {
        row.name.lower()
        for row in db.query(Workspace.id, Workspace.name)
        .filter(Workspace.user_id == user_id)
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


def _artifact_download_url(artifact_id: str) -> str:
    return f"/artifacts/{artifact_id}/download"


def artifact_blob_key(user_id: str, turn_id: str, artifact_id: str, filename: str) -> str:
    safe_filename = _safe_path_segment(filename) or f"{artifact_id}.bin"
    return "/".join([
        _safe_path_segment(user_id),
        _safe_path_segment(turn_id),
        f"{_safe_path_segment(artifact_id)}_{safe_filename}",
    ])


def _write_artifact_file(user_id: str, turn_id: str, artifact: Artifact) -> tuple[str | None, int, str | None]:
    if not artifact.base64_data:
        return None, 0, None
    try:
        payload = base64.b64decode(artifact.base64_data, validate=True)
    except Exception as exc:
        raise ValueError(f"Artifact {artifact.id} contains invalid base64 data.") from exc
    stored = get_blob_store().put(
        artifact_blob_key(user_id, turn_id, artifact.id, artifact.filename),
        payload,
        content_type=artifact.mime_type,
    )
    return stored.location, stored.size_bytes, stored.sha256


def _artifact_base64(row: ArtifactRow) -> str:
    # Stored artifacts are downloaded separately. Returning blob bytes in every
    # historical turn response defeats external storage and can make a single
    # conversation payload tens of megabytes.
    if row.storage_path:
        return ""
    return row.base64_data or ""


def _conversation_stats(db, conversation_id: str) -> tuple[int, int, int, int, float]:
    turns = db.query(Turn).filter(Turn.conversation_id == conversation_id).all()
    if not turns:
        return 0, 0, 0, 0, 0.0
    turn_ids = [turn.id for turn in turns]
    artifact_count = db.query(ArtifactRow).filter(ArtifactRow.turn_id.in_(turn_ids)).count()
    source_count = 0
    total_latency_ms = 0
    total_cost_usd = 0.0
    for turn in turns:
        source_count += len(_loads(turn.sources_json, []))
        total_latency_ms += int(turn.latency_ms or 0)
        total_cost_usd += float(turn.cost_usd or 0.0)
    return len(turns), int(artifact_count), source_count, total_latency_ms, total_cost_usd


def _conversation_stats_bulk(db, conversation_ids: list[str]) -> dict[str, tuple[int, int, int, int, float]]:
    if not conversation_ids:
        return {}

    stats: dict[str, tuple[int, int, int, int, float]] = {}
    turn_ids_by_conversation: dict[str, list[str]] = {}
    for conversation_id, turn_id, sources_json, latency_ms, cost_usd in (
        db.query(Turn.conversation_id, Turn.id, Turn.sources_json, Turn.latency_ms, Turn.cost_usd)
        .filter(Turn.conversation_id.in_(conversation_ids))
        .all()
    ):
        current = stats.get(conversation_id, (0, 0, 0, 0, 0.0))
        turn_count, artifact_count, source_count, total_latency_ms, total_cost_usd = current
        stats[conversation_id] = (
            turn_count + 1,
            artifact_count,
            source_count + len(_loads(sources_json, [])),
            total_latency_ms + int(latency_ms or 0),
            total_cost_usd + float(cost_usd or 0.0),
        )
        turn_ids_by_conversation.setdefault(conversation_id, []).append(turn_id)

    turn_ids = [turn_id for ids in turn_ids_by_conversation.values() for turn_id in ids]
    if turn_ids:
        turn_to_conversation = {
            turn_id: conversation_id
            for conversation_id, ids in turn_ids_by_conversation.items()
            for turn_id in ids
        }
        artifact_counts = (
            db.query(ArtifactRow.turn_id, func.count(ArtifactRow.id))
            .filter(ArtifactRow.turn_id.in_(turn_ids))
            .group_by(ArtifactRow.turn_id)
            .all()
        )
        for turn_id, count in artifact_counts:
            conversation_id = turn_to_conversation.get(turn_id)
            if conversation_id is None:
                continue
            turn_count, artifact_count, source_count, total_latency_ms, total_cost_usd = stats.get(
                conversation_id,
                (0, 0, 0, 0, 0.0),
            )
            stats[conversation_id] = (
                turn_count,
                artifact_count + int(count or 0),
                source_count,
                total_latency_ms,
                total_cost_usd,
            )

    return {conversation_id: stats.get(conversation_id, (0, 0, 0, 0, 0.0)) for conversation_id in conversation_ids}


def _conversation_summary(db, row: Conversation) -> ConversationSummary:
    turn_count, artifact_count, source_count, total_latency_ms, total_cost_usd = _conversation_stats(db, row.id)
    return ConversationSummary(
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


def _conversation_summary_from_stats(row: Conversation, stats: tuple[int, int, int, int, float]) -> ConversationSummary:
    turn_count, artifact_count, source_count, total_latency_ms, total_cost_usd = stats
    return ConversationSummary(
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


def _ensure_default_workspace(db, user_id: str) -> Workspace:
    workspace = (
        db.query(Workspace)
        .filter(Workspace.user_id == user_id)
        .order_by(Workspace.updated_at.desc())
        .first()
    )
    if workspace:
        if not _loads(workspace.context_json, {}):
            workspace.context_json = _dumps(_initial_workspace_context(db, user_id, workspace))
            workspace.context_updated_at = _now()
        return workspace
    workspace = Workspace(id=new_id("ws"), user_id=user_id, name="Personal workspace")
    db.add(workspace)
    db.flush()
    workspace.context_json = _dumps(_initial_workspace_context(db, user_id, workspace))
    workspace.context_updated_at = _now()
    return workspace


def ensure_conversation(user_id: str, conversation_id: str | None, seed_message: str) -> Conversation:
    db = SessionLocal()
    try:
        if conversation_id:
            conversation = db.get(Conversation, conversation_id)
            if conversation and conversation.user_id == user_id:
                return conversation
        workspace = _ensure_default_workspace(db, user_id)
        conversation = Conversation(
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


def list_workspaces(user_id: str, *, ensure_default: bool = True) -> list[WorkspaceSummary]:
    db = SessionLocal()
    try:
        if ensure_default:
            _ensure_default_workspace(db, user_id)
            db.commit()
        workspaces = (
            db.query(Workspace)
            .filter(Workspace.user_id == user_id)
            .order_by(Workspace.updated_at.desc(), Workspace.created_at.desc())
            .all()
        )
        workspace_ids = [workspace.id for workspace in workspaces]
        conversations_by_workspace: dict[str, list[Conversation]] = {workspace_id: [] for workspace_id in workspace_ids}
        conversations: list[Conversation] = []
        if workspace_ids:
            conversations = (
                db.query(Conversation)
                .filter(
                    Conversation.workspace_id.in_(workspace_ids),
                    Conversation.user_id == user_id,
                )
                .order_by(Conversation.updated_at.desc(), Conversation.created_at.desc())
                .all()
            )
            for conversation in conversations:
                conversations_by_workspace.setdefault(conversation.workspace_id, []).append(conversation)
        stats_by_conversation = _conversation_stats_bulk(db, [conversation.id for conversation in conversations])
        result: list[WorkspaceSummary] = []
        for workspace in workspaces:
            workspace_conversations = conversations_by_workspace.get(workspace.id, [])
            result.append(
                WorkspaceSummary(
                    id=workspace.id,
                    name=workspace.name,
                    created_at=workspace.created_at,
                    updated_at=workspace.updated_at,
                    conversations=[
                        _conversation_summary_from_stats(row, stats_by_conversation.get(row.id, (0, 0, 0, 0, 0.0)))
                        for row in workspace_conversations
                    ],
                )
            )
        return result
    finally:
        db.close()


def create_workspace(user_id: str, name: str) -> WorkspaceSummary:
    db = SessionLocal()
    try:
        workspace = Workspace(
            id=new_id("ws"),
            user_id=user_id,
            name=_unique_workspace_name(db, user_id, name),
        )
        workspace.context_json = _dumps(_initial_workspace_context(db, user_id, workspace))
        workspace.context_updated_at = _now()
        db.add(workspace)
        db.commit()
        db.refresh(workspace)
        return WorkspaceSummary(
            id=workspace.id,
            name=workspace.name,
            created_at=workspace.created_at,
            updated_at=workspace.updated_at,
            conversations=[],
        )
    finally:
        db.close()


def update_workspace(user_id: str, workspace_id: str, name: str) -> WorkspaceSummary | None:
    db = SessionLocal()
    try:
        workspace = db.get(Workspace, workspace_id)
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
            db.query(Conversation)
            .filter(Conversation.workspace_id == workspace.id, Conversation.user_id == user_id)
            .order_by(Conversation.updated_at.desc(), Conversation.created_at.desc())
            .all()
        )
        return WorkspaceSummary(
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
        workspace = db.get(Workspace, workspace_id)
        if workspace is None or workspace.user_id != user_id:
            return False
        conversations = (
            db.query(Conversation)
            .filter(Conversation.workspace_id == workspace_id, Conversation.user_id == user_id)
            .all()
        )
        for conversation in conversations:
            _delete_conversation_rows(db, user_id, conversation.id)
        db.delete(workspace)
        db.commit()
        return True
    finally:
        db.close()


def create_conversation(user_id: str, workspace_id: str, title: str) -> ConversationSummary | None:
    db = SessionLocal()
    try:
        workspace = db.get(Workspace, workspace_id)
        if workspace is None or workspace.user_id != user_id:
            return None
        now = _now()
        conversation = Conversation(
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
        db.query(Turn)
        .filter(Turn.user_id == user_id, Turn.conversation_id == conversation_id)
        .all()
    )
    for turn in turns:
        db.query(Event).filter(Event.turn_id == turn.id).delete()
        db.query(ToolCallRow).filter(ToolCallRow.turn_id == turn.id).delete()
        artifacts = db.query(ArtifactRow).filter(ArtifactRow.turn_id == turn.id).all()
        for artifact in artifacts:
            if artifact.storage_path:
                try:
                    delete_blob_location(artifact.storage_path)
                except Exception:
                    logger.warning("Could not delete artifact blob %s", artifact.id, exc_info=True)
            db.delete(artifact)
        db.delete(turn)


def delete_conversation(user_id: str, conversation_id: str) -> bool:
    db = SessionLocal()
    try:
        conversation = db.get(Conversation, conversation_id)
        if conversation is None or conversation.user_id != user_id:
            return False
        workspace = db.get(Workspace, conversation.workspace_id)
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
        existing = db.get(Turn, turn_id)
        if existing:
            existing.user_id = goal.user_id
            existing.conversation_id = goal.conversation_id
            existing.objective = goal.objective
            existing.route = goal.route
            existing.quality_mode = goal.quality_mode
            if existing.status not in {"completed", "failed", "cancelled"}:
                existing.status = "running"
                existing.error_message = None
            existing.updated_at = _now()
            db.commit()
            return
        conversation = None
        if goal.conversation_id:
            conversation = db.get(Conversation, goal.conversation_id)
            if conversation and conversation.user_id == goal.user_id:
                conversation.updated_at = _now()
                workspace = db.get(Workspace, conversation.workspace_id)
                if workspace:
                    workspace.updated_at = conversation.updated_at
        db.add(
            Turn(
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


def enqueue_turn(goal: Goal, turn_id: str, request: TurnRequest, *, max_attempts: int) -> None:
    db = SessionLocal()
    try:
        now = _now()
        db.add(
            Turn(
                id=turn_id,
                user_id=goal.user_id,
                conversation_id=goal.conversation_id,
                objective=goal.objective,
                route=goal.route,
                quality_mode=goal.quality_mode,
                status="queued",
                request_json=_dumps(request.model_dump(mode="json")),
                attempt_count=0,
                max_attempts=max(1, max_attempts),
                created_at=now,
                updated_at=now,
            )
        )
        db.commit()
    finally:
        db.close()


def claim_next_turn(worker_id: str, *, lease_seconds: int) -> tuple[str, str, TurnRequest] | None:
    """Atomically claim one queued or expired turn.

    The conditional UPDATE is the concurrency guard. It works on SQLite and
    Postgres without relying on backend-specific SKIP LOCKED behavior.
    """
    from datetime import timedelta

    db = SessionLocal()
    try:
        now = _now()
        candidates = (
            db.query(Turn)
            .filter(
                Turn.attempt_count < Turn.max_attempts,
                (
                    (Turn.status == "queued")
                    | (
                        (Turn.status == "running")
                        & Turn.lease_expires_at.isnot(None)
                        & (Turn.lease_expires_at < now)
                    )
                ),
            )
            .order_by(Turn.created_at.asc())
            .limit(8)
            .all()
        )
        for candidate in candidates:
            previous_attempt = int(candidate.attempt_count or 0)
            updated = (
                db.query(Turn)
                .filter(
                    Turn.id == candidate.id,
                    Turn.attempt_count == previous_attempt,
                    (
                        (Turn.status == "queued")
                        | (
                            (Turn.status == "running")
                            & Turn.lease_expires_at.isnot(None)
                            & (Turn.lease_expires_at < now)
                        )
                    ),
                )
                .update(
                    {
                        Turn.status: "running",
                        Turn.attempt_count: previous_attempt + 1,
                        Turn.lease_owner: worker_id,
                        Turn.lease_expires_at: now + timedelta(seconds=max(10, lease_seconds)),
                        Turn.heartbeat_at: now,
                        Turn.error_message: None,
                        Turn.updated_at: now,
                    },
                    synchronize_session=False,
                )
            )
            if not updated:
                db.rollback()
                continue
            db.commit()
            payload = _loads(candidate.request_json, {})
            try:
                return candidate.id, candidate.user_id, TurnRequest.model_validate(payload)
            except Exception as exc:
                fail_or_requeue_turn(candidate.id, worker_id, f"Stored turn request is invalid: {exc}")
                return None
        return None
    finally:
        db.close()


def renew_turn_lease(turn_id: str, worker_id: str, *, lease_seconds: int) -> bool:
    from datetime import timedelta

    db = SessionLocal()
    try:
        now = _now()
        updated = (
            db.query(Turn)
            .filter(
                Turn.id == turn_id,
                Turn.status == "running",
                Turn.lease_owner == worker_id,
                Turn.cancel_requested.is_(False),
            )
            .update(
                {
                    Turn.heartbeat_at: now,
                    Turn.lease_expires_at: now + timedelta(seconds=max(10, lease_seconds)),
                    Turn.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return bool(updated)
    finally:
        db.close()


def worker_owns_turn(turn_id: str, worker_id: str) -> bool:
    db = SessionLocal()
    try:
        row = db.get(Turn, turn_id)
        return bool(
            row
            and row.status == "running"
            and row.lease_owner == worker_id
        )
    finally:
        db.close()


def turn_cancel_requested(turn_id: str, worker_id: str) -> bool:
    db = SessionLocal()
    try:
        row = db.get(Turn, turn_id)
        return bool(
            row
            and row.status == "running"
            and row.lease_owner == worker_id
            and row.cancel_requested
        )
    finally:
        db.close()


def request_turn_cancellation(turn_id: str, user_id: str) -> bool:
    db = SessionLocal()
    try:
        turn = db.get(Turn, turn_id)
        if turn is None or turn.user_id != user_id or turn.status not in {"queued", "running"}:
            return False
        turn.cancel_requested = True
        if turn.status == "queued":
            turn.status = "cancelled"
            turn.completed_at = _now()
        turn.updated_at = _now()
        db.commit()
        return True
    finally:
        db.close()


def fail_or_requeue_turn(turn_id: str, worker_id: str, message: str) -> str:
    db = SessionLocal()
    try:
        turn = db.get(Turn, turn_id)
        if turn is None or turn.lease_owner != worker_id:
            return "lost"
        now = _now()
        if turn.cancel_requested:
            turn.status = "cancelled"
            turn.completed_at = now
            outcome = "cancelled"
        elif turn.attempt_count < turn.max_attempts:
            turn.status = "queued"
            outcome = "queued"
        else:
            turn.status = "failed"
            turn.completed_at = now
            outcome = "failed"
        turn.error_message = message[:2000]
        turn.lease_owner = None
        turn.lease_expires_at = None
        turn.heartbeat_at = None
        turn.updated_at = now
        db.commit()
        return outcome
    finally:
        db.close()


def persist_turn_envelope(
    envelope: StreamEnvelope,
    turn_id: str | None,
    *,
    lease_owner: str | None = None,
) -> bool:
    if lease_owner and turn_id and not worker_owns_turn(turn_id, lease_owner):
        return False
    if envelope.type == "start":
        start_turn_id = str(envelope.data.get("turn_id") or turn_id or "")
        goal = Goal.model_validate(envelope.data.get("goal"))
        create_turn(goal, start_turn_id)
    elif envelope.type == "progress":
        progress_event = ProgressEvent.model_validate(envelope.data)
        if not progress_event.data.get("ephemeral"):
            append_event(progress_event)
    elif envelope.type == "result":
        return complete_turn(TurnResult.model_validate(envelope.data), lease_owner=lease_owner)
    return True


def append_event(event: ProgressEvent) -> None:
    db = SessionLocal()
    try:
        if db.get(Event, event.event_id):
            return
        db.add(
            Event(
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


def load_turn_events_after(turn_id: str, user_id: str, after_event_id: str | None = None) -> list[ProgressEvent] | None:
    db = SessionLocal()
    try:
        turn = db.get(Turn, turn_id)
        if turn is None or turn.user_id != user_id:
            return None
        query = db.query(Event).filter(Event.turn_id == turn_id)
        if after_event_id:
            cursor = db.get(Event, after_event_id)
            if cursor is not None and cursor.turn_id == turn_id:
                query = query.filter(
                    or_(
                        Event.created_at > cursor.created_at,
                        and_(Event.created_at == cursor.created_at, Event.id > cursor.id),
                    )
                )
        rows = query.order_by(Event.created_at.asc(), Event.id.asc()).all()
        return [
            ProgressEvent(
                event_id=row.id,
                turn_id=row.turn_id,
                stage=row.stage,
                message=row.message,
                data=_loads(row.data_json, {}),
                created_at=row.created_at,
            )
            for row in rows
        ]
    finally:
        db.close()


def load_turn_state(turn_id: str, user_id: str) -> dict | None:
    db = SessionLocal()
    try:
        turn = db.get(Turn, turn_id)
        if turn is None or turn.user_id != user_id:
            return None
        return {
            "turn_id": turn.id,
            "status": turn.status,
            "error_message": turn.error_message,
            "attempt_count": turn.attempt_count,
            "max_attempts": turn.max_attempts,
            "heartbeat_at": turn.heartbeat_at.isoformat() if turn.heartbeat_at else None,
        }
    finally:
        db.close()


def complete_turn(result: TurnResult, *, lease_owner: str | None = None) -> bool:
    context_snapshot = _context_snapshot_from_result(result)
    should_update_context = bool(result.goal.conversation_id)
    completed_at = datetime.now(timezone.utc)
    completion_values = {
        Turn.user_id: result.goal.user_id,
        Turn.conversation_id: result.goal.conversation_id,
        Turn.objective: result.goal.objective,
        Turn.route: result.route,
        Turn.quality_mode: result.goal.quality_mode,
        Turn.status: "completed",
        Turn.answer: result.answer,
        Turn.model_used: result.model_used,
        Turn.sources_json: _dumps([source.model_dump(mode="json") for source in result.sources]),
        Turn.latency_ms: result.latency_ms,
        Turn.cost_usd: result.cost_usd,
        Turn.completed_at: completed_at,
        Turn.updated_at: completed_at,
        Turn.lease_owner: None,
        Turn.lease_expires_at: None,
        Turn.heartbeat_at: None,
        Turn.error_message: None,
    }
    db = SessionLocal()
    new_artifact_locations: list[str] = []
    try:
        turn = None
        if lease_owner:
            updated = (
                db.query(Turn)
                .filter(
                    Turn.id == result.turn_id,
                    Turn.status == "running",
                    Turn.lease_owner == lease_owner,
                )
                .update(completion_values, synchronize_session=False)
            )
            if not updated:
                db.rollback()
                return False
            turn = db.get(Turn, result.turn_id)
        else:
            turn = db.get(Turn, result.turn_id)
        if turn is None:
            turn = Turn(
                id=result.turn_id,
                user_id=result.goal.user_id,
                conversation_id=result.goal.conversation_id,
                objective=result.goal.objective,
                route=result.route,
                quality_mode=result.goal.quality_mode,
            )
            db.add(turn)
        if lease_owner is None:
            for field, value in completion_values.items():
                setattr(turn, field.key, value)

        if turn.conversation_id:
            conversation = db.get(Conversation, turn.conversation_id)
            if conversation and conversation.user_id == turn.user_id:
                existing_turns = (
                    db.query(Turn)
                    .filter(Turn.conversation_id == conversation.id, Turn.id != result.turn_id)
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
                workspace = db.get(Workspace, conversation.workspace_id)
                if workspace:
                    workspace.updated_at = turn.completed_at

        for tool in result.tool_calls:
            if db.get(ToolCallRow, tool.id):
                continue
            db.add(
                ToolCallRow(
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
            if db.get(ArtifactRow, artifact.id):
                continue
            storage_path, size_bytes, digest = _write_artifact_file(turn.user_id, result.turn_id, artifact)
            if storage_path:
                new_artifact_locations.append(storage_path)
            db.add(
                ArtifactRow(
                    id=artifact.id,
                    turn_id=result.turn_id,
                    kind=artifact.kind,
                    filename=artifact.filename,
                    mime_type=artifact.mime_type,
                    base64_data="",
                    storage_path=storage_path,
                    size_bytes=size_bytes,
                    sha256=digest,
                )
            )
        db.commit()
    except Exception:
        db.rollback()
        for location in new_artifact_locations:
            try:
                delete_blob_location(location)
            except Exception:
                logger.warning("Could not clean up uncommitted artifact blob %s", location, exc_info=True)
        raise
    finally:
        db.close()
    _record_routing_feedback(result)
    if should_update_context:
        _submit_context_update(context_snapshot)
    return True


def _record_routing_feedback(result: TurnResult) -> None:
    try:
        router_event = next((event for event in result.events if event.stage == "fast_router"), None)
        if router_event is None:
            return
        data = router_event.data or {}
        selected_route = str(data.get("path") or result.route)
        matched_signals = data.get("matched_signals")
        routing_policy.record_routing_feedback(
            turn_id=result.turn_id,
            user_id=result.goal.user_id,
            conversation_id=result.goal.conversation_id,
            message=result.goal.objective,
            selected_route=selected_route,
            final_route=result.route,
            matched_signals=matched_signals if isinstance(matched_signals, list) else [],
        )
    except Exception:
        logger.exception("Fronei routing feedback capture failed")


def fail_turn(turn_id: str, message: str) -> None:
    db = SessionLocal()
    try:
        turn = db.get(Turn, turn_id)
        if turn:
            turn.status = "failed"
            turn.error_message = message
            turn.completed_at = datetime.now(timezone.utc)
            turn.updated_at = turn.completed_at
            turn.lease_owner = None
            turn.lease_expires_at = None
            turn.heartbeat_at = None
            db.commit()
    finally:
        db.close()


def load_turn_status(turn_id: str, user_id: str) -> dict | None:
    db = SessionLocal()
    try:
        turn = db.get(Turn, turn_id)
        if turn is None or turn.user_id != user_id:
            return None
        status = turn.status
        error_message = turn.error_message
        attempt_count = turn.attempt_count
        max_attempts = turn.max_attempts
        heartbeat_at = turn.heartbeat_at
    finally:
        db.close()
    result = load_turn(turn_id, user_id)
    if result is None:
        return None
    return {
        "turn_id": turn_id,
        "status": status,
        "error_message": error_message,
        "attempt_count": attempt_count,
        "max_attempts": max_attempts,
        "heartbeat_at": heartbeat_at.isoformat() if heartbeat_at else None,
        "turn": result.model_dump(mode="json"),
    }


def load_turn(turn_id: str, user_id: str) -> TurnResult | None:
    db = SessionLocal()
    try:
        turn = db.get(Turn, turn_id)
        if turn is None or turn.user_id != user_id:
            return None
        events = (
            db.query(Event)
            .filter(Event.turn_id == turn_id)
            .order_by(Event.created_at.asc())
            .all()
        )
        tool_rows = (
            db.query(ToolCallRow)
            .filter(ToolCallRow.turn_id == turn_id)
            .order_by(ToolCallRow.created_at.asc())
            .all()
        )
        artifact_rows = (
            db.query(ArtifactRow)
            .filter(ArtifactRow.turn_id == turn_id)
            .order_by(ArtifactRow.created_at.asc())
            .all()
        )
        return _turn_result_from_rows(turn, events, tool_rows, artifact_rows)
    finally:
        db.close()


def _turn_result_from_rows(
    turn: Turn,
    events: list[Event],
    tool_rows: list[ToolCallRow],
    artifact_rows: list[ArtifactRow],
) -> TurnResult:
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
    return TurnResult(
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


def list_conversation_turns(user_id: str, conversation_id: str, *, limit: int = 20, before: str | None = None) -> list[TurnResult]:
    db = SessionLocal()
    try:
        conversation = db.get(Conversation, conversation_id)
        if conversation is None or conversation.user_id != user_id:
            return []
        query = db.query(Turn).filter(
            Turn.user_id == user_id,
            Turn.conversation_id == conversation_id,
        )
        if before:
            before_turn = db.get(Turn, before)
            if before_turn and before_turn.user_id == user_id:
                query = query.filter(Turn.created_at < before_turn.created_at)
        rows = query.order_by(Turn.created_at.desc()).limit(max(1, min(limit, 50))).all()
        rows = list(reversed(rows))
        ids = [row.id for row in rows]
        if not ids:
            return []
        events_by_turn: dict[str, list[Event]] = {turn_id: [] for turn_id in ids}
        for event in (
            db.query(Event)
            .filter(Event.turn_id.in_(ids))
            .order_by(Event.created_at.asc())
            .all()
        ):
            events_by_turn.setdefault(event.turn_id, []).append(event)
        tools_by_turn: dict[str, list[ToolCallRow]] = {turn_id: [] for turn_id in ids}
        for tool in (
            db.query(ToolCallRow)
            .filter(ToolCallRow.turn_id.in_(ids))
            .order_by(ToolCallRow.created_at.asc())
            .all()
        ):
            tools_by_turn.setdefault(tool.turn_id, []).append(tool)
        artifacts_by_turn: dict[str, list[ArtifactRow]] = {turn_id: [] for turn_id in ids}
        for artifact in (
            db.query(ArtifactRow)
            .filter(ArtifactRow.turn_id.in_(ids))
            .order_by(ArtifactRow.created_at.asc())
            .all()
        ):
            artifacts_by_turn.setdefault(artifact.turn_id, []).append(artifact)
        return [
            _turn_result_from_rows(
                row,
                events_by_turn.get(row.id, []),
                tools_by_turn.get(row.id, []),
                artifacts_by_turn.get(row.id, []),
            )
            for row in rows
        ]
    finally:
        db.close()


def get_artifact_for_user(artifact_id: str, user_id: str) -> tuple[ArtifactRow, bytes | None, str | None] | None:
    db = SessionLocal()
    try:
        artifact = db.get(ArtifactRow, artifact_id)
        if artifact is None:
            return None
        turn = db.get(Turn, artifact.turn_id)
        if turn is None or turn.user_id != user_id:
            return None
        if artifact.storage_path:
            try:
                if artifact.storage_path.startswith(("local:", "s3:")):
                    store = store_for_location(artifact.storage_path)
                    signed_url = store.presigned_download_url(
                        artifact.storage_path,
                        filename=artifact.filename,
                        content_type=artifact.mime_type,
                    )
                    if signed_url:
                        return artifact, None, signed_url
                    return artifact, store.read(artifact.storage_path), None
                return artifact, read_legacy_local_path(artifact.storage_path), None
            except Exception:
                logger.warning("Could not resolve artifact blob %s", artifact.id, exc_info=True)
                return None
        if artifact.base64_data:
            try:
                return artifact, base64.b64decode(artifact.base64_data), None
            except Exception:
                return None
        return None
    finally:
        db.close()


def set_turn_feedback(turn_id: str, user_id: str, rating: str) -> bool:
    """Record user feedback ('positive' or 'negative') for a completed turn.

    Returns True if the turn was found and owned by user_id, False otherwise.
    """
    db = SessionLocal()
    try:
        turn = db.get(Turn, turn_id)
        if turn is None or turn.user_id != user_id:
            return False
        turn.feedback = rating
        db.commit()
        return True
    finally:
        db.close()


def delete_artifacts_for_turn_ids(db, turn_ids: list[str]) -> int:
    if not turn_ids:
        return 0
    rows = db.query(ArtifactRow).filter(ArtifactRow.turn_id.in_(turn_ids)).all()
    for row in rows:
        if row.storage_path:
            try:
                delete_blob_location(row.storage_path)
            except Exception:
                logger.warning("Could not delete artifact blob %s", row.id, exc_info=True)
        db.delete(row)
    return len(rows)
