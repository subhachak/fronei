"""Keeps a buffer of unseen questions ready, so launching a test is instant.

Every sitting consumes its questions: an item served once is retired and never
appears again. That is what makes a retake meaningful (same questions, measure
improvement) while a fresh launch is genuinely fresh.

Generating those questions at click time does not work. A full CELPIP-General
mock needs one item for each of 20 task types; every item costs a generation
call plus an independent validation call, and listening items then need audio
synthesised per speaker turn. All of it runs on a single maintenance worker
thread, so the wait is minutes at best -- and scoring jobs for an already
submitted attempt queue up behind it.

So the buffer is filled ahead of time. `plan_topup` looks at how many unserved
items each task type has, and enqueues one small generation job per deficient
task. Launching then just takes what is already there.

"Fresh" here means "never served to this learner", not "generated seconds ago".
That is the property the learner actually cares about.
"""
from __future__ import annotations

import logging

from app.config import get_settings
from app.db.models import CelpipQuestion, SessionLocal
from app.services.celpip.spec import ALL_TASKS, TASKS_BY_KEY, task_keys_for_test_type

logger = logging.getLogger(__name__)


def target_stock() -> int:
    return max(1, get_settings().celpip_stock_per_task)


def stock_levels(db, task_keys: list[str] | None = None) -> dict[str, int]:
    """Unserved, servable items per task type.

    `times_served == 0` rather than `status == "ready"` alone: an item that has
    been in a test is retired, but counting only status would also count items
    a fallback path had reused.
    """
    wanted = task_keys or [task.key for task in ALL_TASKS]
    counts = {key: 0 for key in wanted}
    rows = (
        db.query(CelpipQuestion.task_key)
        .filter(CelpipQuestion.status == "ready")
        .filter(CelpipQuestion.times_served == 0)
        .filter(CelpipQuestion.task_key.in_(wanted))
        .all()
    )
    for (task_key,) in rows:
        counts[task_key] = counts.get(task_key, 0) + 1
    return counts


def building_counts(db, task_keys: list[str] | None = None) -> dict[str, int]:
    """Items generated but not yet servable -- audio or images still building."""
    wanted = task_keys or [task.key for task in ALL_TASKS]
    counts = {key: 0 for key in wanted}
    rows = (
        db.query(CelpipQuestion.task_key)
        .filter(CelpipQuestion.status.in_(("draft", "awaiting_assets")))
        .filter(CelpipQuestion.task_key.in_(wanted))
        .all()
    )
    for (task_key,) in rows:
        counts[task_key] = counts.get(task_key, 0) + 1
    return counts


def deficits(db, task_keys: list[str] | None = None) -> dict[str, int]:
    """How many more items each task type needs to reach target stock.

    Items still building count towards the target, so a top-up already in
    flight does not trigger another one on the next check.
    """
    target = target_stock()
    ready = stock_levels(db, task_keys)
    building = building_counts(db, task_keys)
    out: dict[str, int] = {}
    for key, count in ready.items():
        shortfall = target - count - building.get(key, 0)
        if shortfall > 0:
            out[key] = shortfall
    return out


def plan_topup(*, user_id: str, task_keys: list[str] | None = None) -> dict:
    """Enqueue a generation job for every task type below target stock.

    One job per task type rather than one job for everything: the jobs stay
    short, a failure on one task type does not lose the rest, and the dedupe
    key means a second call while a top-up is running is a no-op.
    """
    from app.services import maintenance_jobs

    db = SessionLocal()
    try:
        needed = deficits(db, task_keys)
    finally:
        db.close()

    queued: dict[str, str] = {}
    for task_key, count in needed.items():
        if task_key not in TASKS_BY_KEY:
            continue
        queued[task_key] = maintenance_jobs.enqueue_celpip_topup(
            user_id=user_id, task_key=task_key, count=count,
        )
    if queued:
        logger.info("celpip stock top-up queued for %s task type(s)", len(queued))
    return {"queued": queued, "deficits": needed, "target": target_stock()}


def run_topup(*, user_id: str, task_key: str, count: int) -> dict:
    """Job body: generate into the buffer for one task type."""
    from app.services.celpip import generation

    db = SessionLocal()
    try:
        # Re-check under the job rather than trusting the count captured when
        # the job was queued -- a launch may have consumed more since, or a
        # previous top-up may already have covered it.
        still_needed = deficits(db, [task_key]).get(task_key, 0)
    finally:
        db.close()
    if still_needed <= 0:
        return {"task_key": task_key, "generated": 0, "skipped": "already stocked"}

    wanted = max(1, min(count, still_needed))
    run = generation.enqueue_generation_sync(
        user_id=user_id, task_key=task_key, count=wanted,
    )
    return {"task_key": task_key, "requested": wanted, **run}


def readiness_for(db, *, test_type: str, task_keys: list[str] | None = None) -> dict:
    """What the launcher needs to know: can a test be built right now."""
    keys = task_keys or task_keys_for_test_type(test_type)
    ready = stock_levels(db, keys)
    building = building_counts(db, keys)
    missing = [key for key in keys if ready.get(key, 0) < 1]
    return {
        "ready": ready,
        "building": building,
        "missing": missing,
        "can_launch": not missing,
        "target": target_stock(),
    }
