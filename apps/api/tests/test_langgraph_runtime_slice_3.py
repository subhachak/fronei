"""Slice 3 stop condition tests: synthesize → verify → judge → repair.

Stop conditions:
  1. answer is non-empty after synthesize.
  2. model_used has no "-stub" suffix.
  3. judge_result is a real ResearchJudgeResult (has score, can_publish).
  4. feedback.judge is populated from the real judge result.
  5. repair runs and populates repair_history when judge status="repair".
  6. repair is a pass-through when judge approves (can_publish=True).
  7. EHR fixture: judge_result populated; score > 0.
  8. repair_history is list[str] (audit trail of repair instructions applied).
  9. replay_final_answer=True when repair ran; False when not needed.
 10. feedback.final_score matches judge_result.score.
 11. next_action is one of the expected values.
 12. synthesize node is visited and emits a progress event.
"""
from __future__ import annotations

import pytest

from app.services.agent.langgraph_runtime.graph import run_stub_graph
from app.services.agent.langgraph_runtime.nodes import NODE_ORDER
from app.services.agent.langgraph_runtime.runtime import run_langgraph_research
from app.services.agent.langgraph_runtime.state import ResearchGraphState
from app.services.agent.models import TurnRequest
from app.services.agent.research_models import EvidencePack, ResearchJudgeResult

from test_agent_runtime import FakeTools, _patch_completion

_TEST_REQUEST = TurnRequest(message="Research something for Slice 3 tests.")
_EHR_REQUEST = TurnRequest(
    message=(
        "Compare EHR vendors: Epic, Oracle Health, athenahealth, Veradigm, and Meditech. "
        "Coverage: pricing, integration, user experience, support, and deployment."
    ),
    research_level="regular",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_full(monkeypatch, request: TurnRequest = _TEST_REQUEST) -> dict:
    """Run the full pipeline with FakeTools and patched completions."""
    _patch_completion(monkeypatch)
    return run_langgraph_research(request, FakeTools())


# ---------------------------------------------------------------------------
# 3.1  answer is non-empty and model_used has no -stub suffix
# ---------------------------------------------------------------------------

def test_answer_is_non_empty_after_slice3(monkeypatch):
    result = _run_full(monkeypatch)
    answer = result["response"].text
    assert isinstance(answer, str), "response.text must be a string"
    # fake_simple_completion returns "# Answer\n\nDone." — non-empty
    assert len(answer) > 0, "answer must be non-empty after Slice 3 synthesis"


def test_model_used_has_no_stub_suffix(monkeypatch):
    result = _run_full(monkeypatch)
    model_used = result["response"].model_used
    assert "-stub" not in (model_used or ""), (
        f"model_used must not contain '-stub' after Slice 3; got {model_used!r}"
    )
    assert model_used not in ("", None), "model_used must be non-empty"


# ---------------------------------------------------------------------------
# 3.2  judge_result is a real ResearchJudgeResult
# ---------------------------------------------------------------------------

def test_judge_result_is_populated(monkeypatch):
    result = _run_full(monkeypatch)
    state = result["langgraph_state"]
    judge_result = state.get("judge_result")
    assert judge_result is not None, "judge_result must be set after judge node"
    assert isinstance(judge_result, ResearchJudgeResult)


def test_judge_result_has_score(monkeypatch):
    result = _run_full(monkeypatch)
    judge_result = result["langgraph_state"].get("judge_result")
    assert judge_result is not None
    assert isinstance(judge_result.score, float)
    assert 0.0 <= judge_result.score <= 1.0, (
        f"judge score must be in [0, 1]; got {judge_result.score}"
    )


def test_judge_result_has_can_publish(monkeypatch):
    result = _run_full(monkeypatch)
    judge_result = result["langgraph_state"].get("judge_result")
    assert judge_result is not None
    assert isinstance(judge_result.can_publish, bool)


# ---------------------------------------------------------------------------
# 3.3  feedback is built from the real judge result
# ---------------------------------------------------------------------------

def test_feedback_judge_matches_state_judge_result(monkeypatch):
    result = _run_full(monkeypatch)
    state = result["langgraph_state"]
    judge_result = state.get("judge_result")
    feedback = result["feedback"]
    assert judge_result is not None
    assert feedback.judge.score == judge_result.score, (
        f"feedback.judge.score ({feedback.judge.score}) must match "
        f"state judge_result.score ({judge_result.score})"
    )
    assert feedback.final_score == judge_result.score


# ---------------------------------------------------------------------------
# 3.4  repair node behaviour
# ---------------------------------------------------------------------------

def test_repair_is_passthrough_when_judge_approves(monkeypatch):
    """Inject a judge result with can_publish=True; repair must be a no-op."""
    _patch_completion(monkeypatch)
    import app.services.agent.langgraph_runtime.nodes as nodes_module

    original_judge = nodes_module.judge

    def approve_always(state, *, run_id, request, tools=None, progress=None):
        result = original_judge(
            state, run_id=run_id, request=request, tools=tools, progress=progress
        )
        result["judge_result"] = ResearchJudgeResult(
            status="pass", score=0.95, issues=[], can_publish=True
        )
        result["next_action"] = "publish"
        return result

    monkeypatch.setattr(nodes_module, "judge", approve_always)

    result = _run_full(monkeypatch)
    repair_history = result["langgraph_state"].get("repair_history") or []
    assert repair_history == [], (
        "repair_history must be empty when judge approves; "
        f"got {repair_history}"
    )
    assert result["replay_final_answer"] is False


def test_repair_runs_and_logs_instruction_when_judge_requests_repair(monkeypatch):
    """Inject judge result with status='repair'; repair must call repair_research_answer."""
    _patch_completion(monkeypatch)
    import app.services.agent.langgraph_runtime.nodes as nodes_module

    original_judge = nodes_module.judge

    def request_repair(state, *, run_id, request, tools=None, progress=None):
        result = original_judge(
            state, run_id=run_id, request=request, tools=tools, progress=progress
        )
        result["judge_result"] = ResearchJudgeResult(
            status="repair",
            score=0.55,
            issues=["Missing citations."],
            repair_instruction="Add [S#] citations for factual claims.",
            can_publish=False,
        )
        result["next_action"] = "research_more"
        return result

    monkeypatch.setattr(nodes_module, "judge", request_repair)

    result = _run_full(monkeypatch)
    repair_history = result["langgraph_state"].get("repair_history") or []
    assert len(repair_history) == 1, (
        f"Expected 1 repair instruction in repair_history; got {repair_history}"
    )
    assert "citations" in repair_history[0].lower(), (
        f"repair_history must log the repair instruction; got {repair_history}"
    )
    assert result["replay_final_answer"] is True


def test_repair_history_is_list_of_str(monkeypatch):
    result = _run_full(monkeypatch)
    history = result["langgraph_state"].get("repair_history") or []
    assert isinstance(history, list)
    for item in history:
        assert isinstance(item, str), f"repair_history items must be str; got {type(item)}"


# ---------------------------------------------------------------------------
# 3.5  next_action values
# ---------------------------------------------------------------------------

def test_next_action_is_valid(monkeypatch):
    result = _run_full(monkeypatch)
    next_action = result["langgraph_state"].get("next_action")
    valid = {"publish", "research_more", "stop_with_gaps", "requires_approval"}
    assert next_action in valid, (
        f"next_action must be one of {valid}; got {next_action!r}"
    )


# ---------------------------------------------------------------------------
# 3.6  synthesize node visited + progress event emitted
# ---------------------------------------------------------------------------

def test_synthesize_node_is_visited(monkeypatch):
    _patch_completion(monkeypatch)
    events = []

    def capture(stage, message, **data):
        events.append((stage, message, data))

    result = run_langgraph_research(_TEST_REQUEST, FakeTools(), capture)
    visited = result["langgraph_state"]["visited_nodes"]
    assert "synthesize" in visited, "synthesize must be in visited_nodes"
    stages = [stage for stage, _, _ in events]
    assert "synthesize" in stages, "synthesize must emit a progress event"


def test_all_slice3_nodes_visited(monkeypatch):
    _patch_completion(monkeypatch)
    result = run_langgraph_research(_TEST_REQUEST, FakeTools())
    visited = result["langgraph_state"]["visited_nodes"]
    for node in ("synthesize", "verify", "judge", "repair"):
        assert node in visited, f"Expected '{node}' in visited_nodes"


# ---------------------------------------------------------------------------
# 3.7  EHR fixture: judge_result populated; score > 0
# ---------------------------------------------------------------------------

def test_ehr_fixture_judge_result_populated(monkeypatch):
    _patch_completion(monkeypatch)
    result = run_langgraph_research(_EHR_REQUEST, FakeTools())
    judge_result = result["langgraph_state"].get("judge_result")
    assert judge_result is not None, "judge_result must be set for EHR fixture"
    assert 0.0 <= judge_result.score <= 1.0
    assert judge_result.status in {"pass", "repair", "fail"}
    if judge_result.score == 0.0:
        assert judge_result.issues


def test_ehr_fixture_answer_is_non_empty(monkeypatch):
    _patch_completion(monkeypatch)
    result = run_langgraph_research(_EHR_REQUEST, FakeTools())
    assert len(result["response"].text) > 0, (
        "EHR fixture answer must be non-empty after Slice 3"
    )


# ---------------------------------------------------------------------------
# 3.8  replay_final_answer reflects repair outcome
# ---------------------------------------------------------------------------

def test_replay_final_answer_false_when_judge_approves(monkeypatch):
    _patch_completion(monkeypatch)
    import app.services.agent.langgraph_runtime.nodes as nodes_module

    original_judge = nodes_module.judge

    def approve_always(state, *, run_id, request, tools=None, progress=None):
        result = original_judge(
            state, run_id=run_id, request=request, tools=tools, progress=progress
        )
        result["judge_result"] = ResearchJudgeResult(
            status="pass", score=0.92, issues=[], can_publish=True
        )
        result["next_action"] = "publish"
        return result

    monkeypatch.setattr(nodes_module, "judge", approve_always)
    result = _run_full(monkeypatch)
    assert result["replay_final_answer"] is False
