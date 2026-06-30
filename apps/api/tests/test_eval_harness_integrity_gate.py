"""Harness integrity gate (scoring_spec.md §1.9, eval_case_schema.json case_id 120).

Regression test for the exact bug found in evalrun_6784d6e46164.json: 11 of 37
cases had answer_length=0 but judge_score=1.0 — a result of
budget_gate_pre_synthesis routing straight to END before synthesize/judge ever
ran, with the old fallback in run_langgraph_research() defaulting to
ResearchJudgeResult(status="pass", score=1.0) (fixed in PR #31). This test does
NOT call the model — it's a synthetic fixture-level check that the gate itself
still fires, per eval_case_schema.json's harness_integrity_checks contract:
"not run against the model at all... asserts the require_structural_judge_agreement
gate fires." Run on every harness deploy, not just every eval run.
"""
import sys
from pathlib import Path

_API_ROOT = Path(__file__).resolve().parents[1]
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from app.routers.evals import check_judge_structural_agreement, compute_overall_status  # noqa: E402


def test_empty_answer_with_high_judge_score_is_harness_error():
    """The exact synthetic fixture from eval_case_schema.json case_id 120:
    answer='' injected at the harness level, judge_score=1.0."""
    agreement = check_judge_structural_agreement(judge_score=1.0, answer_length=0)
    assert agreement is False, "judge_structural_agreement must fail for judge_score=1.0 with an empty answer"

    status = compute_overall_status(
        judge_structural_agreement=agreement,
        overall_structural_pass=False,
        overall_benchmark_pass=None,
        route_correct=None,
        deep_research_gate=None,
    )
    assert status == "harness_error", "overall_status must be harness_error, not fail/partial/pass"


def test_empty_answer_with_low_judge_score_is_not_harness_error():
    """An empty answer with a LOW judge_score is a real product failure
    (judge correctly scored the empty answer), not a harness integrity defect —
    must not be misclassified as harness_error."""
    agreement = check_judge_structural_agreement(judge_score=0.0, answer_length=0)
    assert agreement is True


def test_judge_score_none_does_not_trip_the_gate():
    """direct/clarify/document routes never run a research judge — judge_score
    is None for them, which must not be treated as a structural disagreement."""
    assert check_judge_structural_agreement(judge_score=None, answer_length=0) is True
    assert check_judge_structural_agreement(judge_score=None, answer_length=120) is True


def test_high_judge_score_with_real_answer_agrees():
    assert check_judge_structural_agreement(judge_score=0.95, answer_length=500) is True


def test_harness_error_takes_priority_over_other_failures():
    """Even if other axes would compute to fail/partial, harness_error must win —
    a structurally-disagreeing result can't be trusted enough to grade at all."""
    status = compute_overall_status(
        judge_structural_agreement=False,
        overall_structural_pass=True,
        overall_benchmark_pass=True,
        route_correct=True,
        deep_research_gate={"pass": True},
    )
    assert status == "harness_error"


def test_overall_status_pass_when_everything_agrees():
    status = compute_overall_status(
        judge_structural_agreement=True,
        overall_structural_pass=True,
        overall_benchmark_pass=True,
        route_correct=True,
        deep_research_gate={"pass": True},
    )
    assert status == "pass"


def test_overall_status_partial_on_benchmark_or_route_or_gate_miss():
    base = dict(judge_structural_agreement=True, overall_structural_pass=True)
    assert compute_overall_status(**base, overall_benchmark_pass=False, route_correct=True, deep_research_gate=None) == "partial"
    assert compute_overall_status(**base, overall_benchmark_pass=True, route_correct=False, deep_research_gate=None) == "partial"
    assert compute_overall_status(**base, overall_benchmark_pass=True, route_correct=True, deep_research_gate={"pass": False}) == "partial"


def test_overall_status_fail_when_structural_pass_is_false():
    status = compute_overall_status(
        judge_structural_agreement=True,
        overall_structural_pass=False,
        overall_benchmark_pass=None,
        route_correct=None,
        deep_research_gate=None,
    )
    assert status == "fail"


def test_against_real_production_run_flags_exactly_the_known_eleven_cases():
    """Reproduces the exact validation done against evalrun_6784d6e46164.json:
    the gate must flag case_ids [1, 4, 6, 8, 9, 10, 11, 12, 25, 30, 35] and no
    others, when applied to that run's actual per-case judge_score/answer_length
    values. Hardcoded from the real run's data rather than re-reading the file,
    so this test has no external file dependency."""
    # (case_id, judge_score, answer_length) for every case in that run.
    real_run_data = [
        (7, None, 620), (2, None, 687), (8, 1.0, 0), (1, 1.0, 0), (9, 1.0, 0),
        (6, 1.0, 0), (12, 1.0, 0), (11, 1.0, 0), (10, 1.0, 0), (4, 1.0, 0),
        (3, 1.0, 5939), (5, 1.0, 8343), (14, 1.0, 11830), (13, 1.0, 9860),
        (24, 0.85, 4121), (25, 1.0, 0), (28, None, 148), (19, 1.0, 11102),
        (22, 1.0, 11739), (23, 1.0, 8692), (31, None, 68), (32, None, 67),
        (17, 0.9, 13205), (35, 1.0, 0), (15, 1.0, 15317), (29, None, 110),
        (26, None, 49), (27, None, 49), (16, 0.9, 15239), (30, 1.0, 0),
        (21, 1.0, 10321), (20, 0.9, 13817), (36, 1.0, 4193), (33, 1.0, 7601),
        (18, 1.0, 18131), (34, 1.0, 14283), (37, 1.0, 10035),
    ]
    flagged = [cid for cid, score, length in real_run_data if not check_judge_structural_agreement(score, length)]
    assert sorted(flagged) == [1, 4, 6, 8, 9, 10, 11, 12, 25, 30, 35]
