import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.services.agent_runtime.document_agent import DocumentAgent
from app.services.agent_runtime.judge_service import JudgeService
from app.services.agent_runtime.models import JudgeResult
from app.services.agent_runtime.registry import _load_from_files
from app.services.turn_graph.state import TurnGraphState


@pytest.fixture
def default_registry():
    return _load_from_files()


def _make_doc_state(msg="Write an executive brief on AI trends"):
    return TurnGraphState(user_message=msg, user_id="u1", turn_id="turn-test-doc", conversation_id="conv1")


def _judge_result(
    *,
    status: str = "pass",
    score: float = 0.9,
    repairs: list[dict] | None = None,
) -> JudgeResult:
    return JudgeResult(
        id="judge-test",
        target_type="document",
        target_id="turn-test-doc",
        judge_agent_id="document_judge",
        status=status,
        can_publish=(status == "pass"),
        score=score,
        issues=[],
        required_repairs=repairs or [],
    )


@contextmanager
def _patch_doc_run(monkeypatch, plan_json, content_answer):
    def fake_invoke_llm_json(*_args, **_kwargs):
        return SimpleNamespace(
            answer=plan_json,
            model_used="model-test",
            latency_ms=10,
            estimated_cost_usd=0.001,
        )

    def fake_invoke_llm(**_kwargs):
        return SimpleNamespace(
            answer=content_answer,
            model_used="model-test",
            latency_ms=15,
            estimated_cost_usd=0.002,
        )

    monkeypatch.setattr("app.services.llm_gateway.invoke_llm_json", fake_invoke_llm_json)
    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", fake_invoke_llm)
    monkeypatch.setattr(
        "app.services.agent_runtime.tool_runner.ToolRunner.run",
        lambda self, *a, **kw: SimpleNamespace(
            latency_ms=5,
            output={"docx_base64": "ZA==", "filename": "test.docx"},
        ),
    )
    monkeypatch.setattr(JudgeService, "evaluate", lambda self, *a, **kw: _judge_result())
    yield


def test_all_five_stage_events_fire(monkeypatch, default_registry):
    plan_json = json.dumps({"document_brief": {"title": "AI Brief", "doc_type": "executive_report"}})
    with _patch_doc_run(monkeypatch, plan_json, "## AI Trends\nContent here."):
        state = _make_doc_state()
        decision = SimpleNamespace(plan={"intent": "document"})
        DocumentAgent(default_registry).run(state, decision)

    nodes = {event.node for event in state.events}
    for expected in (
        "document.content_plan",
        "document.design_plan",
        "document.render",
        "document.qa_polish",
        "document.final_preview",
    ):
        assert expected in nodes


def test_document_brief_written_to_state(monkeypatch, default_registry):
    plan_json = json.dumps({"document_brief": {"title": "Strategy Doc", "doc_type": "executive_report"}})
    with _patch_doc_run(monkeypatch, plan_json, "Body content."):
        state = _make_doc_state()
        DocumentAgent(default_registry).run(state, SimpleNamespace(plan={}))

    assert state.document_brief is not None
    assert state.document_brief.get("title") == "Strategy Doc"


def test_document_content_written_to_state(monkeypatch, default_registry):
    plan_json = json.dumps({"document_brief": {"title": "T", "doc_type": "executive_report"}})
    with _patch_doc_run(monkeypatch, plan_json, "## Section 1\nSome text."):
        state = _make_doc_state()
        DocumentAgent(default_registry).run(state, SimpleNamespace(plan={}))

    assert state.document_content == "## Section 1\nSome text."


def test_judge_repair_loop_replans_on_repair_verdict(monkeypatch, default_registry):
    plan_call_count = [0]
    repair_json = json.dumps({"document_brief": {"title": "Repaired Title", "doc_type": "executive_report"}})

    def fake_invoke_llm_json(*_args, **_kwargs):
        plan_call_count[0] += 1
        return SimpleNamespace(
            answer=repair_json,
            model_used="model-test",
            latency_ms=10,
            estimated_cost_usd=0.001,
        )

    judge_calls = [0]

    def fake_evaluate(self, policy_id, *, content, context, target_id):
        del self, policy_id, content, context, target_id
        judge_calls[0] += 1
        if judge_calls[0] == 1:
            return _judge_result(
                status="repair",
                score=0.4,
                repairs=[
                    {"section": "summary", "instruction": "Add executive summary"},
                    {"section": "title", "instruction": "Strengthen title"},
                ],
            )
        return _judge_result(status="pass", score=0.85)

    monkeypatch.setattr("app.services.llm_gateway.invoke_llm_json", fake_invoke_llm_json)
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm",
        lambda **_kwargs: SimpleNamespace(answer="Body", model_used="m", latency_ms=5, estimated_cost_usd=0.0),
    )
    monkeypatch.setattr(
        "app.services.agent_runtime.tool_runner.ToolRunner.run",
        lambda self, *a, **kw: SimpleNamespace(latency_ms=0, output={"docx_base64": "", "filename": "t.docx"}),
    )
    monkeypatch.setattr(JudgeService, "evaluate", fake_evaluate)

    state = _make_doc_state()
    DocumentAgent(default_registry).run(state, SimpleNamespace(plan={}))

    assert plan_call_count[0] == 2
    assert judge_calls[0] >= 2
    assert state.document_brief["title"] == "Repaired Title"


def test_judge_repair_loop_stops_at_max_iterations(monkeypatch, default_registry):
    plan_call_count = [0]

    def counting_invoke_llm_json(*_args, **_kwargs):
        plan_call_count[0] += 1
        return SimpleNamespace(
            answer=json.dumps({"document_brief": {"title": "T", "doc_type": "executive_report"}}),
            model_used="m",
            latency_ms=5,
            estimated_cost_usd=0.0,
        )

    def always_repair(self, policy_id, *, content, context, target_id):
        del self, policy_id, content, context, target_id
        return _judge_result(
            status="repair",
            score=0.3,
            repairs=[{"section": "all", "instruction": "Fix everything"}],
        )

    monkeypatch.setattr("app.services.llm_gateway.invoke_llm_json", counting_invoke_llm_json)
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm",
        lambda **_kwargs: SimpleNamespace(answer="Body", model_used="m", latency_ms=5, estimated_cost_usd=0.0),
    )
    monkeypatch.setattr(
        "app.services.agent_runtime.tool_runner.ToolRunner.run",
        lambda self, *a, **kw: SimpleNamespace(latency_ms=0, output={"docx_base64": "", "filename": "t.docx"}),
    )
    monkeypatch.setattr(JudgeService, "evaluate", always_repair)

    state = _make_doc_state()
    DocumentAgent(default_registry).run(state, SimpleNamespace(plan={}))

    assert plan_call_count[0] <= 2


def test_judge_repair_loop_skipped_when_no_judge_policy(monkeypatch, default_registry):
    default_registry.agents["document_lead"] = default_registry.agent("document_lead").model_copy(
        update={"judge_policy_id": None}
    )
    plan_call_count = [0]

    def counting_invoke_llm_json(*_args, **_kwargs):
        plan_call_count[0] += 1
        return SimpleNamespace(
            answer=json.dumps({"document_brief": {"title": "T", "doc_type": "executive_report"}}),
            model_used="m",
            latency_ms=5,
            estimated_cost_usd=0.0,
        )

    monkeypatch.setattr("app.services.llm_gateway.invoke_llm_json", counting_invoke_llm_json)
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm",
        lambda **_kwargs: SimpleNamespace(answer="Body", model_used="m", latency_ms=5, estimated_cost_usd=0.0),
    )
    monkeypatch.setattr(
        "app.services.agent_runtime.tool_runner.ToolRunner.run",
        lambda self, *a, **kw: SimpleNamespace(latency_ms=0, output={"docx_base64": "", "filename": "t.docx"}),
    )

    state = _make_doc_state()
    DocumentAgent(default_registry).run(state, SimpleNamespace(plan={}))

    assert plan_call_count[0] == 1


def test_document_result_title_from_repaired_brief(monkeypatch, default_registry):
    plans = iter([
        json.dumps({"document_brief": {"title": "Original Title", "doc_type": "executive_report"}}),
        json.dumps({"document_brief": {"title": "Repaired Title", "doc_type": "executive_report"}}),
    ])

    def next_plan(*_args, **_kwargs):
        return SimpleNamespace(answer=next(plans), model_used="m", latency_ms=5, estimated_cost_usd=0.0)

    judge_calls = [0]

    def one_repair_judge(self, policy_id, *, content, context, target_id):
        del self, policy_id, content, context, target_id
        judge_calls[0] += 1
        if judge_calls[0] == 1:
            return _judge_result(
                status="repair",
                score=0.6,
                repairs=[{"section": "body", "instruction": "Add more detail"}],
            )
        return _judge_result(status="pass", score=0.9)

    monkeypatch.setattr("app.services.llm_gateway.invoke_llm_json", next_plan)
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm",
        lambda **_kwargs: SimpleNamespace(answer="Body", model_used="m", latency_ms=5, estimated_cost_usd=0.0),
    )
    monkeypatch.setattr(
        "app.services.agent_runtime.tool_runner.ToolRunner.run",
        lambda self, *a, **kw: SimpleNamespace(latency_ms=0, output={"docx_base64": "", "filename": "t.docx"}),
    )
    monkeypatch.setattr(JudgeService, "evaluate", one_repair_judge)

    state = _make_doc_state()
    result = DocumentAgent(default_registry).run(state, SimpleNamespace(plan={}))

    assert result.title == "Repaired Title"
    assert state.document_brief["title"] == "Repaired Title"


def test_content_plan_fallback_on_plan_failure(monkeypatch, default_registry):
    def exploding_invoke_llm_json(*_args, **_kwargs):
        raise RuntimeError("LLM gateway down")

    monkeypatch.setattr("app.services.llm_gateway.invoke_llm_json", exploding_invoke_llm_json)
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm",
        lambda **_kwargs: SimpleNamespace(answer="Fallback body", model_used="m", latency_ms=0, estimated_cost_usd=0.0),
    )
    monkeypatch.setattr(
        "app.services.agent_runtime.tool_runner.ToolRunner.run",
        lambda self, *a, **kw: SimpleNamespace(latency_ms=0, output={"docx_base64": "", "filename": "t.docx"}),
    )

    msg = "Create a strategy document"
    state = _make_doc_state(msg)
    result = DocumentAgent(default_registry).run(state, SimpleNamespace(plan={}))

    assert result.title == msg[:120]
    assert result.markdown == "Fallback body"


def test_deck_designer_registered(default_registry):
    agent = default_registry.agent("deck_designer")

    assert agent.id == "deck_designer"
    assert agent.model_policy_id == "model.executive"
    assert agent.allowed_tools == []


def test_evidence_binder_and_content_strategist_registered(default_registry):
    binder = default_registry.agent("evidence_binder")
    strategist = default_registry.agent("content_strategist")

    assert binder.id == "evidence_binder"
    assert binder.judge_policy_id is None
    assert strategist.id == "content_strategist"
    assert strategist.judge_policy_id == "document_judge"
