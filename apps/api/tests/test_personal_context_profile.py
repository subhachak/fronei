import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, UserMemory, UserProfile
from app.services.memory_consolidator import _archive_stale_memories
from app.services.personal_context import build_context, profile_brief


def _session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_profile_brief_is_compact_and_structured():
    brief = profile_brief({
        "role": "Enterprise Architect",
        "company": "Acme",
        "active_projects": ["AI workbench", "cloud deployment"],
        "key_preferences": ["direct answers"],
        "communication_style": {"directness": "high"},
    })

    assert "[profile] Enterprise Architect; Acme" in brief
    assert "[active projects] AI workbench; cloud deployment" in brief
    assert "[communication style] directness: high" in brief


def test_build_context_prepends_profile_before_memories():
    db = _session()
    try:
        db.add(UserProfile(
            user_id="u1",
            profile_json=json.dumps({"role": "Architect", "key_preferences": ["concise output"]}),
        ))
        db.add(UserMemory(
            user_id="u1",
            content="User prefers trade-off tables.",
            category="preference",
            scope="work",
            confidence=0.9,
            status="active",
        ))
        db.commit()

        context = build_context(db, "u1")

        assert context.startswith("USER PROFILE:")
        assert "concise output" in context
        assert "RANKED USER MEMORIES:" in context
        assert "trade-off tables" in context
    finally:
        db.close()


def test_archive_stale_memories_skips_pinned_and_archives_low_confidence():
    db = _session()
    try:
        now = datetime(2026, 1, 10, tzinfo=timezone.utc)
        stale = UserMemory(
            user_id="u1",
            content="Old uncertain project context.",
            category="project",
            confidence=0.3,
            decay_rate=0.2,
            last_seen_at=now - timedelta(days=30),
            status="active",
        )
        pinned = UserMemory(
            user_id="u1",
            content="Pinned context.",
            category="project",
            confidence=0.3,
            decay_rate=0.2,
            last_seen_at=now - timedelta(days=30),
            pinned=True,
            status="active",
        )
        db.add_all([stale, pinned])
        db.commit()

        archived = _archive_stale_memories(db, [stale, pinned], now)
        db.commit()
        db.refresh(stale)
        db.refresh(pinned)

        assert archived == 1
        assert stale.status == "archived"
        assert pinned.status == "active"
    finally:
        db.close()
