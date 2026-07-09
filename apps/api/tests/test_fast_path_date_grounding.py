from __future__ import annotations

import json
from types import SimpleNamespace

from app.services.agent import model_client
from app.services.agent.fast_path import (
    DIRECT_FAST_PROMPT,
    WEB_FAST_PROMPT,
    answer_web_fast,
    decide_fast_path,
)
from app.services.agent.models import Source, TurnRequest
from app.services.agent.research_utils import temporal_context
from app.services.agent.routing_policy import evaluate_routing_signals


def test_router_payload_includes_current_date(monkeypatch):
    captured = {}

    def fake_complete(messages, *, preferred_model=None, role=None, quality_mode="standard", timeout_s=30, max_tokens=1200, **_kwargs):
        captured["payload"] = json.loads(messages[-1]["content"])
        return SimpleNamespace(
            text=json.dumps(
                {
                    "path": "web_fast",
                    "confidence": 0.9,
                    "reason": "Needs a quick current lookup.",
                    "web_query": f"World Cup matches {temporal_context()['current_date']}",
                }
            ),
            model_used="fake-fast-router",
            latency_ms=2,
            cost_usd=0.001,
        )

    monkeypatch.setattr(model_client, "complete", fake_complete)

    decide_fast_path(TurnRequest(message="How many World Cup matches are there tomorrow?"))

    assert captured["payload"]["current_date"] == temporal_context()["current_date"]


def test_router_payload_uses_request_user_timezone(monkeypatch):
    captured = {}

    def fake_complete(messages, *, preferred_model=None, role=None, quality_mode="standard", timeout_s=30, max_tokens=1200, **_kwargs):
        captured["payload"] = json.loads(messages[-1]["content"])
        return SimpleNamespace(
            text=json.dumps({"path": "direct_fast", "confidence": 0.9, "reason": "ok"}),
            model_used="fake-fast-router",
            latency_ms=2,
            cost_usd=0.001,
        )

    monkeypatch.setattr(model_client, "complete", fake_complete)

    decide_fast_path(TurnRequest(message="Explain caching.", user_timezone="Asia/Tokyo"))

    assert captured["payload"]["current_date"] == temporal_context("Asia/Tokyo")["current_date"]


def test_router_preserves_explicit_date_resolved_by_model(monkeypatch):
    resolved_query = "World Cup matches on July 10, 2026 schedule"

    def fake_complete(messages, *, preferred_model=None, role=None, quality_mode="standard", timeout_s=30, max_tokens=1200, **_kwargs):
        return SimpleNamespace(
            text=json.dumps(
                {
                    "path": "web_fast",
                    "confidence": 0.9,
                    "reason": "Needs a quick current lookup.",
                    "web_query": resolved_query,
                }
            ),
            model_used="fake-fast-router",
            latency_ms=2,
            cost_usd=0.001,
        )

    monkeypatch.setattr(model_client, "complete", fake_complete)

    decision = decide_fast_path(TurnRequest(message="What games are on this weekend?"))

    # "how many"/enumeration terms aren't present here, so this stays web_fast
    # and the router's already-resolved explicit date must pass through untouched.
    assert decision.path == "web_fast"
    assert decision.web_query == resolved_query
    assert "this weekend" not in decision.web_query


def test_answer_web_fast_payload_includes_current_date(monkeypatch):
    captured = {}

    def fake_simple_completion(system, user, **kwargs):
        captured["system"] = system
        captured["user"] = json.loads(user)
        return SimpleNamespace(text="ok", model_used="fake", latency_ms=1, cost_usd=0.0)

    monkeypatch.setattr(model_client, "simple_completion", fake_simple_completion)

    answer_web_fast(
        TurnRequest(message="How many World Cup matches are there tomorrow?"),
        web_query="World Cup matches July 10 2026",
        sources=[Source(title="Schedule", url="https://example.com/schedule", snippet="Overview")],
        extracted_sources=[],
    )

    assert captured["user"]["current_date"] == temporal_context()["current_date"]
    assert "current_datetime_iso" in captured["user"]


def test_web_fast_prompt_forbids_unverified_counts():
    assert "Never state a specific count" in WEB_FAST_PROMPT
    assert "could not be confirmed from the sources retrieved" in WEB_FAST_PROMPT


def test_enumeration_query_routing_signal_suggests_agentic():
    decision = evaluate_routing_signals("How many World Cup matches are there tomorrow?")

    assert decision.suggested_route == "agentic"
    assert "enumeration_count_query" in decision.matched_groups


def test_enumeration_query_list_phrasing_suggests_agentic():
    decision = evaluate_routing_signals("Give me a list of flights delayed today.")

    assert decision.suggested_route == "agentic"
    assert "enumeration_count_query" in decision.matched_groups


def test_fast_router_overrides_web_fast_for_enumeration_query(monkeypatch):
    def fake_complete(messages, *, preferred_model=None, role=None, quality_mode="standard", timeout_s=30, max_tokens=1200, **_kwargs):
        assert role == "fast_router"
        return SimpleNamespace(
            text=json.dumps(
                {
                    "path": "web_fast",
                    "confidence": 0.88,
                    "reason": "A quick lookup seems enough.",
                    "web_query": "World Cup matches tomorrow",
                }
            ),
            model_used="fake-fast-router",
            latency_ms=2,
            cost_usd=0.001,
        )

    monkeypatch.setattr(model_client, "complete", fake_complete)

    decision = decide_fast_path(
        TurnRequest(message="How many World Cup matches are there tomorrow?")
    )

    assert decision.path == "agentic"
    assert "enumeration_count_query" in decision.matched_signal_groups


def test_direct_fast_prompt_hedges_low_confidence_facts():
    assert "not confident" in DIRECT_FAST_PROMPT
    assert "I'm not certain, but" in DIRECT_FAST_PROMPT


def test_answer_direct_fast_hedges_on_low_confidence_factual_question(monkeypatch):
    from app.services.agent.fast_path import answer_direct_fast

    captured = {}

    def fake_simple_completion(system, user, **kwargs):
        captured["system"] = system
        # Simulate a well-behaved model following the hedging rule for a
        # question it cannot answer with a confident specific fact.
        return SimpleNamespace(
            text="I'm not certain, but the current record holder is likely still X as of my last update.",
            model_used="fake-model",
            latency_ms=1,
            cost_usd=0.0,
        )

    monkeypatch.setattr(model_client, "simple_completion", fake_simple_completion)

    response = answer_direct_fast(
        TurnRequest(message="What is the exact current world record for X as of today?")
    )

    assert "not confident" in captured["system"]
    assert "not certain" in response.text.lower()
