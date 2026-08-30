"""CELPIP API: the admin boundary, and the exam rules the runner enforces.

The rules under test here are the ones a learner's result depends on and that
cannot be verified by inspection: that a non-admin sees nothing, that the clock
belongs to the server, that a late answer is recorded but flagged, that
simulation mode blocks the answer changes the official flow blocks, and that
unscored content never reaches the client labelled as such.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import AdminPrincipal, require_admin_principal
from app.db.models import (
    Base,
    CelpipAttempt,
    CelpipQuestion,
    CelpipTest,
    CelpipTestItem,
)
from app.main import app
from app.routers import celpip as celpip_router
from app.services import maintenance_jobs
from app.services.celpip import (
    assembly,
    assets,
    attempts,
    generation,
    planner,
    progress,
    readiness,
    scoring,
    stock,
)
from app.services.celpip import lessons as lessons_service

ADMIN = AdminPrincipal(user_id="admin_1", email="admin@example.com")

# Every module that opens its own session has to be pointed at the test DB;
# the CELPIP service layer deliberately manages its own sessions so it can run
# inside a background job with no request in scope.
SESSION_HOLDERS = (
    celpip_router, assembly, assets, attempts, generation, planner, progress, readiness,
    scoring, stock, lessons_service, maintenance_jobs,
)


@pytest.fixture
def session_factory(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)
    for module in SESSION_HOLDERS:
        if hasattr(module, "SessionLocal"):
            monkeypatch.setattr(module, "SessionLocal", factory)
    return factory


@pytest.fixture(autouse=True)
def topup_calls(monkeypatch):
    """Record stock top-ups instead of queueing them.

    Assembly refills the buffer it consumed, which enqueues a real maintenance
    job. Under TestClient the worker thread is running, so it would claim that
    job and make live generation calls. Tests that care about refills assert on
    this list; every other test is simply protected from the network.
    """
    calls: list[tuple[str, ...]] = []

    def record(*, user_id, task_keys=None):
        calls.append(tuple(sorted(task_keys or [])))
        return {"queued": {}, "deficits": {}, "target": 0}

    monkeypatch.setattr(stock, "plan_topup", record)
    return calls


@pytest.fixture
def as_admin():
    app.dependency_overrides[require_admin_principal] = lambda: ADMIN
    yield
    app.dependency_overrides.pop(require_admin_principal, None)


@pytest.fixture
def as_non_admin():
    def _deny():
        raise HTTPException(status_code=403, detail="Admin access required")

    app.dependency_overrides[require_admin_principal] = _deny
    yield
    app.dependency_overrides.pop(require_admin_principal, None)


def _bank_item(db, task_key: str, *, skill: str, part: int, questions: int) -> CelpipQuestion:
    payload = {
        "topic": f"{task_key} topic",
        "stimulus": {"prompt": "Do the thing.", "bullets": ["one", "two"]},
        "questions": [
            {
                "prompt": f"Question {i}",
                "options": {"A": "first", "B": "second", "C": "third", "D": "fourth"},
                "answer": "B" if i % 2 else "A",
                "evidence": "Do the thing.",
                "rationales": {"A": "a", "B": "b", "C": "c", "D": "d"},
            }
            for i in range(questions)
        ],
    }
    row = CelpipQuestion(
        id=f"cq_{task_key}",
        skill=skill,
        task_key=task_key,
        part=part,
        title=task_key,
        payload_json=json.dumps(payload),
        status="ready",
        difficulty=9,
    )
    db.add(row)
    db.commit()
    return row


def _seed_reading_test(session_factory, *, practice_mode: str) -> tuple[str, str]:
    db = session_factory()
    try:
        _bank_item(db, "reading_information", skill="reading", part=3, questions=9)
    finally:
        db.close()

    test = assembly.assemble_test(
        user_id=ADMIN.user_id, mode="single_task", practice_mode=practice_mode,
        task_keys=["reading_information"], label="Reading drill",
    )
    attempt = attempts.create_attempt(
        user_id=ADMIN.user_id, test_id=test["test_id"], practice_mode=practice_mode,
    )
    return test["test_id"], attempt["attempt_id"]


# --- The boundary ---------------------------------------------------------

@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/admin/celpip/home"),
        ("get", "/admin/celpip/spec"),
        ("get", "/admin/celpip/lessons"),
        ("get", "/admin/celpip/bank"),
        ("post", "/admin/celpip/bank/generate"),
        ("get", "/admin/celpip/attempts"),
        ("post", "/admin/celpip/tests"),
        ("get", "/admin/celpip/plan"),
        ("get", "/admin/celpip/progress"),
    ],
)
def test_every_endpoint_refuses_a_non_admin(session_factory, as_non_admin, method, path):
    with TestClient(app) as client:
        response = (
            client.post(path, json={}) if method == "post" else client.get(path)
        )
        assert response.status_code == 403, f"{path} let a non-admin through"


def test_an_attempt_belonging_to_another_user_is_not_readable(session_factory, as_admin):
    db = session_factory()
    try:
        db.add(CelpipTest(id="ctest_x", user_id="someone_else", label="Theirs", mode="custom"))
        db.add(CelpipAttempt(id="cattempt_x", user_id="someone_else", test_id="ctest_x"))
        db.commit()
    finally:
        db.close()

    with TestClient(app) as client:
        assert client.get("/admin/celpip/attempts/cattempt_x").status_code == 404
        assert client.get("/admin/celpip/attempts/cattempt_x/results").status_code == 404


# --- Format truth ---------------------------------------------------------

def test_spec_endpoint_reports_the_official_shape(session_factory, as_admin):
    with TestClient(app) as client:
        spec = client.get("/admin/celpip/spec").json()
    sections = {s["skill"]: s for s in spec["sections"]}
    assert sections["listening"]["scored_questions"] == 38
    assert sections["reading"]["scored_questions"] == 38
    assert len(sections["speaking"]["tasks"]) == 8
    assert len(sections["writing"]["tasks"]) == 2
    # The runner counts down the same numbers the Learn pages display.
    email = next(t for t in sections["writing"]["tasks"] if t["key"] == "writing_email")
    assert email["word_range"] == [150, 200]


# --- The clock ------------------------------------------------------------

def test_section_start_is_idempotent_and_does_not_restart_the_clock(session_factory, as_admin):
    _, attempt_id = _seed_reading_test(session_factory, practice_mode="timed")
    with TestClient(app) as client:
        first = client.post(f"/admin/celpip/attempts/{attempt_id}/sections/reading/start").json()
        second = client.post(f"/admin/celpip/attempts/{attempt_id}/sections/reading/start").json()
    assert first["deadline_at"] == second["deadline_at"]
    assert second["seconds_remaining"] <= first["seconds_remaining"]


def test_time_remaining_is_computed_from_the_server_not_the_client(session_factory, as_admin):
    """Rewinding the stored start time is the only way to move the deadline —
    there is no client-supplied value that can."""
    _, attempt_id = _seed_reading_test(session_factory, practice_mode="timed")
    with TestClient(app) as client:
        client.post(f"/admin/celpip/attempts/{attempt_id}/sections/reading/start")

        db = session_factory()
        try:
            attempt = db.get(CelpipAttempt, attempt_id)
            state = json.loads(attempt.section_state_json)
            started = datetime.now(timezone.utc) - timedelta(minutes=50)
            state["reading"]["started_at"] = started.isoformat()
            state["reading"]["deadline_at"] = (started + timedelta(seconds=state["reading"]["limit_seconds"])).isoformat()
            attempt.section_state_json = json.dumps(state)
            db.commit()
        finally:
            db.close()

        resumed = client.get(f"/admin/celpip/attempts/{attempt_id}").json()
        remaining = resumed["sections"]["reading"]["seconds_remaining"]
    # 60-minute section, 50 minutes consumed: a reload resumes with what is
    # actually left, not a fresh hour.
    assert 550 <= remaining <= 650


def test_an_answer_after_the_deadline_is_saved_but_flagged_late(session_factory, as_admin):
    test_id, attempt_id = _seed_reading_test(session_factory, practice_mode="timed")
    db = session_factory()
    try:
        question_id = db.query(CelpipTestItem).filter(CelpipTestItem.test_id == test_id).first().question_id
    finally:
        db.close()

    with TestClient(app) as client:
        client.post(f"/admin/celpip/attempts/{attempt_id}/sections/reading/start")
        db = session_factory()
        try:
            attempt = db.get(CelpipAttempt, attempt_id)
            state = json.loads(attempt.section_state_json)
            state["reading"]["deadline_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
            attempt.section_state_json = json.dumps(state)
            db.commit()
        finally:
            db.close()

        saved = client.post(
            f"/admin/celpip/attempts/{attempt_id}/responses",
            json={"question_id": question_id, "question_index": 0, "selected_option": "A"},
        )
    assert saved.status_code == 200
    # Recorded rather than rejected -- losing a real answer to network latency
    # would be worse -- but marked so scoring can exclude it.
    assert saved.json()["late"] is True


# --- Simulation constraints ----------------------------------------------

def test_simulation_blocks_changing_an_answer_the_official_flow_locks(session_factory, as_admin):
    db = session_factory()
    try:
        _bank_item(db, "listening_news", skill="listening", part=4, questions=5)
    finally:
        db.close()
    test = assembly.assemble_test(
        user_id=ADMIN.user_id, mode="single_task", practice_mode="simulation",
        task_keys=["listening_news"],
    )
    attempt = attempts.create_attempt(
        user_id=ADMIN.user_id, test_id=test["test_id"], practice_mode="simulation",
    )
    db = session_factory()
    try:
        question_id = db.query(CelpipTestItem).filter(
            CelpipTestItem.test_id == test["test_id"]
        ).first().question_id
    finally:
        db.close()

    with TestClient(app) as client:
        client.post(f"/admin/celpip/attempts/{attempt['attempt_id']}/sections/listening/start")
        first = client.post(
            f"/admin/celpip/attempts/{attempt['attempt_id']}/responses",
            json={"question_id": question_id, "question_index": 0, "selected_option": "A"},
        )
        assert first.status_code == 200
        second = client.post(
            f"/admin/celpip/attempts/{attempt['attempt_id']}/responses",
            json={"question_id": question_id, "question_index": 0, "selected_option": "B"},
        )
    assert second.status_code == 409


def test_timed_mode_allows_changing_the_same_answer(session_factory, as_admin):
    """The lock is a simulation constraint, not a data constraint. A drill you
    cannot correct yourself in is a worse drill."""
    db = session_factory()
    try:
        _bank_item(db, "listening_news", skill="listening", part=4, questions=5)
    finally:
        db.close()
    test = assembly.assemble_test(
        user_id=ADMIN.user_id, mode="single_task", practice_mode="timed", task_keys=["listening_news"],
    )
    attempt = attempts.create_attempt(
        user_id=ADMIN.user_id, test_id=test["test_id"], practice_mode="timed",
    )
    db = session_factory()
    try:
        question_id = db.query(CelpipTestItem).filter(
            CelpipTestItem.test_id == test["test_id"]
        ).first().question_id
    finally:
        db.close()

    with TestClient(app) as client:
        client.post(f"/admin/celpip/attempts/{attempt['attempt_id']}/sections/listening/start")
        for option in ("A", "B", "C"):
            response = client.post(
                f"/admin/celpip/attempts/{attempt['attempt_id']}/responses",
                json={"question_id": question_id, "question_index": 0, "selected_option": option},
            )
            assert response.status_code == 200


# --- What the client is allowed to see ------------------------------------

def test_a_listening_script_is_never_sent_to_the_client(session_factory, as_admin):
    """The audio is the test. A transcript in the payload would turn a
    Listening item into a Reading item."""
    db = session_factory()
    try:
        payload = {
            "topic": "transit",
            "stimulus": {
                "segments": [{
                    "index": 0,
                    "speakers": [{"name": "Announcer", "gender_hint": "female"}],
                    "lines": [{"speaker": "Announcer", "text": "SECRET SCRIPT CONTENT"}],
                }],
            },
            "questions": [{
                "prompt": "What happened?",
                "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
                "answer": "A", "evidence": "SECRET SCRIPT CONTENT",
                "rationales": {"A": "a", "B": "b", "C": "c", "D": "d"},
            }],
        }
        db.add(CelpipQuestion(
            id="cq_listen", skill="listening", task_key="listening_news", part=4,
            payload_json=json.dumps(payload), status="ready",
        ))
        db.commit()
    finally:
        db.close()

    test = assembly.assemble_test(
        user_id=ADMIN.user_id, mode="single_task", practice_mode="timed", task_keys=["listening_news"],
    )
    attempt = attempts.create_attempt(user_id=ADMIN.user_id, test_id=test["test_id"])

    with TestClient(app) as client:
        body = client.get(
            f"/admin/celpip/attempts/{attempt['attempt_id']}/questions/cq_listen"
        ).text
    assert "SECRET SCRIPT CONTENT" not in body
    assert "segments" not in json.loads(body)["stimulus"]


def test_the_answer_key_is_never_sent_during_an_attempt(session_factory, as_admin):
    test_id, attempt_id = _seed_reading_test(session_factory, practice_mode="timed")
    db = session_factory()
    try:
        question_id = db.query(CelpipTestItem).filter(CelpipTestItem.test_id == test_id).first().question_id
    finally:
        db.close()

    with TestClient(app) as client:
        data = client.get(f"/admin/celpip/attempts/{attempt_id}/questions/{question_id}").json()
    for question in data["questions"]:
        assert "answer" not in question
        assert "evidence" not in question
        assert "rationales" not in question


def test_unscored_items_are_not_identified_to_the_client(session_factory, as_admin):
    """A full mock plants unscored content, as the real test does. Telling the
    candidate which item does not count would change how they answer it."""
    test_id, attempt_id = _seed_reading_test(session_factory, practice_mode="simulation")
    db = session_factory()
    try:
        item = db.query(CelpipTestItem).filter(CelpipTestItem.test_id == test_id).first()
        item.is_unscored = True
        db.commit()
    finally:
        db.close()

    with TestClient(app) as client:
        state = client.get(f"/admin/celpip/attempts/{attempt_id}").json()
    assert state["items"]
    for entry in state["items"]:
        assert "is_unscored" not in entry


# --- Assembly failure is actionable ---------------------------------------

def test_an_empty_buffer_starts_a_refill_instead_of_sending_the_learner_away(
    session_factory, as_admin, topup_calls,
):
    """Launching should never require a detour through the Question Bank."""
    with TestClient(app) as client:
        response = client.post(
            "/admin/celpip/tests", json={"mode": "component", "components": ["writing"]},
        )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "writing_email" in detail["shortfalls"]
    assert detail["preparing"] is True
    assert "Question Bank" not in detail["hint"]
    assert topup_calls and "writing_email" in topup_calls[0]


def test_assembly_prefers_items_the_learner_has_not_seen(session_factory):
    db = session_factory()
    try:
        for i in range(2):
            row = _bank_item(db, "writing_email", skill="writing", part=1, questions=0)
            row.id = f"cq_email_{i}"
            db.merge(row)
        db.commit()
        ids = {q.id for q in db.query(CelpipQuestion).all()}
    finally:
        db.close()
    assert len(ids) >= 2

    first = assembly.assemble_test(
        user_id=ADMIN.user_id, mode="single_task", practice_mode="timed", task_keys=["writing_email"],
    )
    attempts.create_attempt(user_id=ADMIN.user_id, test_id=first["test_id"])
    second = assembly.assemble_test(
        user_id=ADMIN.user_id, mode="single_task", practice_mode="timed", task_keys=["writing_email"],
    )

    db = session_factory()
    try:
        picked_first = {i.question_id for i in db.query(CelpipTestItem).filter(
            CelpipTestItem.test_id == first["test_id"]).all()}
        picked_second = {i.question_id for i in db.query(CelpipTestItem).filter(
            CelpipTestItem.test_id == second["test_id"]).all()}
    finally:
        db.close()
    assert picked_first != picked_second


def test_served_questions_are_retired_so_they_never_come_back(session_factory):
    """Questions are single-use: that is what makes a retake meaningful (same
    questions, measure improvement) while a new launch is genuinely fresh."""
    db = session_factory()
    try:
        first = _bank_item(db, "writing_email", skill="writing", part=1, questions=0)
        first.id = "cq_email_a"
        db.merge(first)
        second = _bank_item(db, "writing_email", skill="writing", part=1, questions=0)
        second.id = "cq_email_b"
        db.merge(second)
        db.commit()
    finally:
        db.close()

    test = assembly.assemble_test(
        user_id=ADMIN.user_id, mode="single_task", task_keys=["writing_email"],
    )

    db = session_factory()
    try:
        served = db.query(CelpipTestItem).filter(
            CelpipTestItem.test_id == test["test_id"]
        ).one().question_id
        assert db.get(CelpipQuestion, served).status == "retired"
        others = [q for q in db.query(CelpipQuestion).all() if q.id != served]
        assert all(q.status == "ready" for q in others), "only the served item is retired"
    finally:
        db.close()


def test_consuming_stock_queues_a_refill(session_factory, topup_calls):
    """The buffer refills itself, so the next launch is instant too."""
    db = session_factory()
    try:
        _bank_item(db, "writing_email", skill="writing", part=1, questions=0)
    finally:
        db.close()

    assembly.assemble_test(
        user_id=ADMIN.user_id, mode="single_task", task_keys=["writing_email"],
    )
    assert topup_calls == [("writing_email",)]


def test_a_failed_refill_never_fails_the_launch(session_factory, monkeypatch):
    """The learner is waiting on this request; a background refill problem is
    not their problem."""
    def boom(**kwargs):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(stock, "plan_topup", boom)
    db = session_factory()
    try:
        _bank_item(db, "writing_email", skill="writing", part=1, questions=0)
    finally:
        db.close()

    test = assembly.assemble_test(
        user_id=ADMIN.user_id, mode="single_task", task_keys=["writing_email"],
    )
    assert test["item_count"] == 1


def test_stock_reports_what_the_launcher_can_start(session_factory, as_admin):
    db = session_factory()
    try:
        _bank_item(db, "writing_email", skill="writing", part=1, questions=0)
    finally:
        db.close()

    with TestClient(app) as client:
        report = client.get("/admin/celpip/stock").json()

    assert report["ready"]["writing_email"] == 1
    assert report["can_launch"] is False, "a General profile still needs the other 19 task types"
    assert "speaking_advice" in report["missing"]
    assert any(task["task_key"] == "writing_email" for task in report["tasks"])


def test_stock_counts_only_unserved_items(session_factory):
    """A retired item is still a row; it must not read as available stock."""
    db = session_factory()
    try:
        item = _bank_item(db, "writing_email", skill="writing", part=1, questions=0)
        item.status = "retired"
        item.times_served = 1
        db.merge(item)
        db.commit()
        levels = stock.stock_levels(db, ["writing_email"])
    finally:
        db.close()
    assert levels["writing_email"] == 0


def test_items_still_building_count_towards_the_target(session_factory):
    """Otherwise every check would queue another top-up for work already in
    flight, and one launch would trigger a generation storm."""
    db = session_factory()
    try:
        item = _bank_item(db, "listening_news", skill="listening", part=4, questions=5)
        item.status = "awaiting_assets"
        db.merge(item)
        db.commit()
        shortfall = stock.deficits(db, ["listening_news"])
    finally:
        db.close()
    assert shortfall["listening_news"] == stock.target_stock() - 1


def test_retake_creates_a_new_sitting_on_the_exact_same_test(session_factory, as_admin):
    test_id, attempt_id = _seed_reading_test(session_factory, practice_mode="timed")
    db = session_factory()
    try:
        original = db.get(CelpipAttempt, attempt_id)
        original.status = "completed"
        db.commit()
    finally:
        db.close()

    with TestClient(app) as client:
        response = client.post(f"/admin/celpip/attempts/{attempt_id}/retake")

    assert response.status_code == 200
    assert response.json()["attempt_id"] != attempt_id
    assert response.json()["test_id"] == test_id

    db = session_factory()
    try:
        retake = db.get(CelpipAttempt, response.json()["attempt_id"])
        assert retake.test_id == test_id
        assert retake.status == "not_started"
    finally:
        db.close()


# --- Submission -----------------------------------------------------------

def test_submitting_queues_scoring_and_is_idempotent(session_factory, as_admin, monkeypatch):
    queued: list[str] = []
    from app.services import maintenance_jobs

    monkeypatch.setattr(
        maintenance_jobs, "enqueue_celpip_evaluation",
        lambda *, attempt_id: (queued.append(attempt_id) or "job_1"),
    )

    _, attempt_id = _seed_reading_test(session_factory, practice_mode="timed")
    with TestClient(app) as client:
        client.post(f"/admin/celpip/attempts/{attempt_id}/sections/reading/start")
        first = client.post(f"/admin/celpip/attempts/{attempt_id}/submit").json()
        second = client.post(f"/admin/celpip/attempts/{attempt_id}/submit").json()

    assert first["status"] == "submitted"
    assert second.get("already_submitted") is True
    # A double-clicked submit must not run the scoring pipeline twice.
    assert queued == [attempt_id]


def test_a_submitted_attempt_refuses_further_answers(session_factory, as_admin, monkeypatch):
    from app.services import maintenance_jobs

    monkeypatch.setattr(maintenance_jobs, "enqueue_celpip_evaluation", lambda *, attempt_id: "job_1")
    test_id, attempt_id = _seed_reading_test(session_factory, practice_mode="timed")
    db = session_factory()
    try:
        question_id = db.query(CelpipTestItem).filter(CelpipTestItem.test_id == test_id).first().question_id
    finally:
        db.close()

    with TestClient(app) as client:
        client.post(f"/admin/celpip/attempts/{attempt_id}/sections/reading/start")
        client.post(f"/admin/celpip/attempts/{attempt_id}/submit")
        late = client.post(
            f"/admin/celpip/attempts/{attempt_id}/responses",
            json={"question_id": question_id, "question_index": 0, "selected_option": "A"},
        )
    assert late.status_code == 409


def test_a_finished_section_cannot_be_reopened_with_a_fresh_timer(session_factory, as_admin):
    """A page refresh clears the browser's "already started" guard. If the
    server allowed a restart, reloading after a section expired would hand the
    learner a brand-new full deadline."""
    _, attempt_id = _seed_reading_test(session_factory, practice_mode="timed")
    with TestClient(app) as client:
        opened = client.post(f"/admin/celpip/attempts/{attempt_id}/sections/reading/start").json()
        closed = client.post(
            f"/admin/celpip/attempts/{attempt_id}/sections/reading/complete?auto=true"
        )
        assert closed.status_code == 200

        reopened = client.post(f"/admin/celpip/attempts/{attempt_id}/sections/reading/start")
        assert reopened.status_code == 409

        state = client.get(f"/admin/celpip/attempts/{attempt_id}").json()
    # The original deadline stands; nothing was extended.
    assert state["sections"]["reading"]["deadline_at"] == opened["deadline_at"]


def test_completing_a_section_twice_is_harmless(session_factory, as_admin):
    _, attempt_id = _seed_reading_test(session_factory, practice_mode="timed")
    with TestClient(app) as client:
        client.post(f"/admin/celpip/attempts/{attempt_id}/sections/reading/start")
        first = client.post(f"/admin/celpip/attempts/{attempt_id}/sections/reading/complete")
        second = client.post(f"/admin/celpip/attempts/{attempt_id}/sections/reading/complete")
    assert first.status_code == 200
    assert second.status_code == 200


def _seed_two_section_test(session_factory, *, practice_mode: str = "timed") -> tuple[str, str]:
    """A Reading + Writing test, so section ordering has something to enforce."""
    db = session_factory()
    try:
        _bank_item(db, "reading_information", skill="reading", part=3, questions=9)
        _bank_item(db, "writing_email", skill="writing", part=1, questions=0)
    finally:
        db.close()
    test = assembly.assemble_test(
        user_id=ADMIN.user_id, mode="custom", practice_mode=practice_mode,
        task_keys=["reading_information", "writing_email"], label="Two-section test",
    )
    attempt = attempts.create_attempt(
        user_id=ADMIN.user_id, test_id=test["test_id"], practice_mode=practice_mode,
    )
    return test["test_id"], attempt["attempt_id"]


def test_a_section_not_in_this_test_cannot_be_started(session_factory, as_admin):
    """Without a membership check the API would stamp a deadline for a section
    the test does not contain, and then accept answers against it."""
    _, attempt_id = _seed_reading_test(session_factory, practice_mode="timed")
    with TestClient(app) as client:
        response = client.post(f"/admin/celpip/attempts/{attempt_id}/sections/speaking/start")
    assert response.status_code == 409
    assert "no speaking section" in response.json()["detail"]


def test_only_one_section_may_be_open_at_a_time(session_factory, as_admin):
    _, attempt_id = _seed_two_section_test(session_factory)
    with TestClient(app) as client:
        assert client.post(
            f"/admin/celpip/attempts/{attempt_id}/sections/reading/start"
        ).status_code == 200
        second = client.post(f"/admin/celpip/attempts/{attempt_id}/sections/writing/start")
    assert second.status_code == 409
    assert "finish the reading section" in second.json()["detail"]


def test_sections_must_be_started_in_order(session_factory, as_admin):
    """Skipping ahead would let a learner leave a hard section unopened, sit the
    rest, and return to it later with its clock untouched."""
    _, attempt_id = _seed_two_section_test(session_factory)
    with TestClient(app) as client:
        skipped = client.post(f"/admin/celpip/attempts/{attempt_id}/sections/writing/start")
    assert skipped.status_code == 409
    assert "comes first" in skipped.json()["detail"]


def test_the_next_section_opens_once_the_previous_one_is_finished(session_factory, as_admin):
    _, attempt_id = _seed_two_section_test(session_factory)
    with TestClient(app) as client:
        client.post(f"/admin/celpip/attempts/{attempt_id}/sections/reading/start")
        client.post(f"/admin/celpip/attempts/{attempt_id}/sections/reading/complete")
        opened = client.post(f"/admin/celpip/attempts/{attempt_id}/sections/writing/start")
    assert opened.status_code == 200
    assert opened.json()["seconds_remaining"] > 0


def test_an_answer_to_a_never_started_section_is_refused(session_factory, as_admin):
    """The whole exam timer is bypassed by simply never pressing start: an
    unstarted section has no deadline, and a missing deadline reads as
    "not expired"."""
    test_id, attempt_id = _seed_two_section_test(session_factory)
    db = session_factory()
    try:
        question_id = (
            db.query(CelpipTestItem)
            .filter(CelpipTestItem.test_id == test_id)
            .filter(CelpipTestItem.skill == "reading")
            .first()
            .question_id
        )
    finally:
        db.close()

    with TestClient(app) as client:
        response = client.post(
            f"/admin/celpip/attempts/{attempt_id}/responses",
            json={"question_id": question_id, "question_index": 0, "selected_option": "A"},
        )
    assert response.status_code == 409
    assert "has not been started" in response.json()["detail"]


def test_an_answer_to_a_finished_section_is_refused(session_factory, as_admin):
    test_id, attempt_id = _seed_two_section_test(session_factory)
    db = session_factory()
    try:
        question_id = (
            db.query(CelpipTestItem)
            .filter(CelpipTestItem.test_id == test_id)
            .filter(CelpipTestItem.skill == "reading")
            .first()
            .question_id
        )
    finally:
        db.close()

    with TestClient(app) as client:
        client.post(f"/admin/celpip/attempts/{attempt_id}/sections/reading/start")
        assert client.post(
            f"/admin/celpip/attempts/{attempt_id}/responses",
            json={"question_id": question_id, "question_index": 0, "selected_option": "A"},
        ).status_code == 200
        client.post(f"/admin/celpip/attempts/{attempt_id}/sections/reading/complete")
        after = client.post(
            f"/admin/celpip/attempts/{attempt_id}/responses",
            json={"question_id": question_id, "question_index": 0, "selected_option": "B"},
        )
    assert after.status_code == 409
    assert "no longer accepts answers" in after.json()["detail"]


def test_learn_mode_keeps_free_navigation_across_sections(session_factory, as_admin):
    """Section sequencing reproduces the official flow, which learn mode is
    explicitly not. A drill you cannot move around in is a worse drill."""
    test_id, attempt_id = _seed_two_section_test(session_factory, practice_mode="learn")
    db = session_factory()
    try:
        reading_q = (
            db.query(CelpipTestItem)
            .filter(CelpipTestItem.test_id == test_id)
            .filter(CelpipTestItem.skill == "reading")
            .first()
            .question_id
        )
    finally:
        db.close()

    with TestClient(app) as client:
        # Out of order, both open at once, and an answer with no section started.
        assert client.post(
            f"/admin/celpip/attempts/{attempt_id}/sections/writing/start"
        ).status_code == 200
        assert client.post(
            f"/admin/celpip/attempts/{attempt_id}/sections/reading/start"
        ).status_code == 200
        assert client.post(
            f"/admin/celpip/attempts/{attempt_id}/responses",
            json={"question_id": reading_q, "question_index": 0, "selected_option": "A"},
        ).status_code == 200


def test_results_report_whether_scoring_is_still_in_flight(session_factory, as_admin, monkeypatch):
    """The client polls on this rather than on attempt status alone: a crashed
    worker leaves the attempt mid-state, and a retried failure would otherwise
    never be picked up without a manual reload."""
    from app.services import maintenance_jobs

    monkeypatch.setattr(maintenance_jobs, "enqueue_celpip_evaluation", lambda *, attempt_id: "job_1")
    monkeypatch.setattr(
        maintenance_jobs, "celpip_evaluation_job_state",
        lambda attempt_id: {
            "job_id": "job_1", "status": "queued", "attempt_count": 2,
            "max_attempts": 3, "retry_pending": True, "active": True, "error": "timeout",
        },
    )

    _, attempt_id = _seed_reading_test(session_factory, practice_mode="timed")
    with TestClient(app) as client:
        client.post(f"/admin/celpip/attempts/{attempt_id}/sections/reading/start")
        client.post(f"/admin/celpip/attempts/{attempt_id}/submit")
        results = client.get(f"/admin/celpip/attempts/{attempt_id}/results").json()

    assert results["evaluation_pending"] is True
    assert results["evaluation_job"]["attempt_count"] == 2
    assert results["evaluation_job"]["max_attempts"] == 3


def test_the_dashboard_warms_the_question_buffer(session_factory, as_admin, topup_calls):
    """A fresh deployment should be filling its buffer before the learner ever
    reaches Practice, rather than making the first launch wait for everything."""
    with TestClient(app) as client:
        home = client.get("/admin/celpip/home").json()

    assert topup_calls, "opening the dashboard must queue a top-up"
    assert home["launch_ready"] is False
    assert home["preparing_questions"] > 0


def test_the_dashboard_survives_a_broken_job_queue(session_factory, as_admin, monkeypatch):
    """A background refill problem must not take the whole dashboard down."""
    def boom(**kwargs):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(stock, "plan_topup", boom)
    with TestClient(app) as client:
        response = client.get("/admin/celpip/home")
    assert response.status_code == 200
