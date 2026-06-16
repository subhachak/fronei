import time
from types import SimpleNamespace

from app.services.agent_runtime.guardrails import GuardrailService
from app.services.agent_runtime.registry import _load_from_files
from app.services.agent_runtime import research_agent as research_agent_module
from app.services.agent_runtime.research_agent import ResearchAgent
from app.services.agent_runtime.tool_runner import ToolRunner
from app.services.agent_runtime.utils import strip_json_fence
from app.services.turn_graph.state import TurnGraphState
from app.services.web_context import WebSource


def _state(message: str = "Research AI governance trends") -> TurnGraphState:
    return TurnGraphState(user_message=message, user_id="u1", turn_id="t1", conversation_id="c1")


def _decision(plan: dict | None = None):
    return SimpleNamespace(plan=plan or {})


def _llm(answer: str, *, latency_ms: int = 50):
    return SimpleNamespace(
        answer=answer,
        model_used="test",
        latency_ms=latency_ms,
        estimated_cost_usd=0.001,
    )


def _agent() -> ResearchAgent:
    return ResearchAgent(_load_from_files())


def _tool_runner() -> ToolRunner:
    registry = _load_from_files()
    return ToolRunner(registry, "research_lead", GuardrailService(registry))


def test_decompose_writes_queries_from_llm_json(monkeypatch):
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm",
        lambda **_kwargs: _llm('{"search_queries": ["q1", "q2"]}'),
    )
    state = _state()

    _agent()._decompose(state, _decision(), [])

    assert state.research_queries == ["q1", "q2"]


def test_decompose_falls_back_on_parse_failure(monkeypatch):
    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", lambda **_kwargs: _llm("not json"))
    state = _state()

    _agent()._decompose(state, _decision({"search_queries": ["fallback_q"]}), [])

    assert state.research_queries == ["fallback_q"]


def test_decompose_times_out_and_uses_fallback_queries(monkeypatch):
    monkeypatch.setattr(research_agent_module, "QUERY_DECOMPOSITION_TIMEOUT_SECONDS", 0.01)

    def slow_invoke_llm(**_kwargs):
        time.sleep(0.1)
        return _llm('{"search_queries": ["too_late"]}')

    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", slow_invoke_llm)
    state = _state()

    _agent()._decompose(state, _decision({"search_queries": ["fallback_q"]}), [])

    assert state.research_queries == ["fallback_q"]


def test_strip_json_fence_handles_leading_whitespace():
    raw = '  ```json\n{"ok": true}\n```'

    assert strip_json_fence(raw) == '{"ok": true}'


def test_scout_deduplicates_by_url(monkeypatch):
    def fake_search_web_sources(query, recency=None):
        return (
            "FakeSearch",
            [
                WebSource("Shared", "https://8.8.8.8/shared", "shared"),
                WebSource(f"Unique {query}", f"https://8.8.4.4/{query}", "unique"),
            ],
        )

    monkeypatch.setattr("app.services.web_context.search_web_sources", fake_search_web_sources)
    state = _state()
    tool_calls = []

    _agent()._scout(state, ["q1", "q2"], _tool_runner(), tool_calls)

    assert len(state.research_sources) == 3
    assert len({source["url"] for source in state.research_sources}) == 3
    assert [call.tool_name for call in tool_calls] == ["web_search", "web_search"]


def test_crawl_enriches_sources_with_content(monkeypatch):
    monkeypatch.setattr(
        "app.services.web_context.crawl_url",
        lambda url: WebSource(f"Read {url}", url, "body text"),
    )
    state = _state()
    state.research_sources = [
        {"url": "https://8.8.8.8/a", "title": "A"},
        {"url": "https://8.8.4.4/b", "title": "B"},
        {"url": "https://1.1.1.1/c", "title": "C"},
        {"url": "https://9.9.9.9/d", "title": "D"},
    ]

    _agent()._crawl(state, _tool_runner(), [])

    assert state.research_sources[0]["content"] == "body text"
    assert state.research_sources[2]["content"] == "body text"
    assert "content" not in state.research_sources[3]


def test_extract_writes_claims_from_llm_json(monkeypatch):
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm",
        lambda **_kwargs: _llm(
            '{"claims": [{"text": "Fact A.", "source_url": "https://8.8.8.8/a", "confidence": 0.9}]}'
        ),
    )
    state = _state()
    state.research_sources = [{"url": "https://8.8.8.8/a", "title": "A", "content": "text about topic"}]

    _agent()._extract(state)

    assert state.research_claims[0]["text"] == "Fact A."
    assert state.research_claims[0]["confidence"] == 0.9


def test_sufficiency_passes_with_enough_claims_and_sources():
    state = _state()
    state.research_claims = [{"text": "A"}, {"text": "B"}, {"text": "C"}]
    state.research_sources = [
        {"url": "https://8.8.8.8/a", "content": "a"},
        {"url": "https://8.8.4.4/b", "content": "b"},
    ]

    result = _agent()._check_sufficiency(state)

    assert result["sufficient"] is True


def test_sufficiency_fails_with_no_claims():
    state = _state()
    state.research_claims = []
    state.research_sources = [
        {"url": f"https://8.8.8.8/{index}", "content": "body"}
        for index in range(5)
    ]

    result = _agent()._check_sufficiency(state)

    assert result["sufficient"] is False


def test_run_records_all_seven_stage_events(monkeypatch):
    _patch_full_run(monkeypatch, final_answer="Synthesized answer from claims.")

    state = _state()
    _agent().run(state, _decision())

    nodes = {event.node for event in state.events}
    assert {
        "research.decompose",
        "research.search",
        "research.crawl",
        "research.extract",
        "research.sufficiency",
        "research.synthesize",
        "research.verify",
    }.issubset(nodes)


def test_run_tool_calls_contain_web_search_and_read_url(monkeypatch):
    _patch_full_run(monkeypatch, final_answer="Synthesized answer from claims.")

    result = _agent().run(_state(), _decision())

    assert {"web_search", "read_url"}.issubset({call.tool_name for call in result.tool_calls})


def test_run_answer_sourced_from_claims_synthesis(monkeypatch):
    _patch_full_run(monkeypatch, final_answer="Synthesized answer from claims.")

    result = _agent().run(_state(), _decision())

    assert result.answer == "Synthesized answer from claims."
    assert len(result.sources) == 3


def _patch_full_run(monkeypatch, *, final_answer: str) -> None:
    llm_answers = iter([
        '{"search_queries": ["q1", "q2"]}',
        (
            '{"claims": ['
            '{"text": "Fact A.", "source_url": "https://8.8.8.8/a", "confidence": 0.9},'
            '{"text": "Fact B.", "source_url": "https://8.8.4.4/b", "confidence": 0.8},'
            '{"text": "Fact C.", "source_url": "https://1.1.1.1/c", "confidence": 0.7}'
            ']}'
        ),
        final_answer,
    ])

    def fake_invoke_llm(**_kwargs):
        return _llm(next(llm_answers))

    def fake_search_web_sources(query, recency=None):
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
