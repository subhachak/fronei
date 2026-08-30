"""Scoring orchestration and the study planner, with the models stubbed out.

The two evaluator passes are replaced with scripted responses so the behaviour
that actually matters can be asserted: that disagreement between passes widens
the reported range and lowers confidence rather than being averaged away, that
both passes are stored, and that the plan rebalances without destroying work
the learner has already done.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import (
    Base,
    CelpipAttempt,
    CelpipEvaluation,
    CelpipProfile,
    CelpipQuestion,
    CelpipResponse,
    CelpipStudyPlanItem,
    CelpipTest,
    CelpipTestItem,
)
from app.services.celpip import assembly, generation, planner, readiness, scoring

USER = "admin_1"


@pytest.fixture(autouse=True)
def no_background_topup(monkeypatch):
    """Assembly refills the buffer it consumed; these tests must not queue real
    generation work to do it."""
    from app.services.celpip import stock

    monkeypatch.setattr(
        stock, "plan_topup",
        lambda *, user_id, task_keys=None: {"queued": {}, "deficits": {}, "target": 0},
    )


@pytest.fixture
def db_factory(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)
    for module in (assembly, generation, planner, readiness, scoring):
        if hasattr(module, "SessionLocal"):
            monkeypatch.setattr(module, "SessionLocal", factory)
    return factory


def _evaluation(levels: dict[str, int], confidence: float = 0.8) -> dict:
    return {
        "dimensions": {
            dim: {"level": level, "evidence": [f"quote for {dim}"], "comment": "c"}
            for dim, level in levels.items()
        },
        "overall_level": round(sum(levels.values()) / len(levels)),
        "confidence": confidence,
        "met_requirements": ["addressed the first point"],
        "missing_requirements": ["did not propose a time"],
        "corrections": [
            {"severity": "high", "original": "I go yesterday", "corrected": "I went yesterday", "why": "past tense"}
        ],
        "patterns": ["tense drift"],
        "weakness_tags": ["verb_tense", "idea_development"],
        "outline": ["state the problem", "give two reasons"],
        "strengths": ["clear opening"],
    }


def _stub_scorers(monkeypatch, a: dict, b: dict, reconciled: dict | None = None):
    calls: list[str] = []

    def fake_pass(task_key, context, *, pass_label, role):
        calls.append(role)
        return (a if pass_label == "A" else b), f"model-{pass_label}"

    def fake_reconcile(task_key, context, ea, eb):
        calls.append("reconcile")
        return reconciled or {}, "model-R"

    monkeypatch.setattr(scoring, "_evaluator_pass", fake_pass)
    monkeypatch.setattr(scoring, "_reconcile", fake_reconcile)
    return calls


def _writing_attempt(db_factory) -> tuple[str, str]:
    db = db_factory()
    try:
        db.add(CelpipQuestion(
            id="cq_email", skill="writing", task_key="writing_email", part=1, status="ready",
            payload_json=json.dumps({
                "stimulus": {"prompt": "Write to your manager.", "bullets": ["explain", "propose"]}
            }),
        ))
        db.add(CelpipTest(id="ctest_1", user_id=USER, label="Writing drill", mode="single_task",
                          components_json=json.dumps(["writing"]), practice_mode="timed"))
        db.add(CelpipTestItem(id="citem_1", test_id="ctest_1", question_id="cq_email",
                              skill="writing", task_key="writing_email", position=0))
        db.add(CelpipAttempt(id="cattempt_1", user_id=USER, test_id="ctest_1",
                             practice_mode="timed", status="submitted"))
        db.add(CelpipResponse(id="cresp_1", attempt_id="cattempt_1", question_id="cq_email",
                              skill="writing", task_key="writing_email",
                              response_text="I go yesterday to the office and speak with my manager."))
        db.commit()
    finally:
        db.close()
    return "cattempt_1", "cresp_1"


def test_agreeing_evaluators_produce_a_single_level_and_no_reconciliation(db_factory, monkeypatch):
    levels = {"content_coherence": 9, "vocabulary": 9, "readability": 9, "task_fulfillment": 9}
    calls = _stub_scorers(monkeypatch, _evaluation(levels), _evaluation(levels))
    attempt_id, _ = _writing_attempt(db_factory)

    scoring.evaluate_attempt(attempt_id)

    db = db_factory()
    try:
        evaluation = db.query(CelpipEvaluation).one()
    finally:
        db.close()

    assert evaluation.status == "complete"
    assert evaluation.level_low == evaluation.level_high == 9
    assert evaluation.confidence >= 0.8
    assert "reconcile" not in calls


def test_disagreeing_evaluators_widen_the_range_and_lower_confidence(db_factory, monkeypatch):
    """The honest output of two passes three levels apart is 'somewhere in
    between, and we are not sure' — not their average presented as a fact."""
    high = {"content_coherence": 10, "vocabulary": 10, "readability": 10, "task_fulfillment": 10}
    low = {"content_coherence": 6, "vocabulary": 7, "readability": 6, "task_fulfillment": 7}
    calls = _stub_scorers(
        monkeypatch, _evaluation(high), _evaluation(low),
        reconciled={
            "dimensions": {k: {"level": 8, "comment": "c"} for k in high},
            "confidence": 0.45,
            "disagreements": [{"dimension": "vocabulary", "a": 10, "b": 7, "resolution": "split"}],
            "summary": "Borderline.",
        },
    )
    attempt_id, _ = _writing_attempt(db_factory)

    scoring.evaluate_attempt(attempt_id)

    db = db_factory()
    try:
        evaluation = db.query(CelpipEvaluation).one()
    finally:
        db.close()

    assert "reconcile" in calls
    assert evaluation.level_low < evaluation.level_high, "disagreement must widen the range"
    assert evaluation.level_low <= 8 <= evaluation.level_high
    assert evaluation.confidence <= 0.5, "disagreement must lower confidence"
    assert json.loads(evaluation.reconciliation_json)["summary"] == "Borderline."


def test_both_evaluator_passes_are_stored_verbatim(db_factory, monkeypatch):
    """A score the learner reorganises a week around has to be auditable."""
    a = _evaluation({"content_coherence": 10, "vocabulary": 9, "readability": 9, "task_fulfillment": 10})
    b = _evaluation({"content_coherence": 7, "vocabulary": 7, "readability": 8, "task_fulfillment": 7})
    _stub_scorers(monkeypatch, a, b, reconciled={"dimensions": {}, "confidence": 0.5})
    attempt_id, _ = _writing_attempt(db_factory)

    scoring.evaluate_attempt(attempt_id)

    db = db_factory()
    try:
        evaluation = db.query(CelpipEvaluation).one()
    finally:
        db.close()

    assert json.loads(evaluation.evaluator_a_json)["dimensions"]["content_coherence"]["level"] == 10
    assert json.loads(evaluation.evaluator_b_json)["dimensions"]["content_coherence"]["level"] == 7
    assert evaluation.evaluator_a_model == "model-A"
    assert evaluation.evaluator_b_model == "model-B"
    assert evaluation.rubric_version


def test_an_empty_response_is_not_sent_to_a_model_at_all(db_factory, monkeypatch):
    """Scoring nothing with a model produces a confident-looking number from
    nothing. It is scored as unattempted instead."""
    called: list[str] = []
    monkeypatch.setattr(
        scoring, "_evaluator_pass",
        lambda *a, **k: (called.append("x"), ({}, ""))[1],
    )
    db = db_factory()
    try:
        db.add(CelpipQuestion(id="cq_email", skill="writing", task_key="writing_email", part=1,
                              status="ready", payload_json=json.dumps({"stimulus": {"prompt": "p"}})))
        db.add(CelpipTest(id="ctest_1", user_id=USER, mode="single_task",
                          components_json=json.dumps(["writing"])))
        db.add(CelpipTestItem(id="citem_1", test_id="ctest_1", question_id="cq_email",
                              skill="writing", task_key="writing_email", position=0))
        db.add(CelpipAttempt(id="cattempt_1", user_id=USER, test_id="ctest_1", status="submitted"))
        db.add(CelpipResponse(id="cresp_1", attempt_id="cattempt_1", question_id="cq_email",
                              skill="writing", task_key="writing_email", response_text="   "))
        db.commit()
    finally:
        db.close()

    scoring.evaluate_attempt("cattempt_1")

    db = db_factory()
    try:
        evaluation = db.query(CelpipEvaluation).one()
    finally:
        db.close()
    assert called == []
    assert evaluation.level_low == 0
    assert json.loads(evaluation.weakness_tags_json) == ["incomplete_response"]


def test_receptive_rollup_excludes_unscored_and_practice_items(db_factory):
    """Unscored content is delivered like anything else and dropped here. If it
    counted, the mock would score differently from the real test."""
    payload = {
        "stimulus": {"paragraphs": []},
        "questions": [
            {"prompt": f"q{i}", "options": {"A": "a", "B": "b"}, "answer": "A",
             "evidence": "e", "rationales": {"A": "x", "B": "y"}}
            for i in range(4)
        ],
    }
    db = db_factory()
    try:
        for qid in ("cq_scored", "cq_unscored"):
            db.add(CelpipQuestion(id=qid, skill="reading", task_key="reading_information", part=3,
                                  status="ready", payload_json=json.dumps(payload)))
        db.add(CelpipTest(id="ctest_1", user_id=USER, mode="full",
                          components_json=json.dumps(["reading"])))
        db.add(CelpipTestItem(id="citem_1", test_id="ctest_1", question_id="cq_scored",
                              skill="reading", task_key="reading_information", position=0))
        db.add(CelpipTestItem(id="citem_2", test_id="ctest_1", question_id="cq_unscored",
                              skill="reading", task_key="reading_information", position=1,
                              is_unscored=True))
        db.add(CelpipAttempt(id="cattempt_1", user_id=USER, test_id="ctest_1", status="submitted"))
        for index in range(4):
            db.add(CelpipResponse(id=f"cresp_s{index}", attempt_id="cattempt_1",
                                  question_id="cq_scored", skill="reading",
                                  task_key="reading_information", question_index=index,
                                  selected_option="A"))
            db.add(CelpipResponse(id=f"cresp_u{index}", attempt_id="cattempt_1",
                                  question_id="cq_unscored", skill="reading",
                                  task_key="reading_information", question_index=index,
                                  selected_option="B"))
        db.commit()
    finally:
        db.close()

    scoring.evaluate_attempt("cattempt_1")

    db = db_factory()
    try:
        results = json.loads(db.get(CelpipAttempt, "cattempt_1").results_json)
    finally:
        db.close()
    # Only the four scored questions count, all answered correctly.
    assert results["reading"]["raw_score"] == 4
    assert results["reading"]["max_score"] == 4


# --- Planner --------------------------------------------------------------

def _profile(db, *, days_out: int = 28) -> CelpipProfile:
    profile = CelpipProfile(
        id="cprofile_1", user_id=USER, test_type="general", target_level=9,
        weekday_hours=1.5, weekend_hours=3.0,
        test_date=datetime.now(timezone.utc).date() + timedelta(days=days_out),
    )
    db.add(profile)
    db.commit()
    return profile


def test_plan_covers_every_week_and_respects_the_daily_budget(db_factory):
    db = db_factory()
    try:
        _profile(db)
        result = planner.regenerate_plan(db, user_id=USER)
        items = db.query(CelpipStudyPlanItem).all()

        assert result["created"] > 0
        assert {i.week_index for i in items} == {1, 2, 3, 4}

        by_day: dict[date, int] = {}
        for item in items:
            by_day[item.scheduled_for] = by_day.get(item.scheduled_for, 0) + item.estimated_minutes
        for day, minutes in by_day.items():
            budget = 180 if day.weekday() >= 5 else 90
            assert minutes <= budget, f"{day} scheduled {minutes} minutes against a {budget} budget"
    finally:
        db.close()


def test_the_last_two_days_are_deliberately_light(db_factory):
    db = db_factory()
    try:
        profile = _profile(db, days_out=28)
        planner.regenerate_plan(db, user_id=USER)
        final_days = sorted({i.scheduled_for for i in db.query(CelpipStudyPlanItem).all()})[-2:]
        for day in final_days:
            minutes = sum(
                i.estimated_minutes for i in
                db.query(CelpipStudyPlanItem).filter(CelpipStudyPlanItem.scheduled_for == day).all()
            )
            assert minutes <= planner.TAPER_MINUTES
        assert profile.test_date is not None
    finally:
        db.close()


def test_rebalancing_preserves_work_already_done(db_factory):
    db = db_factory()
    try:
        _profile(db)
        planner.regenerate_plan(db, user_id=USER)
        future = (
            db.query(CelpipStudyPlanItem)
            .filter(CelpipStudyPlanItem.scheduled_for > datetime.now(timezone.utc).date())
            .first()
        )
        future.status = "completed"
        db.commit()
        completed_id = future.id

        planner.regenerate_plan(db, user_id=USER)
        survivor = db.get(CelpipStudyPlanItem, completed_id)
    finally:
        db.close()
    assert survivor is not None, "a rebalance must not delete an activity already completed"
    assert survivor.status == "completed"


def test_missed_work_moves_forward_without_doubling_today(db_factory):
    db = db_factory()
    try:
        _profile(db)
        today = datetime.now(timezone.utc).date()
        # Four missed activities, far more than one day's budget.
        for i, activity in enumerate(("full_mock", "drill", "vocabulary", "lesson")):
            db.add(CelpipStudyPlanItem(
                id=f"cplan_missed_{i}", user_id=USER, scheduled_for=today - timedelta(days=2),
                week_index=2, activity_type=activity, title=activity, estimated_minutes=
                planner.ACTIVITY_MINUTES[activity], status="pending",
            ))
        db.commit()

        result = planner.roll_forward_missed(db, user_id=USER)
        moved_today = sum(
            i.estimated_minutes for i in
            db.query(CelpipStudyPlanItem)
            .filter(CelpipStudyPlanItem.scheduled_for == today)
            .filter(CelpipStudyPlanItem.status == "pending").all()
        )
        skipped = db.query(CelpipStudyPlanItem).filter(
            CelpipStudyPlanItem.status == "skipped").all()
    finally:
        db.close()

    budget = 180 if datetime.now(timezone.utc).date().weekday() >= 5 else 90
    assert moved_today <= budget
    assert result["skipped"] == len(skipped)
    # Whatever did not fit is dropped with a reason, not silently accumulated.
    for item in skipped:
        assert json.loads(item.reschedule_history_json)[-1]["reason"]


def test_readiness_is_zero_with_no_practice_and_names_the_gaps(db_factory):
    db = db_factory()
    try:
        _profile(db)
        result = readiness.compute_readiness(db, user_id=USER)
    finally:
        db.close()
    assert result["readiness"] == 0
    assert len(result["biggest_gaps"]) == 3
    # Weights must still sum to 1 or the composite is not a percentage.
    assert abs(sum(s["weight"] for s in result["signals"].values()) - 1.0) < 1e-9


def test_readiness_credits_a_measured_component(db_factory):
    db = db_factory()
    try:
        _profile(db)
        db.add(CelpipTest(id="ctest_1", user_id=USER, mode="component",
                          components_json=json.dumps(["reading"])))
        db.add(CelpipAttempt(
            id="cattempt_1", user_id=USER, test_id="ctest_1", status="completed",
            completed_at=datetime.now(timezone.utc),
            results_json=json.dumps({"reading": {"level": {"low": 9, "high": 9}}}),
        ))
        db.commit()
        result = readiness.compute_readiness(db, user_id=USER)
    finally:
        db.close()
    assert result["component_levels"]["reading"]["latest"] == 9.0
    assert result["readiness"] > 0
    assert result["signals"]["component_levels"]["score"] > 0


# --- Generation pipeline --------------------------------------------------

def _valid_email_item(topic: str) -> dict:
    return {
        "topic": topic,
        "stimulus": {
            "prompt": f"Your building manager has not fixed {topic}. Write an email about it.",
            "recipient": "your building manager",
            "bullets": ["describe the problem", "say when it started", "propose a time to visit"],
            "register": "semi-formal",
        },
    }


def test_generation_banks_only_items_that_pass_both_gates(db_factory, monkeypatch):
    from app.db.models import CelpipGenerationRun
    from app.services.celpip import validation

    produced = [
        _valid_email_item("the broken lift"),
        {"topic": "malformed", "stimulus": {"prompt": "too short"}},   # fails the schema gate
        _valid_email_item("the leaking radiator"),
    ]
    calls = iter(produced)
    monkeypatch.setattr(
        generation, "_generate_one", lambda *a, **k: (next(calls), "writer-model"),
    )
    reviewed: list[str] = []

    def fake_review(task_key, payload):
        reviewed.append(payload["topic"])
        # The reviewer rejects the second valid item as ambiguous.
        accepted = payload["topic"] != "the leaking radiator"
        return {
            "accepted": accepted, "validator_model": "validator-model",
            "reasons": [] if accepted else [{"reason": validation.REASON_AMBIGUOUS, "detail": "two readings"}],
        }

    monkeypatch.setattr(generation.validation, "review_item", fake_review)
    monkeypatch.setattr(generation, "MAX_ATTEMPT_MULTIPLIER", 1)

    db = db_factory()
    try:
        db.add(CelpipGenerationRun(id="crun_1", user_id=USER, task_key="writing_email",
                                   requested_count=3, status="queued"))
        db.commit()
    finally:
        db.close()

    result = generation.run_generation(
        run_id="crun_1", user_id=USER, task_key="writing_email", count=3,
    )

    assert result["accepted"] == 1
    assert result["rejected"] == 2
    # The malformed item never reached the reviewer — a schema failure must not
    # cost a model call.
    assert reviewed == ["the broken lift", "the leaking radiator"]

    db = db_factory()
    try:
        run = db.get(CelpipGenerationRun, "crun_1")
        banked = db.query(CelpipQuestion).all()
    finally:
        db.close()

    assert run.status == "complete"
    reasons = {r["reason"] for r in json.loads(run.rejections_json)}
    assert reasons == {validation.REASON_SCHEMA, validation.REASON_AMBIGUOUS}
    assert len(banked) == 1
    assert banked[0].status == "ready"
    assert banked[0].content_fingerprint


def test_generation_rejects_a_near_duplicate_of_a_banked_item(db_factory, monkeypatch):
    from app.db.models import CelpipGenerationRun
    from app.services.celpip import validation

    item = _valid_email_item("the broken lift")
    monkeypatch.setattr(generation, "_generate_one", lambda *a, **k: (item, "writer-model"))
    monkeypatch.setattr(
        generation.validation, "review_item",
        lambda *a, **k: {"accepted": True, "validator_model": "v", "reasons": []},
    )
    monkeypatch.setattr(generation, "MAX_ATTEMPT_MULTIPLIER", 1)

    db = db_factory()
    try:
        for run_id in ("crun_1", "crun_2"):
            db.add(CelpipGenerationRun(id=run_id, user_id=USER, task_key="writing_email",
                                       requested_count=1, status="queued"))
        db.commit()
    finally:
        db.close()

    first = generation.run_generation(run_id="crun_1", user_id=USER, task_key="writing_email", count=1)
    second = generation.run_generation(run_id="crun_2", user_id=USER, task_key="writing_email", count=1)

    assert first["accepted"] == 1
    assert second["accepted"] == 0
    db = db_factory()
    try:
        run = db.get(CelpipGenerationRun, "crun_2")
    finally:
        db.close()
    assert json.loads(run.rejections_json)[0]["reason"] == validation.REASON_DUPLICATE


def test_a_listening_item_is_not_servable_until_its_audio_exists(db_factory, monkeypatch):
    """Serving a listening item with no audio would burn a timed section on a
    silent question."""
    from app.db.models import CelpipGenerationRun
    from app.services import maintenance_jobs

    # Must sit inside the spec's 120-350 word bound for a News Item, or the
    # schema gate rejects it before the point this test is making.
    script = "The city council announced a new transit route this morning. " + " ".join(
        f"Detail number {i} concerns the schedule, the cost, and the affected neighbourhoods."
        for i in range(20)
    )
    item = {
        "topic": "a transit announcement",
        "stimulus": {"segments": [{
            "index": 0,
            "speakers": [{"name": "Announcer", "gender_hint": "female"}],
            "lines": [{"speaker": "Announcer", "text": script}],
        }]},
        "questions": [
            {
                "prompt": f"Question {i}",
                "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
                "answer": "A" if i % 2 else "B",
                "evidence": "The city council announced a new transit route this morning",
                "rationales": {"A": "x", "B": "y", "C": "z", "D": "w"},
            }
            for i in range(5)
        ],
    }
    monkeypatch.setattr(generation, "_generate_one", lambda *a, **k: (item, "writer-model"))
    monkeypatch.setattr(
        generation.validation, "review_item",
        lambda *a, **k: {"accepted": True, "validator_model": "v", "reasons": []},
    )
    queued: list[str] = []
    monkeypatch.setattr(
        maintenance_jobs, "enqueue_celpip_assets",
        lambda *, question_id: (queued.append(question_id) or "job_1"),
    )

    db = db_factory()
    try:
        db.add(CelpipGenerationRun(id="crun_1", user_id=USER, task_key="listening_news",
                                   requested_count=1, status="queued"))
        db.commit()
    finally:
        db.close()

    generation.run_generation(run_id="crun_1", user_id=USER, task_key="listening_news", count=1)

    db = db_factory()
    try:
        banked = db.query(CelpipQuestion).one()
    finally:
        db.close()
    assert banked.status == "awaiting_assets"
    assert queued == [banked.id]

    # And assembly must not pick it up while it is in that state.
    with pytest.raises(assembly.NotEnoughItems):
        assembly.assemble_test(
            user_id=USER, mode="single_task", practice_mode="timed", task_keys=["listening_news"],
        )


def test_a_late_receptive_answer_is_not_counted_and_is_reported(db_factory):
    """`late` means the answer arrived after the deadline plus its grace. The
    real test would not have taken it, so neither does scoring -- and the
    learner is told how many were dropped rather than silently docked."""
    payload = {
        "stimulus": {"paragraphs": []},
        "questions": [
            {"prompt": f"q{i}", "options": {"A": "a", "B": "b"}, "answer": "A",
             "evidence": "e", "rationales": {"A": "x", "B": "y"}}
            for i in range(4)
        ],
    }
    db = db_factory()
    try:
        db.add(CelpipQuestion(id="cq_read", skill="reading", task_key="reading_information",
                              part=3, status="ready", payload_json=json.dumps(payload)))
        db.add(CelpipTest(id="ctest_1", user_id=USER, mode="component",
                          components_json=json.dumps(["reading"])))
        db.add(CelpipTestItem(id="citem_1", test_id="ctest_1", question_id="cq_read",
                              skill="reading", task_key="reading_information", position=0))
        db.add(CelpipAttempt(id="cattempt_1", user_id=USER, test_id="ctest_1", status="submitted"))
        for index in range(4):
            db.add(CelpipResponse(
                id=f"cresp_{index}", attempt_id="cattempt_1", question_id="cq_read",
                skill="reading", task_key="reading_information", question_index=index,
                selected_option="A",           # correct
                late=index >= 2,               # the last two arrived after time
            ))
        db.commit()
    finally:
        db.close()

    scoring.evaluate_attempt("cattempt_1")

    db = db_factory()
    try:
        results = json.loads(db.get(CelpipAttempt, "cattempt_1").results_json)
    finally:
        db.close()

    reading = results["reading"]
    assert reading["raw_score"] == 2, "late answers must not earn marks"
    assert reading["max_score"] == 4
    assert reading["late_excluded"] == 2
    review = reading["items"][0]["questions"]
    assert [q["late"] for q in review] == [False, False, True, True]
    # A dropped answer reads as unanswered, not as a wrong choice the learner made.
    assert review[2]["answered"] is False
    assert review[2]["chosen"] is None


def test_a_writing_response_is_never_dropped_for_a_late_autosave(db_factory, monkeypatch):
    """Written text accumulates through autosave, so the final save landing late
    must not discard the whole essay -- unlike a discrete multiple-choice pick."""
    levels = {"content_coherence": 9, "vocabulary": 9, "readability": 9, "task_fulfillment": 9}
    _stub_scorers(monkeypatch, _evaluation(levels), _evaluation(levels))
    attempt_id, response_id = _writing_attempt(db_factory)

    db = db_factory()
    try:
        db.get(CelpipResponse, response_id).late = True
        db.commit()
    finally:
        db.close()

    scoring.evaluate_attempt(attempt_id)

    db = db_factory()
    try:
        evaluation = db.query(CelpipEvaluation).one()
    finally:
        db.close()
    assert evaluation.status == "complete"
    assert evaluation.level_low == 9


# --- Transient evaluation failures ---------------------------------------

def _queue_evaluation_job(db_factory, attempt_id: str, *, attempt_count: int, max_attempts: int = 3):
    from app.db.models import MaintenanceJob
    from app.services.maintenance_jobs import CELPIP_EVALUATION_JOB

    db = db_factory()
    try:
        db.add(MaintenanceJob(
            id="celpip_job_1",
            job_type=CELPIP_EVALUATION_JOB,
            dedupe_key=f"{CELPIP_EVALUATION_JOB}:{attempt_id}",
            status="running",
            payload_json=json.dumps({"attempt_id": attempt_id}),
            result_json="{}",
            attempt_count=attempt_count,
            max_attempts=max_attempts,
        ))
        db.commit()
    finally:
        db.close()


def test_a_retryable_scoring_failure_leaves_the_attempt_evaluating(db_factory, monkeypatch):
    """The results screen stops polling on any status but submitted/evaluating.
    Marking `failed` on the first exception means a successful retry never
    reaches the learner without a manual reload."""
    from app.services import maintenance_jobs

    monkeypatch.setattr(maintenance_jobs, "SessionLocal", db_factory)
    attempt_id, _ = _writing_attempt(db_factory)
    _queue_evaluation_job(db_factory, attempt_id, attempt_count=1, max_attempts=3)

    monkeypatch.setattr(
        scoring, "_transcribe_pending",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("provider timeout")),
    )

    with pytest.raises(RuntimeError):
        scoring.evaluate_attempt(attempt_id)

    db = db_factory()
    try:
        attempt = db.get(CelpipAttempt, attempt_id)
    finally:
        db.close()
    assert attempt.status == "evaluating"
    assert "will be retried" in attempt.error


def test_the_final_scoring_failure_marks_the_attempt_failed(db_factory, monkeypatch):
    from app.services import maintenance_jobs

    monkeypatch.setattr(maintenance_jobs, "SessionLocal", db_factory)
    attempt_id, _ = _writing_attempt(db_factory)
    # Third of three tries: no retry follows this one.
    _queue_evaluation_job(db_factory, attempt_id, attempt_count=3, max_attempts=3)

    monkeypatch.setattr(
        scoring, "_transcribe_pending",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("provider timeout")),
    )

    with pytest.raises(RuntimeError):
        scoring.evaluate_attempt(attempt_id)

    db = db_factory()
    try:
        attempt = db.get(CelpipAttempt, attempt_id)
    finally:
        db.close()
    assert attempt.status == "failed"
    assert "will be retried" not in (attempt.error or "")


def test_a_retry_after_a_failure_still_scores_the_attempt(db_factory, monkeypatch):
    """End to end: first run blows up, the retry succeeds, and the attempt
    lands in `completed` with real results."""
    from app.services import maintenance_jobs

    monkeypatch.setattr(maintenance_jobs, "SessionLocal", db_factory)
    levels = {"content_coherence": 9, "vocabulary": 9, "readability": 9, "task_fulfillment": 9}
    _stub_scorers(monkeypatch, _evaluation(levels), _evaluation(levels))
    attempt_id, _ = _writing_attempt(db_factory)
    _queue_evaluation_job(db_factory, attempt_id, attempt_count=1, max_attempts=3)

    boom = {"fired": False}

    def flaky(db, attempt):
        if not boom["fired"]:
            boom["fired"] = True
            raise RuntimeError("provider timeout")

    monkeypatch.setattr(scoring, "_transcribe_pending", flaky)

    with pytest.raises(RuntimeError):
        scoring.evaluate_attempt(attempt_id)
    scoring.evaluate_attempt(attempt_id)

    db = db_factory()
    try:
        attempt = db.get(CelpipAttempt, attempt_id)
        evaluation = db.query(CelpipEvaluation).filter(
            CelpipEvaluation.status == "complete").one()
    finally:
        db.close()
    assert attempt.status == "completed"
    assert evaluation.level_low == 9
    assert json.loads(attempt.results_json)["writing"]["level"]["low"] == 9
