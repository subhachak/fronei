from datetime import datetime, timedelta, timezone

from app.db.models import UserMemory
from app.services.memory_ranker import rank_memories, score


def _memory(**kwargs) -> UserMemory:
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    defaults = {
        "user_id": "u1",
        "content": "User prefers concise executive summaries.",
        "category": "communication_style",
        "scope": "style",
        "confidence": 0.8,
        "importance": 0.5,
        "seen_count": 1,
        "decay_rate": 0.01,
        "last_seen_at": now,
        "updated_at": now,
        "status": "active",
        "pinned": False,
    }
    defaults.update(kwargs)
    return UserMemory(**defaults)


def test_score_decays_with_age():
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    recent = _memory(last_seen_at=now)
    old = _memory(last_seen_at=now - timedelta(days=60), decay_rate=0.08)

    assert score(recent, now) > score(old, now)


def test_pinned_memory_gets_bonus():
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    pinned = _memory(pinned=True, confidence=0.6, importance=0.4)
    unpinned = _memory(pinned=False, confidence=0.6, importance=0.4)

    assert score(pinned, now) > score(unpinned, now)


def test_rank_memories_excludes_inactive_and_applies_hint():
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    work = _memory(content="User works on platform architecture.", category="work", scope="work")
    style = _memory(content="User likes concise answers.", category="communication_style", scope="style")
    archived = _memory(content="Old plan.", category="temporary_plan", status="archived")

    ranked = rank_memories([style, archived, work], now, "work", 2)

    assert ranked[0] is work
    assert archived not in ranked
