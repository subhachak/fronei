from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.services.agent.context_classifier import ContextDecision
from app.services.agent.context_contracts import LAYER_L3, SCOPE_WORKSPACE, SOURCE_FACT
from app.services.agent.context_registry import get_context_items
from app.services.agent.known_facts import get_facts, get_facts_for_type, upsert_fact
from app.services.agent.models import TurnRequest


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
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


def test_upsert_fact_then_get_facts_round_trip_on_sqlite():
    Session = _session()
    with Session() as db:
        upsert_fact("user_1", "workspace_1", "workspace", "project", "Context OS", db=db, confidence=0.9)
        facts = get_facts("user_1", "workspace_1", db=db)

    assert facts == [{"fact_key": "project", "fact_value": "Context OS", "confidence": 0.9}]


def test_upsert_fact_updates_existing_key_without_duplicate():
    Session = _session()
    with Session() as db:
        upsert_fact("user_1", "workspace_1", "workspace", "project", "Old", db=db)
        upsert_fact("user_1", "workspace_1", "workspace", "project", "New", db=db)
        facts = get_facts("user_1", "workspace_1", db=db)

    assert facts == [{"fact_key": "project", "fact_value": "New", "confidence": 1.0}]


def test_get_facts_unknown_entity_returns_empty():
    Session = _session()
    with Session() as db:
        assert get_facts("user_1", "missing", db=db) == []


def test_get_facts_for_type_returns_multiple_entities():
    Session = _session()
    with Session() as db:
        upsert_fact("user_1", "workspace_1", "workspace", "project", "Context OS", db=db)
        upsert_fact("user_1", "workspace_2", "workspace", "project", "Research OS", db=db)
        facts = get_facts_for_type("user_1", "workspace", db=db)

    assert facts == [
        {"entity_id": "workspace_1", "fact_key": "project", "fact_value": "Context OS", "confidence": 1.0},
        {"entity_id": "workspace_2", "fact_key": "project", "fact_value": "Research OS", "confidence": 1.0},
    ]


def test_context_registry_falls_back_to_l3_facts_when_l2_empty(monkeypatch):
    class RequestWithUser(TurnRequest):
        user_id: str = "user_1"

    def empty_l2(*_args, **_kwargs):
        return []

    def fake_facts(user_id, entity_type, *, db):
        assert user_id == "user_1"
        assert entity_type == "workspace"
        assert db == "db"
        return [{"entity_id": "workspace_1", "fact_key": "project", "fact_value": "Context OS", "confidence": 0.8}]

    monkeypatch.setattr("app.services.agent.session_memory.recall_similar_sessions", empty_l2)
    monkeypatch.setattr("app.services.agent.known_facts.get_facts_for_type", fake_facts)
    decision = ContextDecision(
        intent="same_workspace_recall",
        needs_context=True,
        target_scopes=[SCOPE_WORKSPACE],
        reason="test",
    )

    items = get_context_items(RequestWithUser(message="What do we know here?"), decision, db="db")

    assert len(items) == 1
    assert items[0].layer == LAYER_L3
    assert items[0].scope == SCOPE_WORKSPACE
    assert items[0].source_type == SOURCE_FACT
    assert items[0].content == "workspace_1.project: Context OS"
    assert items[0].confidence == 0.8
    assert items[0].provenance == "L3:fact:workspace_1:project"
