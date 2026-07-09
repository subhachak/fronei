"""Phase 2.2 -- the admin privacy-delete path must purge known_facts and
session_summaries alongside turns/workspaces/templates, matching the
self-service /profile/privacy-delete endpoint (see test_profile.py).
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import AdminPrincipal, require_admin_principal
from app.db.models import Base, Conversation, Turn, User, Workspace
from app.main import app
from app.routers import admin as admin_router


def _sqlite_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
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


def test_admin_privacy_delete_dry_run_counts_known_facts_and_session_summaries(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(admin_router, "SessionLocal", Session)
    with Session() as db:
        db.add(User(clerk_id="u1", email="u1@example.com"))
        db.execute(text(
            "INSERT INTO known_facts (id, user_id, entity_id, entity_type, fact_key, fact_value) "
            "VALUES ('f1', 'u1', 'acme', 'workspace', 'stack', 'Postgres')"
        ))
        db.execute(text(
            "INSERT INTO session_summaries (id, user_id, conversation_id, summary) "
            "VALUES ('s1', 'u1', 'c1', 'Discussed the roadmap.')"
        ))
        db.commit()

    app.dependency_overrides[require_admin_principal] = lambda: AdminPrincipal(
        user_id="admin_1", email="admin@example.com",
    )
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/admin/users/u1/privacy-delete",
                json={"document_templates": False, "agent_data": True},
                params={"dry_run": True},
            )
        assert resp.status_code == 200
        counts = resp.json()["counts"]
        assert counts["known_facts"] == 1
        assert counts["session_summaries"] == 1
    finally:
        app.dependency_overrides.clear()


def test_admin_privacy_delete_purges_known_facts_and_session_summaries_for_target_only(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(admin_router, "SessionLocal", Session)
    with Session() as db:
        db.add(User(clerk_id="u1", email="u1@example.com"))
        db.add(User(clerk_id="u2", email="u2@example.com"))
        db.add(Workspace(id="w1", user_id="u1", name="w1"))
        db.add(Conversation(id="c1", user_id="u1", workspace_id="w1"))
        db.add(Turn(id="t1", user_id="u1", conversation_id="c1", objective="ask", route="direct_answer", status="completed", answer="ok"))
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

    app.dependency_overrides[require_admin_principal] = lambda: AdminPrincipal(
        user_id="admin_1", email="admin@example.com",
    )
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/admin/users/u1/privacy-delete",
                json={"document_templates": False, "agent_data": True, "confirm_user_id": "u1"},
            )
        assert resp.status_code == 200
        deleted = resp.json()["deleted"]
        assert deleted["known_facts"] == 1
        assert deleted["session_summaries"] == 1
    finally:
        app.dependency_overrides.clear()

    with Session() as db:
        assert db.execute(text("SELECT COUNT(*) FROM known_facts WHERE user_id = 'u1'")).scalar() == 0
        assert db.execute(text("SELECT COUNT(*) FROM session_summaries WHERE user_id = 'u1'")).scalar() == 0
        assert db.execute(text("SELECT fact_value FROM known_facts WHERE user_id = 'u2'")).scalar() == "keep me"
        assert db.execute(text("SELECT summary FROM session_summaries WHERE user_id = 'u2'")).scalar() == "keep me"
