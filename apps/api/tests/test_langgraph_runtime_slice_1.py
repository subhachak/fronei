"""Slice 1 stop condition tests.

Key assertions per spec:
- subject_derivation extracts 5 EHR vendors from the golden-set EHR query.
- contract node uses those subjects → 5 subjects in CoverageContract.
- Generic query (no named entities) → named_subjects=[] → fallback template fires.
- generate_coverage_contract still works with named_subjects=None (legacy call sites unaffected).
- 0A/0B tests still pass (model_used updated to slice-1-stub).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.agent.langgraph_runtime.nodes import (
    contract as contract_node,
    plan as plan_node,
    subject_derivation as subject_derivation_node,
)
from app.services.agent.langgraph_runtime.state import ResearchGraphState
from app.services.agent.models import TurnRequest
from app.services.agent.research_models import ResearchBrief, ResearchPlan


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

EHR_QUERY = (
    "Compare Epic, Oracle Cerner, Meditech Expanse, athenahealth, and eClinicalWorks as EHR platforms "
    "for enterprise hospital deployment in 2025. For each: clinical workflow integration, interoperability "
    "standards (HL7 FHIR support), cloud deployment model, pricing model, and known implementation failure modes."
)

EHR_EXPECTED_SUBJECTS = {
    "Epic", "Oracle Cerner", "Meditech Expanse", "athenahealth", "eClinicalWorks"
}

GENERIC_QUERY = (
    "What are the best practices for building a secure REST API with Python FastAPI?"
)


def _ehr_request() -> TurnRequest:
    return TurnRequest(message=EHR_QUERY, research_level="deep")


def _generic_request() -> TurnRequest:
    return TurnRequest(message=GENERIC_QUERY, research_level="regular")


def _base_state(**overrides) -> ResearchGraphState:
    base: ResearchGraphState = {
        "visited_nodes": [],
        "artifacts": {},
        "cost_usd_spent": 0.0,
        "tool_calls_made": 0,
        "model_calls_made": 0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1.1  subject_derivation — EHR fixture extracts all 5 vendors
# ---------------------------------------------------------------------------

def test_subject_derivation_ehr_extracts_five_vendors():
    """Stop condition: EHR query → 5 named vendors, not generic dimension labels."""
    request = _ehr_request()
    state = _base_state(brief=None)

    result = subject_derivation_node(state, run_id="test", request=request)

    subjects = set(result["named_subjects"])
    assert subjects == EHR_EXPECTED_SUBJECTS, (
        f"Expected {EHR_EXPECTED_SUBJECTS}, got {subjects}"
    )


def test_subject_derivation_generic_query_returns_empty():
    """Generic query with no named entities → named_subjects=[]."""
    request = _generic_request()
    state = _base_state(brief=None)

    result = subject_derivation_node(state, run_id="test", request=request)

    assert result["named_subjects"] == [], (
        f"Expected [] for generic query, got {result['named_subjects']}"
    )


def test_subject_derivation_does_not_promote_scope_in_to_subjects():
    """brief.scope_in dimension labels must NOT become named_subjects.

    scope_in is almost always dimension labels ('regulatory compliance',
    'total cost of ownership') — not entity names. The spec explicitly
    forbids gating the fallback on scope_in being empty.
    """
    request = _generic_request()
    brief = ResearchBrief(
        objective=GENERIC_QUERY,
        research_level="regular",
        scope_in=["security best practices", "authentication", "rate limiting"],
    )
    state = _base_state(brief=brief)

    result = subject_derivation_node(state, run_id="test", request=request)

    assert result["named_subjects"] == [], (
        "scope_in dimension labels must not be promoted to named_subjects"
    )


# ---------------------------------------------------------------------------
# 1.2  contract node — EHR subjects propagate into CoverageContract
# ---------------------------------------------------------------------------

def test_contract_node_ehr_uses_five_vendor_subjects():
    """contract node receives named_subjects from state and builds 5-subject contract."""
    request = _ehr_request()
    ehr_brief = ResearchBrief(
        objective=EHR_QUERY,
        research_level="deep",
        research_profile="vendor_comparison",
        scope_in=["clinical workflow", "interoperability", "cloud model", "pricing", "failure modes"],
    )
    state = _base_state(
        brief=ehr_brief,
        named_subjects=list(EHR_EXPECTED_SUBJECTS),
    )

    result = contract_node(state, run_id="test", request=request)

    coverage = result["contract"]
    assert coverage is not None
    contract_subjects = set(coverage.subjects)
    assert contract_subjects == EHR_EXPECTED_SUBJECTS, (
        f"Contract subjects {contract_subjects} != expected {EHR_EXPECTED_SUBJECTS}"
    )


def test_contract_node_generic_query_uses_fallback_template():
    """Generic query with named_subjects=[] uses the generic/profile fallback template."""
    request = _generic_request()
    generic_brief = ResearchBrief(
        objective=GENERIC_QUERY,
        research_level="regular",
        research_profile="technical_architecture",
    )
    state = _base_state(
        brief=generic_brief,
        named_subjects=[],
    )

    result = contract_node(state, run_id="test", request=request)

    coverage = result["contract"]
    assert coverage is not None
    # Generic profile → cells exist but are not anchored to named entities
    assert len(coverage.cells) > 0, "Fallback contract must have coverage cells"
    # No vendor names should appear as subjects in the generic case
    for subj in coverage.subjects:
        assert subj not in EHR_EXPECTED_SUBJECTS, (
            f"EHR vendor name '{subj}' appeared in a generic query contract"
        )


# ---------------------------------------------------------------------------
# 1.3  plan node — produces ResearchPlan with workers
# ---------------------------------------------------------------------------

def test_plan_node_produces_workers_for_ehr_contract():
    """plan_from_contract for 5-subject EHR contract → ≥5 workers (one per subject)."""
    from app.services.agent.research_contracts import generate_coverage_contract

    request = _ehr_request()
    ehr_brief = ResearchBrief(
        objective=EHR_QUERY,
        research_level="deep",
        research_profile="vendor_comparison",
    )
    subjects = list(EHR_EXPECTED_SUBJECTS)
    coverage = generate_coverage_contract(request, ehr_brief, named_subjects=subjects)
    state = _base_state(
        brief=ehr_brief,
        named_subjects=subjects,
        contract=coverage,
    )

    result = plan_node(state, run_id="test", request=request)

    research_plan = result["plan"]
    assert isinstance(research_plan, ResearchPlan)
    assert len(research_plan.workers) >= 5, (
        f"Expected ≥5 workers for 5-subject EHR contract, got {len(research_plan.workers)}"
    )
    worker_queries = " ".join(w.query for w in research_plan.workers)
    # At least one vendor name must appear in the queries
    assert any(vendor.lower() in worker_queries.lower() for vendor in EHR_EXPECTED_SUBJECTS), (
        "No EHR vendor names found in any worker query"
    )


# ---------------------------------------------------------------------------
# 1.4  Legacy call-site compatibility: named_subjects=None still works
# ---------------------------------------------------------------------------

def test_generate_coverage_contract_none_named_subjects_is_backward_compatible():
    """Calling generate_coverage_contract without named_subjects= (legacy) still works."""
    from app.services.agent.research_contracts import generate_coverage_contract

    request = _ehr_request()
    brief = ResearchBrief(
        objective=EHR_QUERY,
        research_level="deep",
        research_profile="vendor_comparison",
    )
    # Legacy call: no named_subjects kwarg
    coverage = generate_coverage_contract(request, brief)
    assert coverage is not None
    assert len(coverage.subjects) >= 1


# ---------------------------------------------------------------------------
# 1.5  Phase 12 regression: subjects are vendor names, not dimension labels
#
# The Phase 12 bug: scope_in contains dimension labels ("regulatory compliance",
# "total cost of ownership") and the old code used them as subjects when
# named entity extraction was empty, producing coverage cells like
# ("regulatory compliance", "regulatory compliance") instead of
# ("Epic", "regulatory compliance").
# ---------------------------------------------------------------------------

def test_phase12_regression_contract_subjects_are_vendors_not_dimensions():
    """Regression: contract subjects for EHR query must be vendor names, never dimension labels."""
    from app.services.agent.research_contracts import generate_coverage_contract

    DIMENSION_LABELS = {
        "regulatory compliance", "integration risk", "total cost of ownership",
        "implementation risk", "clinical workflow integration",
        "interoperability standards", "cloud deployment model",
        "pricing model", "known implementation failure modes",
    }

    request = _ehr_request()
    brief = ResearchBrief(
        objective=EHR_QUERY,
        research_level="deep",
        research_profile="vendor_comparison",
        scope_in=list(DIMENSION_LABELS),  # deliberately populated with dimension labels
    )
    subjects = list(EHR_EXPECTED_SUBJECTS)
    coverage = generate_coverage_contract(request, brief, named_subjects=subjects)

    for subj in coverage.subjects:
        assert subj not in DIMENSION_LABELS, (
            f"Dimension label '{subj}' appeared as a coverage subject — Phase 12 regression"
        )
    assert set(coverage.subjects) == EHR_EXPECTED_SUBJECTS


# ---------------------------------------------------------------------------
# 1.6  brief node emits model_calls_made=1 (mocked LLM call)
# ---------------------------------------------------------------------------

def test_brief_node_emits_model_call_delta(monkeypatch):
    """brief node must emit model_calls_made=1 regardless of LLM latency."""
    from app.services.agent.langgraph_runtime.nodes import brief as brief_node

    mock_brief = ResearchBrief(
        objective=EHR_QUERY,
        research_level="deep",
        research_profile="vendor_comparison",
        cost_usd=0.002,
    )
    monkeypatch.setattr(
        "app.services.agent.langgraph_runtime.nodes.generate_research_brief",
        lambda req: mock_brief,
        raising=False,
    )

    request = _ehr_request()
    state = _base_state()

    # Import after monkeypatching
    import app.services.agent.langgraph_runtime.nodes as nodes_mod
    orig = nodes_mod.__dict__.get("generate_research_brief")
    nodes_mod.__dict__["generate_research_brief"] = lambda req: mock_brief

    try:
        result = brief_node(state, run_id="test", request=request)
    finally:
        if orig is not None:
            nodes_mod.__dict__["generate_research_brief"] = orig
        else:
            nodes_mod.__dict__.pop("generate_research_brief", None)

    assert result["model_calls_made"] == 1
    assert result["brief"] is not None


# ---------------------------------------------------------------------------
# 1.7  End-to-end: brief node generates a real brief (mocked LLM)
# ---------------------------------------------------------------------------

def test_brief_node_uses_generate_research_brief(monkeypatch):
    """brief node delegates to generate_research_brief and stores result in state."""
    from app.services.agent.langgraph_runtime.nodes import brief as brief_node
    from app.services.agent.research_profiles import generate_research_brief

    mock_brief = ResearchBrief(
        objective=EHR_QUERY,
        research_level="deep",
        research_profile="vendor_comparison",
    )
    monkeypatch.setattr(
        "app.services.agent.research_profiles.generate_research_brief",
        lambda req: mock_brief,
    )

    request = _ehr_request()
    state = _base_state()
    result = brief_node(state, run_id="test", request=request)

    stored = result.get("brief")
    assert stored is not None
    assert stored.research_profile == "vendor_comparison"
    assert "brief" in result.get("visited_nodes", [])
