"""Enumeration/count/schedule queries must escalate to research_level "deep".

Root cause (confirmed via a live trace): routing_policy's enumeration_count_query
signal group already escalates suggested_route to "agentic", but that never fed
into orchestrator.choose_research_level(), so it silently fell back to "regular"
-- not enough search breadth for same-day multi-event lookups (e.g. sports
schedules). See orchestrator.py's choose_research_level() for the fix.
"""
from __future__ import annotations

from app.services.agent import routing_policy
from app.services.agent.models import TurnRequest
from app.services.agent.orchestrator import choose_research_level, decide


def test_enumeration_count_query_escalates_to_deep():
    request = TurnRequest(
        message="How many World Cup matches are there tomorrow and the day after?",
    )
    level = choose_research_level(request, "research")
    assert level == "deep"


def test_enumeration_count_query_beats_easy_terms():
    """An enumeration query containing an easy_terms word ("current", "what is")
    must still resolve to "deep", not get pulled back down to "easy"."""
    request = TurnRequest(
        message="What is the current schedule — how many World Cup matches are there tomorrow?",
    )
    level = choose_research_level(request, "research")
    assert level == "deep"


def test_which_matches_phrasing_also_escalates_to_deep():
    request = TurnRequest(message="Which matches are scheduled for tomorrow and the day after?")
    level = choose_research_level(request, "research")
    assert level == "deep"


def test_non_matching_easy_query_still_classifies_easy():
    """No regression: a short, plainly-easy query with no escalating signal
    still resolves to "easy", same as before this change."""
    request = TurnRequest(message="What is the current time in Tokyo?")
    level = choose_research_level(request, "research")
    assert level == "easy"


def test_non_matching_plain_query_still_classifies_regular():
    """No regression: a query with no escalating signal and no easy_terms match
    still resolves to "regular", same as before this change."""
    request = TurnRequest(message="Summarize the history of the Eiffel Tower.")
    level = choose_research_level(request, "research")
    assert level == "regular"


def test_time_sensitive_factual_group_not_forced_to_deep():
    """The "how long is" / "backlog" signal group (time_sensitive_factual, directly
    above enumeration_count_query in routing_policy.py) was checked for the same
    gap and does not have it: its suggested_route is "web_fast", not "agentic" --
    it was deliberately designed to stay on the lightweight path. Confirms it is
    not swept up into the new "deep" escalation."""
    request = TurnRequest(message="What's the current backlog and wait time for passport renewal?")
    # Sanity: this message does match the time_sensitive_factual signal group.
    signal_decision = routing_policy.evaluate_routing_signals(request.message)
    assert any(m.signal_group == "time_sensitive_factual" for m in signal_decision.matched_signals)
    assert not any(m.signal_group == "enumeration_count_query" for m in signal_decision.matched_signals)

    level = choose_research_level(request, "research")
    assert level != "deep"


def test_signal_decision_can_be_threaded_through_without_recomputing():
    """choose_research_level accepts a pre-computed signal_decision (as
    heuristic_decide and _normalize_research_decision now pass through) instead
    of always recomputing it via a fresh evaluate_routing_signals() call."""
    request = TurnRequest(message="How many World Cup matches are there tomorrow?")
    signal_decision = routing_policy.evaluate_routing_signals(request.message)
    level = choose_research_level(request, "research", signal_decision)
    assert level == "deep"


def test_world_cup_schedule_query_resolves_deep_end_to_end_via_decide():
    """Regression test replaying the live trace's query shape end-to-end through
    orchestrator.decide(). Uses force_route to bypass the LLM call -- decide()
    still runs the query through _normalize_research_decision, which is what
    actually resolves research_level."""
    request = TurnRequest(
        message="Which matches are scheduled for tomorrow and the day after in the World Cup?",
        force_route="research",
    )
    decision = decide(request)
    assert decision.route == "research"
    assert decision.research_level == "deep"
