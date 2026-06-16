import json
from types import SimpleNamespace

import pytest

from app.services.agent_runtime.fixtures import PromptFixtureRunner
from app.services.agent_runtime.guardrails import GuardrailDecision, GuardrailService
from app.services.agent_runtime.registry import _load_from_files, load_default_registry
from app.services.agent_runtime.research_agent import ResearchAgent, ResearchResult
from app.services.agent_runtime.tool_runner import (
    MAX_CONTENT_CHARS,
    ToolCallResult,
    ToolNotPermittedError,
    ToolRunner,
)
from app.services.llm_gateway import LLMResult
from app.services.turn_graph import graph as turn_graph
from app.services.turn_graph.state import TurnGraphState
from app.services.web_context import WebSource


def _state(message: str = "Research this") -> TurnGraphState:
    return TurnGraphState(user_message=message, user_id="u1", turn_id="t1", conversation_id="c1")


def _llm(answer: str = "Research answer") -> LLMResult:
    return LLMResult(
        answer=answer,
        model_used="test-model",
        latency_ms=10,
        prompt_tokens=1,
        completion_tokens=1,
        estimated_cost_usd=0.001,
    )


def _tool_runner(agent_id: str = "research_lead", guardrail_service=None) -> ToolRunner:
    registry = _load_from_files()
    return ToolRunner(
        registry=registry,
        agent_id=agent_id,
        guardrail_service=guardrail_service or GuardrailService(registry),
    )


def test_tool_runner_unknown_tool_raises_not_permitted():
    with pytest.raises(ToolNotPermittedError):
        _tool_runner().run("nonexistent", {}, state=_state())


def test_tool_runner_agent_not_in_allowed_list_raises_not_permitted():
    with pytest.raises(ToolNotPermittedError):
        _tool_runner(agent_id="orchestrator").run("web_search", {"query": "test"}, state=_state())


def test_tool_runner_web_search_calls_search_web_sources(monkeypatch):
    monkeypatch.setattr(
        "app.services.web_context.search_web_sources",
        lambda query, recency=None: ("FakeSearch", [WebSource("Fake title", "https://example.com", "Fake content")]),
    )

    result = _tool_runner().run("web_search", {"query": "test"}, state=_state())

    assert result.tool_name == "web_search"
    assert result.output["sources"][0]["title"] == "Fake title"
    assert result.output["provider"] == "FakeSearch"


def test_tool_runner_sanitizes_long_content(monkeypatch):
    monkeypatch.setattr(
        "app.services.web_context.search_web_sources",
        lambda query, recency=None: ("FakeSearch", [WebSource("Long", "https://example.com", "x" * 5000)]),
    )

    result = _tool_runner().run("web_search", {"query": "test"}, state=_state())

    assert len(result.output["sources"][0]["content"]) <= MAX_CONTENT_CHARS


def test_tool_runner_pre_guardrail_block_raises_not_permitted():
    class BlockingGuardrails:
        def evaluate_boundary(self, *_args, **_kwargs):
            return [GuardrailDecision("p1", "block", ["ssrf"], "ssrf")]

    with pytest.raises(ToolNotPermittedError):
        _tool_runner(guardrail_service=BlockingGuardrails()).run("web_search", {"query": "test"}, state=_state())


def test_research_agent_runs_queries_and_synthesizes(monkeypatch):
    monkeypatch.setattr(
        "app.services.web_context.search_web_sources",
        lambda query, recency=None: (
            "FakeSearch",
            [
                WebSource("One", "https://one.example", "A"),
                WebSource("Two", "https://two.example", "B"),
                WebSource("Three", "https://three.example", "C"),
            ],
        ),
    )
    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", lambda **_kwargs: _llm("Research answer"))
    decision = SimpleNamespace(plan={"search_queries": ["q1"]})

    result = ResearchAgent(_load_from_files()).run(_state(), decision)

    assert result.answer == "Research answer"
    assert len(result.tool_calls) == 1
    assert len(result.sources) == 3


def test_research_agent_falls_back_to_user_message_query_when_plan_empty(monkeypatch):
    captured = {}

    def fake_search(query, recency=None):
        captured["query"] = query
        return "FakeSearch", [WebSource("One", "https://one.example", "A")]

    monkeypatch.setattr("app.services.web_context.search_web_sources", fake_search)
    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", lambda **_kwargs: _llm("Research answer"))

    ResearchAgent(_load_from_files()).run(_state("Original user query"), SimpleNamespace(plan={}))

    assert captured["query"] == "Original user query"


def test_research_agent_handles_tool_failure_gracefully(monkeypatch):
    def fail_search(query, recency=None):
        raise RuntimeError("search down")

    monkeypatch.setattr("app.services.web_context.search_web_sources", fail_search)
    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", lambda **_kwargs: _llm("Fallback synthesis"))

    result = ResearchAgent(_load_from_files()).run(_state(), SimpleNamespace(plan={"search_queries": ["q1"]}))

    assert result.answer == "Fallback synthesis"
    assert result.tool_calls == []


def test_orchestrator_node_research_route_returns_handled_true(monkeypatch):
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm_json",
        lambda *_args, **_kwargs: _llm('{"route":"research","reasoning":"current","plan":{"search_queries":["q1"]}}'),
    )
    monkeypatch.setattr(
        "app.services.agent_runtime.research_agent.ResearchAgent.run",
        lambda self, state, decision: ResearchResult(
            answer="Research",
            sources=[{"title": "One", "url": "https://one.example"}],
            tool_calls=[],
            model_used="test-model",
            prompt_id="prompt.research_lead.default",
            latency_ms=12,
            cost_usd=0.001,
        ),
    )
    monkeypatch.setattr(turn_graph, "load_default_registry", _load_from_files)

    state, handled = turn_graph._orchestrator_node(_state(), SimpleNamespace())

    assert handled is True
    assert state.final_answer == "Research"


def test_orchestrator_node_document_route_falls_through(monkeypatch):
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm_json",
        lambda *_args, **_kwargs: _llm('{"route":"document","reasoning":"artifact"}'),
    )
    monkeypatch.setattr(turn_graph, "load_default_registry", _load_from_files)

    state, handled = turn_graph._orchestrator_node(_state(), SimpleNamespace())

    assert handled is False
    assert state.triage_decision["route"] == "document"


def test_fixture_runner_live_eval_response_contains_passes(monkeypatch, tmp_path):
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "prompt.orchestrator.default.json").write_text(json.dumps([
        {
            "scenario": "contains route",
            "input": {
                "user_message": "hi",
                "conversation_context": "",
                "runtime_budget": {},
                "available_tools": ["answer_directly"],
            },
            "expect": {"response_contains": "direct_answer"},
        }
    ]))
    monkeypatch.setattr("app.services.agent_runtime.fixtures.FIXTURES_DIR", fixture_dir)
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm_json",
        lambda *_args, **_kwargs: _llm('{"route":"direct_answer","selected_tools":["answer_directly"]}'),
    )

    summary = PromptFixtureRunner(load_default_registry()).run("prompt.orchestrator.default", live=True)

    assert summary.all_passed is True


def test_fixture_runner_live_eval_response_contains_fails(monkeypatch, tmp_path):
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "prompt.orchestrator.default.json").write_text(json.dumps([
        {
            "scenario": "missing text",
            "input": {
                "user_message": "hi",
                "conversation_context": "",
                "runtime_budget": {},
                "available_tools": ["answer_directly"],
            },
            "expect": {"response_contains": "xyz_not_present"},
        }
    ]))
    monkeypatch.setattr("app.services.agent_runtime.fixtures.FIXTURES_DIR", fixture_dir)
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm_json",
        lambda *_args, **_kwargs: _llm('{"route":"direct_answer","selected_tools":["answer_directly"]}'),
    )

    summary = PromptFixtureRunner(load_default_registry()).run("prompt.orchestrator.default", live=True)

    assert summary.all_passed is False


def test_fixture_runner_live_model_failure_marks_scenario_failed(monkeypatch, tmp_path):
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "prompt.orchestrator.default.json").write_text(json.dumps([
        {
            "scenario": "model down",
            "input": {
                "user_message": "hi",
                "conversation_context": "",
                "runtime_budget": {},
                "available_tools": ["answer_directly"],
            },
            "expect": {"tool_called": "answer_directly"},
        }
    ]))
    monkeypatch.setattr("app.services.agent_runtime.fixtures.FIXTURES_DIR", fixture_dir)

    def fail(*_args, **_kwargs):
        raise RuntimeError("model down")

    monkeypatch.setattr("app.services.llm_gateway.invoke_llm_json", fail)

    summary = PromptFixtureRunner(load_default_registry()).run("prompt.orchestrator.default", live=True)

    assert summary.all_passed is False
    assert all(result.passed is False for result in summary.results)
