from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.db.models import JobCheckpointRow, SessionLocal


logger = logging.getLogger(__name__)


class JobCheckpoint:
    """Per-turn pipeline checkpoint. Never raises on read/write/clear failure."""

    RESUME_MIN_SCORE: float = 0.6

    def save(
        self,
        turn_id: str,
        stage: str,
        payload: dict[str, Any],
        *,
        score: float | None = None,
        was_repair: bool = False,
    ) -> None:
        if not turn_id:
            return
        try:
            envelope = json.dumps({
                "payload": payload,
                "score": score,
                "was_repair": was_repair,
                "saved_at": time.time(),
            })
            self._write(turn_id, stage, envelope)
            logger.debug("Checkpoint saved: turn=%s stage=%s score=%s", turn_id, stage, score)
        except Exception:
            logger.exception("Checkpoint save failed: turn=%s stage=%s", turn_id, stage)

    def load(self, turn_id: str, stage: str) -> tuple[dict[str, Any] | None, float | None]:
        if not turn_id:
            return None, None
        try:
            raw = self._read(turn_id, stage)
            if not raw:
                return None, None
            envelope = json.loads(raw)
            payload = envelope.get("payload")
            return (payload if isinstance(payload, dict) else None), envelope.get("score")
        except Exception:
            logger.exception("Checkpoint load failed: turn=%s stage=%s", turn_id, stage)
            return None, None

    def should_trust(self, score: float | None) -> bool:
        return score is not None and score >= self.RESUME_MIN_SCORE

    def clear(self, turn_id: str) -> None:
        if not turn_id:
            return
        try:
            self._delete_all(turn_id)
        except Exception:
            logger.exception("Checkpoint clear failed: turn=%s", turn_id)

    def _write(self, turn_id: str, stage: str, payload_json: str) -> None:
        with SessionLocal() as db:
            row = (
                db.query(JobCheckpointRow)
                .filter(JobCheckpointRow.turn_id == turn_id, JobCheckpointRow.stage == stage)
                .first()
            )
            if row:
                row.payload = payload_json
            else:
                db.add(JobCheckpointRow(turn_id=turn_id, stage=stage, payload=payload_json))
            db.commit()

    def _read(self, turn_id: str, stage: str) -> str | None:
        with SessionLocal() as db:
            row = (
                db.query(JobCheckpointRow)
                .filter(JobCheckpointRow.turn_id == turn_id, JobCheckpointRow.stage == stage)
                .first()
            )
            return row.payload if row else None

    def _delete_all(self, turn_id: str) -> None:
        with SessionLocal() as db:
            db.query(JobCheckpointRow).filter(JobCheckpointRow.turn_id == turn_id).delete()
            db.commit()


def resume_tier(turn_id: str) -> str:
    checkpoint = JobCheckpoint()
    for stage in [
        "document.generate_complete",
        "document.plan_complete",
        "research.synthesis_complete",
        "research.crawl_complete",
    ]:
        payload, score = checkpoint.load(turn_id, stage)
        if payload is not None and checkpoint.should_trust(score):
            return stage
    return "none"
