"""PLAN_PROMPT query-hygiene and subject-completeness rules.

Root cause (confirmed via a live trace): a broad "which games are scheduled
for tomorrow and the day after" request produced a plan that (1) echoed "the
day after" verbatim into search queries, colliding with the movie *The Day
After Tomorrow*, and (2) bucketed the FIFA World Cup -- the single most
prominent active global event -- under a generic "international soccer"
worker that returned nothing useful. research_profiles.PLAN_PROMPT now has
two new rules addressing both.

This is a prompt-only change: there is no deterministic code in plan_research()
that rewrites a query or names a tournament, so a test that hands the mocked
LLM a fixed "good" response and asserts on it would be trivially true (it
would pass even if the prompt rules were deleted). Instead, each mock inspects
the actual system prompt plan_research() assembles and sent to
model_client.complete, and only returns the "good" (post-fix) response when
the relevant rule text is present -- otherwise it returns the shape the *old*
prompt would plausibly have produced. That makes each test genuinely fail
against a reverted PLAN_PROMPT, not just against broken plumbing.
"""
from __future__ import annotations

import json

from app.services.agent import model_client
from app.services.agent.model_client import ModelResponse
from app.services.agent.models import TurnRequest
from app.services.agent.research_planner import plan_research
from app.services.agent.research_profiles import PLAN_PROMPT

BAD_SCHEDULE_PLAN = json.dumps({
    "questions": ["What games are on the day after tomorrow?"],
    "search_queries": ["the day after schedule", "games the day after tomorrow"],
    "workers": [
        {"question": "What games are scheduled?", "query": "the day after games schedule", "rationale": "date-specific lookup", "max_results": 4},
        {"question": "What international soccer is on?", "query": "international soccer schedule", "rationale": "broad catch-all", "max_results": 4},
    ],
    "max_sources": 6, "min_evidence_items": 2, "judge_threshold": 0.7, "repair_iterations": 1,
})

GOOD_SCHEDULE_PLAN = json.dumps({
    "questions": ["What MLB, MLS, and FIFA World Cup games are scheduled on 2026-07-10 and 2026-07-11?"],
    "search_queries": ["MLB schedule 2026-07-10", "FIFA World Cup schedule 2026-07-11"],
    "workers": [
        {"question": "What MLB games are on 2026-07-10?", "query": "MLB schedule 2026-07-10", "rationale": "date-anchored domain query", "max_results": 4},
        {"question": "What FIFA World Cup matches are on 2026-07-11?", "query": "FIFA World Cup match schedule 2026-07-11", "rationale": "named tournament, not a generic bucket", "max_results": 4},
    ],
    "max_sources": 6, "min_evidence_items": 2, "judge_threshold": 0.7, "repair_iterations": 1,
})


def _conditional_mock(rule_fragment: str, *, good_json: str, bad_json: str):
    """Returns the post-fix plan only if the system prompt actually sent by
    plan_research() contains the given rule fragment; otherwise returns the
    pre-fix shape. Fails the test if PLAN_PROMPT regresses, since the real
    prompt is what's inspected, not a hardcoded assumption."""
    def _complete(messages, **kwargs):
        system_prompt = messages[0]["content"]
        text = good_json if rule_fragment in system_prompt else bad_json
        return ModelResponse(text=text, model_used="fake", latency_ms=1, cost_usd=0.0)
    return _complete


def test_plan_prompt_contains_query_hygiene_and_subject_completeness_rules():
    assert "Query hygiene" in PLAN_PROMPT
    assert "Subject completeness" in PLAN_PROMPT
    # Constraint check: the fix must not hardcode named tournaments in the prompt.
    assert "World Cup" in PLAN_PROMPT  # allowed only as an illustrative example
    assert "e.g." in PLAN_PROMPT.split("Subject completeness")[1].split("\n\n")[0]


def test_plan_research_sends_query_hygiene_rule_to_the_planning_llm(monkeypatch):
    captured = {}

    def _complete(messages, **kwargs):
        captured["system_prompt"] = messages[0]["content"]
        return ModelResponse(text=GOOD_SCHEDULE_PLAN, model_used="fake", latency_ms=1, cost_usd=0.0)

    monkeypatch.setattr(model_client, "complete", _complete)
    request = TurnRequest(message="Which games are scheduled for tomorrow and the day after?")

    plan_research(request)

    assert "Query hygiene" in captured["system_prompt"]
    assert "the day after" in captured["system_prompt"].lower()


def test_plan_research_sends_subject_completeness_rule_to_the_planning_llm(monkeypatch):
    captured = {}

    def _complete(messages, **kwargs):
        captured["system_prompt"] = messages[0]["content"]
        return ModelResponse(text=GOOD_SCHEDULE_PLAN, model_used="fake", latency_ms=1, cost_usd=0.0)

    monkeypatch.setattr(model_client, "complete", _complete)
    request = TurnRequest(message="What's happening in sports today?")

    plan_research(request)

    assert "Subject completeness" in captured["system_prompt"]


def test_plan_avoids_literal_idiom_collision_in_queries(monkeypatch):
    """Replays the trace's shape: a message containing the idiom "the day
    after" (which collides with the movie The Day After Tomorrow) must not
    have that literal phrase pass through unmodified into search_queries or
    workers[].query -- a resolved date or explicit subject term should appear
    instead. The mock only returns the resolved-date plan if PLAN_PROMPT's
    query-hygiene rule is actually present in the sent prompt."""
    monkeypatch.setattr(
        model_client,
        "complete",
        _conditional_mock("Query hygiene", good_json=GOOD_SCHEDULE_PLAN, bad_json=BAD_SCHEDULE_PLAN),
    )
    request = TurnRequest(message="Which games are scheduled for tomorrow and the day after?")

    plan = plan_research(request)

    all_queries = [*plan.search_queries, *(worker.query for worker in plan.workers)]
    assert all_queries, "plan must contain at least one query to check"
    for query in all_queries:
        assert "the day after" not in query.lower(), f"literal idiom leaked into query: {query!r}"
    # A resolved date or explicit subject term should appear instead.
    assert any(
        any(term in query.lower() for term in ("2026-07-10", "2026-07-11", "mlb", "world cup", "schedule"))
        for query in all_queries
    )


def test_plan_names_prominent_tournament_explicitly_not_generic_bucket(monkeypatch):
    """Replays the trace's shape: a broad "what's scheduled" request during an
    active major tournament must produce a worker naming that tournament
    explicitly, not a generic "international soccer"/"international sports"
    catch-all. The mock only returns the tournament-named plan if PLAN_PROMPT's
    subject-completeness rule is actually present in the sent prompt."""
    monkeypatch.setattr(
        model_client,
        "complete",
        _conditional_mock("Subject completeness", good_json=GOOD_SCHEDULE_PLAN, bad_json=BAD_SCHEDULE_PLAN),
    )
    request = TurnRequest(message="Which games are scheduled for tomorrow and the day after?")

    plan = plan_research(request)

    worker_queries = [worker.query.lower() for worker in plan.workers]
    assert not any(
        query in {"international soccer schedule", "international sports schedule"}
        for query in worker_queries
    ), "plan fell back to a generic international-soccer/sports bucket"
    assert any("world cup" in query for query in worker_queries), (
        "plan must name the prominent tournament explicitly rather than a generic bucket"
    )
