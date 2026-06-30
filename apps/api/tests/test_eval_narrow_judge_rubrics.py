"""Step 4 narrow judge rubrics (scoring_spec.md §1.6) + related programmatic
synthesis_requirements checks (must_not_recommend, max/min_answer_length).

gap_honesty and conflict_handling go through _binary_judge_call, which makes
a real model call — mocked here via model_client.simple_completion to test
the aggregation/gating logic deterministically, without live model cost.
Live validation (gap_honesty against a real 3-subject deep-research run with
a known coverage gap) tracked separately; see PR description.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_API_ROOT = Path(__file__).resolve().parents[1]
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from app.routers.evals import (  # noqa: E402
    _binary_judge_call,
    _coverage_by_subject,
    score_answer_length_bounds,
    score_conflict_handling,
    score_gap_honesty,
    score_must_not_recommend,
)


def _mock_completion(text: str):
    return MagicMock(text=text)


# ── _coverage_by_subject ─────────────────────────────────────────────────────

def test_coverage_by_subject_none_without_requirements():
    assert _coverage_by_subject({"v2_spec": {}}, []) is None


def test_coverage_by_subject_per_subject_ratios():
    case = {
        "v2_spec": {"retrieval_requirements": {
            "required_subjects": ["AWS S3", "Azure"],
            "required_dimensions": ["durability", "pricing"],
        }}
    }
    items = [
        SimpleNamespace(url="", title="", evidence="", query="AWS S3 durability"),
        SimpleNamespace(url="", title="", evidence="", query="AWS S3 pricing"),
    ]
    result = _coverage_by_subject(case, items)
    assert result == {"AWS S3": 1.0, "Azure": 0.0}


# ── _binary_judge_call ───────────────────────────────────────────────────────

def test_binary_judge_call_none_for_empty_answer():
    assert _binary_judge_call("Does X?", "", []) is None


def test_binary_judge_call_parses_yes():
    with patch("app.services.agent.model_client.simple_completion", return_value=_mock_completion("YES")):
        assert _binary_judge_call("Does X?", "some answer", []) is True


def test_binary_judge_call_parses_no():
    with patch("app.services.agent.model_client.simple_completion", return_value=_mock_completion("NO")):
        assert _binary_judge_call("Does X?", "some answer", []) is False


def test_binary_judge_call_none_on_unparseable_response():
    with patch("app.services.agent.model_client.simple_completion", return_value=_mock_completion("maybe?")):
        assert _binary_judge_call("Does X?", "some answer", []) is None


def test_binary_judge_call_none_on_exception():
    with patch("app.services.agent.model_client.simple_completion", side_effect=RuntimeError("timeout")):
        assert _binary_judge_call("Does X?", "some answer", []) is None


# ── score_gap_honesty ────────────────────────────────────────────────────────

def test_gap_honesty_none_when_not_required():
    case = {"v2_spec": {}}
    assert score_gap_honesty(case, "answer", []) is None


def test_gap_honesty_none_when_no_gaps_exist():
    """All subjects fully covered — nothing to disclose, so the axis doesn't
    apply (None, not True) rather than a vacuous pass."""
    case = {
        "v2_spec": {
            "retrieval_requirements": {"required_subjects": ["AWS S3"], "required_dimensions": ["durability"]},
            "synthesis_requirements": {"must_disclose_gaps": True},
        }
    }
    items = [SimpleNamespace(url="", title="", evidence="", query="AWS S3 durability")]
    assert score_gap_honesty(case, "answer", items) is None


def test_gap_honesty_true_when_judge_confirms_disclosure():
    case = {
        "v2_spec": {
            "retrieval_requirements": {
                "required_subjects": ["AWS S3", "Azure"], "required_dimensions": ["durability"],
            },
            "synthesis_requirements": {"must_disclose_gaps": True},
        }
    }
    items = [SimpleNamespace(url="", title="", evidence="", query="AWS S3 durability")]  # Azure has no coverage
    with patch("app.services.agent.model_client.simple_completion", return_value=_mock_completion("YES")):
        assert score_gap_honesty(case, "AWS S3 is well documented. Azure evidence is thin/missing.", items) is True


def test_gap_honesty_false_when_judge_finds_silent_omission():
    case = {
        "v2_spec": {
            "retrieval_requirements": {
                "required_subjects": ["AWS S3", "Azure"], "required_dimensions": ["durability"],
            },
            "synthesis_requirements": {"must_disclose_gaps": True},
        }
    }
    items = [SimpleNamespace(url="", title="", evidence="", query="AWS S3 durability")]
    with patch("app.services.agent.model_client.simple_completion", return_value=_mock_completion("NO")):
        assert score_gap_honesty(case, "AWS S3 is well documented.", items) is False


# ── score_conflict_handling ──────────────────────────────────────────────────

def test_conflict_handling_none_when_not_required():
    assert score_conflict_handling({"v2_spec": {}}, "answer", []) is None


def test_conflict_handling_true_when_judge_confirms():
    case = {"v2_spec": {"synthesis_requirements": {"must_surface_conflicts": True}}}
    with patch("app.services.agent.model_client.simple_completion", return_value=_mock_completion("YES")):
        assert score_conflict_handling(case, "Official sources say X; practitioners report Y.", []) is True


# ── score_must_not_recommend (programmatic, no model call) ──────────────────

def test_must_not_recommend_none_when_not_required():
    assert score_must_not_recommend({"v2_spec": {}}, "I recommend AWS S3.") is None


def test_must_not_recommend_passes_with_descriptive_answer():
    case = {"v2_spec": {"synthesis_requirements": {"must_not_recommend": True}}}
    assert score_must_not_recommend(case, "AWS S3 costs $0.023/GB. Azure costs $0.018/GB.") is True


def test_must_not_recommend_fails_when_recommendation_present():
    case = {"v2_spec": {"synthesis_requirements": {"must_not_recommend": True}}}
    assert score_must_not_recommend(case, "Overall, I recommend AWS S3 for most workloads.") is False


# ── score_answer_length_bounds ───────────────────────────────────────────────

def test_answer_length_bounds_none_without_limits():
    assert score_answer_length_bounds({"v2_spec": {}}, 5000) is None


def test_answer_length_bounds_fails_over_max():
    case = {"v2_spec": {"synthesis_requirements": {"max_answer_length": 150}}}
    assert score_answer_length_bounds(case, 4193) is False  # the Tokyo-weather over-research case shape


def test_answer_length_bounds_fails_under_min():
    case = {"v2_spec": {"synthesis_requirements": {"min_answer_length": 100}}}
    assert score_answer_length_bounds(case, 20) is False


def test_answer_length_bounds_passes_within_range():
    case = {"v2_spec": {"synthesis_requirements": {"min_answer_length": 10, "max_answer_length": 100}}}
    assert score_answer_length_bounds(case, 50) is True
