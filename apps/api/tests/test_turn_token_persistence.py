"""Part 1.6 -- input_tokens/output_tokens/context_tokens_json must persist
onto the Turn row from the same complete_turn() call site that already
persists cost_usd, for both the direct-write and lease-owned worker paths.
"""
from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, Turn
from app.services.agent import persistence
from app.services.agent.models import Goal, TurnRequest, TurnResult


def _session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def test_complete_turn_persists_token_fields_on_direct_write(monkeypatch):
    Session = _session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    goal = Goal(user_id="u1", objective="ask", route="direct")
    result = TurnResult(
        turn_id="turn_1",
        goal=goal,
        answer="answer",
        route="direct",
        cost_usd=0.01,
        input_tokens=123,
        output_tokens=45,
        context_tokens={"conversation": 100, "facts": 23},
    )

    assert persistence.complete_turn(result) is True

    with Session() as db:
        row = db.get(Turn, "turn_1")
        assert row.input_tokens == 123
        assert row.output_tokens == 45
        assert json.loads(row.context_tokens_json) == {"conversation": 100, "facts": 23}


def test_complete_turn_persists_token_fields_via_lease_owner_path(monkeypatch):
    Session = _session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    request = TurnRequest(message="durable task")
    goal = Goal(user_id="u1", objective="durable task", route="direct")
    persistence.enqueue_turn(goal, "turn_2", request, max_attempts=3)
    claimed = persistence.claim_next_turn("worker-a", lease_seconds=60)
    assert claimed is not None

    result = TurnResult(
        turn_id="turn_2",
        goal=goal,
        answer="answer",
        route="direct",
        cost_usd=0.02,
        input_tokens=500,
        output_tokens=200,
        context_tokens={"evidence": 3000},
    )

    assert persistence.complete_turn(result, lease_owner="worker-a") is True

    with Session() as db:
        row = db.get(Turn, "turn_2")
        assert row.input_tokens == 500
        assert row.output_tokens == 200
        assert json.loads(row.context_tokens_json) == {"evidence": 3000}


def test_complete_turn_defaults_token_fields_to_zero_when_unset(monkeypatch):
    Session = _session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    goal = Goal(user_id="u1", objective="clarify me", route="clarify")
    result = TurnResult(turn_id="turn_3", goal=goal, answer="What do you mean?", route="clarify")

    assert persistence.complete_turn(result) is True

    with Session() as db:
        row = db.get(Turn, "turn_3")
        assert row.input_tokens == 0
        assert row.output_tokens == 0
        assert json.loads(row.context_tokens_json) == {}
