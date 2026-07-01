from __future__ import annotations

import json
import logging
import socket
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db.models import LangGraphRunContext, MaintenanceJob, SessionLocal
from app.observability import log_event
from app.services.agent.profile_consolidator import consolidate_active_workspace_backlog

logger = logging.getLogger(__name__)
PROFILE_CONSOLIDATION_JOB = "profile_consolidation"
LANGGRAPH_CHECKPOINT_CLEANUP_JOB = "langgraph_checkpoint_cleanup"

# langgraph_run_contexts.status values that must never be touched by cleanup
# (a run in one of these states may still resume and needs its checkpoint).
_LANGGRAPH_RETAINED_STATUSES = ("paused", "running", "resuming")
_LANGGRAPH_CLEANUP_ELIGIBLE_STATUSES = ("completed", "failed", "orphaned")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _loads(value: str | None) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
        return payload if isinstance(payload, dict) else {}
    except (TypeError, ValueError):
        return {}


def _payload(job: MaintenanceJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "job_type": job.job_type,
        "dedupe_key": job.dedupe_key,
        "status": job.status,
        "payload": _loads(job.payload_json),
        "result": _loads(job.result_json),
        "attempt_count": job.attempt_count,
        "max_attempts": job.max_attempts,
        "lease_owner": job.lease_owner,
        "lease_expires_at": job.lease_expires_at.isoformat() if job.lease_expires_at else None,
        "heartbeat_at": job.heartbeat_at.isoformat() if job.heartbeat_at else None,
        "error_message": job.error_message,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def enqueue_profile_consolidation(
    *,
    lookback_days: int = 30,
    max_workspaces: int = 500,
) -> tuple[dict[str, Any], bool]:
    db = SessionLocal()
    try:
        dedupe_key = f"{PROFILE_CONSOLIDATION_JOB}:{_now().date().isoformat()}"
        same_run = (
            db.query(MaintenanceJob)
            .filter(MaintenanceJob.dedupe_key == dedupe_key)
            .first()
        )
        if same_run:
            return _payload(same_run), False
        existing = (
            db.query(MaintenanceJob)
            .filter(
                MaintenanceJob.job_type == PROFILE_CONSOLIDATION_JOB,
                MaintenanceJob.status.in_(("queued", "running")),
            )
            .order_by(MaintenanceJob.created_at.asc())
            .first()
        )
        if existing:
            return _payload(existing), False
        settings = get_settings()
        now = _now()
        job = MaintenanceJob(
            id=f"maintenance_{uuid.uuid4().hex[:24]}",
            job_type=PROFILE_CONSOLIDATION_JOB,
            dedupe_key=dedupe_key,
            status="queued",
            payload_json=json.dumps({
                "lookback_days": max(1, min(365, lookback_days)),
                "max_workspaces": max(1, min(5000, max_workspaces)),
            }),
            result_json="{}",
            max_attempts=max(1, settings.maintenance_worker_max_attempts),
            created_at=now,
            updated_at=now,
        )
        db.add(job)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raced = (
                db.query(MaintenanceJob)
                .filter(MaintenanceJob.dedupe_key == dedupe_key)
                .one()
            )
            return _payload(raced), False
        db.refresh(job)
        return _payload(job), True
    finally:
        db.close()


def enqueue_langgraph_checkpoint_cleanup(
    *,
    retention_days: int | None = None,
) -> tuple[dict[str, Any], bool]:
    """Idempotently enqueue the LangGraph checkpoint/run-context cleanup job.

    Mirrors enqueue_profile_consolidation: a daily dedupe_key means repeated
    triggers (e.g. an external cron firing more than once) collapse onto the
    same queued/running job for the day instead of piling up duplicates.
    """
    db = SessionLocal()
    try:
        dedupe_key = f"{LANGGRAPH_CHECKPOINT_CLEANUP_JOB}:{_now().date().isoformat()}"
        same_run = (
            db.query(MaintenanceJob)
            .filter(MaintenanceJob.dedupe_key == dedupe_key)
            .first()
        )
        if same_run:
            return _payload(same_run), False
        existing = (
            db.query(MaintenanceJob)
            .filter(
                MaintenanceJob.job_type == LANGGRAPH_CHECKPOINT_CLEANUP_JOB,
                MaintenanceJob.status.in_(("queued", "running")),
            )
            .order_by(MaintenanceJob.created_at.asc())
            .first()
        )
        if existing:
            return _payload(existing), False
        settings = get_settings()
        now = _now()
        job = MaintenanceJob(
            id=f"maintenance_{uuid.uuid4().hex[:24]}",
            job_type=LANGGRAPH_CHECKPOINT_CLEANUP_JOB,
            dedupe_key=dedupe_key,
            status="queued",
            payload_json=json.dumps({
                "retention_days": max(
                    1,
                    int(retention_days if retention_days is not None else settings.langgraph_checkpoint_retention_days),
                ),
            }),
            result_json="{}",
            max_attempts=max(1, settings.maintenance_worker_max_attempts),
            created_at=now,
            updated_at=now,
        )
        db.add(job)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raced = (
                db.query(MaintenanceJob)
                .filter(MaintenanceJob.dedupe_key == dedupe_key)
                .one()
            )
            return _payload(raced), False
        db.refresh(job)
        return _payload(job), True
    finally:
        db.close()


def get_job(job_id: str) -> dict[str, Any] | None:
    db = SessionLocal()
    try:
        job = db.get(MaintenanceJob, job_id)
        return _payload(job) if job else None
    finally:
        db.close()


def claim_next_job(worker_id: str, *, lease_seconds: int) -> tuple[str, str, dict[str, Any]] | None:
    db = SessionLocal()
    try:
        now = _now()
        candidates = (
            db.query(MaintenanceJob)
            .filter(
                MaintenanceJob.attempt_count < MaintenanceJob.max_attempts,
                (
                    (MaintenanceJob.status == "queued")
                    | (
                        (MaintenanceJob.status == "running")
                        & MaintenanceJob.lease_expires_at.isnot(None)
                        & (MaintenanceJob.lease_expires_at < now)
                    )
                ),
            )
            .order_by(MaintenanceJob.created_at.asc())
            .limit(8)
            .all()
        )
        for candidate in candidates:
            previous_attempt = int(candidate.attempt_count or 0)
            updated = (
                db.query(MaintenanceJob)
                .filter(
                    MaintenanceJob.id == candidate.id,
                    MaintenanceJob.attempt_count == previous_attempt,
                    (
                        (MaintenanceJob.status == "queued")
                        | (
                            (MaintenanceJob.status == "running")
                            & MaintenanceJob.lease_expires_at.isnot(None)
                            & (MaintenanceJob.lease_expires_at < now)
                        )
                    ),
                )
                .update(
                    {
                        MaintenanceJob.status: "running",
                        MaintenanceJob.attempt_count: previous_attempt + 1,
                        MaintenanceJob.lease_owner: worker_id,
                        MaintenanceJob.lease_expires_at: now + timedelta(seconds=max(10, lease_seconds)),
                        MaintenanceJob.heartbeat_at: now,
                        MaintenanceJob.error_message: None,
                        MaintenanceJob.updated_at: now,
                    },
                    synchronize_session=False,
                )
            )
            if not updated:
                db.rollback()
                continue
            db.commit()
            return candidate.id, candidate.job_type, _loads(candidate.payload_json)
        return None
    finally:
        db.close()


def renew_job_lease(job_id: str, worker_id: str, *, lease_seconds: int) -> bool:
    db = SessionLocal()
    try:
        now = _now()
        updated = (
            db.query(MaintenanceJob)
            .filter(
                MaintenanceJob.id == job_id,
                MaintenanceJob.status == "running",
                MaintenanceJob.lease_owner == worker_id,
            )
            .update(
                {
                    MaintenanceJob.heartbeat_at: now,
                    MaintenanceJob.lease_expires_at: now + timedelta(seconds=max(10, lease_seconds)),
                    MaintenanceJob.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return bool(updated)
    finally:
        db.close()


def complete_job(job_id: str, worker_id: str, result: dict[str, Any]) -> bool:
    db = SessionLocal()
    try:
        now = _now()
        updated = (
            db.query(MaintenanceJob)
            .filter(
                MaintenanceJob.id == job_id,
                MaintenanceJob.status == "running",
                MaintenanceJob.lease_owner == worker_id,
            )
            .update(
                {
                    MaintenanceJob.status: "completed",
                    MaintenanceJob.result_json: json.dumps(result),
                    MaintenanceJob.lease_owner: None,
                    MaintenanceJob.lease_expires_at: None,
                    MaintenanceJob.heartbeat_at: None,
                    MaintenanceJob.error_message: None,
                    MaintenanceJob.updated_at: now,
                    MaintenanceJob.completed_at: now,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return bool(updated)
    finally:
        db.close()


def fail_or_requeue_job(job_id: str, worker_id: str, message: str) -> str:
    db = SessionLocal()
    try:
        job = db.get(MaintenanceJob, job_id)
        if job is None or job.lease_owner != worker_id:
            return "lost"
        now = _now()
        if job.attempt_count < job.max_attempts:
            job.status = "queued"
            outcome = "queued"
        else:
            job.status = "failed"
            job.completed_at = now
            outcome = "failed"
        job.error_message = message[:2000]
        job.lease_owner = None
        job.lease_expires_at = None
        job.heartbeat_at = None
        job.updated_at = now
        db.commit()
        return outcome
    finally:
        db.close()


def _reconcile_stale_paused_langgraph_run(row: LangGraphRunContext) -> bool:
    """Gap 4: cross-check a 'paused' langgraph_run_contexts row against the
    LangGraph checkpoint (source of truth for pause state — see
    pending_langgraph_pause, which already trusts get_state() over this
    status column for the same reason). If the checkpoint no longer shows a
    pending interrupt (resumed via a path that didn't update this row, or
    the checkpoint was deleted), correct the row's status to 'orphaned' and
    log a warning. Returns True if the row was corrected.
    """
    from app.services.agent.langgraph_runtime import pending_langgraph_pause

    try:
        still_paused = pending_langgraph_pause(row.run_id) is not None
    except Exception:
        logger.exception(
            "Could not verify checkpoint state for paused langgraph run_id=%s; leaving status as-is",
            row.run_id,
        )
        return False

    if still_paused:
        return False

    logger.warning(
        "langgraph_run_contexts row run_id=%s claims status='paused' but its checkpoint no "
        "longer shows a pending interrupt (resumed via a path that skipped this row, or the "
        "checkpoint was deleted) — correcting status to 'orphaned'.",
        row.run_id,
    )
    row.status = "orphaned"
    row.updated_at = _now()
    row.completed_at = row.updated_at
    return True


def cleanup_langgraph_checkpoints(*, retention_days: int) -> dict[str, Any]:
    """Gap 2 + Gap 4 maintenance pass.

    Gap 2: delete the LangGraph checkpoint (langgraph_checkpoints.db) and the
    langgraph_run_contexts row for every run that finished (status in
    'completed'/'failed') more than `retention_days` ago. Rows with status
    'paused', 'running', or 'resuming' are never touched here regardless of
    age — a paused run may still be waiting on a human, and a running/
    resuming run may still be in flight.

    We delete rather than archive: the checkpoint row and the run_context row
    are two halves of the same disposable execution record (full node-by-node
    state history + a request/tool-config snapshot), not an audit trail —
    Turn/Event rows in the main app DB are the durable record of what a user
    asked and what they got back. Keeping a growing archive table would just
    move the unbounded-growth problem rather than solve it.

    Gap 4: for every row still claiming status='paused' (regardless of age),
    verify against the LangGraph checkpoint that it's actually still paused;
    correct drifted rows to 'orphaned'.
    """
    from app.services.agent.langgraph_runtime.checkpointer import get_checkpointer

    cutoff = _now() - timedelta(days=max(0, retention_days))
    deleted_run_ids: list[str] = []
    reconciled_run_ids: list[str] = []

    db = SessionLocal()
    try:
        stale = (
            db.query(LangGraphRunContext)
            .filter(
                LangGraphRunContext.status.in_(_LANGGRAPH_CLEANUP_ELIGIBLE_STATUSES),
                LangGraphRunContext.completed_at.isnot(None),
                LangGraphRunContext.completed_at < cutoff,
            )
            .all()
        )
        checkpointer = get_checkpointer()
        for row in stale:
            run_id = row.run_id
            try:
                checkpointer.delete_thread(run_id)
            except Exception:
                logger.exception(
                    "Failed to delete LangGraph checkpoint thread for run_id=%s; leaving "
                    "langgraph_run_contexts row in place so cleanup can retry next pass.",
                    run_id,
                )
                continue
            db.delete(row)
            deleted_run_ids.append(run_id)
        if deleted_run_ids:
            db.commit()

        paused_rows = (
            db.query(LangGraphRunContext)
            .filter(LangGraphRunContext.status == "paused")
            .all()
        )
        for row in paused_rows:
            if _reconcile_stale_paused_langgraph_run(row):
                reconciled_run_ids.append(row.run_id)
        if reconciled_run_ids:
            db.commit()
    finally:
        db.close()

    return {
        "retention_days": retention_days,
        "deleted_count": len(deleted_run_ids),
        "deleted_run_ids": deleted_run_ids,
        "reconciled_paused_to_orphaned_count": len(reconciled_run_ids),
        "reconciled_run_ids": reconciled_run_ids,
    }


def reconcile_orphaned_langgraph_runs() -> dict[str, Any]:
    """Gap 3: on process startup, mark langgraph_run_contexts rows still
    showing status in ('running', 'resuming') from before this process
    started as 'orphaned' — mirrors _mark_orphaned_eval_runs in app/main.py
    (same startup-hook pattern: query stale in-flight status, flip it,
    commit, log a warning with the affected ids). Those runs' in-process
    state (_RUN_CONTEXTS cache, any in-flight graph.stream()/invoke() call)
    is gone with the old process; the row would otherwise stay 'running' or
    'resuming' forever and make an admin "list active runs" query lie.
    """
    db = SessionLocal()
    try:
        orphans = (
            db.query(LangGraphRunContext)
            .filter(LangGraphRunContext.status.in_(("running", "resuming")))
            .all()
        )
        now = _now()
        for row in orphans:
            row.status = "orphaned"
            row.updated_at = now
            row.completed_at = now
        if orphans:
            db.commit()
            logger.warning(
                "Marked %d orphaned langgraph_run_contexts row(s) as 'orphaned' on startup: %s",
                len(orphans),
                [row.run_id for row in orphans],
            )
        return {"orphaned_count": len(orphans), "orphaned_run_ids": [row.run_id for row in orphans]}
    finally:
        db.close()


def execute_job(job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if job_type == PROFILE_CONSOLIDATION_JOB:
        result = consolidate_active_workspace_backlog(
            lookback_days=int(payload.get("lookback_days") or 30),
            max_workspaces=int(payload.get("max_workspaces") or 500),
        )
        result["outcome"] = (
            "partial_success"
            if int(result.get("failed") or 0) > 0
            else "success"
        )
        return result
    if job_type == LANGGRAPH_CHECKPOINT_CLEANUP_JOB:
        settings = get_settings()
        result = cleanup_langgraph_checkpoints(
            retention_days=int(payload.get("retention_days") or settings.langgraph_checkpoint_retention_days),
        )
        result["outcome"] = "success"
        return result
    raise ValueError(f"Unsupported maintenance job type: {job_type}")


class MaintenanceJobWorker:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            worker_id = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}:maintenance"
            self._thread = threading.Thread(
                target=self._run,
                args=(worker_id,),
                name="maintenance-worker",
                daemon=True,
            )
            self._thread.start()
            log_event(logger, logging.INFO, "maintenance_worker_started", worker_id=worker_id)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        self._thread = None
        log_event(logger, logging.INFO, "maintenance_worker_stopped")

    def notify(self) -> None:
        self._wake.set()

    def _run(self, worker_id: str) -> None:
        settings = get_settings()
        while not self._stop.is_set():
            try:
                claimed = claim_next_job(
                    worker_id,
                    lease_seconds=settings.maintenance_worker_lease_seconds,
                )
            except Exception:
                logger.exception("Maintenance worker %s could not claim work", worker_id)
                self._wait(settings.maintenance_worker_poll_seconds)
                continue
            if claimed is None:
                self._wait(settings.maintenance_worker_poll_seconds)
                continue
            job_id, job_type, payload = claimed
            self._execute(worker_id, job_id, job_type, payload)

    def _wait(self, seconds: float) -> None:
        self._wake.wait(timeout=max(0.1, seconds))
        self._wake.clear()

    def _execute(self, worker_id: str, job_id: str, job_type: str, payload: dict[str, Any]) -> None:
        settings = get_settings()
        heartbeat_stop = threading.Event()

        def heartbeat() -> None:
            interval = max(2.0, settings.maintenance_worker_lease_seconds / 3)
            while not heartbeat_stop.wait(interval):
                if not renew_job_lease(
                    job_id,
                    worker_id,
                    lease_seconds=settings.maintenance_worker_lease_seconds,
                ):
                    return

        heartbeat_thread = threading.Thread(
            target=heartbeat,
            name=f"maintenance-heartbeat-{job_id}",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            result = execute_job(job_type, payload)
            if not complete_job(job_id, worker_id, result):
                raise RuntimeError("Maintenance job lease was lost before completion.")
            log_event(
                logger,
                logging.INFO,
                "maintenance_job_completed",
                job_id=job_id,
                job_type=job_type,
                worker_id=worker_id,
            )
        except BaseException as exc:
            outcome = fail_or_requeue_job(job_id, worker_id, str(exc))
            log_event(
                logger,
                logging.WARNING if outcome in {"queued", "lost"} else logging.ERROR,
                "maintenance_job_execution_ended",
                job_id=job_id,
                job_type=job_type,
                worker_id=worker_id,
                outcome=outcome,
                error=str(exc)[:1000],
                exc_info=outcome == "failed",
            )
            if outcome == "queued":
                self.notify()
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=0.2)

    def status(self) -> dict[str, int]:
        return {
            "configured_concurrency": 1,
            "live_threads": int(bool(self._thread and self._thread.is_alive())),
        }


maintenance_job_worker = MaintenanceJobWorker()
