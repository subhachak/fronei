"""Regression tests for orchestrator routing fixes.

Fix 2: clarify over-trigger with prior context.
Fix 4: freshness false-positive on timeless facts.
"""
import sys
from pathlib import Path

_API_ROOT = Path(__file__).resolve().parents[1]
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))


# ── Fix 4: timeless subject exclusion ────────────────────────────────────────

def test_timeless_subject_pi_not_flagged_as_freshness():
    from app.services.agent.routing_policy import _is_timeless_subject, evaluate_routing_signals
    assert _is_timeless_subject("What is the latest value of pi to 5 decimal places?") is True
    # The currentness signal group must be stripped for this query
    result = evaluate_routing_signals("What is the latest value of pi to 5 decimal places?")
    freshness_matches = [m for m in result.matched_signals if m.signal_group == "currentness"]
    assert not freshness_matches, (
        f"'latest value of pi' must not match the currentness group — got {freshness_matches}"
    )


def test_timeless_subject_speed_of_light():
    from app.services.agent.routing_policy import _is_timeless_subject
    assert _is_timeless_subject("What is the current speed of light?") is True


def test_mutable_subject_not_excluded():
    """iOS release features IS a mutable real-world fact — currentness signal must fire."""
    from app.services.agent.routing_policy import _is_timeless_subject, evaluate_routing_signals
    assert _is_timeless_subject("What are the newest features in the latest iOS release?") is False
    result = evaluate_routing_signals("What are the newest features in the latest iOS release?")
    freshness_matches = [m for m in result.matched_signals if m.signal_group == "currentness"]
    assert freshness_matches, "iOS release query must still match the currentness signal group"


def test_boiling_point_not_flagged():
    from app.services.agent.routing_policy import _is_timeless_subject
    assert _is_timeless_subject("What is the boiling point of water?") is True


def test_federal_funds_rate_not_excluded():
    """Federal funds rate changes at FOMC meetings — NOT timeless, must trigger research."""
    from app.services.agent.routing_policy import _is_timeless_subject
    assert _is_timeless_subject("What is the current federal funds rate?") is False


# ── Fix 2: clarify over-trigger with prior context ───────────────────────────

def test_referent_resolves_from_conversation_context_string():
    from app.services.agent.orchestrator import _referent_resolves_from_context
    ctx = "User: Compare Salesforce, HubSpot, and Dynamics 365 for enterprise B2B.\nAssistant: Salesforce leads on customization...\nUser: Pricing and integration ecosystem.\nAssistant: Salesforce Sales Cloud starts at $25/user/month..."
    assert _referent_resolves_from_context("The Salesforce one.", ctx) is True


def test_no_resolution_with_empty_context_string():
    from app.services.agent.orchestrator import _referent_resolves_from_context
    assert _referent_resolves_from_context("The Salesforce one.", "") is False
    assert _referent_resolves_from_context("The Salesforce one.", "   ") is False


def test_thin_context_string_not_resolved():
    from app.services.agent.orchestrator import _referent_resolves_from_context
    assert _referent_resolves_from_context("Can you go deeper on that?", "User: Yes.") is False


def test_format_prior_context_formats_turns():
    from app.routers.evals import _format_prior_context
    turns = [
        {"role": "user", "content": "Compare Salesforce and HubSpot."},
        {"role": "assistant", "content": "Salesforce leads on customization."},
    ]
    result = _format_prior_context(turns)
    assert "User: Compare Salesforce and HubSpot." in result
    assert "Assistant: Salesforce leads on customization." in result
    assert len(result) > 50


def test_format_prior_context_empty():
    from app.routers.evals import _format_prior_context
    assert _format_prior_context(None) == ""
    assert _format_prior_context([]) == ""
