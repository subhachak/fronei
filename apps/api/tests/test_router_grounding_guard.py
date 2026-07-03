"""Tests for the router grounding guard in fast_path.py.

The guard prevents the router from routing to direct_fast when it fabricates
a context claim ("answered from context", "context contains", etc.) but there
is no prior completed turn (last_turn_route is None).

Grounding is determined by last_turn_route, NOT by conversation_context length,
because attachment-only context on a first turn populates conversation_context
without there being any prior conversation history.
"""
from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

from app.services.agent.fast_path import (
    decide_fast_path,
    _CONTEXT_CLAIM_PHRASES,
)
from app.services.agent.models import TurnRequest
from app.services.agent.orchestrator import decide_with_options


def _request(
    message: str,
    *,
    conversation_context: str = "",
    last_turn_route: str | None = None,
) -> TurnRequest:
    return TurnRequest(
        message=message,
        conversation_context=conversation_context,
        last_turn_route=last_turn_route,
    )


def _mock_llm(path: str, reason: str, confidence: float = 0.85) -> MagicMock:
    resp = MagicMock()
    resp.text = json.dumps({"path": path, "confidence": confidence, "reason": reason})
    resp.model_used = "test-model"
    resp.latency_ms = 100
    resp.cost_usd = 0.0001
    return resp


def _mock_orchestrator(route: str, reason: str, confidence: float = 0.85) -> MagicMock:
    resp = MagicMock()
    resp.text = json.dumps({"route": route, "confidence": confidence, "reason": reason})
    resp.model_used = "test-model"
    resp.latency_ms = 100
    resp.cost_usd = 0.0001
    return resp


class TestGroundingGuardFires:
    def test_fabricated_context_claim_no_prior_turn(self):
        req = _request("What did you find earlier?", last_turn_route=None)
        with patch("app.services.agent.fast_path.model_client.complete") as mock:
            mock.return_value = _mock_llm(
                "direct_fast", "Answered from conversation context — prior research visible."
            )
            decision = decide_fast_path(req)
        assert decision.path == "agentic", f"Guard must fire: {decision.reason}"
        assert "fabricated" in decision.reason.lower() or "no prior" in decision.reason.lower()

    def test_context_provides_claim_no_prior_turn(self):
        req = _request("Can you summarize what we discussed?", last_turn_route=None)
        with patch("app.services.agent.fast_path.model_client.complete") as mock:
            mock.return_value = _mock_llm(
                "direct_fast", "Context provides the prior conversation for summarization."
            )
            decision = decide_fast_path(req)
        assert decision.path == "agentic"

    def test_already_answered_claim_no_prior_turn(self):
        req = _request("Yes, what you said before?", last_turn_route=None)
        with patch("app.services.agent.fast_path.model_client.complete") as mock:
            mock.return_value = _mock_llm(
                "direct_fast", "Already answered this in the context above."
            )
            decision = decide_fast_path(req)
        assert decision.path == "agentic"

    def test_attachment_only_context_does_not_prevent_guard(self):
        # Attachment text populates conversation_context but last_turn_route is None —
        # this is a first turn with a file attachment, not a real prior conversation.
        req = _request(
            "What did you find earlier?",
            conversation_context="Attached file context:\nQ4 financials spreadsheet...",
            last_turn_route=None,
        )
        with patch("app.services.agent.fast_path.model_client.complete") as mock:
            mock.return_value = _mock_llm(
                "direct_fast", "Answered from conversation context above."
            )
            decision = decide_fast_path(req)
        assert decision.path == "agentic", "Attachment-only context must not suppress the grounding guard"

    def test_orchestrator_direct_context_claim_fails_closed_to_clarify(self):
        req = _request("What did we decide?", last_turn_route=None)
        with patch("app.services.agent.orchestrator.model_client.complete") as mock:
            mock.return_value = _mock_orchestrator(
                "direct", "Conversation context already contains the decision."
            )
            decision = decide_with_options(
                req,
                available_routes=["direct", "clarify", "research", "document", "research_document"],
                available_tools=[],
            )
        assert decision.route == "clarify"
        assert "fabricated" in decision.reason.lower() or "no prior" in decision.reason.lower()

    def test_orchestrator_non_direct_context_claim_reason_is_sanitized(self):
        req = _request("Research what we discussed.", last_turn_route=None)
        with patch("app.services.agent.orchestrator.model_client.complete") as mock:
            mock.return_value = _mock_orchestrator(
                "research", "Based on context, this needs source-grounded research."
            )
            decision = decide_with_options(
                req,
                available_routes=["direct", "clarify", "research", "document", "research_document"],
                available_tools=[],
            )
        assert decision.route == "research"
        assert "based on context" not in decision.reason.lower()


class TestGroundingGuardDoesNotFire:
    def test_legitimate_direct_fast_no_context_claim(self):
        req = _request("What is the capital of France?", last_turn_route=None)
        with patch("app.services.agent.fast_path.model_client.complete") as mock:
            mock.return_value = _mock_llm(
                "direct_fast", "Simple factual question answerable from general knowledge."
            )
            decision = decide_fast_path(req)
        assert decision.path == "direct_fast", f"Guard must not fire for clean reason: {decision.reason}"

    def test_prior_turn_present_context_claim_is_fine(self):
        req = _request(
            "What did you find earlier?",
            conversation_context="User: tell me about Python\nAssistant: Python is a language...",
            last_turn_route="direct",
        )
        with patch("app.services.agent.fast_path.model_client.complete") as mock:
            mock.return_value = _mock_llm(
                "direct_fast", "Answered from conversation context — prior research visible."
            )
            decision = decide_fast_path(req)
        assert decision.path == "direct_fast", f"Guard must not fire with real prior turn: {decision.reason}"

    def test_claim_phrases_tuple_not_empty(self):
        assert len(_CONTEXT_CLAIM_PHRASES) > 0
        assert all(isinstance(p, str) for p in _CONTEXT_CLAIM_PHRASES)
