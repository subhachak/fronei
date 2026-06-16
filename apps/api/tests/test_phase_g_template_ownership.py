from types import SimpleNamespace

from app.services.agent_runtime.document_agent import DocumentAgent
from app.services.agent_runtime.guardrails import GuardrailContext, GuardrailService
from app.services.agent_runtime.native_backends import register_all
from app.services.agent_runtime.registry import _load_from_files
from app.services.llm_gateway import LLMResult
from app.services.turn_graph.state import TurnGraphState


def _state(user_id: str = "u1") -> TurnGraphState:
    return TurnGraphState(user_message="Create a document", user_id=user_id, turn_id="t1", conversation_id="c1")


def _llm(answer: str = "# Doc\n\nContent.") -> LLMResult:
    return LLMResult(
        answer=answer,
        model_used="test-model",
        latency_ms=10,
        prompt_tokens=1,
        completion_tokens=1,
        estimated_cost_usd=0.001,
    )


def _template_context(template_id: str | None, user_id: str = "u1") -> GuardrailContext:
    tool_input = {"template_id": template_id} if template_id is not None else {}
    return GuardrailContext(
        boundary="tool_pre",
        user_id=user_id,
        tenant_id=None,
        tool_name="generate_document",
        tool_input=tool_input,
        tool_output=None,
        request_text="Create a document",
        plan=None,
        response_text=None,
    )


def test_builtin_template_id_passes_without_db_lookup():
    def fail_lookup(_template_id, _user_id):
        raise RuntimeError("should not query DB for builtin templates")

    decision = GuardrailService(_load_from_files(), template_owner_lookup=fail_lookup).evaluate(
        "document.template_ownership",
        _template_context("fronei-default"),
    )

    assert decision.action == "allow"
    assert decision.triggered_checks == []


def test_user_owned_template_passes():
    service = GuardrailService(_load_from_files(), template_owner_lookup=lambda template_id, user_id: True)

    decision = service.evaluate("document.template_ownership", _template_context("abc123"))

    assert decision.action == "allow"


def test_unowned_template_blocks():
    service = GuardrailService(_load_from_files(), template_owner_lookup=lambda template_id, user_id: False)

    decision = service.evaluate("document.template_ownership", _template_context("abc123"))

    assert decision.action == "block"
    assert "abc123" in decision.reason


def test_missing_user_id_blocks_non_builtin():
    called = {"lookup": False}

    def lookup(_template_id, _user_id):
        called["lookup"] = True
        return True

    decision = GuardrailService(_load_from_files(), template_owner_lookup=lookup).evaluate(
        "document.template_ownership",
        _template_context("abc123", user_id=""),
    )

    assert decision.action == "block"
    assert called["lookup"] is False


def test_no_template_id_allows():
    service = GuardrailService(_load_from_files(), template_owner_lookup=lambda template_id, user_id: False)

    decision = service.evaluate("document.template_ownership", _template_context(None))

    assert decision.action == "allow"
    assert "No user template selected" in decision.reason


def test_document_agent_with_builtin_template_passes_guardrail(monkeypatch):
    register_all()
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm_json",
        lambda *_args, **_kwargs: _llm('{"document_brief":{"title":"Builtin","doc_type":"memo"}}'),
    )
    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", lambda **_kwargs: _llm("# Builtin\n\nContent."))
    monkeypatch.setattr("app.services.document_generator.generate_docx_bytes", lambda *args, **kwargs: b"DOCX")
    decision = SimpleNamespace(plan={"brand_profile": {"template_id": "fronei-default"}})

    result = DocumentAgent(_load_from_files()).run(_state(), decision)

    assert result.docx_base64


def test_document_agent_with_unowned_template_falls_back(monkeypatch):
    register_all()
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm_json",
        lambda *_args, **_kwargs: _llm('{"document_brief":{"title":"Blocked","doc_type":"memo"}}'),
    )
    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", lambda **_kwargs: _llm("# Blocked\n\nContent."))
    monkeypatch.setattr("app.services.agent_runtime.guardrails._template_belongs_to_user_db", lambda *_args: False)
    decision = SimpleNamespace(plan={"brand_profile": {"template_id": "user-abc-owned-by-other"}})

    result = DocumentAgent(_load_from_files()).run(_state(), decision)

    assert result.docx_base64 == ""
    assert result.markdown.startswith("# Blocked")


def test_document_agent_plan_threaded_to_guardrail_context(monkeypatch):
    register_all()
    captured = {}

    def capture_boundary(self, boundary, context):
        if boundary == "tool_pre" and context.tool_name == "generate_document":
            captured["plan"] = context.plan
        return []

    monkeypatch.setattr("app.services.agent_runtime.guardrails.GuardrailService.evaluate_boundary", capture_boundary)
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm_json",
        lambda *_args, **_kwargs: _llm('{"document_brief":{"title":"Plan","doc_type":"memo"}}'),
    )
    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", lambda **_kwargs: _llm("# Plan\n\nContent."))
    monkeypatch.setattr("app.services.document_generator.generate_docx_bytes", lambda *args, **kwargs: b"DOCX")
    plan = {"brand_profile": {"template_id": "user-owned"}, "some_key": "marker"}

    DocumentAgent(_load_from_files()).run(_state(), SimpleNamespace(plan=plan))

    assert captured["plan"]["some_key"] == "marker"
