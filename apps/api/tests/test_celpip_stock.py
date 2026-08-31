"""The question buffer, and the ways background generation stopped.

Every case here is a real stall observed in a running instance: the buffer
filled once and then never refilled, and the jobs that were meant to refill it
sat in the queue looking permanently busy.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, CelpipQuestion, MaintenanceJob
from app.services import maintenance_jobs
from app.services.celpip import stock

USER = "admin_1"


@pytest.fixture
def db_factory(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(maintenance_jobs, "SessionLocal", factory)
    monkeypatch.setattr(stock, "SessionLocal", factory)
    # The worker is not running in tests; notify() would only wake it.
    monkeypatch.setattr(maintenance_jobs.maintenance_job_worker, "notify", lambda: None)
    return factory


def _question(db, question_id: str, *, task_key: str, status: str, updated_at=None, served=0):
    db.add(CelpipQuestion(
        id=question_id,
        skill="listening" if task_key.startswith("listening") else "writing",
        task_key=task_key,
        part=1,
        payload_json="{}",
        status=status,
        times_served=served,
        updated_at=updated_at or datetime.now(timezone.utc),
    ))
    db.commit()


# --- The stall that stopped generation for good -------------------------

def test_a_finished_job_does_not_block_the_next_one(db_factory):
    """The top-up dedupe key names a task type, which recurs forever. Returning
    the completed row meant the buffer filled exactly once and then never
    again -- background generation stopped permanently."""
    first = maintenance_jobs.enqueue_celpip_topup(user_id=USER, task_key="listening_news", count=2)

    db = db_factory()
    try:
        job = db.get(MaintenanceJob, first)
        job.status = "completed"
        job.completed_at = datetime.now(timezone.utc)
        job.attempt_count = 1
        db.commit()
    finally:
        db.close()

    second = maintenance_jobs.enqueue_celpip_topup(user_id=USER, task_key="listening_news", count=2)

    db = db_factory()
    try:
        job = db.get(MaintenanceJob, second)
        assert job.status == "queued", "a finished job must be runnable again"
        assert job.attempt_count == 0, "the retry budget resets with it"
        assert job.completed_at is None
        assert json.loads(job.payload_json)["task_key"] == "listening_news"
    finally:
        db.close()


def test_a_job_still_in_flight_is_not_duplicated(db_factory):
    first = maintenance_jobs.enqueue_celpip_topup(user_id=USER, task_key="writing_email", count=1)
    second = maintenance_jobs.enqueue_celpip_topup(user_id=USER, task_key="writing_email", count=1)
    assert first == second

    db = db_factory()
    try:
        assert db.query(MaintenanceJob).count() == 1
    finally:
        db.close()


def test_a_running_job_holding_a_live_lease_is_left_alone(db_factory):
    job_id = maintenance_jobs.enqueue_celpip_topup(user_id=USER, task_key="writing_email", count=1)
    db = db_factory()
    try:
        job = db.get(MaintenanceJob, job_id)
        job.status = "running"
        job.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        db.commit()
    finally:
        db.close()

    assert maintenance_jobs.enqueue_celpip_topup(
        user_id=USER, task_key="writing_email", count=1,
    ) == job_id
    db = db_factory()
    try:
        assert db.get(MaintenanceJob, job_id).status == "running", "still working; leave it"
    finally:
        db.close()


def test_a_job_orphaned_by_a_dead_worker_is_revived(db_factory):
    """A worker killed mid-job leaves a row that looks busy forever. Once its
    retry budget is spent the queue will never reclaim it, and the dedupe key
    blocks a replacement."""
    job_id = maintenance_jobs.enqueue_celpip_topup(user_id=USER, task_key="reading_information", count=1)
    db = db_factory()
    try:
        job = db.get(MaintenanceJob, job_id)
        job.status = "running"
        job.attempt_count = 3
        job.max_attempts = 3
        job.lease_owner = "dead-worker"
        job.lease_expires_at = datetime.now(timezone.utc) - timedelta(hours=2)
        db.commit()
    finally:
        db.close()

    maintenance_jobs.enqueue_celpip_topup(user_id=USER, task_key="reading_information", count=1)

    db = db_factory()
    try:
        job = db.get(MaintenanceJob, job_id)
        assert job.status == "queued"
        assert job.attempt_count == 0
        assert job.lease_owner is None
    finally:
        db.close()


def test_a_failed_asset_build_can_be_retried(db_factory):
    """This is what the Rebuild assets button does; it was silently a no-op."""
    job_id = maintenance_jobs.enqueue_celpip_assets(question_id="cq_1")
    db = db_factory()
    try:
        job = db.get(MaintenanceJob, job_id)
        job.status = "failed"
        job.error_message = "text-to-speech failed"
        db.commit()
    finally:
        db.close()

    maintenance_jobs.enqueue_celpip_assets(question_id="cq_1")
    db = db_factory()
    try:
        job = db.get(MaintenanceJob, job_id)
        assert job.status == "queued"
        assert job.error_message is None
    finally:
        db.close()


# --- The stall that hid the deficit -------------------------------------

def test_a_stalled_item_stops_counting_as_work_in_progress(db_factory):
    """There is no terminal asset state: a failed build leaves its question in
    `awaiting_assets` forever. Counted as building, one dead item masks the
    deficit for its whole task type and generation never resumes."""
    db = db_factory()
    try:
        _question(
            db, "cq_stuck", task_key="listening_news", status="awaiting_assets",
            updated_at=datetime.now(timezone.utc) - timedelta(hours=3),
        )
        assert stock.building_counts(db, ["listening_news"])["listening_news"] == 0
        assert stock.deficits(db, ["listening_news"])["listening_news"] == stock.target_stock()
        assert stock.stalled_items(db, ["listening_news"]) == ["cq_stuck"]
    finally:
        db.close()


def test_an_item_still_building_does_count(db_factory):
    """Otherwise every check would queue another top-up for work already in
    flight, and one launch would trigger a generation storm."""
    db = db_factory()
    try:
        _question(db, "cq_building", task_key="listening_news", status="awaiting_assets")
        assert stock.building_counts(db, ["listening_news"])["listening_news"] == 1
        assert stock.stalled_items(db, ["listening_news"]) == []
    finally:
        db.close()


def test_topup_retries_the_asset_builds_that_died(db_factory):
    db = db_factory()
    try:
        _question(
            db, "cq_stuck", task_key="listening_news", status="awaiting_assets",
            updated_at=datetime.now(timezone.utc) - timedelta(hours=3),
        )
    finally:
        db.close()

    result = stock.plan_topup(user_id=USER, task_keys=["listening_news"])
    assert "cq_stuck" in result["retried_assets"]

    db = db_factory()
    try:
        job = db.query(MaintenanceJob).filter(
            MaintenanceJob.dedupe_key == "celpip_assets:cq_stuck"
        ).one()
        assert job.status == "queued"
    finally:
        db.close()


def test_served_questions_do_not_count_as_stock(db_factory):
    db = db_factory()
    try:
        _question(db, "cq_used", task_key="writing_email", status="retired", served=1)
        _question(db, "cq_free", task_key="writing_email", status="ready", served=0)
        assert stock.stock_levels(db, ["writing_email"])["writing_email"] == 1
    finally:
        db.close()


def test_topup_asks_only_for_what_is_missing(db_factory):
    db = db_factory()
    try:
        _question(db, "cq_free", task_key="writing_email", status="ready", served=0)
    finally:
        db.close()

    result = stock.plan_topup(user_id=USER, task_keys=["writing_email"])
    assert result["deficits"]["writing_email"] == stock.target_stock() - 1
    assert "writing_email" in result["queued"]
