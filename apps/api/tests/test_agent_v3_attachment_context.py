"""Coverage for app/routers/agent_v3.py:_build_conversation_context, the
merge point for the generic file/photo attachment feature. Attached-file
text is folded into conversation_context rather than threaded as a brand
new field through every prompt-construction site, so every place that
already reads request.conversation_context (fast_path, orchestrator,
runtime, research/document subtrees) picks it up for free.
"""
from __future__ import annotations

from unittest.mock import patch

from app.routers.agent_v3 import ATTACHMENT_CONTEXT_MAX_CHARS, _build_conversation_context
from app.services.agent_v3.models import AgentV3Request


def _request(**overrides) -> AgentV3Request:
    return AgentV3Request(message="Summarize this.", **overrides)


def test_no_attachment_returns_base_context_unchanged():
    with patch("app.routers.agent_v3.persistence.conversation_context_text", return_value="prior turn history"):
        context = _build_conversation_context("u1", "conv1", _request())
    assert context == "prior turn history"


def test_attachment_appended_after_base_context():
    with patch("app.routers.agent_v3.persistence.conversation_context_text", return_value="prior turn history"):
        context = _build_conversation_context("u1", "conv1", _request(attachment_context="Invoice total: $4,200"))
    assert "prior turn history" in context
    assert "Attached file context:" in context
    assert "Invoice total: $4,200" in context
    assert context.index("prior turn history") < context.index("Attached file context:")


def test_attachment_alone_when_no_base_context():
    with patch("app.routers.agent_v3.persistence.conversation_context_text", return_value=""):
        context = _build_conversation_context("u1", "conv1", _request(attachment_context="Invoice total: $4,200"))
    assert context == "Attached file context:\nInvoice total: $4,200"


def test_blank_attachment_is_a_no_op():
    with patch("app.routers.agent_v3.persistence.conversation_context_text", return_value="prior turn history"):
        context = _build_conversation_context("u1", "conv1", _request(attachment_context="   "))
    assert context == "prior turn history"


def test_attachment_is_length_capped():
    oversized = "x" * (ATTACHMENT_CONTEXT_MAX_CHARS + 5000)
    with patch("app.routers.agent_v3.persistence.conversation_context_text", return_value=""):
        context = _build_conversation_context("u1", "conv1", _request(attachment_context=oversized))
    # "Attached file context:\n" prefix plus the capped body.
    assert len(context) == len("Attached file context:\n") + ATTACHMENT_CONTEXT_MAX_CHARS
