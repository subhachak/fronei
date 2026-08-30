"""CELPIP preparation app.

Admin-only, and the admin check is the real boundary: every endpoint here takes
RequireAdmin independently. The frontend gate at /celpip only decides whether to
render the workspace.

The router stays thin on purpose -- request shape, authorisation, and
serialisation. Everything with a rule in it (the exam clock, item validation,
scoring, planning) lives in app/services/celpip/ so it is testable without a
request. See docs/celpip-app-plan.md.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone

from fastapi import APIRouter, File, Form, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field

from app.auth import AdminPrincipal, RequireAdmin
from app.db.models import (
    CelpipAttempt,
    CelpipEvaluation,
    CelpipGenerationRun,
    CelpipProfile,
    CelpipQuestion,
    CelpipQuestionAsset,
    CelpipResponse,
    CelpipStudyPlanItem,
    CelpipTest,
    SessionLocal,
)
from app.services.agent.models import new_id
from app.services.celpip import (
    assembly,
    generation,
    planner,
    scoring,
    speech,
)
from app.services.celpip import (
    assets as assets_service,
)
from app.services.celpip import (
    attempts as attempts_service,
)
from app.services.celpip import (
    lessons as lessons_service,
)
from app.services.celpip import (
    readiness as readiness_service,
)
from app.services.celpip.spec import (
    ALL_TASKS,
    TASKS_BY_KEY,
    WEAKNESS_TAGS,
    components_for_test_type,
    spec_summary,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/celpip", tags=["celpip"])

# Speaking captures are short. This ceiling exists so a broken recorder cannot
# post a gigabyte into the blob store.
MAX_AUDIO_BYTES = 25 * 1024 * 1024


def _loads(raw: str, default):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return value if value is not None else default


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()
    return value.isoformat()


def _get_or_create_profile(db, user_id: str) -> CelpipProfile:
    profile = db.query(CelpipProfile).filter(CelpipProfile.user_id == user_id).first()
    if profile is None:
        profile = CelpipProfile(id=new_id("cprofile"), user_id=user_id)
        db.add(profile)
        db.commit()
        db.refresh(profile)
    return profile


def _serialize_profile(profile: CelpipProfile) -> dict:
    return {
        "test_type": profile.test_type,
        "test_date": _iso(profile.test_date),
        "target_level": profile.target_level,
        "weekday_hours": profile.weekday_hours,
        "weekend_hours": profile.weekend_hours,
        "self_reported_weaknesses": _loads(profile.self_reported_weaknesses_json, []),
        "onboarding_state": profile.onboarding_state,
        "diagnostic_attempt_id": profile.diagnostic_attempt_id,
        "components": list(components_for_test_type(profile.test_type)),
    }


# --- Format and Learn library --------------------------------------------

@router.get("/spec")
def get_spec(admin: AdminPrincipal = RequireAdmin) -> dict:
    """The official format as this app enforces it.

    Served rather than duplicated in the frontend so the timings a learner
    reads on a Learn page are literally the ones the session runner counts down.
    """
    return spec_summary()


@router.get("/lessons")
def get_lessons(
    skill: str | None = Query(default=None),
    category: str | None = Query(default=None),
    admin: AdminPrincipal = RequireAdmin,
) -> dict:
    db = SessionLocal()
    try:
        return {"lessons": lessons_service.list_lessons(db, skill=skill, category=category)}
    finally:
        db.close()


@router.get("/lessons/{slug}")
def get_lesson(slug: str, admin: AdminPrincipal = RequireAdmin) -> dict:
    db = SessionLocal()
    try:
        lesson = lessons_service.get_lesson(db, slug)
        if lesson is None:
            raise HTTPException(status_code=404, detail="Lesson not found.")
        return lesson
    finally:
        db.close()


# --- Profile, onboarding, dashboard --------------------------------------

class ProfileIn(BaseModel):
    test_type: str | None = Field(default=None, pattern="^(general|general_ls)$")
    test_date: date | None = None
    target_level: int | None = Field(default=None, ge=1, le=12)
    weekday_hours: float | None = Field(default=None, ge=0, le=12)
    weekend_hours: float | None = Field(default=None, ge=0, le=12)
    self_reported_weaknesses: list[str] | None = None
    onboarding_state: str | None = Field(default=None, pattern="^(pending|complete|skipped)$")


@router.get("/profile")
def get_profile(admin: AdminPrincipal = RequireAdmin) -> dict:
    db = SessionLocal()
    try:
        return _serialize_profile(_get_or_create_profile(db, admin.user_id))
    finally:
        db.close()


@router.put("/profile")
def update_profile(payload: ProfileIn, admin: AdminPrincipal = RequireAdmin) -> dict:
    db = SessionLocal()
    try:
        profile = _get_or_create_profile(db, admin.user_id)
        if payload.test_type is not None:
            profile.test_type = payload.test_type
        if payload.test_date is not None:
            profile.test_date = payload.test_date
        if payload.target_level is not None:
            profile.target_level = payload.target_level
        if payload.weekday_hours is not None:
            profile.weekday_hours = payload.weekday_hours
        if payload.weekend_hours is not None:
            profile.weekend_hours = payload.weekend_hours
        if payload.self_reported_weaknesses is not None:
            valid = [t for t in payload.self_reported_weaknesses if t in WEAKNESS_TAGS]
            profile.self_reported_weaknesses_json = json.dumps(valid)
        if payload.onboarding_state is not None:
            profile.onboarding_state = payload.onboarding_state
        db.commit()
        return _serialize_profile(profile)
    finally:
        db.close()


@router.get("/home")
def get_home(admin: AdminPrincipal = RequireAdmin) -> dict:
    """Everything the dashboard shows, in one call."""
    db = SessionLocal()
    try:
        profile = _get_or_create_profile(db, admin.user_id)
        readiness = readiness_service.compute_readiness(db, user_id=admin.user_id, profile=profile)
        profile.readiness_json = json.dumps(readiness)
        db.commit()

        today = _now().date()
        plan_today = (
            db.query(CelpipStudyPlanItem)
            .filter(CelpipStudyPlanItem.user_id == admin.user_id)
            .filter(CelpipStudyPlanItem.scheduled_for == today)
            .order_by(CelpipStudyPlanItem.estimated_minutes.desc())
            .all()
        )
        overdue = (
            db.query(CelpipStudyPlanItem)
            .filter(CelpipStudyPlanItem.user_id == admin.user_id)
            .filter(CelpipStudyPlanItem.status == "pending")
            .filter(CelpipStudyPlanItem.scheduled_for < today)
            .count()
        )
        week_done = (
            db.query(CelpipStudyPlanItem)
            .filter(CelpipStudyPlanItem.user_id == admin.user_id)
            .filter(CelpipStudyPlanItem.status == "completed")
            .count()
        )
        week_total = (
            db.query(CelpipStudyPlanItem)
            .filter(CelpipStudyPlanItem.user_id == admin.user_id)
            .count()
        )

        recent = (
            db.query(CelpipAttempt)
            .filter(CelpipAttempt.user_id == admin.user_id)
            .order_by(CelpipAttempt.created_at.desc())
            .limit(5)
            .all()
        )
        tests = {
            row.id: row for row in
            db.query(CelpipTest).filter(CelpipTest.id.in_([a.test_id for a in recent])).all()
        } if recent else {}

        resumable = next(
            (a for a in recent if a.status == "in_progress"), None
        )

        weaknesses = planner.measured_weaknesses(db, admin.user_id, limit=5)

        return {
            "profile": _serialize_profile(profile),
            "readiness": readiness,
            "today": [_serialize_plan_item(i) for i in plan_today],
            "overdue_count": overdue,
            "plan_progress": {"completed": week_done, "total": week_total},
            "weakest": [
                {"tag": tag, "label": WEAKNESS_TAGS[tag], "count": count}
                for tag, count in weaknesses
            ],
            "weakest_tasks": [
                {"task_key": key, "label": TASKS_BY_KEY[key].label}
                for key in planner.weakest_tasks(db, admin.user_id)
                if key in TASKS_BY_KEY
            ],
            "recent_attempts": [
                {
                    "attempt_id": a.id,
                    "status": a.status,
                    "label": tests[a.test_id].label if a.test_id in tests else "",
                    "mode": tests[a.test_id].mode if a.test_id in tests else "",
                    "created_at": _iso(a.created_at),
                    "completed_at": _iso(a.completed_at),
                }
                for a in recent
            ],
            "resume_attempt_id": resumable.id if resumable else None,
            "speech_configured": speech.is_configured(),
        }
    finally:
        db.close()


# --- Question bank --------------------------------------------------------

class GenerateIn(BaseModel):
    task_key: str
    count: int = Field(default=1, ge=1, le=10)
    difficulty: int = Field(default=9, ge=1, le=12)
    topic_hint: str = ""


@router.get("/bank")
def get_bank(
    task_key: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    admin: AdminPrincipal = RequireAdmin,
) -> dict:
    db = SessionLocal()
    try:
        query = db.query(CelpipQuestion)
        if task_key:
            query = query.filter(CelpipQuestion.task_key == task_key)
        if status:
            query = query.filter(CelpipQuestion.status == status)
        rows = query.order_by(CelpipQuestion.created_at.desc()).limit(limit).all()
        counts = assembly.available_counts(db)
        return {
            "questions": [_serialize_question(r, detail=False) for r in rows],
            "coverage": [
                {
                    "task_key": task.key,
                    "label": task.label,
                    "skill": task.skill,
                    "part": task.part,
                    "ready": counts.get(task.key, 0),
                }
                for task in ALL_TASKS
            ],
        }
    finally:
        db.close()


def _serialize_question(row: CelpipQuestion, *, detail: bool) -> dict:
    data = {
        "id": row.id,
        "skill": row.skill,
        "task_key": row.task_key,
        "label": TASKS_BY_KEY[row.task_key].label if row.task_key in TASKS_BY_KEY else row.task_key,
        "part": row.part,
        "title": row.title,
        "topic": row.topic,
        "difficulty": row.difficulty,
        "status": row.status,
        "source": row.source,
        "times_served": row.times_served,
        "last_served_at": _iso(row.last_served_at),
        "approved_at": _iso(row.approved_at),
        "generator_model": row.generator_model,
        "validator_model": row.validator_model,
        "created_at": _iso(row.created_at),
    }
    if detail:
        data["payload"] = _loads(row.payload_json, {})
        data["validation"] = _loads(row.validation_json, {})
    return data


@router.get("/bank/{question_id}")
def get_bank_item(question_id: str, admin: AdminPrincipal = RequireAdmin) -> dict:
    db = SessionLocal()
    try:
        row = db.get(CelpipQuestion, question_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Question not found.")
        data = _serialize_question(row, detail=True)
        data["assets"] = assets_service.asset_payload(db, row.id)
        return data
    finally:
        db.close()


@router.post("/bank/generate")
def generate_items(payload: GenerateIn, admin: AdminPrincipal = RequireAdmin) -> dict:
    if payload.task_key not in TASKS_BY_KEY:
        raise HTTPException(status_code=400, detail=f"Unknown task type {payload.task_key!r}.")
    return generation.enqueue_generation(
        user_id=admin.user_id,
        task_key=payload.task_key,
        count=payload.count,
        difficulty=payload.difficulty,
        topic_hint=payload.topic_hint,
    )


@router.get("/bank/runs/list")
def list_generation_runs(
    limit: int = Query(default=25, ge=1, le=100), admin: AdminPrincipal = RequireAdmin,
) -> dict:
    db = SessionLocal()
    try:
        rows = (
            db.query(CelpipGenerationRun)
            .filter(CelpipGenerationRun.user_id == admin.user_id)
            .order_by(CelpipGenerationRun.created_at.desc())
            .limit(limit)
            .all()
        )
        return {
            "runs": [
                {
                    "id": r.id,
                    "task_key": r.task_key,
                    "label": TASKS_BY_KEY[r.task_key].label if r.task_key in TASKS_BY_KEY else r.task_key,
                    "status": r.status,
                    "requested": r.requested_count,
                    "accepted": r.accepted_count,
                    "rejected": r.rejected_count,
                    # The rejections are the useful half: a task type that keeps
                    # failing validation is a prompt problem, not bad luck.
                    "rejections": _loads(r.rejections_json, []),
                    "error": r.error,
                    "created_at": _iso(r.created_at),
                    "completed_at": _iso(r.completed_at),
                }
                for r in rows
            ]
        }
    finally:
        db.close()


class BankActionIn(BaseModel):
    action: str = Field(pattern="^(approve|disable|enable|retire)$")


@router.post("/bank/{question_id}/action")
def act_on_item(question_id: str, payload: BankActionIn, admin: AdminPrincipal = RequireAdmin) -> dict:
    db = SessionLocal()
    try:
        row = db.get(CelpipQuestion, question_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Question not found.")
        if payload.action == "approve":
            # A quality signal that biases assembly, never a gate: validated
            # items are already servable.
            row.approved_at = _now()
        elif payload.action == "disable":
            row.status = "disabled"
        elif payload.action == "enable":
            row.status = "ready"
        elif payload.action == "retire":
            row.status = "retired"
        db.commit()
        return _serialize_question(row, detail=False)
    finally:
        db.close()


@router.post("/bank/{question_id}/rebuild-assets")
def rebuild_assets(question_id: str, admin: AdminPrincipal = RequireAdmin) -> dict:
    from app.services import maintenance_jobs

    db = SessionLocal()
    try:
        if db.get(CelpipQuestion, question_id) is None:
            raise HTTPException(status_code=404, detail="Question not found.")
    finally:
        db.close()
    return {"job_id": maintenance_jobs.enqueue_celpip_assets(question_id=question_id)}


@router.get("/media/{asset_id}")
def get_media(asset_id: str, admin: AdminPrincipal = RequireAdmin) -> Response:
    """Stream a generated asset.

    Served through the API rather than by presigned URL so the admin check
    applies: a listening audio URL is the answer key to a listening item.
    """
    from app.services.blob_store import store_for_location

    db = SessionLocal()
    try:
        asset = db.get(CelpipQuestionAsset, asset_id)
        if asset is None or not asset.blob_location or asset.status != "ready":
            raise HTTPException(status_code=404, detail="Asset not available.")
        location = asset.blob_location
        content_type = asset.content_type or "application/octet-stream"
    finally:
        db.close()

    try:
        payload = store_for_location(location).read(location)
    except Exception as exc:
        logger.warning("celpip asset read failed for %s: %s", asset_id, exc)
        raise HTTPException(status_code=404, detail="Asset could not be read.") from exc
    return Response(content=payload, media_type=content_type)


# --- Tests and attempts ---------------------------------------------------

class AssembleIn(BaseModel):
    mode: str = Field(pattern="^(full|full_ls|component|custom|diagnostic|single_task)$")
    practice_mode: str = Field(default="timed", pattern="^(learn|timed|simulation)$")
    components: list[str] | None = None
    task_keys: list[str] | None = None
    repeats: int = Field(default=1, ge=1, le=5)
    # Bounded to the column width: Postgres enforces VARCHAR(255), SQLite does
    # not, so an unbounded label passes every local test and fails in production.
    label: str = Field(default="", max_length=255)


@router.post("/tests")
def create_test(payload: AssembleIn, admin: AdminPrincipal = RequireAdmin) -> dict:
    db = SessionLocal()
    try:
        profile = _get_or_create_profile(db, admin.user_id)
        test_type = profile.test_type
        target = profile.target_level
    finally:
        db.close()

    try:
        test = assembly.assemble_test(
            user_id=admin.user_id,
            mode=payload.mode,
            practice_mode=payload.practice_mode,
            test_type=test_type,
            components=payload.components,
            task_keys=payload.task_keys,
            repeats=payload.repeats,
            target_level=target,
            label=payload.label,
        )
    except assembly.NotEnoughItems as exc:
        # Actionable rather than a bare 409: the caller is told exactly which
        # task types to generate more of.
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "shortfalls": exc.shortfalls,
                "hint": "Generate more items for these task types in the Question Bank.",
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    attempt = attempts_service.create_attempt(
        user_id=admin.user_id, test_id=test["test_id"], practice_mode=payload.practice_mode,
    )
    return {**test, "attempt_id": attempt["attempt_id"]}


@router.get("/attempts")
def list_attempts(
    limit: int = Query(default=30, ge=1, le=100), admin: AdminPrincipal = RequireAdmin,
) -> dict:
    db = SessionLocal()
    try:
        rows = (
            db.query(CelpipAttempt)
            .filter(CelpipAttempt.user_id == admin.user_id)
            .order_by(CelpipAttempt.created_at.desc())
            .limit(limit)
            .all()
        )
        tests = {
            t.id: t for t in
            db.query(CelpipTest).filter(CelpipTest.id.in_([r.test_id for r in rows])).all()
        } if rows else {}
        out = []
        for row in rows:
            test = tests.get(row.test_id)
            results = _loads(row.results_json, {})
            out.append({
                "attempt_id": row.id,
                "label": test.label if test else "",
                "mode": test.mode if test else "",
                "practice_mode": row.practice_mode,
                "status": row.status,
                "created_at": _iso(row.created_at),
                "completed_at": _iso(row.completed_at),
                "levels": {
                    skill: (data.get("level") or {})
                    for skill, data in results.items()
                },
            })
        return {"attempts": out}
    finally:
        db.close()


@router.get("/attempts/{attempt_id}")
def get_attempt(attempt_id: str, admin: AdminPrincipal = RequireAdmin) -> dict:
    try:
        return attempts_service.attempt_state(user_id=admin.user_id, attempt_id=attempt_id)
    except attempts_service.AttemptError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/attempts/{attempt_id}/sections/{skill}/start")
def start_section(attempt_id: str, skill: str, admin: AdminPrincipal = RequireAdmin) -> dict:
    try:
        return attempts_service.start_section(
            user_id=admin.user_id, attempt_id=attempt_id, skill=skill
        )
    except attempts_service.AttemptError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/attempts/{attempt_id}/sections/{skill}/complete")
def complete_section(
    attempt_id: str, skill: str, auto: bool = Query(default=False),
    admin: AdminPrincipal = RequireAdmin,
) -> dict:
    try:
        return attempts_service.complete_section(
            user_id=admin.user_id, attempt_id=attempt_id, skill=skill, auto=auto
        )
    except attempts_service.AttemptError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/attempts/{attempt_id}/questions/{question_id}")
def get_attempt_question(
    attempt_id: str, question_id: str, admin: AdminPrincipal = RequireAdmin,
) -> dict:
    try:
        return attempts_service.question_for_attempt(
            user_id=admin.user_id, attempt_id=attempt_id, question_id=question_id
        )
    except attempts_service.AttemptError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class ResponseIn(BaseModel):
    question_id: str
    question_index: int = 0
    selected_option: str | None = None
    response_text: str | None = None
    time_spent_ms: int = 0
    flagged: bool | None = None


@router.post("/attempts/{attempt_id}/responses")
def save_response(
    attempt_id: str, payload: ResponseIn, admin: AdminPrincipal = RequireAdmin,
) -> dict:
    try:
        return attempts_service.save_response(
            user_id=admin.user_id,
            attempt_id=attempt_id,
            question_id=payload.question_id,
            question_index=payload.question_index,
            selected_option=payload.selected_option,
            response_text=payload.response_text,
            time_spent_ms=payload.time_spent_ms,
            flagged=payload.flagged,
        )
    except attempts_service.AttemptError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/attempts/{attempt_id}/responses/audio")
async def save_audio(
    attempt_id: str,
    question_id: str = Form(...),
    duration_seconds: float = Form(default=0.0),
    file: UploadFile = File(...),
    admin: AdminPrincipal = RequireAdmin,
) -> dict:
    audio = await file.read()
    if not audio:
        raise HTTPException(status_code=400, detail="No audio was received.")
    if len(audio) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Recording is too large.")
    try:
        return attempts_service.save_audio_response(
            user_id=admin.user_id,
            attempt_id=attempt_id,
            question_id=question_id,
            audio=audio,
            content_type=file.content_type or "audio/webm",
            duration_seconds=duration_seconds,
        )
    except attempts_service.AttemptError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/attempts/{attempt_id}/submit")
def submit_attempt(attempt_id: str, admin: AdminPrincipal = RequireAdmin) -> dict:
    try:
        return attempts_service.submit_attempt(user_id=admin.user_id, attempt_id=attempt_id)
    except attempts_service.AttemptError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/attempts/{attempt_id}/responses/{response_id}/audio")
def get_response_audio(
    attempt_id: str, response_id: str, admin: AdminPrincipal = RequireAdmin,
) -> Response:
    """Play back a recorded speaking response, after the attempt is scored."""
    from app.services.blob_store import store_for_location

    db = SessionLocal()
    try:
        attempt = db.get(CelpipAttempt, attempt_id)
        if attempt is None or attempt.user_id != admin.user_id:
            raise HTTPException(status_code=404, detail="Attempt not found.")
        response = db.get(CelpipResponse, response_id)
        if response is None or response.attempt_id != attempt_id or not response.audio_blob_location:
            raise HTTPException(status_code=404, detail="Recording not found.")
        location = response.audio_blob_location
    finally:
        db.close()

    try:
        payload = store_for_location(location).read(location)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Recording could not be read.") from exc
    return Response(content=payload, media_type="audio/webm")


# --- Results --------------------------------------------------------------

@router.get("/attempts/{attempt_id}/results")
def get_results(attempt_id: str, admin: AdminPrincipal = RequireAdmin) -> dict:
    db = SessionLocal()
    try:
        attempt = db.get(CelpipAttempt, attempt_id)
        if attempt is None or attempt.user_id != admin.user_id:
            raise HTTPException(status_code=404, detail="Attempt not found.")
        test = db.get(CelpipTest, attempt.test_id)
        results = _loads(attempt.results_json, {})

        evaluations = (
            db.query(CelpipEvaluation)
            .filter(CelpipEvaluation.attempt_id == attempt_id)
            .all()
        )
        responses = {
            r.id: r for r in
            db.query(CelpipResponse).filter(CelpipResponse.attempt_id == attempt_id).all()
        }

        serialized = []
        for evaluation in evaluations:
            response = responses.get(evaluation.response_id) if evaluation.response_id else None
            task = TASKS_BY_KEY.get(evaluation.task_key)
            serialized.append({
                "id": evaluation.id,
                "skill": evaluation.skill,
                "task_key": evaluation.task_key,
                "label": task.label if task else evaluation.task_key,
                "status": evaluation.status,
                "level": {"low": evaluation.level_low, "high": evaluation.level_high},
                "dimensions": _loads(evaluation.dimensions_json, {}),
                "confidence": evaluation.confidence,
                "feedback": _loads(evaluation.feedback_json, {}),
                "delivery_metrics": _loads(evaluation.delivery_metrics_json, {}),
                "weakness_tags": _loads(evaluation.weakness_tags_json, []),
                "has_exemplar": bool(evaluation.exemplar_json and evaluation.exemplar_json != "{}"),
                "exemplar": _loads(evaluation.exemplar_json, {}),
                # Model identities and rubric version travel with the score so
                # an old result stays interpretable after either changes.
                "provenance": {
                    "evaluator_a": evaluation.evaluator_a_model,
                    "evaluator_b": evaluation.evaluator_b_model,
                    "reconciler": evaluation.reconciler_model,
                    "rubric_version": evaluation.rubric_version,
                    "scored_at": _iso(evaluation.completed_at),
                },
                "response": {
                    "id": response.id,
                    "text": response.response_text,
                    "transcript": response.transcript,
                    "has_audio": bool(response.audio_blob_location),
                    "duration_seconds": response.audio_duration_seconds,
                } if response else None,
                "error": evaluation.error,
            })

        # Whether scoring is still in flight, including between a failed run
        # and its retry. The client polls on this rather than on the attempt
        # status alone: a crashed worker leaves the attempt mid-state, and a
        # retried failure would otherwise never be picked up without a reload.
        from app.services import maintenance_jobs

        try:
            job = maintenance_jobs.celpip_evaluation_job_state(attempt.id)
        except Exception:
            job = None

        return {
            "attempt_id": attempt.id,
            "label": test.label if test else "",
            "mode": test.mode if test else "",
            "practice_mode": attempt.practice_mode,
            "status": attempt.status,
            "submitted_at": _iso(attempt.submitted_at),
            "completed_at": _iso(attempt.completed_at),
            "components": results,
            "evaluations": serialized,
            "error": attempt.error,
            "evaluation_pending": bool(job and job["active"]),
            "evaluation_job": job,
        }
    finally:
        db.close()


@router.post("/evaluations/{evaluation_id}/exemplar")
def build_exemplar(evaluation_id: str, admin: AdminPrincipal = RequireAdmin) -> dict:
    db = SessionLocal()
    try:
        evaluation = db.get(CelpipEvaluation, evaluation_id)
        if evaluation is None:
            raise HTTPException(status_code=404, detail="Evaluation not found.")
        attempt = db.get(CelpipAttempt, evaluation.attempt_id)
        if attempt is None or attempt.user_id != admin.user_id:
            raise HTTPException(status_code=404, detail="Evaluation not found.")
    finally:
        db.close()

    try:
        return scoring.generate_exemplar(evaluation_id)
    except Exception as exc:
        logger.warning("celpip exemplar generation failed for %s: %s", evaluation_id, exc)
        raise HTTPException(
            status_code=502, detail="Could not generate an improved response. Try again."
        ) from exc


# --- Study plan -----------------------------------------------------------

def _serialize_plan_item(item: CelpipStudyPlanItem) -> dict:
    return {
        "id": item.id,
        "scheduled_for": _iso(item.scheduled_for),
        "week_index": item.week_index,
        "activity_type": item.activity_type,
        "title": item.title,
        "rationale": item.rationale,
        "skill": item.skill,
        "task_keys": _loads(item.task_keys_json, []),
        "weakness_tags": _loads(item.weakness_tags_json, []),
        "lesson_id": item.lesson_id,
        "estimated_minutes": item.estimated_minutes,
        "status": item.status,
        "attempt_id": item.attempt_id,
        "rescheduled": _loads(item.reschedule_history_json, []),
    }


@router.get("/plan")
def get_plan(admin: AdminPrincipal = RequireAdmin) -> dict:
    db = SessionLocal()
    try:
        items = (
            db.query(CelpipStudyPlanItem)
            .filter(CelpipStudyPlanItem.user_id == admin.user_id)
            .order_by(CelpipStudyPlanItem.scheduled_for.asc())
            .all()
        )
        by_week: dict[int, list[dict]] = {}
        for item in items:
            by_week.setdefault(item.week_index, []).append(_serialize_plan_item(item))
        return {
            "weeks": [{"week": week, "items": entries} for week, entries in sorted(by_week.items())],
            "total": len(items),
            "completed": sum(1 for i in items if i.status == "completed"),
        }
    finally:
        db.close()


@router.post("/plan/regenerate")
def regenerate_plan(admin: AdminPrincipal = RequireAdmin) -> dict:
    db = SessionLocal()
    try:
        _get_or_create_profile(db, admin.user_id)
        try:
            return planner.regenerate_plan(db, user_id=admin.user_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        db.close()


@router.post("/plan/roll-forward")
def roll_forward(admin: AdminPrincipal = RequireAdmin) -> dict:
    db = SessionLocal()
    try:
        return planner.roll_forward_missed(db, user_id=admin.user_id)
    finally:
        db.close()


class PlanItemStatusIn(BaseModel):
    status: str = Field(pattern="^(pending|in_progress|completed|skipped|deferred)$")
    attempt_id: str | None = None


@router.post("/plan/items/{item_id}/status")
def set_plan_item_status(
    item_id: str, payload: PlanItemStatusIn, admin: AdminPrincipal = RequireAdmin,
) -> dict:
    db = SessionLocal()
    try:
        item = db.get(CelpipStudyPlanItem, item_id)
        if item is None or item.user_id != admin.user_id:
            raise HTTPException(status_code=404, detail="Plan item not found.")
        item.status = payload.status
        if payload.attempt_id:
            item.attempt_id = payload.attempt_id
        if payload.status == "completed":
            item.completed_at = _now()
        db.commit()
        return _serialize_plan_item(item)
    finally:
        db.close()


@router.get("/progress")
def get_progress(admin: AdminPrincipal = RequireAdmin) -> dict:
    """Trends: component levels over time, weakness frequency, task accuracy."""
    db = SessionLocal()
    try:
        profile = _get_or_create_profile(db, admin.user_id)
        readiness = readiness_service.compute_readiness(db, user_id=admin.user_id, profile=profile)

        attempt_ids = [
            row[0] for row in
            db.query(CelpipAttempt.id).filter(CelpipAttempt.user_id == admin.user_id).all()
        ]
        task_accuracy: dict[str, dict] = {}
        for attempt in (
            db.query(CelpipAttempt)
            .filter(CelpipAttempt.user_id == admin.user_id)
            .filter(CelpipAttempt.status == "completed")
            .all()
        ):
            for component in _loads(attempt.results_json, {}).values():
                for task_key, counts in (component.get("accuracy_by_task") or {}).items():
                    bucket = task_accuracy.setdefault(task_key, {"correct": 0, "total": 0})
                    bucket["correct"] += counts.get("correct", 0)
                    bucket["total"] += counts.get("total", 0)

        task_levels: dict[str, list[float]] = {}
        if attempt_ids:
            for evaluation in (
                db.query(CelpipEvaluation)
                .filter(CelpipEvaluation.attempt_id.in_(attempt_ids))
                .filter(CelpipEvaluation.status == "complete")
                .all()
            ):
                if evaluation.level_low is None or not evaluation.task_key:
                    continue
                task_levels.setdefault(evaluation.task_key, []).append(
                    (evaluation.level_low + (evaluation.level_high or evaluation.level_low)) / 2
                )

        return {
            "readiness": readiness,
            "component_levels": readiness["component_levels"],
            "weaknesses": [
                {"tag": tag, "label": WEAKNESS_TAGS[tag], "count": count}
                for tag, count in planner.measured_weaknesses(db, admin.user_id, limit=15)
            ],
            "task_accuracy": [
                {
                    "task_key": key,
                    "label": TASKS_BY_KEY[key].label if key in TASKS_BY_KEY else key,
                    "correct": data["correct"],
                    "total": data["total"],
                    "accuracy": round(data["correct"] / data["total"], 3) if data["total"] else None,
                }
                for key, data in sorted(task_accuracy.items())
            ],
            "task_levels": [
                {
                    "task_key": key,
                    "label": TASKS_BY_KEY[key].label if key in TASKS_BY_KEY else key,
                    "average_level": round(sum(values) / len(values), 1),
                    "attempts": len(values),
                }
                for key, values in sorted(task_levels.items())
            ],
        }
    finally:
        db.close()
