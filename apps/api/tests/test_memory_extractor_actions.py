from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, UserMemory
from app.services.memory_extractor import apply_actions
from app.services.personal_context import build_context


def _session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_apply_actions_new_and_reinforce():
    db = _session()
    try:
        apply_actions(db, "u1", 1, [
            {
                "action": "new",
                "content": "User prefers direct answers.",
                "category": "communication_style",
                "scope": "style",
                "source": "stated",
                "confidence": 0.8,
                "importance": 0.7,
            }
        ])
        db.commit()
        memory = db.query(UserMemory).one()

        apply_actions(db, "u1", 1, [{"action": "reinforce", "memory_id": memory.id}])
        db.commit()
        db.refresh(memory)

        assert memory.seen_count == 2
        assert memory.confidence > 0.8
        assert memory.importance > 0.7
    finally:
        db.close()


def test_update_respects_pinned_memory():
    db = _session()
    try:
        memory = UserMemory(
            user_id="u1",
            content="User works in banking.",
            category="work",
            pinned=True,
        )
        db.add(memory)
        db.commit()

        apply_actions(db, "u1", 1, [{
            "action": "update",
            "memory_id": memory.id,
            "content": "User works in retail.",
            "confidence": 0.9,
        }])
        db.commit()
        db.refresh(memory)

        assert memory.content == "User works in banking."
    finally:
        db.close()


def test_contradict_supersedes_unpinned_memory():
    db = _session()
    try:
        old = UserMemory(
            user_id="u1",
            content="User is evaluating Render.",
            category="project",
            scope="project",
            pinned=False,
        )
        db.add(old)
        db.commit()

        apply_actions(db, "u1", 1, [{
            "action": "contradict",
            "memory_id": old.id,
            "content": "User selected Railway for deployment.",
            "confidence": 0.8,
        }])
        db.commit()
        db.refresh(old)
        active = db.query(UserMemory).filter(UserMemory.status == "active").one()

        assert old.status == "superseded"
        assert old.superseded_by_id == active.id
        assert active.content == "User selected Railway for deployment."
    finally:
        db.close()


def test_build_context_excludes_archived_and_marks_uncertain():
    db = _session()
    try:
        db.add(UserMemory(
            user_id="u1",
            content="User prefers concise answers.",
            category="communication_style",
            scope="style",
            confidence=0.9,
            status="active",
        ))
        db.add(UserMemory(
            user_id="u1",
            content="User may be comparing deployment hosts.",
            category="project",
            scope="project",
            confidence=0.4,
            status="active",
        ))
        db.add(UserMemory(
            user_id="u1",
            content="Archived context.",
            category="project",
            status="archived",
        ))
        db.commit()

        context = build_context(db, "u1", limit=10)

        assert "concise answers" in context
        assert "may be comparing" in context
        assert "(uncertain)" in context
        assert "Archived context" not in context
    finally:
        db.close()
