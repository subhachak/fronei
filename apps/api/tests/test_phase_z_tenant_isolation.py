from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.agent_runtime.guardrails import GuardrailContext, GuardrailService
from app.services.agent_runtime.native_backends import register_all
from app.services.agent_runtime.registry import _load_from_files
from app.services.agent_runtime.source_classifier import classify_source_content
from app.services.agent_runtime.tool_runner import ToolNotPermittedError, ToolRunner
from app.services.turn_graph.state import TurnGraphState


def test_source_classifier_passes_clean_public_content():
    content = "This is a public article about market trends. " * 3
    assert classify_source_content("https://example.com/article", content).is_public


def test_source_classifier_blocks_auth_header_pattern():
    content = "Authorization: Bearer abc123\n" + ("private dashboard content " * 5)
    result = classify_source_content("https://app.example.com", content)
    assert not result.is_public
    assert result.reason and result.reason.startswith("private_content_pattern")


def test_source_classifier_blocks_cookie_pattern():
    content = "Set-Cookie: sid=secret\n" + ("account page content " * 5)
    assert not classify_source_content("https://app.example.com", content).is_public


def test_source_classifier_passes_short_content():
    assert classify_source_content("https://example.com", "Bearer x").is_public


def test_guardrail_blocks_private_url_content_on_post_boundary():
    registry = _load_from_files()
    context = GuardrailContext(
        boundary="tool_post",
        user_id="u1",
        tenant_id="u1",
        tool_name="read_url",
        tool_input={"url": "https://app.example.com"},
        tool_output={"content": "Authorization: Bearer abc123\n" + ("private " * 10), "url": "https://app.example.com"},
        request_text="read this",
        plan=None,
        response_text=None,
    )

    decision = GuardrailService(registry).evaluate("tool.source_content_public", context)

    assert decision.action == "block"
    assert "source_content_public" in decision.triggered_checks


def test_guardrail_passes_public_url_content_on_post_boundary():
    registry = _load_from_files()
    context = GuardrailContext(
        boundary="tool_post",
        user_id="u1",
        tenant_id="u1",
        tool_name="read_url",
        tool_input={"url": "https://example.com"},
        tool_output={"content": "Public research content. " * 5, "url": "https://example.com"},
        request_text="read this",
        plan=None,
        response_text=None,
    )

    decision = GuardrailService(registry).evaluate("tool.source_content_public", context)

    assert decision.action == "allow"


def test_tool_runner_propagates_tenant_id_from_state(monkeypatch):
    register_all()
    registry = _load_from_files()
    captured = []

    class CapturingGuardrails(GuardrailService):
        def evaluate_boundary(self, boundary, context):
            captured.append((boundary, context.tenant_id, context.user_role))
            return []

    result = ToolRunner(
        registry,
        "orchestrator",
        CapturingGuardrails(registry),
    ).run(
        "admin_override_judge",
        {"turn_id": "t1", "new_status": "pass", "reason": "test"},
        state=TurnGraphState(user_message="admin", user_id="u1", tenant_id="tenant-1", user_role="admin"),
    )

    assert result.output["overridden"] is True
    assert ("tool_pre", "tenant-1", "admin") in captured
    assert ("tool_post", "tenant-1", "admin") in captured


def test_tenant_id_defaults_to_user_id(monkeypatch):
    register_all()
    registry = _load_from_files()
    captured = []

    class CapturingGuardrails(GuardrailService):
        def evaluate_boundary(self, boundary, context):
            captured.append(context.tenant_id)
            return []

    ToolRunner(
        registry,
        "orchestrator",
        CapturingGuardrails(registry),
    ).run(
        "admin_override_judge",
        {"turn_id": "t1", "new_status": "pass", "reason": "test"},
        state=TurnGraphState(user_message="admin", user_id="u1", user_role="admin"),
    )

    assert captured == ["u1", "u1"]


def test_tool_runner_blocks_private_read_url_output(monkeypatch):
    registry = _load_from_files()
    monkeypatch.setattr("app.services.web_context.crawl_url", lambda url: SimpleNamespace(
        title="Private",
        url=url,
        content="Authorization: Bearer abc123\n" + ("private " * 10),
    ))

    with pytest.raises(ToolNotPermittedError):
        ToolRunner(
            registry,
            "direct_answer_agent",
            GuardrailService(registry),
        ).run(
            "read_url",
            {"url": "https://example.com/private"},
            state=TurnGraphState(user_message="read", user_id="u1"),
        )
