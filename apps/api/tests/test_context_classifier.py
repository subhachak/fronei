from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from app.services.agent import model_client
from app.services.agent.context_classifier import classify_context_need
from app.services.agent.models import TurnRequest


FIXTURE = Path(__file__).resolve().parents[1] / "evals" / "context_classifier_cases.json"


def _request(case: dict) -> TurnRequest:
    return TurnRequest(
        message=case["message"],
        conversation_context=case.get("conversation_context", ""),
        prior_turn_context=case.get("prior_turn_context", ""),
        attachment_context=case.get("attachment_context", ""),
        last_turn_route=case.get("last_turn_route"),
    )


def _classify_case(case: dict):
    if not case.get("requires_llm_assist"):
        return classify_context_need(_request(case))
    original = model_client.simple_completion
    model_client.simple_completion = lambda *_args, **_kwargs: SimpleNamespace(
        text=json.dumps(
            {
                "intent": case["expected_intent"],
                "needs_context": case.get("expected_needs_context", case.get("needs_context", False)),
                "reason": "test_fixture",
            }
        ),
        model_used="test-context-classifier",
        latency_ms=1,
        cost_usd=0.0,
    )
    try:
        return classify_context_need(_request(case))
    finally:
        model_client.simple_completion = original


def test_context_classifier_cases_match_expected_intent():
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    failures: list[str] = []
    for case in cases:
        decision = _classify_case(case)
        if decision.intent != case["expected_intent"]:
            failures.append(f"{case['id']}: expected intent {case['expected_intent']!r}, got {decision.intent!r}")
        if decision.needs_context != case["needs_context"]:
            failures.append(f"{case['id']}: expected needs_context {case['needs_context']!r}, got {decision.needs_context!r}")
        if decision.live_search != case["live_search"]:
            failures.append(f"{case['id']}: expected live_search {case['live_search']!r}, got {decision.live_search!r}")
    assert not failures, "\n".join(failures)


def test_context_classifier_fixture_has_minimum_category_coverage():
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    counts = Counter(case["category"] for case in cases)
    expected_categories = {
        "standalone",
        "same_conversation_followup",
        "vague_unresolved_followup",
        "same_workspace_recall",
        "explicit_cross_workspace_recall",
        "live_current_lookup",
        "attachment_context",
    }
    assert set(counts) == expected_categories
    too_small = {category: count for category, count in counts.items() if count < 15}
    assert not too_small, f"Expected at least 15 cases per category, got {too_small}"


def test_context_classifier_precision_recall_thresholds():
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    true_positive = false_positive = false_negative = 0
    vague_total = vague_recalled = 0
    cross_workspace_false_positive = 0

    for case in cases:
        decision = _classify_case(case)
        expected_needs_context = bool(case["needs_context"])
        if decision.needs_context and expected_needs_context:
            true_positive += 1
        elif decision.needs_context and not expected_needs_context:
            false_positive += 1
        elif not decision.needs_context and expected_needs_context:
            false_negative += 1

        if case["category"] == "vague_unresolved_followup":
            vague_total += 1
            if decision.needs_context:
                vague_recalled += 1

        if (
            decision.intent == "explicit_cross_workspace_recall"
            and case["category"] != "explicit_cross_workspace_recall"
        ):
            cross_workspace_false_positive += 1

    recall = true_positive / max(1, true_positive + false_negative)
    precision = true_positive / max(1, true_positive + false_positive)
    vague_recall = vague_recalled / max(1, vague_total)

    assert recall >= 0.95
    assert precision >= 0.85
    assert vague_recall >= 0.95
    assert cross_workspace_false_positive == 0


def test_llm_assist_upgrades_standalone_when_prior_context_exists(monkeypatch):
    calls: list[dict] = []

    def fake_simple_completion(_system, _user, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            text=json.dumps({
                "intent": "same_workspace_recall",
                "needs_context": True,
                "reason": "implicit project constraint question",
            }),
            model_used="test-context-classifier",
            latency_ms=1,
            cost_usd=0.0,
        )

    monkeypatch.setattr(model_client, "simple_completion", fake_simple_completion)
    decision = classify_context_need(TurnRequest(
        message="Are there any constraints I should be aware of?",
        prior_turn_context="User: Let's start planning the migration.\nAssistant: Sure, what's the scope?",
    ))

    assert calls
    assert calls[0]["role"] == "context_classifier"
    assert calls[0]["timeout_s"] == 12
    assert decision.intent == "same_workspace_recall"
    assert decision.needs_context is True
    assert decision.reason.startswith("llm_assist_upgrade:")


def test_llm_assist_failure_falls_back_to_standalone(monkeypatch):
    def fail_simple_completion(*_args, **_kwargs):
        raise RuntimeError("provider timeout")

    monkeypatch.setattr(model_client, "simple_completion", fail_simple_completion)
    decision = classify_context_need(TurnRequest(
        message="Are there any constraints I should be aware of?",
        prior_turn_context="User: Let's start planning the migration.\nAssistant: Sure, what's the scope?",
    ))

    assert decision.intent == "standalone"
    assert decision.needs_context is False
    assert decision.reason == "deterministic_rule: standalone"
