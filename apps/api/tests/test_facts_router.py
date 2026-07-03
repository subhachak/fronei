from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import get_current_user_id
from app.routers import facts as facts_router


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        connection.execute(
            text(
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
                    last_verified_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE UNIQUE INDEX ux_known_facts_user_entity_key
                ON known_facts (user_id, entity_id, fact_key)
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE INDEX ix_known_facts_user_entity_type
                ON known_facts (user_id, entity_type)
                """
            )
        )
    return sessionmaker(bind=engine)


def _client(monkeypatch, *, authenticated: bool = True) -> TestClient:
    monkeypatch.setattr(facts_router, "SessionLocal", _session_factory())

    app = FastAPI()
    app.include_router(facts_router.router, prefix="/api")
    if authenticated:
        app.dependency_overrides[get_current_user_id] = lambda: "user_1"
    return TestClient(app)


def test_list_facts_returns_empty_for_new_user(monkeypatch):
    with _client(monkeypatch) as client:
        response = client.get("/api/facts")

    assert response.status_code == 200
    assert response.json() == []


def test_put_fact_upserts_and_returns_fact(monkeypatch):
    with _client(monkeypatch) as client:
        response = client.put(
            "/api/facts",
            json={
                "entity_id": "workspace_1",
                "entity_type": "workspace",
                "fact_key": "project",
                "fact_value": "Context OS",
                "confidence": 0.8,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["entity_id"] == "workspace_1"
    assert body["entity_type"] == "workspace"
    assert body["fact_key"] == "project"
    assert body["fact_value"] == "Context OS"
    assert body["confidence"] == 0.8
    assert body["source_conversation_id"] is None
    assert body["id"].startswith("fact_")
    assert body["created_at"]
    assert body["updated_at"]


def test_put_fact_updates_existing_key_without_duplicate(monkeypatch):
    with _client(monkeypatch) as client:
        first = {
            "entity_id": "workspace_1",
            "entity_type": "workspace",
            "fact_key": "project",
            "fact_value": "Old",
        }
        assert client.put("/api/facts", json=first).status_code == 200
        second = {**first, "fact_value": "New"}
        assert client.put("/api/facts", json=second).status_code == 200
        response = client.get("/api/facts")

    assert response.status_code == 200
    facts = response.json()
    assert len(facts) == 1
    assert facts[0]["fact_value"] == "New"


def test_delete_fact_removes_it_from_subsequent_get(monkeypatch):
    with _client(monkeypatch) as client:
        assert client.put(
            "/api/facts",
            json={
                "entity_id": "workspace_1",
                "entity_type": "workspace",
                "fact_key": "project",
                "fact_value": "Context OS",
            },
        ).status_code == 200
        delete_response = client.delete("/api/facts/workspace_1/project")
        get_response = client.get("/api/facts")

    assert delete_response.status_code == 204
    assert get_response.status_code == 200
    assert get_response.json() == []


def test_facts_endpoint_requires_auth(monkeypatch):
    with _client(monkeypatch, authenticated=False) as client:
        response = client.get("/api/facts")

    assert response.status_code == 401
