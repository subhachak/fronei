"""Step 6 dashboard rollup (scoring_spec.md §3, §6) + canary drift (§2).

Pure unit tests against synthetic per-case result dicts — compute_dashboard
is a presentation-layer aggregation over already-computed "scores" dicts,
no model calls.
"""
import sys
from pathlib import Path

_API_ROOT = Path(__file__).resolve().parents[1]
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from app.routers.evals import compute_dashboard, score_canary_drift  # noqa: E402


def _case(case_id, route, research_level=None, overall_status="pass", canary_drift=False, **scores):
    return {
        "case_id": case_id, "route": route, "research_level": research_level,
        "overall_status": overall_status, "canary_drift": canary_drift, "scores": scores,
    }


# ── score_canary_drift ───────────────────────────────────────────────────────

def test_canary_drift_none_when_not_a_canary():
    assert score_canary_drift({"v2_spec": {}}, 0.9) is None


def test_canary_drift_none_without_band():
    case = {"v2_spec": {"harness_integrity_checks": {"is_canary": True}}}
    assert score_canary_drift(case, 0.9) is None


def test_canary_drift_none_without_judge_score():
    case = {"v2_spec": {"harness_integrity_checks": {"is_canary": True, "expected_judge_score_band": [0.95, 1.0]}}}
    assert score_canary_drift(case, None) is None


def test_canary_drift_false_within_band():
    case = {"v2_spec": {"harness_integrity_checks": {"is_canary": True, "expected_judge_score_band": [0.95, 1.0]}}}
    assert score_canary_drift(case, 0.98) is False


def test_canary_drift_true_outside_band():
    """The known-clean-fail anchor shape: expected_judge_score_band=[0.0,0.2]
    (e.g. the Tokyo weather over-research case) — if it ever scores high,
    that's drift, not product improvement."""
    case = {"v2_spec": {"harness_integrity_checks": {"is_canary": True, "expected_judge_score_band": [0.0, 0.2]}}}
    assert score_canary_drift(case, 0.9) is True


# ── compute_dashboard ────────────────────────────────────────────────────────

def test_dashboard_empty_cases():
    dash = compute_dashboard([])
    assert dash["integrity"]["ok"] is True
    assert dash["total_cases"] == 0
    assert dash["table"]["route_correct"]["by_tier"]["direct"] is None


def test_dashboard_tier_routing_splits_research_by_level():
    cases = [
        _case(1, "research", "easy", route_correct=True),
        _case(2, "research", "regular", route_correct=True),
        _case(3, "research", "deep", route_correct=False),
    ]
    dash = compute_dashboard(cases)
    table = dash["table"]["route_correct"]["by_tier"]
    assert table["research_easy"] == {"rate": 1.0, "n": 1}
    assert table["research_regular"] == {"rate": 1.0, "n": 1}
    assert table["research_deep"] == {"rate": 0.0, "n": 1}


def test_dashboard_float_axis_computes_mean():
    cases = [
        _case(1, "research", "deep", retrieval_completeness=1.0),
        _case(2, "research", "deep", retrieval_completeness=0.5),
    ]
    dash = compute_dashboard(cases)
    assert dash["table"]["retrieval_completeness"]["by_tier"]["research_deep"] == {"mean": 0.75, "n": 2}


def test_dashboard_excludes_harness_error_cases_from_aggregates():
    """The core sequencing requirement from §3/§9: a harness_error case's
    scores can't be trusted, so it must not silently dilute the aggregate —
    excluding it entirely (not averaging it in as a 0) is the only way to
    avoid a real defect looking like a merely-mediocre number."""
    cases = [
        _case(1, "research", "regular", overall_status="harness_error", route_correct=True, latency_pass=True),
    ]
    dash = compute_dashboard(cases)
    assert dash["integrity"]["harness_error_count"] == 1
    assert dash["integrity"]["harness_error_case_ids"] == [1]
    assert dash["integrity"]["ok"] is False
    # excluded entirely — not present in the tier's n at all
    assert dash["table"]["route_correct"]["by_tier"]["research_regular"] is None
    assert dash["trustworthy_cases"] == 0


def test_dashboard_integrity_flags_canary_drift_separately_from_harness_error():
    cases = [
        _case(1, "research", "deep", canary_drift=True, route_correct=True),
    ]
    dash = compute_dashboard(cases)
    assert dash["integrity"]["canary_drift_count"] == 1
    assert dash["integrity"]["canary_drift_case_ids"] == [1]
    assert dash["integrity"]["harness_error_count"] == 0
    assert dash["integrity"]["ok"] is False
    # canary-drifted cases are NOT excluded from aggregates (unlike
    # harness_error) — drift is a calibration signal, not untrustworthy data.
    assert dash["table"]["route_correct"]["by_tier"]["research_deep"] == {"rate": 1.0, "n": 1}


def test_dashboard_integrity_ok_when_nothing_flagged():
    cases = [_case(1, "direct", route_correct=True)]
    dash = compute_dashboard(cases)
    assert dash["integrity"]["ok"] is True
    assert dash["integrity"]["harness_error_count"] == 0
    assert dash["integrity"]["canary_drift_count"] == 0


def test_dashboard_document_and_research_document_are_distinct_tiers():
    cases = [
        _case(1, "document", format_correct=True),
        _case(2, "research_document", format_correct=False),
    ]
    dash = compute_dashboard(cases)
    table = dash["table"]["format_correct"]["by_tier"]
    assert table["document"] == {"rate": 1.0, "n": 1}
    assert table["research_document"] == {"rate": 0.0, "n": 1}


# ── score_canary_drift — bidirectional primary-signal pattern ─────────────

def test_canary_drift_primary_signal_no_drift_when_expected_value_matches():
    """answer_length_ok=False expected, actual=False → no drift (canary healthy)."""
    from app.routers.evals import score_canary_drift
    case = {"v2_spec": {"harness_integrity_checks": {
        "is_canary": True,
        "canary_primary_signal": "answer_length_ok",
        "canary_expected_primary_signal_value": False,
    }}}
    assert score_canary_drift(case, None, scores={"answer_length_ok": False}) is False


def test_canary_drift_primary_signal_fires_when_value_differs():
    """answer_length_ok=False expected, actual=True → drift (over-research stopped — investigate)."""
    from app.routers.evals import score_canary_drift
    case = {"v2_spec": {"harness_integrity_checks": {
        "is_canary": True,
        "canary_primary_signal": "answer_length_ok",
        "canary_expected_primary_signal_value": False,
    }}}
    assert score_canary_drift(case, None, scores={"answer_length_ok": True}) is True


def test_canary_drift_primary_signal_none_when_signal_not_computed():
    """Signal is absent from scores dict (wrong route type, etc.) → None, not False."""
    from app.routers.evals import score_canary_drift
    case = {"v2_spec": {"harness_integrity_checks": {
        "is_canary": True,
        "canary_primary_signal": "answer_length_ok",
        "canary_expected_primary_signal_value": False,
    }}}
    assert score_canary_drift(case, None, scores={"latency_pass": False}) is None


def test_canary_drift_primary_signal_none_when_scores_not_passed():
    """Primary-signal canary with scores=None → None (not an error)."""
    from app.routers.evals import score_canary_drift
    case = {"v2_spec": {"harness_integrity_checks": {
        "is_canary": True,
        "canary_primary_signal": "answer_length_ok",
        "canary_expected_primary_signal_value": False,
    }}}
    assert score_canary_drift(case, None, scores=None) is None


def test_canary_drift_band_pattern_still_works_alongside_primary_signal():
    """Band-pattern canaries (know-clean-pass/fail) still work unchanged."""
    from app.routers.evals import score_canary_drift
    case = {"v2_spec": {"harness_integrity_checks": {
        "is_canary": True,
        "expected_judge_score_band": [0.95, 1.0],
    }}}
    assert score_canary_drift(case, 0.98) is False
    assert score_canary_drift(case, 0.5) is True
