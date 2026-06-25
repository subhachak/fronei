from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import AdminAuditLog, Base, Turn
from app.observability import JsonLogFormatter, log_event
from app.routers import admin as admin_router


def _session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _turn(turn_id: str, status: str, **values) -> Turn:
    now = datetime.now(timezone.utc)
    return Turn(
        id=turn_id,
        user_id="u1",
        conversation_id=None,
        objective=f"Objective for {turn_id}",
        route="research",
        quality_mode="standard",
        status=status,
        created_at=now,
        updated_at=now,
        **values,
    )


def test_admin_jobs_reports_queue_health(monkeypatch):
    Session = _session()
    monkeypatch.setattr(admin_router, "SessionLocal", Session)
    monkeypatch.setattr(admin_router.turn_job_worker, "status", lambda: {
        "configured_concurrency": 2,
        "live_threads": 2,
    })
    with Session() as db:
        db.add_all([
            _turn("queued", "queued"),
            _turn(
                "stale",
                "running",
                attempt_count=2,
                lease_owner="worker-a",
                lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=10),
            ),
            _turn("failed", "failed", attempt_count=3, max_attempts=3, error_message="provider failed"),
        ])
        db.commit()

    response = admin_router.jobs(
        status=None,
        limit=50,
        offset=0,
        admin=admin_router.AdminPrincipal(user_id="admin"),
    )

    assert response["summary"]["queued"] == 1
    assert response["summary"]["running"] == 1
    assert response["summary"]["failed"] == 1
    assert response["summary"]["stale_leases"] == 1
    assert response["summary"]["retried_jobs"] == 2
    assert response["summary"]["retry_exhausted"] == 1
    assert response["summary"]["worker"]["live_threads"] == 2
    assert response["total"] == 3


def test_admin_can_cancel_queued_job_and_audit(monkeypatch):
    Session = _session()
    monkeypatch.setattr(admin_router, "SessionLocal", Session)
    monkeypatch.setattr(admin_router.turn_job_worker, "notify", lambda: None)
    with Session() as db:
        db.add(_turn("queued", "queued"))
        db.commit()

    response = admin_router.cancel_job(
        "queued",
        admin_router.AdminJobCancelRequest(reason="operator request"),
        admin=admin_router.AdminPrincipal(user_id="admin"),
    )

    assert response["status"] == "cancelled"
    with Session() as db:
        turn = db.get(Turn, "queued")
        assert turn.cancel_requested is True
        assert turn.completed_at is not None
        audit = db.query(AdminAuditLog).one()
        assert audit.action == "job.cancel"
        assert json.loads(audit.details_json)["turn_id"] == "queued"


def test_json_log_formatter_includes_structured_fields():
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="turn_job_claimed",
        args=(),
        exc_info=None,
    )
    record.event = "turn_job_claimed"
    record.turn_id = "turn_1"
    payload = json.loads(JsonLogFormatter().format(record))

    assert payload["message"] == "turn_job_claimed"
    assert payload["event"] == "turn_job_claimed"
    assert payload["turn_id"] == "turn_1"


def test_log_event_passes_fields(caplog):
    logger = logging.getLogger("test.observability")
    with caplog.at_level(logging.INFO, logger=logger.name):
        log_event(logger, logging.INFO, "turn_job_completed", turn_id="turn_1")

    assert caplog.records[0].event == "turn_job_completed"
    assert caplog.records[0].turn_id == "turn_1"
