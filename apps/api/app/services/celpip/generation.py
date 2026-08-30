"""The item generation pipeline.

    test specification
       -> stimulus + question generation      (celpip_item_writer)
       -> deterministic schema validation     (schemas.validate_payload)
       -> independent blind answer check      (celpip_item_validator)
       -> duplicate rejection against the bank
       -> persisted as `ready`, servable immediately

Validated content is servable without human approval. A review queue the
learner has to clear before practising would cost more preparation time than
it saves; the independent answer check is the real gate, and the Question Bank
keeps manual approve/disable as a quality signal rather than a blocker.

Every rejection is recorded on the run with its reason. A task type whose
items keep failing is a prompt problem, and without that record it shows up
only as a bank that mysteriously will not fill.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.db.models import CelpipGenerationRun, CelpipQuestion, SessionLocal
from app.services.agent.models import new_id
from app.services.celpip import prompts, schemas, validation
from app.services.celpip.spec import SPEC_VERSION, TASKS_BY_KEY

# Speaking tasks answered about a picture; their image is built by the asset
# job before the item becomes servable.
ASSET_TASKS = {"speaking_scene", "speaking_predictions", "speaking_comparing", "speaking_unusual"}

logger = logging.getLogger(__name__)

# Jaccard overlap above which a new item counts as a rewrite of one already
# banked. 0.55 rejects "same scenario, reworded" while leaving room for items
# that merely share a domain (two different transit conversations).
DUPLICATE_THRESHOLD = 0.55

# Shingle overlap is only meaningful on a substantial stimulus. Writing and
# speaking prompts are 40-60 words and heavily formulaic -- "Write an email to
# X about Y" -- so two genuinely different scenarios sharing a sentence frame
# overlap enough to look like duplicates. Below this length, only an exact
# fingerprint match or a repeated topic counts, because rejecting a valid
# prompt is more costly than banking a similar one.
SHINGLE_MIN_WORDS = 80

# How many extra attempts a run may make to hit its requested count. Items are
# rejected often enough by design that a run without headroom routinely returns
# short.
MAX_ATTEMPT_MULTIPLIER = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _existing_signatures(db, task_key: str) -> tuple[set[str], list[set[str]], list[str]]:
    """Fingerprints, shingle sets, and topics already in the bank for a task."""
    rows = (
        db.query(CelpipQuestion)
        .filter(CelpipQuestion.task_key == task_key)
        .filter(CelpipQuestion.status.in_(("ready", "draft")))
        .all()
    )
    fingerprints = {r.content_fingerprint for r in rows if r.content_fingerprint}
    shingles: list[set[str]] = []
    topics: list[str] = []
    for row in rows:
        if row.topic:
            topics.append(row.topic)
        try:
            payload = json.loads(row.payload_json)
        except (TypeError, ValueError):
            continue
        shingles.append(schemas.shingle_set(task_key, payload))
    return fingerprints, shingles, topics


def _generate_one(task_key: str, difficulty: int, topic_hint: str, avoid: list[str]) -> tuple[dict, str]:
    from app.services.agent import model_client
    from app.services.agent.research_utils import _parse_json

    system, user = prompts.build_generation_prompt(
        task_key, difficulty=difficulty, topic_hint=topic_hint, avoid_topics=avoid,
    )
    response = model_client.complete(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        role="celpip_item_writer",
        max_tokens=8000,
        timeout_s=300,
    )
    return _parse_json(response.text), getattr(response, "model", "") or ""


def run_generation(
    *,
    run_id: str,
    user_id: str,
    task_key: str,
    count: int,
    difficulty: int = 9,
    topic_hint: str = "",
) -> dict:
    """Generate, validate, and bank `count` items. Returns a summary dict.

    Runs synchronously; the caller is the background job worker, because a
    single item costs a generation call plus a review call and a batch of five
    is minutes of work, not a request.
    """
    task = TASKS_BY_KEY.get(task_key)
    if task is None:
        raise ValueError(f"unknown task type {task_key!r}")

    db = SessionLocal()
    try:
        run = db.get(CelpipGenerationRun, run_id)
        if run is None:
            raise ValueError(f"generation run {run_id} not found")
        run.status = "running"
        db.commit()

        fingerprints, shingles, topics = _existing_signatures(db, task_key)
        accepted_ids: list[str] = []
        rejections: list[dict] = []
        generator_model = ""
        validator_model = ""
        attempts = 0
        max_attempts = max(count, count * MAX_ATTEMPT_MULTIPLIER)

        while len(accepted_ids) < count and attempts < max_attempts:
            attempts += 1
            try:
                payload, generator_model = _generate_one(task_key, difficulty, topic_hint, topics)
            except Exception as exc:
                logger.warning("celpip generation call failed (%s): %s", task_key, exc)
                rejections.append({"reason": validation.REASON_PARSE, "detail": str(exc)[:400]})
                continue

            schema_verdict = validation.check_schema(task_key, payload)
            if not schema_verdict["accepted"]:
                rejections.append({
                    "reason": validation.REASON_SCHEMA,
                    "detail": "; ".join(r["detail"] for r in schema_verdict["reasons"])[:600],
                })
                continue

            fingerprint = schemas.fingerprint(task_key, payload)
            candidate_shingles = schemas.shingle_set(task_key, payload)
            long_enough = (
                schemas.word_count(schemas.stimulus_text(task_key, payload)) >= SHINGLE_MIN_WORDS
            )
            overlap = (
                max(
                    (schemas.similarity(candidate_shingles, existing) for existing in shingles),
                    default=0.0,
                )
                if long_enough
                else 0.0
            )
            topic = str(payload.get("topic", "")).strip().lower()
            repeated_topic = bool(topic) and topic in {t.strip().lower() for t in topics}
            if fingerprint in fingerprints or overlap >= DUPLICATE_THRESHOLD or repeated_topic:
                rejections.append({
                    "reason": validation.REASON_DUPLICATE,
                    "detail": (
                        f"overlaps an existing item ({overlap:.0%})"
                        if overlap
                        else "repeats a scenario already in the bank"
                    ),
                })
                topics.append(str(payload.get("topic", ""))[:120])
                continue

            verdict = validation.review_item(task_key, payload)
            validator_model = verdict.get("validator_model") or validator_model
            if not verdict.get("accepted"):
                rejections.append({
                    "reason": (verdict.get("reasons") or [{}])[0].get("reason", "rejected"),
                    "detail": "; ".join(
                        str(r.get("detail", "")) for r in verdict.get("reasons") or []
                    )[:600],
                })
                continue

            question = CelpipQuestion(
                id=new_id("cq"),
                skill=task.skill,
                task_key=task_key,
                part=task.part,
                title=str(payload.get("topic", ""))[:255] or task.label,
                payload_json=json.dumps(payload, ensure_ascii=False),
                difficulty=difficulty,
                topic=str(payload.get("topic", ""))[:120],
                source="generated",
                # Listening items need audio before they can be served; the
                # asset job flips them to ready once synthesis succeeds.
                status=(
                    "awaiting_assets"
                    if task.skill == "listening" or task_key in ASSET_TASKS
                    else "ready"
                ),
                validation_json=json.dumps(
                    {"schema": schema_verdict, "review": verdict}, ensure_ascii=False
                ),
                validated_at=_now(),
                generation_run_id=run_id,
                generator_model=generator_model[:120],
                validator_model=str(verdict.get("validator_model", ""))[:120],
                spec_version=SPEC_VERSION,
                content_fingerprint=fingerprint,
            )
            db.add(question)
            db.commit()

            # Listening audio and speaking-task images are built on their own
            # job: synthesis is slow enough that folding it into this loop
            # would make a five-item batch time out, and an audio failure
            # should cost the item its `ready` status, not the whole run.
            if task.skill == "listening" or task_key in ASSET_TASKS:
                from app.services import maintenance_jobs

                maintenance_jobs.enqueue_celpip_assets(question_id=question.id)

            accepted_ids.append(question.id)
            fingerprints.add(fingerprint)
            shingles.append(candidate_shingles)
            topics.append(question.topic)

        run = db.get(CelpipGenerationRun, run_id)
        run.status = "complete"
        run.accepted_count = len(accepted_ids)
        run.rejected_count = len(rejections)
        run.rejections_json = json.dumps(rejections, ensure_ascii=False)
        run.question_ids_json = json.dumps(accepted_ids)
        run.generator_model = generator_model[:120]
        run.validator_model = validator_model[:120]
        run.spec_version = SPEC_VERSION
        run.completed_at = _now()
        db.commit()

        return {
            "run_id": run_id,
            "task_key": task_key,
            "requested": count,
            "accepted": len(accepted_ids),
            "rejected": len(rejections),
            "question_ids": accepted_ids,
            "attempts": attempts,
        }
    except Exception as exc:
        db.rollback()
        run = db.get(CelpipGenerationRun, run_id)
        if run is not None:
            run.status = "failed"
            run.error = str(exc)[:2000]
            run.completed_at = _now()
            db.commit()
        raise
    finally:
        db.close()


def enqueue_generation(
    *, user_id: str, task_key: str, count: int, difficulty: int = 9, topic_hint: str = "",
) -> dict:
    """Create the run row and queue the background job that fills it."""
    from app.services import maintenance_jobs

    if task_key not in TASKS_BY_KEY:
        raise ValueError(f"unknown task type {task_key!r}")
    count = max(1, min(int(count), 10))

    run_id = new_id("crun")
    db = SessionLocal()
    try:
        run = CelpipGenerationRun(
            id=run_id,
            user_id=user_id,
            task_key=task_key,
            requested_count=count,
            difficulty=difficulty,
            topic_hint=topic_hint[:255],
            status="queued",
            spec_version=SPEC_VERSION,
        )
        db.add(run)
        db.commit()
    finally:
        db.close()

    job_id = maintenance_jobs.enqueue_celpip_generation(
        run_id=run_id, user_id=user_id, task_key=task_key, count=count,
        difficulty=difficulty, topic_hint=topic_hint,
    )

    db = SessionLocal()
    try:
        run = db.get(CelpipGenerationRun, run_id)
        if run is not None:
            run.job_id = job_id
            db.commit()
    finally:
        db.close()

    return {"run_id": run_id, "job_id": job_id, "task_key": task_key, "requested": count}
