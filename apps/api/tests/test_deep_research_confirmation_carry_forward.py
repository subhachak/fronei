"""Don't let a confirmation reply to a deep-research offer silently downgrade
to research_level="regular".

Root cause (confirmed via a live trace): the orchestrator's own LLM call
correctly reasoned that a bare "Yes" reply meant "proceed with the deep
research previously offered," and routed to "research" -- but nothing
restored research_level="deep". request.research_level defaults to "auto",
which falls through to choose_research_level(request, ...), a heuristic that
can't detect "deep" signals in a one-word reply and silently produced
"regular" instead of the "deep" tier the user had just been asked to confirm.

Covers:
  - Turn.offered_deep_research persistence
  - persistence.last_turn_offered_deep_research_for_conversation()'s read path
    (mirrors had_unresolved_gaps_for_conversation's query shape exactly)
  - orchestrator's deep-research-confirmation carry-forward rule, including
    no-regression coverage (explicit research_level respected; a genuinely
    new, substantive message is not forced into "deep"; no carry-forward
    when the prior turn wasn't actually a deep-research offer)
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, Conversation, Turn
from app.services.agent import persistence
from app.services.agent.models import Goal, TurnRequest, TurnResult
from app.services.agent.orchestrator import decide_with_options


def _session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


# ---------------------------------------------------------------------------
# Turn.offered_deep_research persistence
# ---------------------------------------------------------------------------

def test_complete_turn_persists_offered_deep_research_true(monkeypatch):
    Session = _session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    goal = Goal(user_id="u1", objective="deep research request", route="research")
    result = TurnResult(
        turn_id="turn_1",
        goal=goal,
        answer="This looks like deep research. Continue with deep research, use regular research instead, or answer directly?",
        route="clarify",
        offered_deep_research=True,
    )

    assert persistence.complete_turn(result) is True

    with Session() as db:
        row = db.get(Turn, "turn_1")
        assert row.offered_deep_research is True


def test_complete_turn_defaults_offered_deep_research_false(monkeypatch):
    Session = _session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    goal = Goal(user_id="u1", objective="ask", route="research")
    result = TurnResult(turn_id="turn_2", goal=goal, answer="Confirmed.", route="research")

    assert persistence.complete_turn(result) is True

    with Session() as db:
        row = db.get(Turn, "turn_2")
        assert row.offered_deep_research is False


# ---------------------------------------------------------------------------
# last_turn_offered_deep_research_for_conversation() -- direct read-path
# ---------------------------------------------------------------------------

def _conversation_with_recent_turns(db, *, user_id: str, recent_turns: list[dict]) -> str:
    from app.services.agent.persistence import _dumps

    conversation = Conversation(
        id="conv_1",
        user_id=user_id,
        workspace_id="ws_1",
        title="Test conversation",
        context_json=_dumps({"recent_turns": recent_turns}),
    )
    db.add(conversation)
    db.commit()
    return conversation.id


def test_last_turn_offered_deep_research_true_when_last_turn_offered_it(monkeypatch):
    Session = _session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    with Session() as db:
        conv_id = _conversation_with_recent_turns(
            db, user_id="u1",
            recent_turns=[{"turn_id": "t1", "route": "clarify", "offered_deep_research": True}],
        )

    assert persistence.last_turn_offered_deep_research_for_conversation("u1", conv_id) is True


def test_last_turn_offered_deep_research_false_when_last_turn_did_not(monkeypatch):
    Session = _session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    with Session() as db:
        conv_id = _conversation_with_recent_turns(
            db, user_id="u1",
            recent_turns=[
                {"turn_id": "t1", "route": "clarify", "offered_deep_research": True},
                {"turn_id": "t2", "route": "research", "offered_deep_research": False},
            ],
        )

    # Only the MOST RECENT turn matters, not any earlier one.
    assert persistence.last_turn_offered_deep_research_for_conversation("u1", conv_id) is False


def test_last_turn_offered_deep_research_false_when_no_turns(monkeypatch):
    Session = _session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    with Session() as db:
        conv_id = _conversation_with_recent_turns(db, user_id="u1", recent_turns=[])

    assert persistence.last_turn_offered_deep_research_for_conversation("u1", conv_id) is False


def test_last_turn_offered_deep_research_false_when_conversation_missing(monkeypatch):
    Session = _session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    assert persistence.last_turn_offered_deep_research_for_conversation("u1", "does-not-exist") is False


def test_last_turn_offered_deep_research_false_when_conversation_id_none(monkeypatch):
    Session = _session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    assert persistence.last_turn_offered_deep_research_for_conversation("u1", None) is False


def test_last_turn_offered_deep_research_round_trip_via_complete_turn(monkeypatch):
    """Full write path: complete_turn() -> async context snapshot -> context_json
    -> last_turn_offered_deep_research_for_conversation() reads it back correctly."""
    Session = _session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    conversation = persistence.ensure_conversation("u1", None, "Assess the JAVAH re-platforming decision")

    goal = Goal(user_id="u1", conversation_id=conversation.id, objective="Assess the JAVAH re-platforming decision", route="research")
    result = TurnResult(
        turn_id="turn_3",
        goal=goal,
        answer="This looks like deep research. Continue with deep research, use regular research instead, or answer directly?",
        route="clarify",
        offered_deep_research=True,
    )

    assert persistence.complete_turn(result) is True
    persistence.wait_for_context_updates()

    assert persistence.last_turn_offered_deep_research_for_conversation("u1", conversation.id) is True


# ---------------------------------------------------------------------------
# Orchestrator deep-research-confirmation carry-forward rule
# ---------------------------------------------------------------------------

def _mock_orchestrator_response(route: str, reason: str, confidence: float = 0.95) -> MagicMock:
    resp = MagicMock()
    resp.text = json.dumps({"route": route, "confidence": confidence, "reason": reason})
    resp.model_used = "test-model"
    resp.latency_ms = 100
    resp.cost_usd = 0.0001
    return resp


def test_bare_confirmation_reply_restores_deep_research_level():
    """Regression test replaying the live trace's shape: a bare 'Yes' reply
    confirming a prior deep-research offer must execute at research_level=
    "deep", not silently downgrade to "regular"."""
    request = TurnRequest(
        message="Yes",
        last_turn_route="clarify",
        last_turn_offered_deep_research=True,
    )
    with patch("app.services.agent.orchestrator.model_client.complete") as mock:
        mock.return_value = _mock_orchestrator_response(
            "research",
            "User confirmed with 'Yes' after prior prompt for deep research confirmation; proceed with deep research.",
        )
        decision = decide_with_options(
            request,
            available_routes=["direct", "clarify", "research", "document", "research_document"],
            available_tools=[],
        )
    assert decision.research_level == "deep"
    assert decision.requires_confirmation is False
    assert "confirming prior deep-research offer" in decision.reason.lower()


def test_carry_forward_does_not_fire_without_prior_deep_research_offer():
    """No regression: an ordinary clarify follow-up (not a deep-research
    offer) does not force research_level="deep"."""
    request = TurnRequest(
        message="Yes",
        last_turn_route="clarify",
        last_turn_offered_deep_research=False,
    )
    with patch("app.services.agent.orchestrator.model_client.complete") as mock:
        mock.return_value = _mock_orchestrator_response("research", "User confirmed; proceed.")
        decision = decide_with_options(
            request,
            available_routes=["direct", "clarify", "research", "document", "research_document"],
            available_tools=[],
        )
    assert decision.research_level != "deep"


def test_carry_forward_does_not_override_explicit_research_level():
    """No regression: if the client explicitly set research_level (e.g. the
    user clicked "Use regular research" rather than typing a free-text
    reply), that explicit choice is respected, not overridden back to deep."""
    request = TurnRequest(
        message="Yes",
        last_turn_route="clarify",
        last_turn_offered_deep_research=True,
        research_level="regular",
    )
    with patch("app.services.agent.orchestrator.model_client.complete") as mock:
        mock.return_value = _mock_orchestrator_response("research", "User confirmed; proceed.")
        decision = decide_with_options(
            request,
            available_routes=["direct", "clarify", "research", "document", "research_document"],
            available_tools=[],
        )
    assert decision.research_level == "regular"


def test_carry_forward_does_not_fire_for_a_long_substantive_new_message():
    """No regression: a genuinely new, substantive request (not a short
    confirmation reply) is not forced into "deep" just because the prior
    turn happened to offer deep research."""
    long_message = "Actually, let's research something completely different: " + " ".join(["word"] * 20)
    request = TurnRequest(
        message=long_message,
        last_turn_route="clarify",
        last_turn_offered_deep_research=True,
    )
    with patch("app.services.agent.orchestrator.model_client.complete") as mock:
        mock.return_value = _mock_orchestrator_response("research", "New research request.")
        decision = decide_with_options(
            request,
            available_routes=["direct", "clarify", "research", "document", "research_document"],
            available_tools=[],
        )
    assert decision.research_level != "deep"


def test_carry_forward_does_not_fire_when_last_turn_route_is_not_clarify():
    """No regression: last_turn_offered_deep_research alone isn't enough --
    the immediately preceding turn must actually have been the clarify that
    offered it."""
    request = TurnRequest(
        message="Yes",
        last_turn_route="research",
        last_turn_offered_deep_research=True,
    )
    with patch("app.services.agent.orchestrator.model_client.complete") as mock:
        mock.return_value = _mock_orchestrator_response("research", "User confirmed; proceed.")
        decision = decide_with_options(
            request,
            available_routes=["direct", "clarify", "research", "document", "research_document"],
            available_tools=[],
        )
    assert decision.research_level != "deep"
