import base64
from pathlib import Path
from types import SimpleNamespace

from app.services.agent_runtime.document_agent import DocumentAgent
from app.services.agent_runtime.native_backends import _render_pptx_output, register_all
from app.services.agent_runtime.registry import _load_from_files
from app.services.llm_gateway import LLMResult
from app.services.turn_graph import graph as turn_graph
from app.services.turn_graph.state import TurnGraphState


def _state(message: str = "Create a presentation") -> TurnGraphState:
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


def test_render_pptx_output_builtin_template(monkeypatch):
    captured = {}

    def fake_generate_pptx_bytes(**kwargs):
        captured.update(kwargs)
        return b"FAKE_PPTX_BYTES"

    monkeypatch.setattr("app.services.document_generator.generate_pptx_bytes", fake_generate_pptx_bytes)
    monkeypatch.setattr(
        "app.services.document_templates.resolve_pptx_template_path",
        lambda template_id: Path("/fake/template.pptx"),
    )

    output = _render_pptx_output({
        "title": "Q1 Plan",
        "content": "{}",
        "template_id": "modern-tech",
        "user_id": "u1",
    })

    assert output["pptx_base64"] == base64.b64encode(b"FAKE_PPTX_BYTES").decode("ascii")
    assert output["filename"].endswith(".pptx")
    assert captured["template_path"] == Path("/fake/template.pptx")


def test_render_pptx_output_freehand_no_template(monkeypatch):
    captured = {}

    def fake_generate_pptx_bytes(**kwargs):
        captured.update(kwargs)
        return b"FAKE_PPTX_BYTES"

    monkeypatch.setattr("app.services.document_generator.generate_pptx_bytes", fake_generate_pptx_bytes)
    monkeypatch.setattr("app.services.document_templates.resolve_pptx_template_path", lambda template_id: None)

    output = _render_pptx_output({
        "title": "Plan",
        "content": "slides content",
        "template_id": None,
        "user_id": "u1",
    })

    assert captured["template_path"] is None
    assert captured["template_id"] is None
    assert output["pptx_base64"]


def test_render_pptx_output_template_path_failure_falls_back(monkeypatch):
    captured = {}

    def fake_generate_pptx_bytes(**kwargs):
        captured.update(kwargs)
        return b"FAKE_PPTX_BYTES"

    def fail_session():
        raise Exception("db_unavailable")

    monkeypatch.setattr("app.services.document_generator.generate_pptx_bytes", fake_generate_pptx_bytes)
    monkeypatch.setattr("app.services.document_templates.resolve_pptx_template_path", lambda template_id: None)
    monkeypatch.setattr("app.db.models.SessionLocal", fail_session)

    output = _render_pptx_output({
        "title": "Plan",
        "content": "slides content",
        "template_id": "user-abc",
        "user_id": "u1",
    })

    assert captured["template_path"] is None
    assert output["pptx_base64"]


def test_document_agent_presentation_calls_render_pptx(monkeypatch):
    register_all()
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm_json",
        lambda *_args, **_kwargs: _llm('{"document_brief":{"title":"Deck","doc_type":"presentation"}}'),
    )
    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", lambda **_kwargs: _llm("# Deck\n## Slide 1"))
    monkeypatch.setattr("app.services.document_generator.generate_pptx_bytes", lambda **_kwargs: b"PPTX")
    monkeypatch.setattr("app.services.agent_runtime.document_agent._fetch_template_grammar", lambda *args, **kwargs: {})

    result = DocumentAgent(_load_from_files()).run(
        _state(),
        SimpleNamespace(plan={"brand_profile": {"doc_type": "presentation"}}),
    )

    assert result.pptx_base64
    assert base64.b64decode(result.pptx_base64) == b"PPTX"
    assert result.docx_base64 == ""
    assert result.filename.endswith(".pptx")


def test_document_agent_non_presentation_calls_generate_document(monkeypatch):
    register_all()
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm_json",
        lambda *_args, **_kwargs: _llm('{"document_brief":{"title":"Report","doc_type":"executive_report"}}'),
    )
    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", lambda **_kwargs: _llm("# Report body"))
    monkeypatch.setattr("app.services.document_generator.generate_docx_bytes", lambda *args, **kwargs: b"DOCX")

    result = DocumentAgent(_load_from_files()).run(_state("Create a report"), SimpleNamespace(plan={"brand_profile": {}}))

    assert result.docx_base64
    assert base64.b64decode(result.docx_base64) == b"DOCX"
    assert result.pptx_base64 == ""


def test_document_agent_includes_research_context_in_doc_context(monkeypatch):
    register_all()
    captured = {}
    state = _state("Create a report from this research")
    state.research_result = {"answer": "Key finding: AI adoption is accelerating."}

    def capture_llm(**kwargs):
        doc_context = kwargs.get("doc_context")
        if doc_context is not None:
            captured["doc_context"] = doc_context
        return _llm("# Report body")

    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm_json",
        lambda *_args, **_kwargs: _llm('{"document_brief":{"title":"Report","doc_type":"executive_report"}}'),
    )
    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", capture_llm)
    monkeypatch.setattr("app.services.document_generator.generate_docx_bytes", lambda *args, **kwargs: b"DOCX")

    DocumentAgent(_load_from_files()).run(state, SimpleNamespace(plan={"brand_profile": {"doc_type": "executive_report"}}))

    assert "Key finding: AI adoption" in captured["doc_context"]


def test_document_agent_presentation_with_grammar_context(monkeypatch):
    register_all()
    captured = {}

    def capture_llm(**kwargs):
        doc_context = kwargs.get("doc_context")
        if doc_context is not None:
            captured["doc_context"] = doc_context
        return _llm("# Deck\n## Slide 1")

    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm_json",
        lambda *_args, **_kwargs: _llm('{"document_brief":{"title":"Deck","doc_type":"presentation"}}'),
    )
    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", capture_llm)
    monkeypatch.setattr("app.services.document_generator.generate_pptx_bytes", lambda **_kwargs: b"PPTX")
    monkeypatch.setattr(
        "app.services.agent_runtime.document_agent._fetch_template_grammar",
        lambda *args, **kwargs: {"mode": "template_following"},
    )
    monkeypatch.setattr(
        "app.services.document_templates.template_design_context",
        lambda grammar: "TEMPLATE-FIRST PRESENTATION DESIGN BRIEF: use the source deck grammar.",
    )

    DocumentAgent(_load_from_files()).run(
        _state(),
        SimpleNamespace(plan={"brand_profile": {"doc_type": "presentation", "template_id": "modern-tech"}}),
    )

    assert "TEMPLATE-FIRST PRESENTATION DESIGN BRIEF" in captured["doc_context"]


def test_render_pptx_blocked_for_unowned_template(monkeypatch):
    register_all()
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm_json",
        lambda *_args, **_kwargs: _llm('{"document_brief":{"title":"Deck","doc_type":"presentation"}}'),
    )
    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", lambda **_kwargs: _llm("# Deck\n## Slide 1"))
    monkeypatch.setattr("app.services.agent_runtime.document_agent._fetch_template_grammar", lambda *args, **kwargs: {})
    monkeypatch.setattr("app.services.agent_runtime.guardrails._template_belongs_to_user_db", lambda *_args: False)

    result = DocumentAgent(_load_from_files()).run(
        _state(),
        SimpleNamespace(plan={"brand_profile": {"doc_type": "presentation", "template_id": "user-abc-other"}}),
    )

    assert result.pptx_base64 == ""
    assert result.markdown.startswith("# Deck")


def test_render_pptx_allowed_for_builtin_template(monkeypatch):
    register_all()
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm_json",
        lambda *_args, **_kwargs: _llm('{"document_brief":{"title":"Deck","doc_type":"presentation"}}'),
    )
    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", lambda **_kwargs: _llm("# Deck\n## Slide 1"))
    monkeypatch.setattr("app.services.document_generator.generate_pptx_bytes", lambda **_kwargs: b"PPTX")
    monkeypatch.setattr("app.services.agent_runtime.document_agent._fetch_template_grammar", lambda *args, **kwargs: {})

    result = DocumentAgent(_load_from_files()).run(
        _state(),
        SimpleNamespace(plan={"brand_profile": {"doc_type": "presentation", "template_id": "executive-navy"}}),
    )

    assert result.pptx_base64


def test_render_pptx_tool_registered_in_document_lead():
    registry = _load_from_files()

    tool = registry.tool("render_pptx")

    assert "render_pptx" in registry.agent("document_lead").allowed_tools
    assert tool.backend == "native"
    assert tool.backend_ref == "documents.render_pptx_output"
    assert tool.guardrail_policy_ids == ["document.template_ownership"]


def test_shadow_tool_input_reads_template_id_from_brand_profile():
    state = _state()
    state.plan = {"brand_profile": {"template_id": "user-abc"}}

    tool_input = turn_graph._shadow_tool_input("render_pptx", state)

    assert tool_input == {"document_brief": {}, "template_id": "user-abc"}
