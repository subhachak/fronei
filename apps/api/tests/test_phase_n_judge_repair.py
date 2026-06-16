import json
from types import SimpleNamespace

import pytest

from app.services.agent_runtime.document_agent import DocumentAgent
from app.services.agent_runtime.judge_service import JudgeService
from app.services.agent_runtime.models import JudgeResult
from app.services.agent_runtime.registry import _load_from_files
from app.services.agent_runtime.research_agent import ResearchAgent
from app.services.agent_runtime.tool_runner import ToolRunner
from app.services.agent_runtime.utils import effective_max_repair_iters
from app.services.turn_graph.state import TurnGraphState


@pytest.fixture
def default_registry():
    return _load_from_files()


def _judge_result(
    *,
    status: str = "pass",
    score: float = 0.9,
    repairs: list[dict] | None = None,
    target_type: str = "document",
    target_id: str = "t",
    judge_agent_id: str = "document_judge",
) -> JudgeResult:
    return JudgeResult(
        id="judge-test",
        target_type=target_type,
        target_id=target_id,
        judge_agent_id=judge_agent_id,
        score=score,
        status=status,
        issues=[],
        required_repairs=repairs or [],
        can_publish=(status == "pass"),
    )


def _patch_research_tools(monkeypatch) -> None:
    def fake_tool_run(self, tool, args, **kwargs):
        del self, args, kwargs
        if tool == "web_search":
            return SimpleNamespace(
                latency_ms=5,
                output={"sources": [{"title": "T", "url": "https://8.8.8.8/r"}]},
            )
        return SimpleNamespace(latency_ms=5, output={"content": "source body"})

    monkeypatch.setattr("app.services.agent_runtime.tool_runner.ToolRunner.run", fake_tool_run)


def test_research_synthesis_repair_triggered_on_repair_verdict(monkeypatch, default_registry):
    answers = iter([
        '{"search_queries": ["q1"]}',
        '{"claims": [{"text": "Claim.", "source_url": "https://8.8.8.8/r", "confidence": 0.8}]}',
        "Initial synthesis.",
        "Repaired synthesis.",
    ])
    llm_calls: list[str] = []

    def fake_llm(**kwargs):
        answer = next(answers)
        llm_calls.append(answer)
        return SimpleNamespace(answer=answer, model_used="m", latency_ms=5, estimated_cost_usd=0.0)

    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", fake_llm)
    _patch_research_tools(monkeypatch)

    judge_calls = [0]

    def fake_evaluate(self, policy_id, *, content, context, target_id):
        del self, context
        judge_calls[0] += 1
        if judge_calls[0] == 1:
            return _judge_result(
                status="repair",
                score=0.4,
                repairs=[{"section": "citations", "instruction": "Add more source citations"}],
                target_type="research",
                target_id=target_id,
                judge_agent_id=policy_id,
            )
        return _judge_result(target_type="research", target_id=target_id, judge_agent_id=policy_id)

    monkeypatch.setattr(JudgeService, "evaluate", fake_evaluate)

    state = TurnGraphState(user_message="Explain AI safety.", turn_id="t1", quality_mode="standard")
    result = ResearchAgent(default_registry).run(state, SimpleNamespace(plan={}))

    assert result.answer == "Repaired synthesis."
    assert "Repaired synthesis." in llm_calls


def test_research_repair_skipped_on_economy_mode(monkeypatch, default_registry):
    answers = iter([
        '{"search_queries": ["q1"]}',
        '{"claims": [{"text": "C.", "source_url": "https://8.8.8.8/e", "confidence": 0.8}]}',
        "Initial synthesis.",
    ])
    invoke_count = [0]

    def fake_llm(**kwargs):
        del kwargs
        invoke_count[0] += 1
        return SimpleNamespace(answer=next(answers), model_used="m", latency_ms=5, estimated_cost_usd=0.0)

    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", fake_llm)
    _patch_research_tools(monkeypatch)
    monkeypatch.setattr(
        JudgeService,
        "evaluate",
        lambda self, pid, *, content, context, target_id: _judge_result(
            status="repair",
            score=0.3,
            repairs=[{"section": "all", "instruction": "Fix everything"}],
            target_type="research",
            target_id=target_id,
            judge_agent_id=pid,
        ),
    )

    state = TurnGraphState(user_message="Q.", turn_id="t2", quality_mode="economy")
    result = ResearchAgent(default_registry).run(state, SimpleNamespace(plan={}))

    assert invoke_count[0] == 3
    assert result.answer == "Initial synthesis."


def test_research_repair_respects_standard_cap(monkeypatch, default_registry):
    answers = iter([
        '{"search_queries": ["q1"]}',
        '{"claims": [{"text": "C.", "source_url": "https://1.1.1.1/r", "confidence": 0.9}]}',
        "Initial answer.",
        "Repaired answer.",
        "Should not reach here.",
    ])
    invoke_count = [0]

    def fake_llm(**kwargs):
        del kwargs
        invoke_count[0] += 1
        return SimpleNamespace(answer=next(answers), model_used="m", latency_ms=5, estimated_cost_usd=0.0)

    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", fake_llm)
    _patch_research_tools(monkeypatch)
    monkeypatch.setattr(
        JudgeService,
        "evaluate",
        lambda self, pid, *, content, context, target_id: _judge_result(
            status="repair",
            score=0.35,
            repairs=[{"section": "all", "instruction": "Always broken"}],
            target_type="research",
            target_id=target_id,
            judge_agent_id=pid,
        ),
    )

    state = TurnGraphState(user_message="Q.", turn_id="t3", quality_mode="standard")
    ResearchAgent(default_registry).run(state, SimpleNamespace(plan={}))

    assert invoke_count[0] == 4


def test_effective_max_repair_iters_economy_and_draft():
    policy = SimpleNamespace(max_repair_iterations=5)
    assert effective_max_repair_iters("economy", policy) == 0
    assert effective_max_repair_iters("draft", policy) == 0
    assert effective_max_repair_iters("economy", None) == 0


def test_effective_max_repair_iters_standard_caps_at_one():
    policy = SimpleNamespace(max_repair_iterations=5)
    assert effective_max_repair_iters("standard", policy) == 1
    assert effective_max_repair_iters("standard", None) == 1


def test_effective_max_repair_iters_premium_and_executive_use_policy():
    policy = SimpleNamespace(max_repair_iterations=3)
    assert effective_max_repair_iters("premium", policy) == 3
    assert effective_max_repair_iters("executive", policy) == 3
    assert effective_max_repair_iters("premium", None) == 1


def test_document_plan_repair_triggers_replan(monkeypatch, default_registry):
    plans = iter([
        json.dumps({"document_brief": {"title": "Original", "doc_type": "executive_report"}}),
        json.dumps({"document_brief": {"title": "Repaired", "doc_type": "executive_report"}}),
    ])
    plan_call_count = [0]

    def fake_json(*_args, **_kwargs):
        plan_call_count[0] += 1
        return SimpleNamespace(answer=next(plans), model_used="m", latency_ms=5, estimated_cost_usd=0.0)

    monkeypatch.setattr("app.services.llm_gateway.invoke_llm_json", fake_json)
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm",
        lambda **_kwargs: SimpleNamespace(answer="Body.", model_used="m", latency_ms=5, estimated_cost_usd=0.0),
    )
    monkeypatch.setattr(
        ToolRunner,
        "run",
        lambda self, *a, **kw: SimpleNamespace(latency_ms=0, output={"docx_base64": "", "filename": "t.docx"}),
    )

    judge_calls = [0]

    def fake_evaluate(self, policy_id, *, content, context, target_id):
        del self, content
        judge_calls[0] += 1
        if context.get("stage") == "plan" and judge_calls[0] == 1:
            return _judge_result(
                status="repair",
                score=0.4,
                repairs=[{"section": "title", "instruction": "Strengthen title"}],
                target_id=target_id,
                judge_agent_id=policy_id,
            )
        return _judge_result(target_id=target_id, judge_agent_id=policy_id)

    monkeypatch.setattr(JudgeService, "evaluate", fake_evaluate)

    state = TurnGraphState(user_message="Write a strategy doc.", turn_id="t4", quality_mode="standard")
    result = DocumentAgent(default_registry).run(state, SimpleNamespace(plan={}))

    assert plan_call_count[0] == 2
    assert result.title == "Repaired"
    assert state.document_brief["title"] == "Repaired"


def test_document_plan_repair_skipped_on_economy(monkeypatch, default_registry):
    plan_call_count = [0]

    def fake_json(*_args, **_kwargs):
        plan_call_count[0] += 1
        return SimpleNamespace(
            answer=json.dumps({"document_brief": {"title": "T", "doc_type": "executive_report"}}),
            model_used="m",
            latency_ms=5,
            estimated_cost_usd=0.0,
        )

    monkeypatch.setattr("app.services.llm_gateway.invoke_llm_json", fake_json)
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm",
        lambda **_kwargs: SimpleNamespace(answer="Body.", model_used="m", latency_ms=5, estimated_cost_usd=0.0),
    )
    monkeypatch.setattr(
        ToolRunner,
        "run",
        lambda self, *a, **kw: SimpleNamespace(latency_ms=0, output={"docx_base64": "", "filename": "t.docx"}),
    )
    monkeypatch.setattr(
        JudgeService,
        "evaluate",
        lambda self, pid, *, content, context, target_id: _judge_result(
            status="repair",
            score=0.3,
            repairs=[{"section": "all", "instruction": "Fix it"}],
            target_id=target_id,
            judge_agent_id=pid,
        ),
    )

    state = TurnGraphState(user_message="Doc.", turn_id="t5", quality_mode="economy")
    DocumentAgent(default_registry).run(state, SimpleNamespace(plan={}))

    assert plan_call_count[0] == 1


def test_document_content_repair_triggered_on_repair_verdict(monkeypatch, default_registry):
    plan_json = json.dumps({"document_brief": {"title": "Strategy", "doc_type": "executive_report"}})
    content_answers = iter(["Initial content.", "Repaired content."])
    content_call_count = [0]

    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm_json",
        lambda *_args, **_kwargs: SimpleNamespace(answer=plan_json, model_used="m", latency_ms=5, estimated_cost_usd=0.0),
    )

    def fake_llm(**_kwargs):
        content_call_count[0] += 1
        return SimpleNamespace(answer=next(content_answers), model_used="m", latency_ms=5, estimated_cost_usd=0.0)

    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", fake_llm)
    monkeypatch.setattr(
        ToolRunner,
        "run",
        lambda self, *a, **kw: SimpleNamespace(latency_ms=0, output={"docx_base64": "", "filename": "t.docx"}),
    )

    def fake_evaluate(self, policy_id, *, content, context, target_id):
        del self
        if context.get("stage") == "content" and "Repaired" not in content:
            return _judge_result(
                status="repair",
                score=0.4,
                repairs=[{"section": "body", "instruction": "Needs more depth"}],
                target_id=target_id,
                judge_agent_id=policy_id,
            )
        return _judge_result(target_id=target_id, judge_agent_id=policy_id)

    monkeypatch.setattr(JudgeService, "evaluate", fake_evaluate)

    state = TurnGraphState(user_message="Write strategy.", turn_id="t6", quality_mode="standard")
    result = DocumentAgent(default_registry).run(state, SimpleNamespace(plan={}))

    assert content_call_count[0] == 2
    assert result.markdown == "Repaired content."
    assert state.document_content == "Repaired content."


def test_document_content_repair_skipped_on_economy(monkeypatch, default_registry):
    plan_json = json.dumps({"document_brief": {"title": "T", "doc_type": "executive_report"}})
    content_call_count = [0]

    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm_json",
        lambda *_args, **_kwargs: SimpleNamespace(answer=plan_json, model_used="m", latency_ms=5, estimated_cost_usd=0.0),
    )

    def fake_llm(**_kwargs):
        content_call_count[0] += 1
        return SimpleNamespace(answer="Economy content.", model_used="m", latency_ms=5, estimated_cost_usd=0.0)

    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", fake_llm)
    monkeypatch.setattr(
        ToolRunner,
        "run",
        lambda self, *a, **kw: SimpleNamespace(latency_ms=0, output={"docx_base64": "", "filename": "t.docx"}),
    )
    monkeypatch.setattr(
        JudgeService,
        "evaluate",
        lambda self, pid, *, content, context, target_id: _judge_result(
            status="repair",
            score=0.2,
            repairs=[{"section": "all", "instruction": "Fix everything"}],
            target_id=target_id,
            judge_agent_id=pid,
        ),
    )

    state = TurnGraphState(user_message="Doc.", turn_id="t7", quality_mode="economy")
    result = DocumentAgent(default_registry).run(state, SimpleNamespace(plan={}))

    assert content_call_count[0] == 1
    assert result.markdown == "Economy content."


def test_research_resynthesize_injects_repair_note_into_context(monkeypatch, default_registry):
    answers = iter([
        '{"search_queries": ["q1"]}',
        '{"claims": [{"text": "C.", "source_url": "https://8.8.4.4/x", "confidence": 0.8}]}',
        "Initial answer.",
        "Repaired answer.",
    ])
    captured_contexts: list[str] = []

    def fake_llm(**kwargs):
        captured_contexts.append(kwargs.get("web_context", "") or "")
        return SimpleNamespace(answer=next(answers), model_used="m", latency_ms=5, estimated_cost_usd=0.0)

    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", fake_llm)
    _patch_research_tools(monkeypatch)

    judge_calls = [0]

    def fake_evaluate(self, pid, *, content, context, target_id):
        del self, content, context
        judge_calls[0] += 1
        if judge_calls[0] == 1:
            return _judge_result(
                status="repair",
                score=0.4,
                repairs=[
                    {"section": "citations", "instruction": "Add source citations"},
                    {"section": "depth", "instruction": "Expand on key points"},
                ],
                target_type="research",
                target_id=target_id,
                judge_agent_id=pid,
            )
        return _judge_result(target_type="research", target_id=target_id, judge_agent_id=pid)

    monkeypatch.setattr(JudgeService, "evaluate", fake_evaluate)

    state = TurnGraphState(user_message="Explain ML.", turn_id="t8", quality_mode="standard")
    ResearchAgent(default_registry).run(state, SimpleNamespace(plan={}))

    repair_context = "\n\n".join(captured_contexts)
    assert "Add source citations" in repair_context
    assert "REVISION REQUIRED" in repair_context


def test_premium_mode_uses_full_policy_max_iterations(monkeypatch, default_registry):
    policy = default_registry.judges.get("research_judge")
    assert policy is not None
    original_max = policy.max_repair_iterations
    policy.max_repair_iterations = 2

    answers = iter([
        '{"search_queries": ["q1"]}',
        '{"claims": [{"text": "C.", "source_url": "https://1.1.1.1/p", "confidence": 0.9}]}',
        "Synthesis 0.",
        "Synthesis 1.",
        "Synthesis 2.",
    ])
    invoke_count = [0]

    def fake_llm(**kwargs):
        del kwargs
        invoke_count[0] += 1
        return SimpleNamespace(answer=next(answers), model_used="m", latency_ms=5, estimated_cost_usd=0.0)

    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", fake_llm)
    _patch_research_tools(monkeypatch)
    monkeypatch.setattr(
        JudgeService,
        "evaluate",
        lambda self, pid, *, content, context, target_id: _judge_result(
            status="repair",
            score=0.35,
            repairs=[{"section": "all", "instruction": "Always broken"}],
            target_type="research",
            target_id=target_id,
            judge_agent_id=pid,
        ),
    )

    try:
        state = TurnGraphState(user_message="Q.", turn_id="t9", quality_mode="premium")
        ResearchAgent(default_registry).run(state, SimpleNamespace(plan={}))
        assert invoke_count[0] == 5
    finally:
        policy.max_repair_iterations = original_max
