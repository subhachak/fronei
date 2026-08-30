"""The progress report: sittings, levels, trend, and the focus ranking.

The ranking is the part that changes behaviour -- it decides where the learner
spends their next hour -- so it is deterministic and tested rather than being a
model's opinion.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import (
    Base,
    CelpipAttempt,
    CelpipEvaluation,
    CelpipResponse,
    CelpipTest,
)
from app.services.celpip import lessons as lessons_service
from app.services.celpip import progress

USER = "admin_1"


@pytest.fixture
def db_factory(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(lessons_service, "SessionLocal", factory)
    lessons_service.seed_lessons()
    return factory


def _attempt(db, attempt_id: str, *, results: dict, when: datetime) -> None:
    db.add(CelpipTest(id=f"ctest_{attempt_id}", user_id=USER, mode="component",
                      components_json=json.dumps(["reading"])))
    db.add(CelpipAttempt(
        id=attempt_id, user_id=USER, test_id=f"ctest_{attempt_id}", status="completed",
        created_at=when, completed_at=when, results_json=json.dumps(results),
    ))


def _reading_results(correct: int, total: int) -> dict:
    return {
        "reading": {
            "method": "deterministic",
            "level": {"low": 7, "high": 9},
            "accuracy_by_task": {"reading_information": {"correct": correct, "total": total}},
            "weakness_tags": ["distractor_confusion"],
        }
    }


def test_report_covers_every_category_and_sub_category(db_factory):
    db = db_factory()
    try:
        report = progress.build_report(db, user_id=USER, test_type="general", target_level=9)
    finally:
        db.close()

    assert [c["skill"] for c in report["categories"]] == [
        "listening", "reading", "writing", "speaking",
    ]
    counts = {c["skill"]: len(c["tasks"]) for c in report["categories"]}
    assert counts == {"listening": 6, "reading": 4, "writing": 2, "speaking": 8}
    assert report["total_sittings"] == 0


def test_sittings_count_distinct_attempts_not_responses(db_factory):
    """Eight speaking answers in one mock is one sitting of each speaking task,
    not eight of anything."""
    now = datetime.now(timezone.utc)
    db = db_factory()
    try:
        _attempt(db, "cattempt_1", results=_reading_results(6, 9), when=now)
        for index in range(9):
            db.add(CelpipResponse(
                id=f"cresp_{index}", attempt_id="cattempt_1", question_id="q1",
                skill="reading", task_key="reading_information", question_index=index,
                selected_option="A", created_at=now,
            ))
        db.commit()
        report = progress.build_report(db, user_id=USER, test_type="general", target_level=9)
    finally:
        db.close()

    reading = next(c for c in report["categories"] if c["skill"] == "reading")
    task = next(t for t in reading["tasks"] if t["task_key"] == "reading_information")
    assert task["sittings"] == 1
    assert task["correct"] == 6
    assert task["total"] == 9


def test_a_never_attempted_task_ranks_top_of_focus(db_factory):
    """An unmeasured task type is a risk before a test, not a strength."""
    db = db_factory()
    try:
        report = progress.build_report(db, user_id=USER, test_type="general", target_level=9)
    finally:
        db.close()

    assert report["focus"], "focus list must never be empty"
    assert all(item["focus_score"] == 1.0 for item in report["focus"])
    assert all(item["sittings"] == 0 for item in report["focus"])
    assert "Never attempted" in report["focus"][0]["reason"]


def test_focus_reason_states_the_gap_to_target(db_factory):
    now = datetime.now(timezone.utc)
    db = db_factory()
    try:
        _attempt(db, "cattempt_1", results=_reading_results(4, 9), when=now)
        db.add(CelpipResponse(
            id="cresp_1", attempt_id="cattempt_1", question_id="q1", skill="reading",
            task_key="reading_information", selected_option="A", created_at=now,
        ))
        db.commit()
        report = progress.build_report(db, user_id=USER, test_type="general", target_level=9)
    finally:
        db.close()

    reading = next(c for c in report["categories"] if c["skill"] == "reading")
    task = next(t for t in reading["tasks"] if t["task_key"] == "reading_information")
    assert task["accuracy"] == pytest.approx(0.444, abs=0.001)
    assert task["level"] is not None and task["level"] < 9
    assert 0 < task["focus_score"] < 1.0


def test_trend_reports_improvement_across_sittings(db_factory):
    now = datetime.now(timezone.utc)
    db = db_factory()
    try:
        for index, correct in enumerate((3, 6, 9)):
            when = now - timedelta(days=6 - index * 2)
            _attempt(db, f"cattempt_{index}", results=_reading_results(correct, 9), when=when)
            db.add(CelpipResponse(
                id=f"cresp_{index}", attempt_id=f"cattempt_{index}", question_id="q1",
                skill="reading", task_key="reading_information", selected_option="A",
                created_at=when,
            ))
        db.commit()
        report = progress.build_report(db, user_id=USER, test_type="general", target_level=9)
    finally:
        db.close()

    reading = next(c for c in report["categories"] if c["skill"] == "reading")
    task = next(t for t in reading["tasks"] if t["task_key"] == "reading_information")
    assert task["sittings"] == 3
    assert task["trend"] == "improving"
    assert len(task["history"]) == 3


def test_a_single_sitting_reports_an_unknown_trend(db_factory):
    """One score is not a direction, and drawing an arrow from it would be a
    claim the data cannot support."""
    now = datetime.now(timezone.utc)
    db = db_factory()
    try:
        _attempt(db, "cattempt_1", results=_reading_results(7, 9), when=now)
        db.add(CelpipResponse(
            id="cresp_1", attempt_id="cattempt_1", question_id="q1", skill="reading",
            task_key="reading_information", selected_option="A", created_at=now,
        ))
        db.commit()
        report = progress.build_report(db, user_id=USER, test_type="general", target_level=9)
    finally:
        db.close()

    reading = next(c for c in report["categories"] if c["skill"] == "reading")
    task = next(t for t in reading["tasks"] if t["task_key"] == "reading_information")
    assert task["trend"] == "unknown"


def test_productive_tasks_read_their_level_from_evaluations(db_factory):
    now = datetime.now(timezone.utc)
    db = db_factory()
    try:
        _attempt(db, "cattempt_1", results={}, when=now)
        db.add(CelpipResponse(
            id="cresp_1", attempt_id="cattempt_1", question_id="q1", skill="writing",
            task_key="writing_email", created_at=now,
        ))
        db.add(CelpipEvaluation(
            id="ceval_1", attempt_id="cattempt_1", skill="writing", task_key="writing_email",
            status="complete", level_low=8, level_high=9, confidence=0.8,
            weakness_tags_json=json.dumps(["idea_development"]),
            created_at=now, completed_at=now,
        ))
        db.commit()
        report = progress.build_report(db, user_id=USER, test_type="general", target_level=9)
    finally:
        db.close()

    writing = next(c for c in report["categories"] if c["skill"] == "writing")
    task = next(t for t in writing["tasks"] if t["task_key"] == "writing_email")
    assert task["level"] == 8.5
    assert task["sittings"] == 1
    assert [tag["tag"] for tag in task["weakness_tags"]] == ["idea_development"]


def test_every_sub_category_offers_a_next_step(db_factory):
    """A task with no measured weakness still needs somewhere to send the
    learner, or the dashboard is a scoreboard rather than a study tool."""
    db = db_factory()
    try:
        report = progress.build_report(db, user_id=USER, test_type="general", target_level=9)
    finally:
        db.close()

    for category in report["categories"]:
        for task in category["tasks"]:
            assert task["tips"], f"{task['task_key']} has no lesson to point at"


def test_measured_weaknesses_drive_the_tips(db_factory):
    now = datetime.now(timezone.utc)
    db = db_factory()
    try:
        _attempt(db, "cattempt_1", results={}, when=now)
        db.add(CelpipResponse(
            id="cresp_1", attempt_id="cattempt_1", question_id="q1", skill="speaking",
            task_key="speaking_scene", created_at=now,
        ))
        db.add(CelpipEvaluation(
            id="ceval_1", attempt_id="cattempt_1", skill="speaking", task_key="speaking_scene",
            status="complete", level_low=6, level_high=6,
            weakness_tags_json=json.dumps(["vocabulary_range"]),
            created_at=now, completed_at=now,
        ))
        db.commit()
        report = progress.build_report(db, user_id=USER, test_type="general", target_level=9)
    finally:
        db.close()

    speaking = next(c for c in report["categories"] if c["skill"] == "speaking")
    task = next(t for t in speaking["tasks"] if t["task_key"] == "speaking_scene")
    matched = {tag for tip in task["tips"] for tag in tip.get("matched_tags", [])}
    assert "vocabulary_range" in matched


def test_general_ls_reports_only_its_two_components(db_factory):
    db = db_factory()
    try:
        report = progress.build_report(db, user_id=USER, test_type="general_ls", target_level=5)
    finally:
        db.close()
    assert [c["skill"] for c in report["categories"]] == ["listening", "speaking"]
