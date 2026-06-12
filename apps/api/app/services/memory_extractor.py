"""
Background service that extracts persistent facts from each conversation turn
and stores them in user_memories. Runs in the thread pool from memory_writer.
Silent failure — no memory is ever lost on error.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import re

from litellm import completion

from app.db.models import DEFAULT_DECAY_RATES, SessionLocal, UserMemory

_pool = ThreadPoolExecutor(max_workers=2)
_MODEL = "gemini/gemini-2.5-flash"
_MAX_CHARS = 600
_MAX_MEMORY_CHARS = 500
_ALLOWED_ACTIONS = {"new", "reinforce", "update", "contradict", "ignore"}
_ALLOWED_CATEGORIES = set(DEFAULT_DECAY_RATES)
_ALLOWED_SCOPES = {"global", "work", "project", "style", "personal"}
_ALLOWED_SOURCES = {"stated", "inferred"}
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?i)(api[_ -]?key|password|secret|token)\s*[:=]\s*\S+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]

_PROMPT = """\
Extract useful durable context about the USER from this conversation turn.
This can include bio, work context, personal context, preferences, constraints,
communication style, tools, relationships, active projects, and temporary plans
when they may help personalize future responses.

Do NOT store secrets, credentials, API keys, passwords, private tokens, auth
headers, or one-off task details unlikely to matter later.

You will receive existing active memories as JSON with id/content/category.
Return a JSON array of actions. Each action must be one of:
[
  {"action": "new", "content": "...", "category": "...", "scope": "...", "source": "stated|inferred", "confidence": 0.0, "importance": 0.0},
  {"action": "reinforce", "memory_id": 123},
  {"action": "update", "memory_id": 123, "content": "new text", "confidence": 0.0},
  {"action": "contradict", "memory_id": 123, "content": "new text", "confidence": 0.0},
  {"action": "ignore"}
]

category must be one of: bio, work, project, preference, communication_style,
relationship, constraint, temporary_plan, tool, personal, general.
scope must be one of: global, work, project, style, personal.
Use "stated" only for facts the user directly says. Use "inferred" for guesses.
Output only valid JSON — no fences, no explanation."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clamp(value, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _contains_secret(content: str) -> bool:
    return any(pattern.search(content) for pattern in _SECRET_PATTERNS)


def _active_memory_summary(db, user_id: str) -> list[dict]:
    rows = (
        db.query(UserMemory)
        .filter(UserMemory.user_id == user_id, UserMemory.status == "active")
        .order_by(UserMemory.updated_at.desc())
        .limit(40)
        .all()
    )
    return [
        {"id": m.id, "content": m.content[:240], "category": m.category, "scope": m.scope}
        for m in rows
    ]


def _memory_for_action(db, user_id: str, memory_id) -> UserMemory | None:
    try:
        mid = int(memory_id)
    except (TypeError, ValueError):
        return None
    memory = db.get(UserMemory, mid)
    if not memory or memory.user_id != user_id:
        return None
    if (memory.status or "active") != "active":
        return None
    return memory


def apply_actions(db, user_id: str, conv_id: int, actions: list[dict]) -> None:
    now = _now()
    for action in actions:
        if not isinstance(action, dict):
            continue
        kind = str(action.get("action", "")).strip()
        if kind not in _ALLOWED_ACTIONS or kind == "ignore":
            continue

        if kind == "new":
            content = str(action.get("content", "")).strip()
            if not content or len(content) > _MAX_MEMORY_CHARS or _contains_secret(content):
                continue
            category = str(action.get("category", "general")).strip()
            if category not in _ALLOWED_CATEGORIES:
                category = "general"
            scope = str(action.get("scope", "global")).strip()
            if scope not in _ALLOWED_SCOPES:
                scope = "global"
            source = str(action.get("source", "inferred")).strip()
            if source not in _ALLOWED_SOURCES:
                source = "inferred"
            db.add(UserMemory(
                user_id=user_id,
                content=content,
                category=category,
                scope=scope,
                source=source,
                confidence=_clamp(action.get("confidence"), 0.6),
                importance=_clamp(action.get("importance"), 0.5),
                decay_rate=DEFAULT_DECAY_RATES.get(category, DEFAULT_DECAY_RATES["general"]),
                seen_count=1,
                last_seen_at=now,
                status="active",
                source_conversation_id=conv_id,
                created_at=now,
                updated_at=now,
            ))
            continue

        memory = _memory_for_action(db, user_id, action.get("memory_id"))
        if not memory:
            continue

        if kind == "reinforce":
            memory.seen_count = (memory.seen_count or 1) + 1
            memory.last_seen_at = now
            memory.importance = min(1.0, (memory.importance or 0.5) + 0.05)
            memory.confidence = min(1.0, (memory.confidence or 0.6) + 0.05)
            continue

        content = str(action.get("content", "")).strip()
        if not content or len(content) > _MAX_MEMORY_CHARS or _contains_secret(content):
            continue

        if kind == "update":
            if memory.pinned:
                continue
            memory.content = content
            memory.confidence = _clamp(action.get("confidence"), memory.confidence or 0.6)
            memory.seen_count = (memory.seen_count or 1) + 1
            memory.last_seen_at = now
            memory.updated_at = now
            continue

        if kind == "contradict":
            if memory.pinned:
                continue
            category = memory.category or "general"
            new_memory = UserMemory(
                user_id=user_id,
                content=content,
                category=category,
                scope=memory.scope or "global",
                source="inferred",
                confidence=_clamp(action.get("confidence"), 0.6),
                importance=memory.importance or 0.5,
                decay_rate=memory.decay_rate or DEFAULT_DECAY_RATES.get(category, DEFAULT_DECAY_RATES["general"]),
                seen_count=1,
                last_seen_at=now,
                status="active",
                source_conversation_id=conv_id,
                created_at=now,
                updated_at=now,
            )
            db.add(new_memory)
            db.flush()
            memory.status = "superseded"
            memory.superseded_by_id = new_memory.id
            memory.updated_at = now


def _extract(user_id: str, conv_id: int, user_msg: str, assistant_answer: str) -> None:
    try:
        u = user_msg[:_MAX_CHARS]
        a = assistant_answer[:_MAX_CHARS]
        db = SessionLocal()
        try:
            existing = _active_memory_summary(db, user_id)
        finally:
            db.close()
        resp = completion(
            model=_MODEL,
            messages=[
                {"role": "system", "content": _PROMPT},
                {"role": "user", "content": (
                    f"Existing active memories:\n{json.dumps(existing, ensure_ascii=False)}\n\n"
                    f"User message: {u}\nAssistant answer: {a}"
                )},
            ],
            temperature=0.1,
            max_tokens=512,
        )
        raw = (resp.choices[0].message.content or "").strip()
        # Strip markdown fences if present
        raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("` \n")
        actions = json.loads(raw)
        if not isinstance(actions, list):
            return
        db = SessionLocal()
        try:
            apply_actions(db, user_id, conv_id, actions)
            db.commit()
        finally:
            db.close()
    except Exception:
        pass  # silent failure


def schedule(user_id: str, conv_id: int, user_msg: str, assistant_answer: str) -> None:
    """Fire-and-forget: extract and store memorable facts from this turn."""
    _pool.submit(_extract, user_id, conv_id, user_msg, assistant_answer)
