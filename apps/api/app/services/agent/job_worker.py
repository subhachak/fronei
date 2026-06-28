from __future__ import annotations

import logging
import socket
import threading
import uuid

from app.config import get_settings
from app.observability import log_event
from app.services.agent import persistence
from app.services.agent.models import ProgressEvent, TurnRequest
from app.services.agent.runtime import Runtime

logger = logging.getLogger(__name__)


class TurnJobWorker:
    """Bounded in-process workers backed by durable database leases.

    Worker threads are intentionally disposable. The durable state is the Turn
    row: after a crash or deploy, an expired running lease becomes claimable by
    a new process and resumes from the beginning of the idempotent turn flow.
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        settings = get_settings()
        with self._lock:
            if any(thread.is_alive() for thread in self._threads):
                return
            self._stop.clear()
            self._threads = []
            for index in range(max(1, settings.turn_worker_concurrency)):
                worker_id = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}:{index}"
                thread = threading.Thread(
                    target=self._run,
                    args=(worker_id,),
                    name=f"turn-worker-{index}",
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)
            log_event(
                logger,
                logging.INFO,
                "turn_worker_pool_started",
                worker_concurrency=len(self._threads),
            )

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for thread in list(self._threads):
            thread.join(timeout=1.0)
        self._threads = []
        log_event(logger, logging.INFO, "turn_worker_pool_stopped")

    def notify(self) -> None:
        self._wake.set()

    def _run(self, worker_id: str) -> None:
        settings = get_settings()
        # Exponential backoff state for DB-error path — resets on a successful claim.
        _error_backoff = 1.0
        _MAX_ERROR_BACKOFF = 60.0
        while not self._stop.is_set():
            try:
                claimed = persistence.claim_next_turn(
                    worker_id,
                    lease_seconds=settings.turn_worker_lease_seconds,
                )
            except Exception:
                logger.exception("Turn worker %s could not claim work", worker_id)
                # Exponential backoff on repeated DB failures (e.g. quota exceeded).
                # Caps at 60 s so a transient outage self-heals within a minute.
                self._wake.wait(timeout=_error_backoff)
                self._wake.clear()
                _error_backoff = min(_error_backoff * 2, _MAX_ERROR_BACKOFF)
                continue
            # Successful DB contact — reset error backoff.
            _error_backoff = 1.0
            if claimed is None:
                # No work available — sleep the full configured poll interval.
                # The 0.05-floor was replaced with the configured value (default 5 s)
                # to dramatically reduce idle DB traffic.  notify() wakes workers
                # immediately when real work arrives, so latency is unaffected.
                self._wake.wait(timeout=settings.turn_worker_poll_seconds)
                self._wake.clear()
                continue
            turn_id, user_id, request = claimed
            log_event(
                logger,
                logging.INFO,
                "turn_job_claimed",
                turn_id=turn_id,
                user_id=user_id,
                worker_id=worker_id,
            )
            self._execute(worker_id, turn_id, user_id, request)

    def _execute(self, worker_id: str, turn_id: str, user_id: str, request: TurnRequest) -> None:
        settings = get_settings()
        heartbeat_stop = threading.Event()

        def heartbeat() -> None:
            interval = max(2.0, settings.turn_worker_lease_seconds / 3)
            while not heartbeat_stop.wait(interval):
                if not persistence.renew_turn_lease(
                    turn_id,
                    worker_id,
                    lease_seconds=settings.turn_worker_lease_seconds,
                ):
                    return

        heartbeat_thread = threading.Thread(
            target=heartbeat,
            name=f"turn-heartbeat-{turn_id}",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            if persistence.turn_cancel_requested(turn_id, worker_id):
                raise RuntimeError("Turn cancelled by user.")
            runtime = Runtime()
            saw_result = False
            for envelope in runtime.run_stream(request, user_id=user_id, turn_id=turn_id):
                if not persistence.worker_owns_turn(turn_id, worker_id):
                    raise RuntimeError("Turn lease was lost to another worker.")
                if persistence.turn_cancel_requested(turn_id, worker_id):
                    raise RuntimeError("Turn cancelled by user.")
                if envelope.type == "error":
                    raise RuntimeError(str(envelope.data.get("detail") or envelope.data.get("message") or "Agent failed"))
                if not persistence.persist_turn_envelope(envelope, turn_id, lease_owner=worker_id):
                    raise RuntimeError("Turn lease was lost before persistence.")
                saw_result = saw_result or envelope.type == "result"
            if not saw_result:
                raise RuntimeError("Agent runtime ended without a result.")
            log_event(
                logger,
                logging.INFO,
                "turn_job_completed",
                turn_id=turn_id,
                user_id=user_id,
                worker_id=worker_id,
            )
        except BaseException as exc:  # pragma: no cover - defensive worker boundary.
            outcome = persistence.fail_or_requeue_turn(turn_id, worker_id, str(exc))
            log_event(
                logger,
                logging.WARNING if outcome in {"queued", "cancelled", "lost"} else logging.ERROR,
                "turn_job_execution_ended",
                turn_id=turn_id,
                user_id=user_id,
                worker_id=worker_id,
                outcome=outcome,
                error=str(exc)[:1000],
                exc_info=outcome == "failed",
            )
            if outcome == "queued":
                persistence.append_event(
                    ProgressEvent(
                        turn_id=turn_id,
                        stage="job_retry",
                        message="The turn will retry after a recoverable worker failure.",
                        data={"error": str(exc)[:500]},
                    )
                )
                self.notify()
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=0.2)

    def status(self) -> dict[str, int]:
        with self._lock:
            return {
                "configured_concurrency": max(1, get_settings().turn_worker_concurrency),
                "live_threads": sum(thread.is_alive() for thread in self._threads),
            }


turn_job_worker = TurnJobWorker()
