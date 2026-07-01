from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, Event, Turn
from app.services.agent import persistence
from app.services.agent.models import Goal, TurnRequest, TurnResult


def _session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _enqueue(Session, monkeypatch, *, turn_id: str = "turn_1", max_attempts: int = 3):
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    goal = Goal(user_id="u1", conversation_id=None, objective="durable task", route="direct")
    request = TurnRequest(message="durable task")
    persistence.enqueue_turn(goal, turn_id, request, max_attempts=max_attempts)


def test_turn_lease_can_only_be_claimed_once(monkeypatch):
    Session = _session()
    _enqueue(Session, monkeypatch)

    first = persistence.claim_next_turn("worker-a", lease_seconds=60)
    second = persistence.claim_next_turn("worker-b", lease_seconds=60)

    assert first is not None
    assert first[:2] == ("turn_1", "u1")
    assert second is None
    with Session() as db:
        row = db.get(Turn, "turn_1")
        assert row.status == "running"
        assert row.attempt_count == 1
        assert row.lease_owner == "worker-a"


def test_expired_turn_lease_is_reclaimed(monkeypatch):
    Session = _session()
    _enqueue(Session, monkeypatch)
    assert persistence.claim_next_turn("worker-a", lease_seconds=60)

    with Session() as db:
        row = db.get(Turn, "turn_1")
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

    reclaimed = persistence.claim_next_turn("worker-b", lease_seconds=60)

    assert reclaimed is not None
    with Session() as db:
        row = db.get(Turn, "turn_1")
        event = db.query(Event).filter(Event.turn_id == "turn_1", Event.stage == "job_reclaimed").one()
        assert row.status == "running"
        assert row.attempt_count == 2
        assert row.lease_owner == "worker-b"
        assert "previous worker stopped responding" in event.message


def test_failed_attempt_requeues_then_exhausts_retry_budget(monkeypatch):
    Session = _session()
    _enqueue(Session, monkeypatch, max_attempts=2)
    assert persistence.claim_next_turn("worker-a", lease_seconds=60)

    assert persistence.fail_or_requeue_turn("turn_1", "worker-a", "first failure") == "queued"
    assert persistence.claim_next_turn("worker-b", lease_seconds=60)
    assert persistence.fail_or_requeue_turn("turn_1", "worker-b", "second failure") == "failed"

    with Session() as db:
        row = db.get(Turn, "turn_1")
        assert row.status == "failed"
        assert row.attempt_count == 2
        assert row.error_message == "second failure"
        assert row.lease_owner is None


def test_cancellation_is_owned_and_terminal(monkeypatch):
    Session = _session()
    _enqueue(Session, monkeypatch)
    assert persistence.claim_next_turn("worker-a", lease_seconds=60)

    assert persistence.request_turn_cancellation("turn_1", "other-user") is False
    assert persistence.request_turn_cancellation("turn_1", "u1") is True
    assert persistence.turn_cancel_requested("turn_1", "worker-a") is True
    assert persistence.fail_or_requeue_turn("turn_1", "worker-a", "cancelled") == "cancelled"

    with Session() as db:
        row = db.get(Turn, "turn_1")
        assert row.status == "cancelled"
        assert row.completed_at is not None


def test_stale_worker_cannot_complete_reclaimed_turn(monkeypatch):
    Session = _session()
    _enqueue(Session, monkeypatch)
    assert persistence.claim_next_turn("worker-a", lease_seconds=60)

    with Session() as db:
        row = db.get(Turn, "turn_1")
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    assert persistence.claim_next_turn("worker-b", lease_seconds=60)

    goal = Goal(user_id="u1", objective="durable task", route="direct")
    stale_result = TurnResult(
        turn_id="turn_1",
        goal=goal,
        answer="stale",
        route="direct",
    )
    fresh_result = stale_result.model_copy(update={"answer": "fresh"})

    assert persistence.complete_turn(stale_result, lease_owner="worker-a") is False
    assert persistence.complete_turn(fresh_result, lease_owner="worker-b") is True
    with Session() as db:
        row = db.get(Turn, "turn_1")
        assert row.status == "completed"
        assert row.answer == "fresh"


def test_duplicate_start_event_does_not_reopen_completed_turn(monkeypatch):
    Session = _session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    goal = Goal(user_id="u1", conversation_id=None, objective="durable task", route="direct")
    with Session() as db:
        db.add(Turn(
            id="turn_1",
            user_id="u1",
            conversation_id=None,
            objective="durable task",
            route="direct",
            quality_mode="standard",
            status="completed",
        ))
        db.commit()

    persistence.create_turn(goal, "turn_1")

    with Session() as db:
        assert db.get(Turn, "turn_1").status == "completed"
