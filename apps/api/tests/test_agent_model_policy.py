"""Coverage for the DB-backed Fronei model policy."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.db.models as db_models
from app.db.models import Base
from app.services.agent import model_client, model_policy


@pytest.fixture
def policy_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(db_models, "SessionLocal", Session)
    model_policy.invalidate_cache()
    yield Session
    model_policy.invalidate_cache()


def test_defaults_with_no_overrides(policy_db):
    policy = model_policy.get_effective_model_policy(fresh=True)
    assert policy["roles"] == model_policy.DEFAULT_MODEL_POLICY
    assert policy["fallback_models"] == model_policy.DEFAULT_FALLBACK_MODELS


def test_direct_answer_defaults_to_mini_tier(policy_db):
    """The specific fix this module exists for: the highest-volume role
    should not default to a frontier-tier model."""
    assert model_policy.DEFAULT_MODEL_POLICY["direct_answer"] == "gpt-4.1-mini"


def test_research_roles_default_to_frontier_tier(policy_db):
    research_roles = [
        "research_brief",
        "coverage_contract",
        "research_planner",
        "query_author",
        "reflection",
        "citation_verifier",
        "repair",
        "synthesis",
        "synthesis_executive",
    ]

    assert all("mini" not in model_policy.DEFAULT_MODEL_POLICY[role] for role in research_roles)
    assert model_policy.DEFAULT_MODEL_POLICY["synthesis"] == "claude-opus-4-8"
    assert model_policy.DEFAULT_MODEL_POLICY["query_author"] == "claude-opus-4-8"


def test_fallback_models_have_provider_prefix(policy_db):
    """Regression guard for the missing 'gemini/' prefix bug."""
    gemini_entries = [m for m in model_policy.DEFAULT_FALLBACK_MODELS if "gemini" in m]
    assert gemini_entries and all(m.startswith("gemini/") for m in gemini_entries)


def test_partial_override_merges_over_defaults(policy_db):
    Session = policy_db
    with Session() as session:
        updated = model_policy.set_model_policy(session, role_overrides={"direct_answer": "claude-sonnet-4-6"})
        session.commit()
    assert updated["roles"]["direct_answer"] == "claude-sonnet-4-6"
    # Everything else still matches defaults.
    for role, default_model in model_policy.DEFAULT_MODEL_POLICY.items():
        if role != "direct_answer":
            assert updated["roles"][role] == default_model


def test_unknown_role_rejected(policy_db):
    Session = policy_db
    with Session() as session:
        with pytest.raises(ValueError):
            model_policy.set_model_policy(session, role_overrides={"not_a_real_role": "gpt-4.1"})


def test_clearing_override_with_empty_string_reverts_to_default(policy_db):
    Session = policy_db
    with Session() as session:
        model_policy.set_model_policy(session, role_overrides={"synthesis": "gpt-4.1"})
        session.commit()
        cleared = model_policy.set_model_policy(session, role_overrides={"synthesis": ""})
        session.commit()
    assert cleared["roles"]["synthesis"] == model_policy.DEFAULT_MODEL_POLICY["synthesis"]


def test_reset_clears_all_overrides(policy_db):
    Session = policy_db
    with Session() as session:
        model_policy.set_model_policy(session, role_overrides={"direct_answer": "claude-sonnet-4-6"}, fallback_models=["gpt-4.1"])
        session.commit()
        reset = model_policy.reset_model_policy(session)
        session.commit()
    assert reset["roles"] == model_policy.DEFAULT_MODEL_POLICY
    assert reset["fallback_models"] == model_policy.DEFAULT_FALLBACK_MODELS


def test_fallback_models_full_replace(policy_db):
    Session = policy_db
    with Session() as session:
        updated = model_policy.set_model_policy(session, fallback_models=["gpt-4.1", "claude-sonnet-4-6"])
        session.commit()
    assert updated["fallback_models"] == ["gpt-4.1", "claude-sonnet-4-6"]


def test_canonical_role_resolves_aliases(policy_db):
    assert model_policy.canonical_role("direct") == "direct_answer"
    assert model_policy.canonical_role("brief") == "research_brief"
    assert model_policy.canonical_role("contract") == "coverage_contract"
    assert model_policy.canonical_role("repair_agent") == "repair"
    assert model_policy.canonical_role("lead_research") == "research_planner"


def test_dead_judge_roles_resolve_to_none(policy_db):
    """research_judge / document_judge / judge never call a model (pure
    heuristic scoring) -- they must not resolve to a model policy entry."""
    assert model_policy.canonical_role("judge") is None
    assert model_policy.canonical_role("research_judge") is None
    assert model_policy.canonical_role("document_judge") is None


def test_model_for_role_reads_effective_policy(policy_db):
    Session = policy_db
    with Session() as session:
        model_policy.set_model_policy(session, role_overrides={"direct_answer": "gemini/gemini-2.5-flash"})
        session.commit()
    model_policy.invalidate_cache()
    assert model_client.model_for_role("direct_answer") == "gemini/gemini-2.5-flash"
    assert model_client.model_for_role("direct") == "gemini/gemini-2.5-flash"  # alias


def test_model_for_role_executive_quality_mode(policy_db):
    assert model_client.model_for_role("synthesis", quality_mode="standard") == model_policy.DEFAULT_MODEL_POLICY["synthesis"]
    assert model_client.model_for_role("synthesis", quality_mode="executive") == model_policy.DEFAULT_MODEL_POLICY["synthesis_executive"]


def test_model_for_role_unknown_role_returns_none(policy_db):
    assert model_client.model_for_role("totally_made_up_role") is None
    assert model_client.model_for_role(None) is None


def test_per_turn_overrides_take_precedence_over_stored_policy(policy_db):
    """The admin-only per-turn override (TurnRequest.model_overrides)
    must win over the org-wide stored policy for that one call, without
    changing the stored policy itself."""
    Session = policy_db
    with Session() as session:
        model_policy.set_model_policy(session, role_overrides={"direct_answer": "claude-sonnet-4-6"})
        session.commit()
    model_policy.invalidate_cache()

    # No per-turn override: org default applies.
    assert model_client.model_for_role("direct_answer") == "claude-sonnet-4-6"
    # Per-turn override wins for this call only.
    assert model_client.model_for_role("direct_answer", overrides={"direct_answer": "claude-opus-4-8"}) == "claude-opus-4-8"
    # Stored policy is untouched by the override.
    assert model_client.model_for_role("direct_answer") == "claude-sonnet-4-6"


def test_per_turn_override_resolves_synthesis_executive_alias(policy_db):
    assert model_client.model_for_role(
        "synthesis", quality_mode="executive", overrides={"synthesis_executive": "gpt-4.1"}
    ) == "gpt-4.1"
    # An override keyed to the base role does not leak into the executive variant.
    assert model_client.model_for_role(
        "synthesis", quality_mode="executive", overrides={"synthesis": "gpt-4.1"}
    ) == model_policy.DEFAULT_MODEL_POLICY["synthesis_executive"]


def test_per_turn_override_falls_back_when_role_not_overridden(policy_db):
    """Overrides only apply to the roles explicitly listed; other roles in
    the same turn still resolve from the stored policy."""
    assert model_client.model_for_role(
        "fast_router", overrides={"direct_answer": "claude-opus-4-8"}
    ) == model_policy.DEFAULT_MODEL_POLICY["fast_router"]


def test_explicit_preferred_model_beats_everything(policy_db):
    """complete()'s own preferred_model argument is the lowest-level escape
    hatch and outranks both the per-turn override and the stored policy."""
    candidates = model_client._candidate_models("explicit-pin")
    assert candidates[0] == "explicit-pin"
