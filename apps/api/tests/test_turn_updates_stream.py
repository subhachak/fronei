from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import get_current_user_id
from app.db.models import Base, Event, Turn
from app.main import app
from app.services.agent import persistence


def _session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _seed_terminal_turn(Session, *, status: str = "completed"):
    now = datetime.now(timezone.utc)
    with Session() as db:
        db.add(Turn(
            id="turn_1",
            user_id="u1",
            conversation_id=None,
            objective="Stream updates",
            route="direct",
            quality_mode="standard",
            status=status,
            answer="Done" if status == "completed" else "",
            error_message="Cancelled" if status == "cancelled" else None,
            created_at=now,
            updated_at=now,
            completed_at=now,
        ))
        db.add_all([
            Event(
                id="event_1",
                turn_id="turn_1",
                stage="planning",
                message="Planning",
                data_json="{}",
                created_at=now - timedelta(seconds=2),
            ),
            Event(
                id="event_2",
                turn_id="turn_1",
                stage="writing",
                message="Writing",
                data_json="{}",
                created_at=now - timedelta(seconds=1),
            ),
        ])
        db.commit()


def test_turn_stream_replays_events_and_terminal_snapshot(monkeypatch):
    Session = _session()
    _seed_terminal_turn(Session)
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    app.dependency_overrides[get_current_user_id] = lambda: "u1"
    try:
        with TestClient(app) as client:
            response = client.get("/turns/turn_1/stream")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "id: event_1\nevent: progress" in response.text
        assert "id: event_2\nevent: progress" in response.text
        assert "event: turn" in response.text
        assert '"status": "completed"' in response.text
    finally:
        app.dependency_overrides.clear()


def test_turn_stream_resumes_after_last_event_id(monkeypatch):
    Session = _session()
    _seed_terminal_turn(Session)
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    app.dependency_overrides[get_current_user_id] = lambda: "u1"
    try:
        with TestClient(app) as client:
            response = client.get(
                "/turns/turn_1/stream",
                headers={"Last-Event-ID": "event_1"},
            )
        assert response.status_code == 200
        assert "id: event_1" not in response.text
        assert "id: event_2" in response.text
        assert "event: turn" in response.text
    finally:
        app.dependency_overrides.clear()


def test_turn_stream_is_user_isolated(monkeypatch):
    Session = _session()
    _seed_terminal_turn(Session)
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    app.dependency_overrides[get_current_user_id] = lambda: "u2"
    try:
        with TestClient(app) as client:
            response = client.get("/turns/turn_1/stream")
        assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_cancelled_turn_stream_emits_terminal_status(monkeypatch):
    Session = _session()
    _seed_terminal_turn(Session, status="cancelled")
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    app.dependency_overrides[get_current_user_id] = lambda: "u1"
    try:
        with TestClient(app) as client:
            response = client.get("/turns/turn_1/stream?after=event_2")
        assert response.status_code == 200
        assert "event: progress" not in response.text
        assert "event: turn" in response.text
        assert '"status": "cancelled"' in response.text
    finally:
        app.dependency_overrides.clear()
