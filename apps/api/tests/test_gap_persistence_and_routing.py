"""Don't let an unresolved research gap get treated as a confirmed fact later.

Root cause (confirmed via a live trace): a research turn explicitly flagged an
unresolved gap (couldn't confirm World Cup fixtures for two dates), but the
next turn in the same conversation routed direct, confidence 1.0, and restated
the gap as a settled negative. Nothing persisted whether the prior turn had
gaps for a later turn to check.

Covers: Turn.had_unresolved_gaps persistence, persistence.
had_unresolved_gaps_for_conversation()'s read path (mirroring
last_turn_route_for_conversation), and the orchestrator's gap-rule backstop in
both the LLM-success path (_normalize_research_decision) and the LLM-failure
fallback path (heuristic_decide), including no-regression coverage.
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
from app.services.agent.orchestrator import decide, decide_with_options


def _session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


# ---------------------------------------------------------------------------
# Turn.had_unresolved_gaps persistence (mirrors test_turn_token_persistence.py)
# ---------------------------------------------------------------------------

def test_complete_turn_persists_had_unresolved_gaps_true(monkeypatch):
    Session = _session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    goal = Goal(user_id="u1", objective="ask", route="research")
    result = TurnResult(
        turn_id="turn_1",
        goal=goal,
        answer="Couldn't confirm fixtures for either date.",
        route="research",
        had_unresolved_gaps=True,
    )

    assert persistence.complete_turn(result) is True

    with Session() as db:
        row = db.get(Turn, "turn_1")
        assert row.had_unresolved_gaps is True


def test_complete_turn_defaults_had_unresolved_gaps_false(monkeypatch):
    Session = _session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    goal = Goal(user_id="u1", objective="ask", route="research")
    result = TurnResult(turn_id="turn_2", goal=goal, answer="Confirmed.", route="research")

    assert persistence.complete_turn(result) is True

    with Session() as db:
        row = db.get(Turn, "turn_2")
        assert row.had_unresolved_gaps is False


# ---------------------------------------------------------------------------
# had_unresolved_gaps_for_conversation() -- direct read-path tests, mirroring
# last_turn_route_for_conversation's own query shape (context_json.recent_turns)
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


def test_had_unresolved_gaps_for_conversation_true_when_last_turn_flagged(monkeypatch):
    Session = _session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    with Session() as db:
        conv_id = _conversation_with_recent_turns(
            db,
            user_id="u1",
            recent_turns=[{"turn_id": "t1", "route": "research", "had_unresolved_gaps": True}],
        )

    assert persistence.had_unresolved_gaps_for_conversation("u1", conv_id) is True


def test_had_unresolved_gaps_for_conversation_false_when_last_turn_clean(monkeypatch):
    Session = _session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    with Session() as db:
        conv_id = _conversation_with_recent_turns(
            db,
            user_id="u1",
            recent_turns=[
                {"turn_id": "t1", "route": "research", "had_unresolved_gaps": True},
                {"turn_id": "t2", "route": "research", "had_unresolved_gaps": False},
            ],
        )

    # Only the MOST RECENT turn's gap status matters, not any earlier one.
    assert persistence.had_unresolved_gaps_for_conversation("u1", conv_id) is False


def test_had_unresolved_gaps_for_conversation_false_when_no_turns(monkeypatch):
    Session = _session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    with Session() as db:
        conv_id = _conversation_with_recent_turns(db, user_id="u1", recent_turns=[])

    assert persistence.had_unresolved_gaps_for_conversation("u1", conv_id) is False


def test_had_unresolved_gaps_for_conversation_false_when_conversation_missing(monkeypatch):
    Session = _session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    assert persistence.had_unresolved_gaps_for_conversation("u1", "does-not-exist") is False


def test_had_unresolved_gaps_for_conversation_false_when_conversation_id_none(monkeypatch):
    Session = _session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    assert persistence.had_unresolved_gaps_for_conversation("u1", None) is False


def test_had_unresolved_gaps_for_conversation_round_trip_via_complete_turn(monkeypatch):
    """Full write path: complete_turn() -> async context snapshot -> context_json
    -> had_unresolved_gaps_for_conversation() reads it back correctly."""
    Session = _session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    conversation = persistence.ensure_conversation("u1", None, "World Cup schedule question")

    goal = Goal(user_id="u1", conversation_id=conversation.id, objective="which matches are tomorrow", route="research")
    result = TurnResult(
        turn_id="turn_3",
        goal=goal,
        answer="I could not confirm fixtures for either date from available sources.",
        route="research",
        had_unresolved_gaps=True,
    )

    assert persistence.complete_turn(result) is True
    persistence.wait_for_context_updates()

    assert persistence.had_unresolved_gaps_for_conversation("u1", conversation.id) is True


# ---------------------------------------------------------------------------
# Orchestrator gap-rule backstop -- LLM-success path (_normalize_research_decision)
# ---------------------------------------------------------------------------

def _mock_orchestrator_response(route: str, reason: str, confidence: float = 1.0) -> MagicMock:
    resp = MagicMock()
    resp.text = json.dumps({"route": route, "confidence": confidence, "reason": reason})
    resp.model_used = "test-model"
    resp.latency_ms = 100
    resp.cost_usd = 0.0001
    return resp


def test_gap_rule_overrides_confident_direct_when_same_subject_follow_up():
    """Regression test replaying the live trace's shape: a same-conversation,
    same-subject follow-up after a gapped turn must not be answered direct,
    even when the (mocked) LLM confidently restates the gap as a settled fact."""
    request = TurnRequest(
        message="So there are no matches then?",
        prior_turn_context="Fronei could not confirm World Cup fixtures for the requested dates.",
        last_turn_route="research",
        last_turn_had_gaps=True,
    )
    with patch("app.services.agent.orchestrator.model_client.complete") as mock:
        mock.return_value = _mock_orchestrator_response(
            "direct",
            "The current best verified information shows no confirmed matches per the latest official sources and prior research delivered.",
            confidence=1.0,
        )
        decision = decide_with_options(
            request,
            available_routes=["direct", "clarify", "research", "document", "research_document"],
            available_tools=[],
        )
    assert decision.route == "research"
    assert "unresolved research gap" in decision.reason.lower()


def test_gap_rule_does_not_override_when_no_gaps():
    """No regression: without last_turn_had_gaps, a confident direct decision
    is left alone."""
    request = TurnRequest(
        message="So there are no matches then?",
        last_turn_route="research",
        last_turn_had_gaps=False,
    )
    with patch("app.services.agent.orchestrator.model_client.complete") as mock:
        mock.return_value = _mock_orchestrator_response("direct", "Answered from general knowledge.", confidence=1.0)
        decision = decide_with_options(
            request,
            available_routes=["direct", "clarify", "research", "document", "research_document"],
            available_tools=[],
        )
    assert decision.route == "direct"


def test_gap_rule_does_not_override_when_topic_is_unrelated():
    """No regression: a long, self-contained, unrelated new question after a
    gapped turn is not forced into research just because gaps exist."""
    request = TurnRequest(
        message="Can you give me a detailed recipe for a really good homemade three cheese lasagna?",
        last_turn_route="research",
        last_turn_had_gaps=True,
    )
    with patch("app.services.agent.orchestrator.model_client.complete") as mock:
        mock.return_value = _mock_orchestrator_response("direct", "Here is a lasagna recipe.", confidence=1.0)
        decision = decide_with_options(
            request,
            available_routes=["direct", "clarify", "research", "document", "research_document"],
            available_tools=[],
        )
    assert decision.route == "direct"


# ---------------------------------------------------------------------------
# Orchestrator gap-rule backstop -- LLM-failure fallback path (heuristic_decide),
# exercised end-to-end through the public decide() entrypoint.
# ---------------------------------------------------------------------------

def test_decide_falls_back_to_research_when_gaps_and_same_subject_on_llm_failure():
    """End-to-end regression via orchestrator.decide(): if the orchestrator's
    LLM call fails, the heuristic fallback must not reintroduce the bug either."""
    request = TurnRequest(
        message="So no matches then?",
        prior_turn_context="Fronei could not confirm World Cup fixtures for the requested dates.",
        last_turn_route="research",
        last_turn_had_gaps=True,
    )
    with patch("app.services.agent.orchestrator.model_client.complete", side_effect=RuntimeError("llm down")):
        decision = decide(request)
    assert decision.route == "research"
    assert decision.source == "heuristic"


def test_decide_heuristic_fallback_stays_direct_when_no_gaps():
    """No regression: heuristic fallback still routes direct normally without
    last_turn_had_gaps."""
    request = TurnRequest(message="So no matches then?", last_turn_had_gaps=False)
    with patch("app.services.agent.orchestrator.model_client.complete", side_effect=RuntimeError("llm down")):
        decision = decide(request)
    assert decision.route == "direct"
