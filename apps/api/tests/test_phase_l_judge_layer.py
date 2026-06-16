import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.services.agent_runtime.document_agent import DocumentAgent
from app.services.agent_runtime.judge_service import JudgeService
from app.services.agent_runtime.models import JudgePolicy, JudgeResult
from app.services.agent_runtime.native_backends import register_all
from app.services.agent_runtime.registry import _load_from_files
from app.services.agent_runtime.research_agent import ResearchAgent
from app.services.turn_graph.state import TurnGraphState
from app.services.web_context import WebSource


def _state(message: str = "Research AI governance trends") -> TurnGraphState:
    return TurnGraphState(user_message=message, user_id="u1", turn_id="t1", conversation_id="c1")


def _decision(plan: dict | None = None):
    return SimpleNamespace(plan=plan or {})


def _llm(answer: str, *, latency_ms: int = 10):
    return SimpleNamespace(
        answer=answer,
        model_used="test-model",
        latency_ms=latency_ms,
        estimated_cost_usd=0.001,
    )


def _judge_result(status: str = "pass", score: float = 0.9) -> JudgeResult:
    return JudgeResult(
        id="judge-1",
        target_type="research",
        target_id="t1",
        judge_agent_id="research_judge",
        score=score,
        status=status,
        issues=[],
        required_repairs=[],
        can_publish=(status == "pass"),
    )


def test_judge_policy_rejects_repair_above_pass():
    with pytest.raises(ValidationError):
        JudgePolicy(
            id="j",
            name="Judge",
            target_type="research",
            model_policy_id="model.executive",
            criteria=["Check quality."],
            pass_threshold=0.6,
            repair_threshold=0.7,
        )


def test_judge_policy_valid():
    policy = JudgePolicy(
        id="j",
        name="Judge",
        target_type="document",
        model_policy_id="model.executive",
        criteria=["Check quality."],
        pass_threshold=0.75,
        repair_threshold=0.5,
    )

    assert policy.enabled is True
    assert policy.version == "1.0.0"


def test_judge_service_returns_pass_when_score_above_threshold(monkeypatch):
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm",
        lambda **_kwargs: _llm('{"score": 0.9, "issues": [], "required_repairs": []}'),
    )

    result = JudgeService(_load_from_files()).evaluate("research_judge", content="Good answer.", context={})

    assert result.status == "pass"
    assert result.can_publish is True


def test_judge_service_returns_repair_when_score_between_thresholds(monkeypatch):
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm",
        lambda **_kwargs: _llm(json.dumps({
            "score": 0.6,
            "issues": [{"type": "gap", "message": "Missing source citation."}],
            "required_repairs": [{"section": "intro", "instruction": "Add citation."}],
        })),
    )

    result = JudgeService(_load_from_files()).evaluate("research_judge", content="Needs work.", context={})

    assert result.status == "repair"
    assert result.can_publish is False
    assert len(result.required_repairs) == 1


def test_judge_service_returns_fail_when_score_below_repair_threshold(monkeypatch):
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm",
        lambda **_kwargs: _llm(json.dumps({
            "score": 0.3,
            "issues": [{"type": "accuracy", "message": "Hallucinated facts."}],
            "required_repairs": [],
        })),
    )

    result = JudgeService(_load_from_files()).evaluate("research_judge", content="Bad answer.", context={})

    assert result.status == "fail"
    assert result.can_publish is False


def test_judge_service_returns_pass_on_disabled_policy(monkeypatch):
    registry = _load_from_files()
    registry.judges["research_judge"].enabled = False

    def fail_if_called(**_kwargs):
        raise AssertionError("invoke_llm should not be called for disabled policies")

    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", fail_if_called)

    result = JudgeService(registry).evaluate("research_judge", content="Any answer.", context={})

    assert result.status == "pass"
    assert result.score == 1.0


def test_judge_service_returns_fail_on_llm_exception(monkeypatch):
    def raise_network(**_kwargs):
        raise RuntimeError("network error")

    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", raise_network)

    result = JudgeService(_load_from_files()).evaluate("research_judge", content="Answer.", context={})

    assert result.status == "fail"
    assert result.issues[0]["type"] == "judge_error"


def test_registry_judge_lookup_returns_correct_policy():
    registry = _load_from_files()

    assert registry.judge("research_judge").id == "research_judge"
    with pytest.raises(KeyError):
        registry.judge("nonexistent")


def test_research_agent_calls_judge_after_synthesis(monkeypatch):
    _patch_research_run(monkeypatch)
    calls: list[tuple[str, str]] = []

    def fake_evaluate(self, policy_id, *, content, context=None, target_id=None):
        del self, context, target_id
        calls.append((policy_id, content))
        return _judge_result()

    monkeypatch.setattr("app.services.agent_runtime.research_agent.JudgeService.evaluate", fake_evaluate)

    result = ResearchAgent(_load_from_files()).run(_state(), _decision())

    assert result.answer == "Synthesized answer from claims."
    assert calls == [("research_judge", "Synthesized answer from claims.")]


def test_document_agent_calls_judge_after_plan(monkeypatch):
    register_all()
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        "app.services.agent_runtime.document_agent.DocumentAgent._plan",
        lambda self, state, brand_profile, quality_mode: _llm(
            '{"document_brief":{"title":"Board Memo","doc_type":"memo"}}'
        ),
    )
    monkeypatch.setattr(
        "app.services.agent_runtime.document_agent.DocumentAgent._generate_content",
        lambda self, state, brief, decision, grammar=None, research_summary=None: _llm("# Board Memo\n\nContent."),
    )
    monkeypatch.setattr("app.services.document_generator.generate_docx_bytes", lambda *args, **kwargs: b"DOCX")

    def fake_evaluate(self, policy_id, *, content, context=None, target_id=None):
        del self, context, target_id
        calls.append((policy_id, content))
        return JudgeResult(
            id="judge-2",
            target_type="document",
            target_id="t1",
            judge_agent_id="document_judge",
            score=0.9,
            status="pass",
            issues=[],
            required_repairs=[],
            can_publish=True,
        )

    monkeypatch.setattr("app.services.agent_runtime.document_agent.JudgeService.evaluate", fake_evaluate)

    result = DocumentAgent(_load_from_files()).run(_state("Create a board memo"), _decision())

    assert result.title == "Board Memo"
    assert len(calls) == 2
    assert calls[0][0] == "document_judge"
    assert '"title": "Board Memo"' in calls[0][1]
    assert calls[1] == ("document_judge", "# Board Memo\n\nContent.")


def _patch_research_run(monkeypatch) -> None:
    llm_answers = iter([
        '{"search_queries": ["q1", "q2"]}',
        (
            '{"claims": ['
            '{"text": "Fact A.", "source_url": "https://8.8.8.8/a", "confidence": 0.9},'
            '{"text": "Fact B.", "source_url": "https://8.8.4.4/b", "confidence": 0.8},'
            '{"text": "Fact C.", "source_url": "https://1.1.1.1/c", "confidence": 0.7}'
            ']}'
        ),
        "Synthesized answer from claims.",
    ])

    def fake_invoke_llm(**_kwargs):
        return _llm(next(llm_answers))

    def fake_search_web_sources(query, recency=None):
        del recency
        if query == "q1":
            return (
                "FakeSearch",
                [
                    WebSource("A", "https://8.8.8.8/a", "summary a"),
                    WebSource("B", "https://8.8.4.4/b", "summary b"),
                ],
            )
        return (
            "FakeSearch",
            [
                WebSource("B Duplicate", "https://8.8.4.4/b", "summary b"),
                WebSource("C", "https://1.1.1.1/c", "summary c"),
            ],
        )

    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", fake_invoke_llm)
    monkeypatch.setattr("app.services.web_context.search_web_sources", fake_search_web_sources)
    monkeypatch.setattr(
        "app.services.web_context.crawl_url",
        lambda url: WebSource(f"Read {url}", url, f"full content for {url}"),
    )
