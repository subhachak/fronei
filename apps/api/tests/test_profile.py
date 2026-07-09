import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import get_current_user_id, get_current_user_is_admin
from app.db.models import Base, Conversation, DocumentTemplate, Turn, User, Workspace
from app.main import app
from app.routers import profile as profile_router


def _sqlite_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    # known_facts and session_summaries are raw-SQL tables (see
    # alembic/versions/f0b1c2d3e4f6_*, f0a1b2c3d4e5_*), not ORM-mapped, so
    # Base.metadata.create_all() above doesn't create them.
    with engine.begin() as connection:
        connection.execute(text(
            """
            CREATE TABLE known_facts (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                fact_key TEXT NOT NULL,
                fact_value TEXT NOT NULL,
                source_conversation_id TEXT,
                confidence REAL NOT NULL DEFAULT 1.0,
                as_of_date TEXT,
                last_verified_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        ))
        connection.execute(text(
            """
            CREATE TABLE session_summaries (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                summary TEXT NOT NULL,
                embedding TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        ))
    return sessionmaker(bind=engine)


def _as_user(user_id: str):
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    app.dependency_overrides[get_current_user_is_admin] = lambda: False


def _make_workspace(db, workspace_id: str, user_id: str, *, priorities=None, pinned_facts=None) -> Workspace:
    workspace = Workspace(
        id=workspace_id,
        user_id=user_id,
        name=workspace_id,
        priorities_json=json.dumps(priorities or []),
        pinned_facts_json=json.dumps(pinned_facts or []),
    )
    db.add(workspace)
    db.commit()
    return workspace


def _make_conversation(db, conversation_id: str, user_id: str, workspace_id: str) -> Conversation:
    conversation = Conversation(id=conversation_id, user_id=user_id, workspace_id=workspace_id)
    db.add(conversation)
    db.commit()
    return conversation


def _make_turn(db, user_id: str, conversation_id: str, **kwargs) -> Turn:
    defaults = dict(
        id=f"t_{user_id}_{conversation_id}_{datetime.now(timezone.utc).timestamp()}",
        user_id=user_id,
        conversation_id=conversation_id,
        objective="ask",
        route="direct_answer",
        status="completed",
        answer="ok",
        model_used="gpt-4.1-mini",
        cost_usd=0.01,
        latency_ms=120,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kwargs)
    turn = Turn(**defaults)
    db.add(turn)
    db.commit()
    return turn


def test_get_profile_creates_user_row_and_returns_defaults(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(profile_router, "SessionLocal", Session)
    _as_user("u1")
    try:
        with TestClient(app) as client:
            resp = client.get("/profile/me")
        assert resp.status_code == 200
        body = resp.json()
        assert body["user_id"] == "u1"
        assert body["preferences"] == []
        assert body["settings"] == {}
        with Session() as db:
            assert db.query(User).filter(User.clerk_id == "u1").first() is not None
    finally:
        app.dependency_overrides.clear()


def test_update_and_get_preferences_round_trip(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(profile_router, "SessionLocal", Session)
    _as_user("u1")
    try:
        with TestClient(app) as client:
            patched = client.patch("/profile/preferences", json={"preferences": ["Prefers terse responses", ""]})
            assert patched.status_code == 200
            assert patched.json()["preferences"] == ["Prefers terse responses"]

            fetched = client.get("/profile/me")
            assert fetched.json()["preferences"] == ["Prefers terse responses"]
    finally:
        app.dependency_overrides.clear()


def test_settings_round_trip_and_partial_update(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(profile_router, "SessionLocal", Session)
    _as_user("u1")
    try:
        with TestClient(app) as client:
            first = client.patch("/profile/settings", json={"quality_mode": "executive"})
            assert first.json() == {"quality_mode": "executive"}

            second = client.patch("/profile/settings", json={"output_format": "pptx"})
            assert second.json() == {"quality_mode": "executive", "output_format": "pptx"}

            third = client.patch("/profile/settings", json={"default_template_id": "tpl_123"})
            assert third.json() == {"quality_mode": "executive", "output_format": "pptx", "default_template_id": "tpl_123"}

            fetched = client.get("/profile/settings")
            assert fetched.json() == {"quality_mode": "executive", "output_format": "pptx", "default_template_id": "tpl_123"}
    finally:
        app.dependency_overrides.clear()


def test_settings_rejects_invalid_enum_value(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(profile_router, "SessionLocal", Session)
    _as_user("u1")
    try:
        with TestClient(app) as client:
            resp = client.patch("/profile/settings", json={"quality_mode": "ultra"})
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_list_workspace_profiles_includes_priorities_and_stats(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(profile_router, "SessionLocal", Session)
    db = Session()
    _make_workspace(db, "w1", "u1", priorities=["Roadmap planning"], pinned_facts=["Customer is Acme"])
    _make_conversation(db, "c1", "u1", "w1")
    _make_turn(db, "u1", "c1", cost_usd=0.5)
    _make_turn(db, "u1", "c1", cost_usd=0.25)
    db.close()

    _as_user("u1")
    try:
        with TestClient(app) as client:
            resp = client.get("/profile/workspaces")
        assert resp.status_code == 200
        workspaces = resp.json()["workspaces"]
        assert len(workspaces) == 1
        assert workspaces[0]["priorities"] == ["Roadmap planning"]
        assert workspaces[0]["pinned_facts"] == ["Customer is Acme"]
        assert workspaces[0]["turn_count"] == 2
        assert workspaces[0]["total_cost_usd"] == 0.75
    finally:
        app.dependency_overrides.clear()


def test_cannot_see_or_edit_another_users_workspace(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(profile_router, "SessionLocal", Session)
    db = Session()
    _make_workspace(db, "other_ws", "other_user", priorities=["Confidential project"])
    db.close()

    _as_user("u1")
    try:
        with TestClient(app) as client:
            listed = client.get("/profile/workspaces")
            assert listed.json()["workspaces"] == []

            patched = client.patch("/profile/workspaces/other_ws/priorities", json={"priorities": ["hijacked"]})
            assert patched.status_code == 404
            facts = client.patch("/profile/workspaces/other_ws/facts", json={"facts": ["hijacked"]})
            assert facts.status_code == 404
    finally:
        app.dependency_overrides.clear()

    with Session() as db:
        other = db.get(Workspace, "other_ws")
        assert json.loads(other.priorities_json) == ["Confidential project"]
        assert json.loads(other.pinned_facts_json) == []


def test_update_own_workspace_priorities(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(profile_router, "SessionLocal", Session)
    db = Session()
    _make_workspace(db, "w1", "u1")
    db.close()

    _as_user("u1")
    try:
        with TestClient(app) as client:
            resp = client.patch("/profile/workspaces/w1/priorities", json={"priorities": ["New priority"]})
        assert resp.status_code == 200
        assert resp.json()["priorities"] == ["New priority"]
    finally:
        app.dependency_overrides.clear()


def test_update_own_workspace_pinned_facts(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(profile_router, "SessionLocal", Session)
    db = Session()
    _make_workspace(db, "w1", "u1")
    db.close()

    _as_user("u1")
    try:
        with TestClient(app) as client:
            long_fact = "x" * 250
            resp = client.patch("/profile/workspaces/w1/facts", json={"facts": [" Customer is Acme ", "", long_fact]})
            assert resp.status_code == 200
            assert resp.json()["facts"] == ["Customer is Acme", "x" * 200]

            fetched = client.get("/profile/workspaces")
            assert fetched.json()["workspaces"][0]["pinned_facts"] == ["Customer is Acme", "x" * 200]
    finally:
        app.dependency_overrides.clear()


def test_workspace_pinned_facts_render_into_context(monkeypatch):
    from app.services.agent import persistence

    Session = _sqlite_session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    db = Session()
    workspace = _make_workspace(
        db,
        "w1",
        "u1",
        pinned_facts=["Customer is Acme", "Use invoice IDs from ERP"],
    )
    _make_conversation(db, "c1", "u1", "w1")

    assert persistence._workspace_pinned_facts(None) == []
    workspace.pinned_facts_json = json.dumps(["x" * 250 for _ in range(25)])
    assert len(persistence._workspace_pinned_facts(workspace)) == 12
    assert persistence._workspace_pinned_facts(workspace)[0] == "x" * 200
    workspace.pinned_facts_json = "{bad json"
    assert persistence._workspace_pinned_facts(workspace) == []
    workspace.pinned_facts_json = json.dumps(["Customer is Acme", "Use invoice IDs from ERP"])
    db.commit()
    db.close()

    context = persistence.conversation_context_text("u1", "c1", current_message="hello")
    assert "Pinned facts for this workspace" in context
    assert "Customer is Acme" in context
    assert "Use invoice IDs from ERP" in context


def test_usage_report_scoped_to_caller_and_includes_bi_dimensions(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(profile_router, "SessionLocal", Session)
    db = Session()
    _make_workspace(db, "w1", "u1")
    _make_conversation(db, "c1", "u1", "w1")
    _make_turn(db, "u1", "c1", cost_usd=0.10, latency_ms=100, model_used="gpt-4.1-mini", route="direct_answer")
    _make_turn(db, "u1", "c1", cost_usd=0.20, latency_ms=300, model_used="claude-sonnet-4-6", route="research", status="failed")
    # A different user's turn must never leak into u1's report.
    _make_workspace(db, "w2", "u2")
    _make_conversation(db, "c2", "u2", "w2")
    _make_turn(db, "u2", "c2", cost_usd=99.0)
    db.close()

    _as_user("u1")
    try:
        with TestClient(app) as client:
            resp = client.get("/profile/usage?range=all")
        assert resp.status_code == 200
        body = resp.json()
        assert body["summary"]["requests"] == 2
        assert body["summary"]["total_cost"] == 0.30
        assert body["summary"]["failed_requests"] == 1
        assert body["summary"]["failure_rate"] == 0.5
        routes = {r["route"]: r["count"] for r in body["route_distribution"]}
        assert routes == {"direct_answer": 1, "research": 1}
        models = {m["model"]: m for m in body["model_performance"]}
        assert "gpt-4.1-mini" in models and "claude-sonnet-4-6" in models
        assert models["claude-sonnet-4-6"]["failure_count"] == 1
    finally:
        app.dependency_overrides.clear()


def test_export_my_data_includes_turns_workspaces_and_preferences(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(profile_router, "SessionLocal", Session)
    db = Session()
    user = User(
        clerk_id="u1",
        email="u1@example.com",
        profile_json=json.dumps({"preferences": ["terse"]}),
        settings_json=json.dumps({"quality_mode": "executive", "output_format": "pptx"}),
    )
    db.add(user)
    _make_workspace(db, "w1", "u1", priorities=["Roadmap"], pinned_facts=["Customer is Acme"])
    _make_conversation(db, "c1", "u1", "w1")
    _make_turn(db, "u1", "c1", objective="Draft a memo", answer="Here it is")
    db.close()

    _as_user("u1")
    try:
        with TestClient(app) as client:
            resp = client.get("/profile/export")
        assert resp.status_code == 200
        body = resp.json()
        assert body["preferences"] == ["terse"]
        assert body["settings"] == {"quality_mode": "executive", "output_format": "pptx"}
        assert body["workspaces"][0]["priorities"] == ["Roadmap"]
        assert body["workspaces"][0]["pinned_facts"] == ["Customer is Acme"]
        assert body["turns"][0]["objective"] == "Draft a memo"
    finally:
        app.dependency_overrides.clear()


def test_privacy_delete_requires_explicit_confirm(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(profile_router, "SessionLocal", Session)
    _as_user("u1")
    try:
        with TestClient(app) as client:
            resp = client.post("/profile/privacy-delete", json={"confirm": False})
        assert resp.status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_privacy_delete_removes_everything_for_caller_only(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(profile_router, "SessionLocal", Session)
    db = Session()
    user = User(
        clerk_id="u1",
        email="u1@example.com",
        profile_json=json.dumps({"preferences": ["terse"]}),
        settings_json=json.dumps({"quality_mode": "executive"}),
    )
    db.add(user)
    other_user = User(clerk_id="u2", email="u2@example.com", profile_json=json.dumps({"preferences": ["keep me"]}))
    db.add(other_user)
    _make_workspace(db, "w1", "u1")
    _make_conversation(db, "c1", "u1", "w1")
    _make_turn(db, "u1", "c1")
    _make_workspace(db, "w2", "u2")
    db.add(DocumentTemplate(user_id="u1", name="My deck", storage_key="templates/u1/deck.pptx"))
    db.execute(text(
        "INSERT INTO known_facts (id, user_id, entity_id, entity_type, fact_key, fact_value) "
        "VALUES ('f1', 'u1', 'acme', 'workspace', 'stack', 'Postgres')"
    ))
    db.execute(text(
        "INSERT INTO known_facts (id, user_id, entity_id, entity_type, fact_key, fact_value) "
        "VALUES ('f2', 'u2', 'acme', 'workspace', 'stack', 'keep me')"
    ))
    db.execute(text(
        "INSERT INTO session_summaries (id, user_id, conversation_id, summary) "
        "VALUES ('s1', 'u1', 'c1', 'Discussed the roadmap.')"
    ))
    db.execute(text(
        "INSERT INTO session_summaries (id, user_id, conversation_id, summary) "
        "VALUES ('s2', 'u2', 'c2', 'keep me')"
    ))
    db.commit()
    db.close()

    _as_user("u1")
    try:
        with TestClient(app) as client:
            resp = client.post("/profile/privacy-delete", json={"confirm": True})
        assert resp.status_code == 200
        deleted = resp.json()["deleted"]
        assert deleted["turns"] == 1
        assert deleted["workspaces"] == 1
        assert deleted["consolidated_preferences"] == 1
        assert deleted["settings"] == 1
        assert deleted["document_templates"] == 1
        assert deleted["known_facts"] == 1
        assert deleted["session_summaries"] == 1
    finally:
        app.dependency_overrides.clear()

    with Session() as db:
        assert db.query(Turn).filter(Turn.user_id == "u1").count() == 0
        assert db.query(Workspace).filter(Workspace.user_id == "u1").count() == 0
        assert db.query(DocumentTemplate).filter(DocumentTemplate.user_id == "u1").count() == 0
        u1 = db.query(User).filter(User.clerk_id == "u1").first()
        assert json.loads(u1.profile_json) == {}
        assert json.loads(u1.settings_json) == {}
        assert db.execute(text("SELECT COUNT(*) FROM known_facts WHERE user_id = 'u1'")).scalar() == 0
        assert db.execute(text("SELECT COUNT(*) FROM session_summaries WHERE user_id = 'u1'")).scalar() == 0
        # The other user's data must be untouched.
        assert db.query(Workspace).filter(Workspace.user_id == "u2").count() == 1
        u2 = db.query(User).filter(User.clerk_id == "u2").first()
        assert json.loads(u2.profile_json)["preferences"] == ["keep me"]
        assert db.execute(text("SELECT fact_value FROM known_facts WHERE user_id = 'u2'")).scalar() == "keep me"
        assert db.execute(text("SELECT summary FROM session_summaries WHERE user_id = 'u2'")).scalar() == "keep me"
