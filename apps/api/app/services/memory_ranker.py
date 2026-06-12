from __future__ import annotations

import math
from datetime import datetime, timezone

from app.db.models import UserMemory


def _aware(dt: datetime | None, fallback: datetime) -> datetime:
    if dt is None:
        return fallback
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def score(memory: UserMemory, now: datetime, turn_category_hint: str | None = None) -> float:
    last_seen_at = _aware(memory.last_seen_at, _aware(memory.updated_at, now))
    current = _aware(now, now)
    age_days = max(0.0, (current - last_seen_at).total_seconds() / 86400)
    decay_rate = memory.decay_rate if memory.decay_rate is not None else 0.05
    recency_weight = math.exp(-decay_rate * age_days)
    repetition = min(1.0, (memory.seen_count or 1) / 10)
    pin_bonus = 0.5 if memory.pinned else 0.0
    relevance_bonus = 0.2 if turn_category_hint and memory.category == turn_category_hint else 0.0
    return (
        (memory.importance or 0.5)
        + (memory.confidence or 0.6)
        + repetition
        + recency_weight
        + pin_bonus
        + relevance_bonus
    )


def rank_memories(
    memories: list[UserMemory],
    now: datetime,
    turn_category_hint: str | None,
    limit: int,
) -> list[UserMemory]:
    active = [m for m in memories if (m.status or "active") == "active"]
    return sorted(active, key=lambda m: score(m, now, turn_category_hint), reverse=True)[:limit]
