from types import SimpleNamespace

import pytest

from app.services.agent_runtime.document_agent import DocumentAgent
from app.services.agent_runtime.registry import _load_from_files
from app.services.agent_runtime.research_agent import ResearchAgent
from app.services.agent_runtime.sub_agent_runner import SubAgentRunner
from app.services.turn_graph.state import TurnGraphState


@pytest.fixture
def default_registry():
    return _load_from_files()


def _llm(answer: str = "ok"):
    return SimpleNamespace(answer=answer, model_used="m", latency_ms=5, estimated_cost_usd=0.0)


def test_sub_agent_runner_init_resolves_agent_and_policy(default_registry):
    runner = SubAgentRunner("evidence_binder", default_registry)

    assert runner.agent_id == "evidence_binder"
    assert runner.agent_def.id == "evidence_binder"
    assert runner.model_policy.id == "model.executive"
    assert runner.prompt_def.id == "prompt.evidence_binder.default"


def test_sub_agent_runner_unknown_agent_raises(default_registry):
    with pytest.raises(KeyError):
        SubAgentRunner("missing_agent", default_registry)


def test_sub_agent_runner_build_messages_system_only(default_registry):
    runner = SubAgentRunner("evidence_binder", default_registry)

    messages = runner.build_messages("write the doc")

    assert messages == [
        {"role": "system", "content": runner.system_prompt},
        {"role": "user", "content": "write the doc"},
    ]


def test_sub_agent_runner_build_messages_with_developer_prompt(default_registry):
    default_registry.prompts["prompt.evidence_binder.default"] = default_registry.prompt(
        "prompt.evidence_binder.default"
    ).model_copy(update={"developer_prompt": "Use concise prose."})
    runner = SubAgentRunner("evidence_binder", default_registry)

    messages = runner.build_messages("write the doc")

    assert messages[0]["role"] == "system"
    assert messages[1] == {
        "role": "developer" if runner.is_claude else "system",
        "content": "Use concise prose.",
    }
    assert messages[2] == {"role": "user", "content": "write the doc"}


def test_sub_agent_runner_invoke_routes_through_llm_gateway(monkeypatch, default_registry):
    captured = {}

    def fake_invoke_llm(**kwargs):
        captured.update(kwargs)
        return _llm("answer")

    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", fake_invoke_llm)
    runner = SubAgentRunner("research_synthesizer", default_registry)

    result = runner.invoke("Question?", web_context="Evidence")

    assert result.answer == "answer"
    assert captured["message"] == "Question?"
    assert captured["web_context"] == "Evidence"
    assert captured["route"].primary_model == runner.model_policy.primary_model


def test_sub_agent_runner_invoke_json_routes_through_llm_gateway(monkeypatch, default_registry):
    captured = {}

    def fake_invoke_llm_json(messages, route):
        captured["messages"] = messages
        captured["route"] = route
        return _llm('{"ok": true}')

    monkeypatch.setattr("app.services.llm_gateway.invoke_llm_json", fake_invoke_llm_json)
    runner = SubAgentRunner("content_strategist", default_registry)
    messages = runner.build_messages("plan")

    result = runner.invoke_json(messages)

    assert result.answer == '{"ok": true}'
    assert captured["messages"] == messages
    assert captured["route"].primary_model == runner.model_policy.primary_model


def test_sub_agent_runner_tool_runner_scoped_to_agent_id(monkeypatch, default_registry):
    captured = {}

    def fake_run(self, tool_name, args, *, state, plan=None):
        captured["agent_id"] = self.agent_id
        captured["tool_name"] = tool_name
        captured["args"] = args
        captured["state"] = state
        captured["plan"] = plan
        return SimpleNamespace(latency_ms=1, output={"docx_base64": "ZA=="})

    monkeypatch.setattr("app.services.agent_runtime.tool_runner.ToolRunner.run", fake_run)
    state = TurnGraphState(user_message="doc", user_id="u1")
    runner = SubAgentRunner("evidence_binder", default_registry)

    result = runner.run_tool("generate_document", {"title": "T"}, state=state, plan={"p": True})

    assert result.output["docx_base64"] == "ZA=="
    assert captured["agent_id"] == "evidence_binder"
    assert captured["tool_name"] == "generate_document"
    assert captured["args"] == {"title": "T"}
    assert captured["state"] is state
    assert captured["plan"] == {"p": True}


def test_resynthesize_uses_research_synthesizer_not_direct_llm(monkeypatch, default_registry):
    agent_ids: list[str] = []

    def fake_invoke(self, message, **kwargs):
        agent_ids.append(self.agent_id)
        assert "REVISION REQUIRED" in kwargs["web_context"]
        return _llm("Repaired research")

    monkeypatch.setattr("app.services.agent_runtime.sub_agent_runner.SubAgentRunner.invoke", fake_invoke)
    state = TurnGraphState(user_message="Research this", turn_id="t1")
    state.research_claims = [{"text": "Claim.", "source_url": "https://example.com", "confidence": 0.8}]

    result = ResearchAgent(default_registry)._resynthesize_with_repairs(
        state,
        SimpleNamespace(plan={}),
        [{"section": "citations", "instruction": "Add citations"}],
    )

    assert result.answer == "Repaired research"
    assert agent_ids == ["research_synthesizer"]


def test_regenerate_uses_evidence_binder_for_docx(monkeypatch, default_registry):
    agent_ids: list[str] = []
    tool_agents: list[str] = []

    def fake_invoke(self, message, **kwargs):
        agent_ids.append(self.agent_id)
        assert "REVISION REQUIRED" in kwargs["doc_context"]
        return _llm("Repaired doc")

    def fake_run_tool(self, tool_name, args, *, state, plan=None):
        tool_agents.append(self.agent_id)
        assert tool_name == "generate_document"
        return SimpleNamespace(latency_ms=2, output={"docx_base64": "ZA==", "filename": "doc.docx"})

    monkeypatch.setattr("app.services.agent_runtime.sub_agent_runner.SubAgentRunner.invoke", fake_invoke)
    monkeypatch.setattr("app.services.agent_runtime.sub_agent_runner.SubAgentRunner.run_tool", fake_run_tool)
    state = TurnGraphState(user_message="Write doc", user_id="u1", turn_id="t2")

    content, tool_result = DocumentAgent(default_registry)._regenerate_with_repairs(
        state,
        {"title": "Doc", "doc_type": "executive_report"},
        [{"section": "body", "instruction": "Add depth"}],
        False,
        SimpleNamespace(plan={}),
        None,
        None,
        None,
    )

    assert content.answer == "Repaired doc"
    assert tool_result["docx_base64"] == "ZA=="
    assert agent_ids == ["evidence_binder"]
    assert tool_agents == ["evidence_binder"]


def test_regenerate_uses_deck_designer_for_pptx(monkeypatch, default_registry):
    agent_ids: list[str] = []
    tool_agents: list[str] = []

    def fake_invoke(self, message, **kwargs):
        agent_ids.append(self.agent_id)
        assert "REVISION REQUIRED" in kwargs["doc_context"]
        return _llm("## Slide 1")

    def fake_run_tool(self, tool_name, args, *, state, plan=None):
        tool_agents.append(self.agent_id)
        assert tool_name == "render_pptx"
        return SimpleNamespace(latency_ms=3, output={"pptx_base64": "UFBUWA==", "filename": "deck.pptx"})

    monkeypatch.setattr("app.services.agent_runtime.sub_agent_runner.SubAgentRunner.invoke", fake_invoke)
    monkeypatch.setattr("app.services.agent_runtime.sub_agent_runner.SubAgentRunner.run_tool", fake_run_tool)
    state = TurnGraphState(user_message="Make deck", user_id="u1", turn_id="t3")

    content, tool_result = DocumentAgent(default_registry)._regenerate_with_repairs(
        state,
        {"title": "Deck", "doc_type": "presentation"},
        [{"section": "slides", "instruction": "Improve storyline"}],
        True,
        SimpleNamespace(plan={}),
        {"mode": "template_following"},
        "Research summary",
        "template-1",
    )

    assert content.answer == "## Slide 1"
    assert tool_result["pptx_base64"] == "UFBUWA=="
    assert agent_ids == ["deck_designer"]
    assert tool_agents == ["deck_designer"]
