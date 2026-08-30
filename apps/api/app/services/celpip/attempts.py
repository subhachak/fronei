"""The session runner: starting, autosaving, timing, and submitting an attempt.

The clock is the point of this module. Every deadline is derived from a
server-recorded `started_at` plus the official limit from the spec; the browser
is told how many seconds remain but is never asked. That single rule is what
makes all three of these work at once:

* a refresh mid-section resumes with the correct time left, not a fresh timer;
* a closed laptop does not pause the exam;
* a response saved after the deadline is recorded but flagged and excluded
  from scoring, rather than silently accepted.

`practice_mode` decides how much the runner enforces. Learn mode is untimed
and lets answers change freely. Simulation refuses to reveal anything until
submission and blocks the answer changes the official flow blocks.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from app.db.models import (
    CelpipAttempt,
    CelpipQuestion,
    CelpipResponse,
    CelpipTest,
    CelpipTestItem,
    SessionLocal,
)
from app.services.agent.models import new_id
from app.services.celpip import assets
from app.services.celpip.spec import SECTIONS, TASKS_BY_KEY

logger = logging.getLogger(__name__)

# Grace on the section deadline, absorbing network latency on the final
# autosave. Long enough that a slow request does not lose a real answer,
# short enough that it is not extra exam time.
DEADLINE_GRACE_SECONDS = 5


class AttemptError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; treat stored times as UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _loads(raw: str, default):
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def create_attempt(*, user_id: str, test_id: str, practice_mode: str | None = None) -> dict:
    db = SessionLocal()
    try:
        test = db.get(CelpipTest, test_id)
        if test is None or test.user_id != user_id:
            raise AttemptError("test not found")

        attempt = CelpipAttempt(
            id=new_id("cattempt"),
            user_id=user_id,
            test_id=test_id,
            practice_mode=practice_mode or test.practice_mode,
            status="not_started",
            section_state_json="{}",
        )
        db.add(attempt)
        db.commit()
        return {"attempt_id": attempt.id, "test_id": test_id, "status": attempt.status}
    finally:
        db.close()


def _enforces_exam_order(practice_mode: str) -> bool:
    """Whether section sequencing is enforced for this mode.

    Timed and simulation runs reproduce the official flow: one section open at
    a time, in order, and no answers to a section that is not currently open.
    Learn mode is explicitly untimed with free navigation and changeable
    answers, so those constraints would only get in the way of a drill.
    """
    return practice_mode != "learn"


def _components(db, attempt: CelpipAttempt) -> list[str]:
    test = db.get(CelpipTest, attempt.test_id)
    return _loads(test.components_json, []) if test else []


def _active_sections(state: dict) -> list[str]:
    return [
        skill for skill, section in state.items()
        if section.get("started_at") and not section.get("completed_at")
    ]


def _section_limit(skill: str, practice_mode: str) -> int | None:
    """Seconds allowed for a section, or None when the mode is untimed."""
    if practice_mode == "learn":
        return None
    section = SECTIONS.get(skill)
    return section.limit_seconds if section else None


def start_section(*, user_id: str, attempt_id: str, skill: str) -> dict:
    """Open a section and stamp its server-side deadline.

    Idempotent: calling it again for a section already open returns the
    existing deadline rather than restarting the clock, so a double-mounted
    component or a retried request cannot buy extra time.
    """
    db = SessionLocal()
    try:
        attempt = _load(db, user_id, attempt_id)
        if attempt.status in {"submitted", "evaluating", "completed"}:
            raise AttemptError("this attempt has already been submitted")

        components = _components(db, attempt)
        # A skill that is not part of this test has no section to open, and
        # without this check the API would happily stamp a deadline for one and
        # accept answers against it.
        if skill not in components:
            raise AttemptError(f"this test has no {skill} section")

        state = _loads(attempt.section_state_json, {})

        if _enforces_exam_order(attempt.practice_mode):
            already_open = [s for s in _active_sections(state) if s != skill]
            if already_open:
                raise AttemptError(
                    f"finish the {already_open[0]} section before starting {skill}"
                )
            # Sections run in the order the test lists them. Skipping ahead
            # would let a learner leave a hard section unopened, sit the rest,
            # and come back to it with the clock untouched.
            earlier = components[: components.index(skill)]
            unfinished = [
                s for s in earlier if not (state.get(s) or {}).get("completed_at")
            ]
            if unfinished:
                raise AttemptError(
                    f"the {unfinished[0]} section comes first and is not finished"
                )

        existing = state.get(skill)
        if existing and existing.get("started_at"):
            # Idempotent for an open section, and refuses outright for a closed
            # one. Without the second half, a section that expired could be
            # restarted with a brand-new full deadline just by reloading the
            # page -- the browser's "already started this" guard does not
            # survive a refresh, but the exam clock has to.
            if existing.get("completed_at"):
                raise AttemptError("this section is already finished and cannot be reopened")
            return _state_payload(attempt, state, skill)

        limit = _section_limit(skill, attempt.practice_mode)
        now = _now()
        state[skill] = {
            "started_at": now.isoformat(),
            "deadline_at": (now + timedelta(seconds=limit)).isoformat() if limit else None,
            "limit_seconds": limit,
            "completed_at": None,
            "auto_submitted": False,
        }
        attempt.section_state_json = json.dumps(state)
        attempt.current_skill = skill
        if attempt.status == "not_started":
            attempt.status = "in_progress"
            attempt.started_at = now
        db.commit()
        return _state_payload(attempt, state, skill)
    finally:
        db.close()


def _state_payload(attempt: CelpipAttempt, state: dict, skill: str) -> dict:
    section = state.get(skill) or {}
    deadline = section.get("deadline_at")
    remaining = None
    if deadline:
        remaining = max(0, int((datetime.fromisoformat(deadline) - _now()).total_seconds()))
    return {
        "attempt_id": attempt.id,
        "skill": skill,
        "status": attempt.status,
        "started_at": section.get("started_at"),
        "deadline_at": deadline,
        "limit_seconds": section.get("limit_seconds"),
        # The client renders this and counts down locally, but every write is
        # re-checked against the server clock -- a tampered local timer buys
        # nothing.
        "seconds_remaining": remaining,
        "expired": remaining == 0 if remaining is not None else False,
        # The client needs the real completion signal: having moved past a
        # section is not the same as having finished it.
        "completed_at": section.get("completed_at"),
        "auto_submitted": bool(section.get("auto_submitted")),
    }


def _load(db, user_id: str, attempt_id: str) -> CelpipAttempt:
    attempt = db.get(CelpipAttempt, attempt_id)
    if attempt is None or attempt.user_id != user_id:
        raise AttemptError("attempt not found")
    return attempt


def _require_open_section(attempt: CelpipAttempt, state: dict, skill: str) -> None:
    """Refuse an answer to a section that is not currently open.

    Without this, an answer could be saved to a section that was never started
    -- it has no deadline, and `_section_expired` reads a missing deadline as
    "not expired", so the clock check waves it through. That is the whole exam
    timer bypassed by never pressing start.
    """
    if not _enforces_exam_order(attempt.practice_mode):
        return
    section = state.get(skill) or {}
    if not section.get("started_at"):
        raise AttemptError(f"the {skill} section has not been started")
    if section.get("completed_at"):
        raise AttemptError(f"the {skill} section is finished and no longer accepts answers")


def _section_expired(attempt: CelpipAttempt, state: dict, skill: str) -> bool:
    section = state.get(skill) or {}
    deadline = section.get("deadline_at")
    if not deadline:
        return False
    limit = datetime.fromisoformat(deadline) + timedelta(seconds=DEADLINE_GRACE_SECONDS)
    return _now() > limit


def save_response(
    *,
    user_id: str,
    attempt_id: str,
    question_id: str,
    question_index: int = 0,
    selected_option: str | None = None,
    response_text: str | None = None,
    time_spent_ms: int = 0,
    flagged: bool | None = None,
) -> dict:
    """Autosave one answer. Called on every change, not just on section end."""
    db = SessionLocal()
    try:
        attempt = _load(db, user_id, attempt_id)
        if attempt.status in {"submitted", "evaluating", "completed"}:
            raise AttemptError("this attempt has already been submitted")

        item = (
            db.query(CelpipTestItem)
            .filter(CelpipTestItem.test_id == attempt.test_id)
            .filter(CelpipTestItem.question_id == question_id)
            .first()
        )
        if item is None:
            raise AttemptError("that question is not part of this test")

        state = _loads(attempt.section_state_json, {})
        _require_open_section(attempt, state, item.skill)
        late = _section_expired(attempt, state, item.skill)

        response = (
            db.query(CelpipResponse)
            .filter(CelpipResponse.attempt_id == attempt.id)
            .filter(CelpipResponse.question_id == question_id)
            .filter(CelpipResponse.question_index == question_index)
            .first()
        )

        task = TASKS_BY_KEY.get(item.task_key)
        if (
            response is not None
            and attempt.practice_mode == "simulation"
            and task is not None
            and not task.allows_answer_change
            and response.selected_option
        ):
            # The official flow does not let a candidate return to a listening
            # question once the audio has moved on. Enforced only in
            # simulation, where the point is to reproduce the real constraint.
            raise AttemptError("this answer cannot be changed in exam simulation")

        if response is None:
            response = CelpipResponse(
                id=new_id("cresp"),
                attempt_id=attempt.id,
                question_id=question_id,
                skill=item.skill,
                task_key=item.task_key,
                question_index=question_index,
            )
            db.add(response)

        if selected_option is not None:
            response.selected_option = selected_option
        if response_text is not None:
            response.response_text = response_text
        if flagged is not None:
            response.flagged = flagged
        if time_spent_ms:
            response.time_spent_ms = time_spent_ms
        response.late = late
        db.commit()

        return {
            "saved": True,
            "late": late,
            "response_id": response.id,
            "word_count": len((response.response_text or "").split()),
        }
    finally:
        db.close()


def save_audio_response(
    *, user_id: str, attempt_id: str, question_id: str, audio: bytes,
    content_type: str = "audio/webm", duration_seconds: float = 0.0,
) -> dict:
    """Store a captured speaking response. Transcription happens at scoring."""
    from app.services.blob_store import get_blob_store

    db = SessionLocal()
    try:
        attempt = _load(db, user_id, attempt_id)
        if attempt.status in {"submitted", "evaluating", "completed"}:
            raise AttemptError("this attempt has already been submitted")

        item = (
            db.query(CelpipTestItem)
            .filter(CelpipTestItem.test_id == attempt.test_id)
            .filter(CelpipTestItem.question_id == question_id)
            .first()
        )
        if item is None or item.skill != "speaking":
            raise AttemptError("that question is not a speaking task in this test")

        _require_open_section(attempt, _loads(attempt.section_state_json, {}), item.skill)

        response = (
            db.query(CelpipResponse)
            .filter(CelpipResponse.attempt_id == attempt.id)
            .filter(CelpipResponse.question_id == question_id)
            .first()
        )
        if response is not None and attempt.practice_mode == "simulation" and response.audio_blob_location:
            raise AttemptError("a recording cannot be retaken in exam simulation")

        extension = "webm" if "webm" in content_type else "m4a" if "mp4" in content_type else "ogg"
        stored = get_blob_store().put(
            f"celpip/responses/{attempt.id}/{question_id}.{extension}", audio, content_type=content_type,
        )

        if response is None:
            response = CelpipResponse(
                id=new_id("cresp"),
                attempt_id=attempt.id,
                question_id=question_id,
                skill="speaking",
                task_key=item.task_key,
            )
            db.add(response)
        response.audio_blob_location = stored.location
        response.audio_duration_seconds = duration_seconds
        response.transcription_status = "pending"
        response.transcript = ""
        response.transcript_words_json = "[]"
        db.commit()

        return {
            "saved": True,
            "response_id": response.id,
            "size_bytes": stored.size_bytes,
            "duration_seconds": duration_seconds,
        }
    finally:
        db.close()


def complete_section(*, user_id: str, attempt_id: str, skill: str, auto: bool = False) -> dict:
    db = SessionLocal()
    try:
        attempt = _load(db, user_id, attempt_id)
        state = _loads(attempt.section_state_json, {})
        section = state.get(skill)
        if not section:
            raise AttemptError("that section was never started")
        if not section.get("completed_at"):
            section["completed_at"] = _now().isoformat()
            section["auto_submitted"] = auto
            attempt.section_state_json = json.dumps(state)
            db.commit()
        return {"attempt_id": attempt.id, "skill": skill, "completed": True, "auto_submitted": auto}
    finally:
        db.close()


def submit_attempt(*, user_id: str, attempt_id: str) -> dict:
    """Close the attempt and queue scoring."""
    from app.services import maintenance_jobs

    db = SessionLocal()
    try:
        attempt = _load(db, user_id, attempt_id)
        if attempt.status in {"submitted", "evaluating", "completed"}:
            return {"attempt_id": attempt.id, "status": attempt.status, "already_submitted": True}

        state = _loads(attempt.section_state_json, {})
        now = _now()
        for skill, section in state.items():
            if not section.get("completed_at"):
                section["completed_at"] = now.isoformat()
        attempt.section_state_json = json.dumps(state)
        attempt.status = "submitted"
        attempt.submitted_at = now
        db.commit()
        attempt_id_value = attempt.id
    finally:
        db.close()

    job_id = maintenance_jobs.enqueue_celpip_evaluation(attempt_id=attempt_id_value)
    return {"attempt_id": attempt_id_value, "status": "submitted", "evaluation_job_id": job_id}


def attempt_state(*, user_id: str, attempt_id: str) -> dict:
    """Everything the runner needs to resume, including real time remaining."""
    db = SessionLocal()
    try:
        attempt = _load(db, user_id, attempt_id)
        test = db.get(CelpipTest, attempt.test_id)
        state = _loads(attempt.section_state_json, {})

        items = (
            db.query(CelpipTestItem)
            .filter(CelpipTestItem.test_id == attempt.test_id)
            .order_by(CelpipTestItem.position.asc())
            .all()
        )
        responses = {
            (r.question_id, r.question_index): r
            for r in db.query(CelpipResponse).filter(CelpipResponse.attempt_id == attempt.id).all()
        }

        sections: dict[str, dict] = {}
        expired_sections: list[str] = []
        for skill in _loads(test.components_json, []) if test else []:
            payload = _state_payload(attempt, state, skill)
            sections[skill] = payload
            if payload.get("expired") and not (state.get(skill) or {}).get("completed_at"):
                expired_sections.append(skill)

        rendered = []
        for item in items:
            question = db.get(CelpipQuestion, item.question_id)
            if question is None:
                continue
            rendered.append({
                "question_id": question.id,
                "skill": item.skill,
                "task_key": item.task_key,
                "position": item.position,
                "is_practice_task": item.is_practice_task,
                # `is_unscored` is deliberately NOT sent: the real test does
                # not tell candidates which items do not count, and knowing
                # would change how they answer.
                "answered": sum(
                    1 for (qid, _), r in responses.items()
                    if qid == question.id and (r.selected_option or r.response_text or r.audio_blob_location)
                ),
            })

        return {
            "attempt_id": attempt.id,
            "test_id": attempt.test_id,
            "label": test.label if test else "",
            "mode": test.mode if test else "",
            "practice_mode": attempt.practice_mode,
            "status": attempt.status,
            "current_skill": attempt.current_skill,
            "components": _loads(test.components_json, []) if test else [],
            "sections": sections,
            "expired_sections": expired_sections,
            "items": rendered,
            "flagged": _loads(attempt.flagged_json, []),
            "started_at": _aware(attempt.started_at).isoformat() if attempt.started_at else None,
            "submitted_at": _aware(attempt.submitted_at).isoformat() if attempt.submitted_at else None,
        }
    finally:
        db.close()


def question_for_attempt(*, user_id: str, attempt_id: str, question_id: str) -> dict:
    """One item, stripped of everything the learner must not see mid-attempt."""
    db = SessionLocal()
    try:
        attempt = _load(db, user_id, attempt_id)
        item = (
            db.query(CelpipTestItem)
            .filter(CelpipTestItem.test_id == attempt.test_id)
            .filter(CelpipTestItem.question_id == question_id)
            .first()
        )
        if item is None:
            raise AttemptError("that question is not part of this test")
        question = db.get(CelpipQuestion, question_id)
        if question is None:
            raise AttemptError("question not found")

        payload = _loads(question.payload_json, {})
        task = TASKS_BY_KEY.get(question.task_key)
        stimulus = dict(payload.get("stimulus") or {})

        # Listening scripts never reach the client. The audio is the test; a
        # transcript in the network response would make it a reading task.
        if question.skill == "listening":
            stimulus.pop("segments", None)

        questions = [
            {
                "index": i,
                "prompt": q.get("prompt", ""),
                "options": q.get("options", {}),
                "segment_index": q.get("segment_index", 0),
            }
            for i, q in enumerate(payload.get("questions") or [])
        ]

        existing = {
            r.question_index: {
                "selected_option": r.selected_option,
                "response_text": r.response_text,
                "has_audio": bool(r.audio_blob_location),
                "flagged": r.flagged,
            }
            for r in db.query(CelpipResponse)
            .filter(CelpipResponse.attempt_id == attempt.id)
            .filter(CelpipResponse.question_id == question_id)
            .all()
        }

        return {
            "question_id": question.id,
            "skill": question.skill,
            "task_key": question.task_key,
            "part": question.part,
            "label": task.label if task else question.task_key,
            "description": task.description if task else "",
            "prep_seconds": task.prep_seconds if task else 0,
            "response_seconds": task.response_seconds if task else 0,
            "word_range": list(task.word_range) if task and task.word_range else None,
            "audio_replays": task.audio_replays if task else 0,
            "allows_answer_change": (task.allows_answer_change if task else True)
            or attempt.practice_mode != "simulation",
            "stimulus": stimulus,
            "questions": questions,
            "assets": assets.asset_payload(db, question.id),
            "responses": existing,
        }
    finally:
        db.close()
