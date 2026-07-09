from __future__ import annotations

from types import SimpleNamespace

from app.services.agent import model_client
from app.services.agent.context_contracts import (
    LAYER_L1,
    LAYER_L2,
    SCOPE_CONVERSATION,
    SCOPE_WORKSPACE,
    SOURCE_PRIOR_TURN,
    SOURCE_SUMMARY,
    ContextItem,
)
from app.services.agent.fast_path import answer_direct_fast
from app.services.agent.models import TurnRequest
from app.services.agent.research_utils import temporal_context


def test_answer_direct_fast_without_items_prepends_current_date(monkeypatch):
    captured = {}

    def fake_simple_completion(system, user, **kwargs):
        captured["user"] = user
        return SimpleNamespace(text="ok", model_used="fake", latency_ms=1, cost_usd=0.0)

    monkeypatch.setattr(model_client, "simple_completion", fake_simple_completion)

    answer_direct_fast(TurnRequest(message="Explain caching."))

    current_date = temporal_context()["current_date"]
    assert captured["user"] == f"Current date: {current_date}\n\nExplain caching."


def test_answer_direct_fast_prepends_l1_context(monkeypatch):
    captured = {}

    def fake_simple_completion(system, user, **kwargs):
        captured["user"] = user
        return SimpleNamespace(text="ok", model_used="fake", latency_ms=1, cost_usd=0.0)

    monkeypatch.setattr(model_client, "simple_completion", fake_simple_completion)
    item = ContextItem(
        layer=LAYER_L1,
        scope=SCOPE_CONVERSATION,
        source_type=SOURCE_PRIOR_TURN,
        content="User asked about API gateway limits.",
    )

    answer_direct_fast(TurnRequest(message="Make that shorter."), context_items=[item])

    current_date = temporal_context()["current_date"]
    assert captured["user"] == (
        f"Current date: {current_date}\n\n"
        "[L1 · conversation · prior_turn]\nUser asked about API gateway limits.\n\nMake that shorter."
    )


def test_answer_direct_fast_prepends_l2_context(monkeypatch):
    captured = {}

    def fake_simple_completion(system, user, **kwargs):
        captured["user"] = user
        return SimpleNamespace(text="ok", model_used="fake", latency_ms=1, cost_usd=0.0)

    monkeypatch.setattr(model_client, "simple_completion", fake_simple_completion)
    item = ContextItem(
        layer=LAYER_L2,
        scope=SCOPE_WORKSPACE,
        source_type=SOURCE_SUMMARY,
        content="Prior session decided to use pgvector for summaries.",
        provenance="L2:summary:conv_conv_1",
    )

    answer_direct_fast(TurnRequest(message="Continue the plan."), context_items=[item])

    current_date = temporal_context()["current_date"]
    assert captured["user"] == (
        f"Current date: {current_date}\n\n"
        "[L2 · workspace · summary | L2:summary:conv_conv_1]\n"
        "Prior session decided to use pgvector for summaries.\n\nContinue the plan."
    )
