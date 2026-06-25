from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, MaintenanceJob
from app.services import maintenance_jobs


def _session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _wire(monkeypatch):
    Session = _session()
    monkeypatch.setattr(maintenance_jobs, "SessionLocal", Session)
    return Session


def test_profile_job_enqueue_is_idempotent_while_active(monkeypatch):
    Session = _wire(monkeypatch)

    first, created = maintenance_jobs.enqueue_profile_consolidation()
    second, created_again = maintenance_jobs.enqueue_profile_consolidation()

    assert created is True
    assert created_again is False
    assert second["id"] == first["id"]
    with Session() as db:
        assert db.query(MaintenanceJob).count() == 1


def test_expired_maintenance_lease_is_reclaimed(monkeypatch):
    Session = _wire(monkeypatch)
    job, _ = maintenance_jobs.enqueue_profile_consolidation()
    assert maintenance_jobs.claim_next_job("worker-a", lease_seconds=60)

    with Session() as db:
        row = db.get(MaintenanceJob, job["id"])
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

    reclaimed = maintenance_jobs.claim_next_job("worker-b", lease_seconds=60)

    assert reclaimed is not None
    with Session() as db:
        row = db.get(MaintenanceJob, job["id"])
        assert row.attempt_count == 2
        assert row.lease_owner == "worker-b"


def test_maintenance_retry_exhaustion_and_stale_worker_fencing(monkeypatch):
    Session = _wire(monkeypatch)
    monkeypatch.setenv("MAINTENANCE_WORKER_MAX_ATTEMPTS", "2")
    maintenance_jobs.get_settings.cache_clear()
    try:
        job, _ = maintenance_jobs.enqueue_profile_consolidation()
        assert maintenance_jobs.claim_next_job("worker-a", lease_seconds=60)
        assert maintenance_jobs.fail_or_requeue_job(job["id"], "worker-a", "first") == "queued"
        assert maintenance_jobs.claim_next_job("worker-b", lease_seconds=60)
        assert maintenance_jobs.complete_job(job["id"], "worker-a", {"stale": True}) is False
        assert maintenance_jobs.fail_or_requeue_job(job["id"], "worker-b", "second") == "failed"
    finally:
        maintenance_jobs.get_settings.cache_clear()

    with Session() as db:
        row = db.get(MaintenanceJob, job["id"])
        assert row.status == "failed"
        assert row.attempt_count == 2
        assert row.error_message == "second"


def test_worker_executes_profile_consolidation_job(monkeypatch):
    Session = _wire(monkeypatch)
    job, _ = maintenance_jobs.enqueue_profile_consolidation(lookback_days=14, max_workspaces=25)
    claimed = maintenance_jobs.claim_next_job("worker-a", lease_seconds=60)
    monkeypatch.setattr(
        maintenance_jobs,
        "consolidate_active_workspace_backlog",
        lambda **kwargs: {"workspaces_considered": 2, "consolidated": 2, **kwargs},
    )

    assert claimed is not None
    job_id, job_type, payload = claimed
    result = maintenance_jobs.execute_job(job_type, payload)
    assert maintenance_jobs.complete_job(job_id, "worker-a", result) is True

    with Session() as db:
        row = db.get(MaintenanceJob, job["id"])
        assert row.status == "completed"
        assert '"consolidated": 2' in row.result_json


def test_profile_consolidation_failure_raises_for_job_retry(monkeypatch):
    monkeypatch.setattr(
        maintenance_jobs,
        "consolidate_active_workspace_backlog",
        lambda **_kwargs: {"workspaces_considered": 2, "consolidated": 1, "failed": 1},
    )

    try:
        maintenance_jobs.execute_job(
            maintenance_jobs.PROFILE_CONSOLIDATION_JOB,
            {"lookback_days": 30, "max_workspaces": 500},
        )
    except RuntimeError as exc:
        assert "failed for 1 workspace" in str(exc)
    else:
        raise AssertionError("Expected workspace failures to trigger a job retry")
