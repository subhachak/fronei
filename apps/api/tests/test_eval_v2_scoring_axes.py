"""Step 2 programmatic scoring axes (scoring_spec.md §1.1-1.3, §1.8).

Pure unit tests against synthetic case_dicts / evidence-item stand-ins — no
model calls. Live validation against case_id 112 (cloud_storage_three_subject)
from eval_cases_v2_starter.json confirmed retrieval_completeness=0.833
(10/12 cells) on a real run, a large improvement over the ~0.33 (4/12) seen
in the original evalrun_6784d6e46164.json run, consistent with PR #26's
source-balancing fix — and confirming the metric is sensitive enough to
detect the smaller residual gap.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

_API_ROOT = Path(__file__).resolve().parents[1]
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from app.routers.evals import (  # noqa: E402
    score_gate_correct,
    score_latency_pass,
    score_retrieval_completeness,
    score_retrieval_independence,
)


def _evidence_item(url="", title="", evidence="", query="", source_family=""):
    return SimpleNamespace(url=url, title=title, evidence=evidence, query=query, source_family=source_family)


# ── score_gate_correct ──────────────────────────────────────────────────────

def test_gate_correct_none_when_case_asserts_nothing():
    assert score_gate_correct({"v2_spec": {}}, {"pass": True}) is None
    assert score_gate_correct({}, None) is None


def test_gate_correct_true_when_expected_to_fire_and_does():
    case = {"v2_spec": {"routing": {"expected_gate_fires": True}}}
    assert score_gate_correct(case, {"pass": True}) is True


def test_gate_correct_false_when_expected_to_fire_but_silent():
    case = {"v2_spec": {"routing": {"expected_gate_fires": True}}}
    assert score_gate_correct(case, None) is False


def test_gate_correct_false_when_expected_silent_but_fires():
    case = {"v2_spec": {"routing": {"expected_gate_silent": True}}}
    assert score_gate_correct(case, {"pass": True}) is False


def test_gate_correct_true_when_expected_silent_and_silent():
    case = {"v2_spec": {"routing": {"expected_gate_silent": True}}}
    assert score_gate_correct(case, None) is True


# ── score_retrieval_completeness ────────────────────────────────────────────

def test_retrieval_completeness_none_without_requirements():
    assert score_retrieval_completeness({"v2_spec": {}}, []) is None


def test_retrieval_completeness_full_coverage():
    case = {
        "v2_spec": {"retrieval_requirements": {
            "required_subjects": ["AWS S3", "Azure Blob Storage"],
            "required_dimensions": ["durability", "pricing"],
        }}
    }
    items = [
        _evidence_item(query="AWS S3 durability official documentation"),
        _evidence_item(query="AWS S3 pricing tiers"),
        _evidence_item(query="Azure Blob Storage durability redundancy"),
        _evidence_item(query="Azure Blob Storage pricing per GB"),
    ]
    assert score_retrieval_completeness(case, items) == 1.0


def test_retrieval_completeness_partial_coverage_matches_v1_defect_shape():
    """Reproduces the exact v1 failure shape: first-listed subject gets full
    coverage, the others get none — this is what min_coverage_cells in the
    v2 schema is designed to catch as a programmatic fail."""
    case = {
        "v2_spec": {"retrieval_requirements": {
            "required_subjects": ["AWS S3", "Azure Blob Storage", "Google Cloud Storage"],
            "required_dimensions": ["durability", "pricing"],
        }}
    }
    items = [
        _evidence_item(query="AWS S3 durability documentation"),
        _evidence_item(query="AWS S3 pricing tiers"),
    ]
    # Only AWS S3's 2 cells filled out of 3 subjects x 2 dimensions = 6 required.
    assert score_retrieval_completeness(case, items) == 2 / 6


def test_retrieval_completeness_zero_with_no_evidence():
    case = {
        "v2_spec": {"retrieval_requirements": {
            "required_subjects": ["X"], "required_dimensions": ["Y"],
        }}
    }
    assert score_retrieval_completeness(case, []) == 0.0


# ── score_retrieval_independence ────────────────────────────────────────────

def test_retrieval_independence_none_without_thresholds():
    assert score_retrieval_independence({"v2_spec": {}}, {}, []) is None


def test_retrieval_independence_passes_min_sources():
    case = {"v2_spec": {"retrieval_requirements": {"min_independent_sources": 2}}}
    run = {"independent_source_count": 3}
    assert score_retrieval_independence(case, run, []) is True


def test_retrieval_independence_fails_min_sources():
    case = {"v2_spec": {"retrieval_requirements": {"min_independent_sources": 5}}}
    run = {"independent_source_count": 1}
    assert score_retrieval_independence(case, run, []) is False


def test_retrieval_independence_catches_single_domain_dominance():
    """The exact GPT-4o forum case from scoring_spec.md §1.3: 6 evidence
    items, all from the same domain — independent_source_count=1 is correct,
    but max_single_domain_share formalizes this as an explicit fail rather
    than relying on independent_source_count alone to surface it."""
    case = {"v2_spec": {"retrieval_requirements": {"max_single_domain_share": 0.5}}}
    items = [_evidence_item(source_family="community.openai.com") for _ in range(6)]
    assert score_retrieval_independence(case, {}, items) is False


def test_retrieval_independence_passes_with_diverse_domains():
    case = {"v2_spec": {"retrieval_requirements": {"max_single_domain_share": 0.5}}}
    items = [
        _evidence_item(source_family="aws.amazon.com"),
        _evidence_item(source_family="cloud.google.com"),
        _evidence_item(source_family="learn.microsoft.com"),
        _evidence_item(source_family="aws.amazon.com"),
    ]
    # max share = 2/4 = 0.5, not > 0.5, so passes.
    assert score_retrieval_independence(case, {}, items) is True


# ── score_latency_pass ──────────────────────────────────────────────────────

def test_latency_pass_uses_tier_ceiling_by_default():
    assert score_latency_pass({}, "direct", None, 1500) is True
    assert score_latency_pass({}, "direct", None, 2500) is False


def test_latency_pass_research_uses_research_level_tier():
    assert score_latency_pass({}, "research", "easy", 15000) is True
    assert score_latency_pass({}, "research", "easy", 25000) is False
    assert score_latency_pass({}, "research", "deep", 250000) is True
    assert score_latency_pass({}, "research", "deep", 350000) is False


def test_latency_pass_case_override_takes_priority():
    case = {"v2_spec": {"cost_latency_budget": {"latency_ms_ceiling": 1000}}}
    assert score_latency_pass(case, "research", "deep", 999) is True
    assert score_latency_pass(case, "research", "deep", 1001) is False
