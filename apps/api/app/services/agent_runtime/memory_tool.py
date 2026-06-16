from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any


logger = logging.getLogger(__name__)

MAX_MEMORIES = 8
MAX_CHARS = 1200


def read_scoped_memory(user_id: str, *, category_hint: str | None = None) -> dict[str, Any]:
    """Fetch top-ranked active memories for user_id. Never raises."""

    if not user_id:
        return {"memories": [], "count": 0, "truncated": False}
    try:
        from app.db.models import SessionLocal, UserMemory
        from app.services.memory_ranker import rank_memories

        now = datetime.now(timezone.utc)
        with SessionLocal() as db:
            rows = (
                db.query(UserMemory)
                .filter(UserMemory.user_id == user_id, UserMemory.status == "active")
                .all()
            )
        ranked = rank_memories(rows, now, category_hint, limit=MAX_MEMORIES)
        items: list[str] = []
        total = 0
        truncated = False
        for memory in ranked:
            text = str(getattr(memory, "content", "") or "").strip()
            if not text:
                continue
            if total + len(text) > MAX_CHARS:
                truncated = True
                break
            items.append(text)
            total += len(text)
        return {"memories": items, "count": len(items), "truncated": truncated}
    except Exception:
        logger.exception("memory_read failed for user %s", user_id)
        return {"memories": [], "count": 0, "truncated": False}


def write_scoped_memory(
    user_id: str,
    content: str,
    *,
    category: str = "general",
    source: str = "agent",
) -> dict[str, Any]:
    """Write a memory item for user_id. Never raises."""

    cleaned = str(content or "").strip()
    if not user_id or not cleaned:
        return {"written": False, "reason": "empty"}
    try:
        from app.db.models import DEFAULT_DECAY_RATES, SessionLocal, UserMemory

        now = datetime.now(timezone.utc)
        with SessionLocal() as db:
            db.add(UserMemory(
                user_id=user_id,
                content=cleaned[:500],
                category=category,
                scope="global",
                source=source,
                confidence=0.7,
                importance=0.5,
                decay_rate=DEFAULT_DECAY_RATES.get(category, 0.05),
                status="active",
                created_at=now,
                updated_at=now,
            ))
            db.commit()
        return {"written": True}
    except Exception:
        logger.exception("memory_write failed for user %s", user_id)
        return {"written": False, "reason": "db_error"}
