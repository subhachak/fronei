"""Fixes for the query-hygiene / subject-completeness gaps found while tracing
dispatch_search / search_worker.

Root cause (confirmed by code trace, not just the prompt-level report from the
prior task): the coverage-contract planning path (plan_from_contract() ->
_targeted_query()) builds search queries with NO LLM call at all -- it's
deterministic string concatenation, and its general-case fallback branch
echoed the user's raw message verbatim into the query. That's a different code
path from plan_research() (the one PLAN_PROMPT's query-hygiene rule covers),
and it's the one that actually produces the many-worker fan-out shape from the
live trace (contract-driven plans commonly produce ~9 workers; plan_research()'s
own JSON schema caps at 2-4).

Covers:
  - research_utils.resolve_relative_date_phrases() -- the new deterministic
    date-idiom resolver
  - research_planner._targeted_query()'s default branch no longer echoing the
    raw message verbatim
  - research_contracts.COVERAGE_CONTRACT_PROMPT's new subject-completeness rule
  - research_planner.flag_untargeted_worker_queries() -- the new generic
    self-check, and that the LangGraph "plan" node actually calls it regardless
    of which planning path produced the plan
"""
from __future__ import annotations

import json
import logging

from app.services.agent import model_client
from app.services.agent.model_client import ModelResponse
from app.services.agent.models import TurnRequest
from app.services.agent.research_contracts import COVERAGE_CONTRACT_PROMPT, generate_coverage_contract
from app.services.agent.research_models import CoverageCell, CoverageContract, ResearchBrief, ResearchPlan, SearchWorkerPlan
from app.services.agent.research_planner import (
    _targeted_query,
    flag_untargeted_worker_queries,
    plan_from_contract,
)
from app.services.agent.research_utils import resolve_relative_date_phrases

TZ = "America/New_York"


def _plan_with_workers(*, workers: list[SearchWorkerPlan]) -> ResearchPlan:
    return ResearchPlan(workers=workers)


# ---------------------------------------------------------------------------
# resolve_relative_date_phrases()
# ---------------------------------------------------------------------------

def test_resolves_tomorrow_and_the_day_after_to_two_distinct_dates():
    """Replays the trace's exact phrasing: "tomorrow" and "the day after" must
    resolve to two different, sequential dates, not collapse to the same one."""
    resolved = resolve_relative_date_phrases(
        "Which games are scheduled for tomorrow and the day after?", TZ
    )
    assert "the day after" not in resolved.lower()
    assert "tomorrow" not in resolved.lower()

    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo(TZ)).date()
    assert (today + timedelta(days=1)).isoformat() in resolved
    assert (today + timedelta(days=2)).isoformat() in resolved
    # Must be two distinct dates, not the same one repeated.
    assert (today + timedelta(days=1)).isoformat() != (today + timedelta(days=2)).isoformat()


def test_resolves_day_after_tomorrow_as_a_single_compound_phrase():
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    resolved = resolve_relative_date_phrases("What about the day after tomorrow?", TZ)
    expected = (datetime.now(ZoneInfo(TZ)).date() + timedelta(days=2)).isoformat()
    assert resolved.strip() == f"What about {expected}?"


def test_resolves_today():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    resolved = resolve_relative_date_phrases("Today is a good day", TZ)
    expected = datetime.now(ZoneInfo(TZ)).date().isoformat()
    assert resolved.startswith(expected)


def test_leaves_text_without_relative_dates_unchanged():
    text = "FIFA World Cup match schedule and results"
    assert resolve_relative_date_phrases(text, TZ) == text


def test_empty_text_returns_empty():
    assert resolve_relative_date_phrases("", TZ) == ""


# ---------------------------------------------------------------------------
# _targeted_query()'s default branch
# ---------------------------------------------------------------------------

def test_targeted_query_default_branch_does_not_echo_literal_idiom():
    query = _targeted_query(
        "international soccer",
        ["schedule"],
        "Which games are scheduled for tomorrow and the day after?",
        tz=TZ,
    )
    assert "the day after" not in query.lower()
    assert "international soccer" in query.lower()


def test_targeted_query_still_includes_resolved_date():
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    query = _targeted_query("MLB", ["schedule"], "What games are on tomorrow?", tz=TZ)
    expected = (datetime.now(ZoneInfo(TZ)).date() + timedelta(days=1)).isoformat()
    assert expected in query


def test_targeted_query_without_relative_dates_is_unaffected():
    query = _targeted_query("FIFA World Cup", ["schedule"], "Which teams have qualified so far?", tz=TZ)
    assert "FIFA World Cup" in query
    assert "Which teams have qualified so far?" in query


# ---------------------------------------------------------------------------
# plan_from_contract() end-to-end -- the actual code path from the live trace
# ---------------------------------------------------------------------------

def test_plan_from_contract_avoids_idiom_collision_in_worker_queries():
    """Regression test replaying the trace's shape through the real contract-
    driven planning path (no LLM call in this path at all -- fully deterministic)."""
    request = TurnRequest(
        message="Which games are scheduled for tomorrow and the day after?",
        user_timezone=TZ,
    )
    contract = CoverageContract(
        subjects=["international soccer", "MLB"],
        dimensions=["schedule"],
        cells=[
            CoverageCell(subject="international soccer", dimension="schedule"),
            CoverageCell(subject="MLB", dimension="schedule"),
        ],
        source="test",
    )

    plan = plan_from_contract(request, contract)

    for worker in plan.workers:
        assert "the day after" not in worker.query.lower(), f"literal idiom leaked into: {worker.query!r}"


# ---------------------------------------------------------------------------
# COVERAGE_CONTRACT_PROMPT's subject-completeness rule
# ---------------------------------------------------------------------------

def test_coverage_contract_prompt_contains_subject_completeness_rule():
    assert "Subject completeness" in COVERAGE_CONTRACT_PROMPT


def test_generate_coverage_contract_sends_subject_completeness_rule_to_llm(monkeypatch):
    """Mock inspects the actual system prompt generate_coverage_contract() sends,
    only returning the tournament-named subject list if the rule is present --
    meaningful the same way the PLAN_PROMPT tests are (fails against a reverted
    COVERAGE_CONTRACT_PROMPT, not just against broken plumbing)."""
    good = json.dumps({
        "subjects": ["FIFA World Cup", "MLB", "MLS"],
        "dimensions": ["schedule"],
        "cells": [{"dimension": "schedule", "subject": s, "required": True} for s in ("FIFA World Cup", "MLB", "MLS")],
    })
    bad = json.dumps({
        "subjects": ["international soccer", "MLB", "MLS"],
        "dimensions": ["schedule"],
        "cells": [{"dimension": "schedule", "subject": s, "required": True} for s in ("international soccer", "MLB", "MLS")],
    })

    def _complete(messages, **kwargs):
        system_prompt = messages[0]["content"]
        text = good if "Subject completeness" in system_prompt else bad
        return ModelResponse(text=text, model_used="fake", latency_ms=1, cost_usd=0.0)

    monkeypatch.setattr(model_client, "complete", _complete)
    request = TurnRequest(message="What's happening in sports today?")
    brief = ResearchBrief(objective=request.message, research_profile="general")

    contract = generate_coverage_contract(request, brief, named_subjects=[])

    assert "FIFA World Cup" in contract.subjects
    assert "international soccer" not in contract.subjects


# ---------------------------------------------------------------------------
# flag_untargeted_worker_queries() -- generic self-check
# ---------------------------------------------------------------------------

def test_flags_worker_whose_query_is_the_raw_message_verbatim(caplog):
    request = TurnRequest(message="Which games are scheduled for tomorrow and the day after?")
    plan = _plan_with_workers(workers=[
        SearchWorkerPlan(question="q", query=request.message, rationale="", max_results=4),
    ])
    with caplog.at_level(logging.DEBUG, logger="app.services.agent.research_planner"):
        flag_untargeted_worker_queries(plan, request)
    assert any("research_plan_untargeted_worker_query" in record.message for record in caplog.records)


def test_does_not_flag_a_properly_targeted_worker_query(caplog):
    request = TurnRequest(message="Which games are scheduled for tomorrow and the day after?")
    plan = _plan_with_workers(workers=[
        SearchWorkerPlan(question="q", query="MLB schedule 2026-07-10", rationale="", max_results=4),
    ])
    with caplog.at_level(logging.DEBUG, logger="app.services.agent.research_planner"):
        flag_untargeted_worker_queries(plan, request)
    assert not any("research_plan_untargeted_worker_query" in record.message for record in caplog.records)


def test_plan_node_calls_the_self_check_regardless_of_planning_path(monkeypatch):
    """Confirms the LangGraph "plan" node wires flag_untargeted_worker_queries()
    in, so it runs for both plan_research() and plan_from_contract() output --
    the two functions don't share any other common post-processing step.
    The node does `from app.services.agent.research_planner import
    flag_untargeted_worker_queries` *inside* the function body, so the only
    thing that needs patching is the source attribute, not anything on the
    nodes module itself."""
    from app.services.agent.langgraph_runtime import nodes as nodes_module

    called = {}

    def _fake_flag(plan, request):
        called["invoked"] = True

    monkeypatch.setattr(
        "app.services.agent.research_planner.flag_untargeted_worker_queries",
        _fake_flag,
    )
    monkeypatch.setattr(
        "app.services.agent.research_planner.plan_from_contract",
        lambda request, contract: ResearchPlan(workers=[]),
    )

    state = {"visited_nodes": [], "artifacts": {}, "contract": CoverageContract(source="test")}
    request = TurnRequest(message="Which games are scheduled for tomorrow and the day after?")
    nodes_module.plan(state, run_id="r1", request=request, tools=None, progress=None)

    assert called.get("invoked") is True
