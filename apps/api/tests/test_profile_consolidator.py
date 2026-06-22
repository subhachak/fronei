import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, Conversation, Turn, User, Workspace
from app.services.agent import model_client, profile_consolidator


def _sqlite_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _make_user(db, user_id: str) -> User:
    user = User(clerk_id=user_id, email=f"{user_id}@example.com")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_workspace(db, workspace_id: str, user_id: str) -> Workspace:
    workspace = Workspace(id=workspace_id, user_id=user_id, name=workspace_id)
    db.add(workspace)
    db.commit()
    db.refresh(workspace)
    return workspace


def _make_conversation(db, conversation_id: str, user_id: str, workspace_id: str) -> Conversation:
    conversation = Conversation(id=conversation_id, user_id=user_id, workspace_id=workspace_id)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


def _make_turn(db, user_id: str, conversation_id: str, *, objective: str, answer: str, created_at=None) -> Turn:
    turn = Turn(
        id=f"t_{user_id}_{conversation_id}_{objective[:8]}_{(created_at or datetime.now(timezone.utc)).timestamp()}",
        user_id=user_id,
        conversation_id=conversation_id,
        objective=objective,
        route="direct_answer",
        status="completed",
        answer=answer,
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.add(turn)
    db.commit()
    return turn


def _fake_simple_completion(preferences, priorities):
    def fake(system, user, *, role=None, max_tokens=600, **kwargs):
        assert role == "profile_consolidation"
        return type("R", (), {"text": json.dumps({
            "preferences": preferences,
            "current_priorities": priorities,
        })})()
    return fake


def test_consolidate_workspace_persists_priorities_on_workspace_and_preferences_on_user(monkeypatch):
    Session = _sqlite_session()
    db = Session()
    _make_user(db, "u1")
    _make_workspace(db, "w1", "u1")
    _make_conversation(db, "c1", "u1", "w1")
    for i in range(5):
        _make_turn(db, "u1", "c1", objective=f"Draft a memo about Q{i} roadmap", answer="Sure, here it is.")

    monkeypatch.setattr(
        model_client, "simple_completion",
        _fake_simple_completion(["Prefers terse, table-based responses"], ["Roadmap planning for Q4"]),
    )

    result = profile_consolidator.consolidate_workspace(db, "w1")
    assert result["status"] == "consolidated"
    assert result["preference_count"] == 1
    assert result["priority_count"] == 1

    workspace = db.get(Workspace, "w1")
    assert json.loads(workspace.priorities_json) == ["Roadmap planning for Q4"]
    assert workspace.priorities_consolidated_at is not None

    user = db.query(User).filter(User.clerk_id == "u1").first()
    assert json.loads(user.profile_json)["preferences"] == ["Prefers terse, table-based responses"]
    db.close()


def test_workspace_priorities_do_not_bleed_into_a_different_workspace(monkeypatch):
    """Direct regression test for the cross-workspace bleed: a user with two
    workspaces should get distinct, non-overlapping priorities for each."""
    Session = _sqlite_session()
    db = Session()
    _make_user(db, "u1")
    _make_workspace(db, "acme", "u1")
    _make_workspace(db, "personal", "u1")
    _make_conversation(db, "c_acme", "u1", "acme")
    _make_conversation(db, "c_personal", "u1", "personal")
    _make_turn(db, "u1", "c_acme", objective="Plan Acme Corp's billing migration", answer="ok")
    _make_turn(db, "u1", "c_personal", objective="Help me plan my home renovation", answer="ok")

    calls = []

    def fake(system, user_text, *, role=None, max_tokens=600, **kwargs):
        calls.append(user_text)
        if "Acme" in user_text:
            priorities = ["Acme Corp billing migration"]
        else:
            priorities = ["Home renovation planning"]
        return type("R", (), {"text": json.dumps({"preferences": [], "current_priorities": priorities})})()

    monkeypatch.setattr(model_client, "simple_completion", fake)

    profile_consolidator.consolidate_workspace(db, "acme")
    profile_consolidator.consolidate_workspace(db, "personal")

    # Each call's transcript should only ever contain that one workspace's turns.
    assert len(calls) == 2
    assert "Acme" in calls[0] and "renovation" not in calls[0]
    assert "renovation" in calls[1] and "Acme" not in calls[1]

    acme = db.get(Workspace, "acme")
    personal = db.get(Workspace, "personal")
    assert json.loads(acme.priorities_json) == ["Acme Corp billing migration"]
    assert json.loads(personal.priorities_json) == ["Home renovation planning"]
    # The bug being regression-tested: personal's priorities must not contain
    # anything derived from the acme workspace, and vice versa.
    assert "Acme" not in json.dumps(json.loads(personal.priorities_json))
    assert "renovation" not in json.dumps(json.loads(acme.priorities_json)).lower()
    db.close()


def test_consolidate_workspace_skips_with_no_completed_turns(monkeypatch):
    Session = _sqlite_session()
    db = Session()
    _make_user(db, "u1")
    _make_workspace(db, "w1", "u1")

    def fail_completion(*args, **kwargs):
        raise AssertionError("should not call the model with no turns")

    monkeypatch.setattr(model_client, "simple_completion", fail_completion)

    result = profile_consolidator.consolidate_workspace(db, "w1")
    assert result == {"workspace_id": "w1", "status": "skipped", "reason": "no_completed_turns"}
    db.close()


def test_consolidate_workspace_skips_insufficient_new_activity(monkeypatch):
    Session = _sqlite_session()
    db = Session()
    _make_user(db, "u1")
    workspace = _make_workspace(db, "w1", "u1")
    _make_conversation(db, "c1", "u1", "w1")
    _make_turn(db, "u1", "c1", objective="First ask", answer="ok")
    workspace.priorities_consolidated_at = datetime.now(timezone.utc)
    db.commit()

    def fail_completion(*args, **kwargs):
        raise AssertionError("should not reconsolidate with too little new activity")

    monkeypatch.setattr(model_client, "simple_completion", fail_completion)

    result = profile_consolidator.consolidate_workspace(db, "w1")
    assert result["status"] == "skipped"
    assert result["reason"] == "insufficient_new_activity"
    db.close()


def test_consolidate_workspace_force_bypasses_activity_gate(monkeypatch):
    Session = _sqlite_session()
    db = Session()
    _make_user(db, "u1")
    workspace = _make_workspace(db, "w1", "u1")
    _make_conversation(db, "c1", "u1", "w1")
    _make_turn(db, "u1", "c1", objective="First ask", answer="ok")
    workspace.priorities_consolidated_at = datetime.now(timezone.utc)
    db.commit()

    monkeypatch.setattr(model_client, "simple_completion", _fake_simple_completion([], []))

    result = profile_consolidator.consolidate_workspace(db, "w1", force=True)
    assert result["status"] == "consolidated"
    db.close()


def test_consolidate_workspace_handles_malformed_model_output(monkeypatch):
    Session = _sqlite_session()
    db = Session()
    _make_user(db, "u1")
    _make_workspace(db, "w1", "u1")
    _make_conversation(db, "c1", "u1", "w1")
    _make_turn(db, "u1", "c1", objective="ask", answer="ok")

    def fake(system, user, *, role=None, max_tokens=600, **kwargs):
        return type("R", (), {"text": "not json at all"})()

    monkeypatch.setattr(model_client, "simple_completion", fake)

    result = profile_consolidator.consolidate_workspace(db, "w1")
    assert result["status"] == "consolidated"
    assert result["preference_count"] == 0
    assert result["priority_count"] == 0
    db.close()


def test_consolidate_workspace_handles_model_failure(monkeypatch):
    Session = _sqlite_session()
    db = Session()
    _make_user(db, "u1")
    _make_workspace(db, "w1", "u1")
    _make_conversation(db, "c1", "u1", "w1")
    _make_turn(db, "u1", "c1", objective="ask", answer="ok")

    def fake(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(model_client, "simple_completion", fake)

    result = profile_consolidator.consolidate_workspace(db, "w1")
    assert result == {"workspace_id": "w1", "status": "failed", "reason": "model_call_failed"}
    db.close()


def test_consolidate_all_active_workspaces_only_considers_recent_completed_turns(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(profile_consolidator, "SessionLocal", Session)
    db = Session()
    _make_user(db, "active_user")
    _make_user(db, "stale_user")
    _make_workspace(db, "active_ws", "active_user")
    _make_workspace(db, "stale_ws", "stale_user")
    _make_conversation(db, "active_c", "active_user", "active_ws")
    _make_conversation(db, "stale_c", "stale_user", "stale_ws")
    _make_turn(db, "active_user", "active_c", objective="ask", answer="ok")
    _make_turn(
        db,
        "stale_user",
        "stale_c",
        objective="old ask",
        answer="ok",
        created_at=datetime.now(timezone.utc) - timedelta(days=90),
    )
    db.close()

    monkeypatch.setattr(model_client, "simple_completion", _fake_simple_completion([], []))

    result = profile_consolidator.consolidate_all_active_workspaces(lookback_days=30)
    assert result["workspaces_considered"] == 1
    assert result.get("consolidated") == 1
    assert result["workspaces_remaining"] == 0


def test_consolidate_all_active_workspaces_caps_batch_and_reports_remaining(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(profile_consolidator, "SessionLocal", Session)
    db = Session()
    for i in range(5):
        user_id = f"u{i}"
        ws_id = f"w{i}"
        conv_id = f"c{i}"
        _make_user(db, user_id)
        _make_workspace(db, ws_id, user_id)
        _make_conversation(db, conv_id, user_id, ws_id)
        _make_turn(db, user_id, conv_id, objective="ask", answer="ok")
    db.close()

    monkeypatch.setattr(model_client, "simple_completion", _fake_simple_completion([], []))

    result = profile_consolidator.consolidate_all_active_workspaces(lookback_days=30, limit=2)
    assert result["workspaces_considered"] == 2
    assert result["workspaces_remaining"] == 3


def test_default_and_max_batch_limit_stay_small_enough_to_avoid_request_timeouts():
    """Regression guard for the timeout risk: with a 30s per-workspace model
    call and no background job queue, the default and max batch sizes must
    stay small enough that even the worst case (every workspace in the batch
    blocking for the full timeout) finishes well within a typical
    platform/CI HTTP request timeout (commonly 30-120s)."""
    assert profile_consolidator.DEFAULT_BATCH_LIMIT <= 5
    assert profile_consolidator.MAX_BATCH_LIMIT <= 10


def test_merge_preferences_keeps_new_items_and_preserves_unduplicated_old_ones():
    existing = ["Prefers terse, table-based responses", "Likes tables over prose"]
    new = ["Asks for executive summaries"]
    merged = profile_consolidator._merge_preferences(existing, new)
    assert merged[0] == "Asks for executive summaries"
    assert "Prefers terse, table-based responses" in merged
    assert "Likes tables over prose" in merged


def test_merge_preferences_dedupes_case_and_whitespace_insensitively():
    existing = ["Prefers terse responses"]
    new = ["  prefers   TERSE responses  "]
    merged = profile_consolidator._merge_preferences(existing, new)
    assert merged == ["  prefers   TERSE responses  "]


def test_merge_preferences_caps_and_drops_oldest_first():
    existing = [f"old preference {i}" for i in range(6)]
    new = ["brand new preference"]
    merged = profile_consolidator._merge_preferences(existing, new, cap=6)
    assert len(merged) == 6
    assert merged[0] == "brand new preference"
    assert "old preference 5" not in merged  # the least-recently-reinforced one dropped


def test_a_second_workspaces_empty_extraction_does_not_wipe_first_workspaces_preferences(monkeypatch):
    """Direct regression test for the preference-wipe bug: consolidating a
    second, low-signal workspace must not erase a preference a different
    workspace already established for the same user."""
    Session = _sqlite_session()
    db = Session()
    _make_user(db, "u1")
    _make_workspace(db, "active_ws", "u1")
    _make_workspace(db, "quiet_ws", "u1")
    _make_conversation(db, "c_active", "u1", "active_ws")
    _make_conversation(db, "c_quiet", "u1", "quiet_ws")
    for i in range(5):
        _make_turn(db, "u1", "c_active", objective=f"Draft note {i}", answer="ok")
    _make_turn(db, "u1", "c_quiet", objective="quick one-off question", answer="ok")

    monkeypatch.setattr(
        model_client, "simple_completion",
        _fake_simple_completion(["Prefers terse, table-based responses"], ["Roadmap planning"]),
    )
    profile_consolidator.consolidate_workspace(db, "active_ws")

    # The second workspace's transcript has too little signal to detect any
    # durable preference -- the model (correctly) returns an empty list.
    monkeypatch.setattr(model_client, "simple_completion", _fake_simple_completion([], []))
    profile_consolidator.consolidate_workspace(db, "quiet_ws", force=True)

    user = db.query(User).filter(User.clerk_id == "u1").first()
    assert json.loads(user.profile_json)["preferences"] == ["Prefers terse, table-based responses"]
    db.close()
