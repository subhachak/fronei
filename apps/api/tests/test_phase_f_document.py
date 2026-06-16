import base64
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import AgentRunLog, AgentStep, Base
from app.services.agent_runtime.document_agent import DocumentAgent, DocumentResult
from app.services.agent_runtime.guardrails import GuardrailService
from app.services.agent_runtime.models import ToolDefinition
from app.services.agent_runtime.native_backends import _generate_document_output, register_all
from app.services.agent_runtime.registry import _load_from_files
from app.services.agent_runtime.research_agent import ResearchAgent
from app.services.agent_runtime.tool_runner import (
    ToolExecutionError,
    ToolRunner,
    register_native_backend,
)
from app.services.llm_gateway import LLMResult
from app.services.turn_graph import graph as turn_graph
from app.services.turn_graph.state import TurnGraphState
from app.services.web_context import WebSource


def _state(message: str = "Create a document") -> TurnGraphState:
    return TurnGraphState(user_message=message, user_id="u1", turn_id="t1", conversation_id="c1")


def _llm(answer: str, *, latency_ms: int = 10) -> LLMResult:
    return LLMResult(
        answer=answer,
        model_used="test-model",
        latency_ms=latency_ms,
        prompt_tokens=1,
        completion_tokens=1,
        estimated_cost_usd=0.001,
    )


def _registry_with_tool(tool: ToolDefinition):
    registry = _load_from_files()
    registry.tools[tool.id] = tool
    return registry


def _native_tool(tool_id: str, ref: str) -> ToolDefinition:
    return ToolDefinition(
        id=tool_id,
        name=tool_id,
        description="Test native tool",
        input_schema={},
        output_schema={},
        allowed_agent_ids=["document_lead"],
        guardrail_policy_ids=[],
        backend="native",
        backend_ref=ref,
    )


def test_register_and_dispatch_native_backend():
    register_native_backend("test.ref.phase_f", lambda inputs: {"ok": True, "echo": inputs.get("value")})
    registry = _registry_with_tool(_native_tool("test_native", "test.ref.phase_f"))

    result = ToolRunner(registry, "document_lead", GuardrailService(registry)).run(
        "test_native",
        {"value": "yes"},
        state=_state(),
    )

    assert result.output == {"ok": True, "echo": "yes"}


def test_unregistered_native_backend_raises_tool_execution_error():
    registry = _registry_with_tool(_native_tool("native_tool", "unknown.phase_f.ref"))

    with pytest.raises(ToolExecutionError):
        ToolRunner(registry, "document_lead", GuardrailService(registry)).run(
            "native_tool",
            {},
            state=_state(),
        )


def test_generate_document_native_backend_produces_docx(monkeypatch):
    monkeypatch.setattr("app.services.document_generator.generate_docx_bytes", lambda *args, **kwargs: b"DOCX")

    result = _generate_document_output({"title": "Test", "content": "# Hello", "doc_type": "memo"})

    assert base64.b64decode(result["docx_base64"]) == b"DOCX"
    assert result["filename"].endswith(".docx")


def test_document_agent_planning_call_extracts_brief(monkeypatch):
    register_all()
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm_json",
        lambda *_args, **_kwargs: _llm(
            '{"document_brief":{"title":"Q3 Review","doc_type":"executive_report"}}',
            latency_ms=5,
        ),
    )
    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", lambda **_kwargs: _llm("# Q3 Review\n\nContent."))
    monkeypatch.setattr("app.services.document_generator.generate_docx_bytes", lambda *args, **kwargs: b"DOCX")

    result = DocumentAgent(_load_from_files()).run(_state("Create Q3 review"), SimpleNamespace(plan={}))

    assert result.title == "Q3 Review"
    assert result.doc_type == "executive_report"
    assert result.markdown.startswith("# Q3 Review")


def test_document_agent_with_unowned_template_id_falls_back(monkeypatch):
    register_all()
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm_json",
        lambda *_args, **_kwargs: _llm('{"document_brief":{"title":"Safe","doc_type":"memo"}}'),
    )
    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", lambda **_kwargs: _llm("# Safe\n\nContent."))
    monkeypatch.setattr("app.services.agent_runtime.guardrails._template_belongs_to_user_db", lambda *_args: False)
    decision = SimpleNamespace(plan={"brand_profile": {"template_id": "user-abc-123"}})

    result = DocumentAgent(_load_from_files()).run(_state(), decision)

    assert result.doc_type == "memo"
    assert result.docx_base64 == ""
    assert result.markdown.startswith("# Safe")


def test_document_agent_handles_planning_json_parse_failure(monkeypatch):
    register_all()
    monkeypatch.setattr("app.services.llm_gateway.invoke_llm_json", lambda *_args, **_kwargs: _llm("not json at all"))
    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", lambda **_kwargs: _llm("# Fallback\n\nContent."))
    monkeypatch.setattr("app.services.document_generator.generate_docx_bytes", lambda *args, **kwargs: b"DOCX")

    result = DocumentAgent(_load_from_files()).run(_state("Fallback title"), SimpleNamespace(plan={}))

    assert result.title == "Fallback title"
    assert result.markdown


def test_document_agent_handles_generate_document_tool_failure(monkeypatch):
    register_all()
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm_json",
        lambda *_args, **_kwargs: _llm('{"document_brief":{"title":"Broken","doc_type":"memo"}}'),
    )
    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", lambda **_kwargs: _llm("# Broken\n\nContent."))

    def fail_render(*_args, **_kwargs):
        raise RuntimeError("render failed")

    monkeypatch.setattr("app.services.document_generator.generate_docx_bytes", fail_render)

    result = DocumentAgent(_load_from_files()).run(_state(), SimpleNamespace(plan={}))

    assert result.docx_base64 == ""
    assert result.markdown.startswith("# Broken")


def test_orchestrator_node_document_route_returns_handled_true(monkeypatch):
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm_json",
        lambda *_args, **_kwargs: _llm('{"route":"document","reasoning":"artifact","selected_tools":["generate_document"]}'),
    )
    monkeypatch.setattr(
        "app.services.agent_runtime.document_agent.DocumentAgent.run",
        lambda self, state, decision: DocumentResult(
            title="Test",
            doc_type="memo",
            markdown="# Test",
            docx_base64="RE9DWA==",
            filename="test.docx",
            model_used="test-model",
            prompt_id="prompt.document_lead.default",
            planning_latency_ms=1,
            content_latency_ms=2,
            latency_ms=3,
            cost_usd=0.001,
        ),
    )
    monkeypatch.setattr(turn_graph, "load_default_registry", _load_from_files)

    state, handled = turn_graph._orchestrator_node(_state(), SimpleNamespace())

    assert handled is True
    assert state.final_answer == "# Test"
    assert state.document_result["title"] == "Test"


def test_write_orchestrator_trace_writes_three_document_steps(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        decision = SimpleNamespace(
            run_id="orch-doc",
            cost_usd=0.001,
            latency_ms=10,
            model_used="model-o",
            prompt_id="prompt.orchestrator.default",
            route="document",
        )
        doc_result = DocumentResult(
            title="Doc",
            doc_type="memo",
            markdown="# Doc",
            docx_base64="RE9DWA==",
            filename="doc.docx",
            model_used="model-d",
            prompt_id="prompt.document_lead.default",
            planning_latency_ms=3,
            content_latency_ms=7,
            latency_ms=15,
            cost_usd=0.002,
        )

        turn_graph._write_orchestrator_trace(
            _state(),
            decision,
            _load_from_files(),
            db_session=db,
            doc_result=doc_result,
        )

        steps = db.query(AgentStep).filter_by(run_id=doc_result.run_id).all()
        step_types = sorted(step.step_type for step in steps)
        step_metadata = [
            json.loads(step.metadata_json or "{}")
            for step in steps
        ]
        assert step_types == ["llm_call", "llm_call", "tool_call"]
        assert {meta.get("step") for meta in step_metadata if meta.get("step")} == {
            "planning",
            "content_generation",
        }
        assert any(step.tool_name == "generate_document" for step in steps)
        assert db.query(AgentRunLog).filter_by(agent_id="document_lead").one().latency_ms == 15
    finally:
        db.close()


def test_research_result_synthesis_latency_ms_is_separate(monkeypatch):
    times = iter([0.0, 0.05])
    monkeypatch.setattr("app.services.agent_runtime.tool_runner.time.perf_counter", lambda: next(times))
    monkeypatch.setattr(
        "app.services.web_context.search_web_sources",
        lambda query, recency=None: ("FakeSearch", [WebSource("One", "https://one.example", "A")]),
    )
    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", lambda **_kwargs: _llm("Research", latency_ms=100))

    result = ResearchAgent(_load_from_files()).run(_state("Research"), SimpleNamespace(plan={"search_queries": ["q"]}))

    assert result.synthesis_latency_ms == 100
    assert result.latency_ms > 100
