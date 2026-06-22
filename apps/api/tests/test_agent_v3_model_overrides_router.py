"""Coverage for the admin-only enforcement of AgentV3Request.model_overrides
at the router boundary (app/routers/agent_v3.py:_sanitize_model_overrides).

This is the gate that stops a non-admin from setting model_overrides on a
request body and getting it honored -- the field exists on the schema, but
must be inert for anyone who isn't an admin.
"""
from __future__ import annotations

from app.routers.agent_v3 import _sanitize_model_overrides
from app.services.agent_v3.models import AgentV3Request


def _request(**overrides) -> AgentV3Request:
    return AgentV3Request(message="hello", **overrides)


def test_non_admin_loses_overrides_entirely():
    request = _request(model_overrides={"direct_answer": "claude-opus-4-8"})
    cleaned = _sanitize_model_overrides(request, is_admin=False)
    assert cleaned.model_overrides is None


def test_admin_keeps_valid_overrides():
    request = _request(model_overrides={"direct_answer": "claude-opus-4-8", "synthesis": "claude-sonnet-4-6"})
    cleaned = _sanitize_model_overrides(request, is_admin=True)
    assert cleaned.model_overrides == {"direct_answer": "claude-opus-4-8", "synthesis": "claude-sonnet-4-6"}


def test_admin_unknown_role_silently_dropped():
    request = _request(model_overrides={"direct_answer": "claude-opus-4-8", "not_a_role": "gpt-4.1"})
    cleaned = _sanitize_model_overrides(request, is_admin=True)
    assert cleaned.model_overrides == {"direct_answer": "claude-opus-4-8"}


def test_admin_blank_value_dropped():
    request = _request(model_overrides={"direct_answer": "   "})
    cleaned = _sanitize_model_overrides(request, is_admin=True)
    assert cleaned.model_overrides is None


def test_admin_alias_role_resolved_to_canonical():
    request = _request(model_overrides={"direct": "claude-opus-4-8"})
    cleaned = _sanitize_model_overrides(request, is_admin=True)
    assert cleaned.model_overrides == {"direct_answer": "claude-opus-4-8"}


def test_no_overrides_is_a_no_op_for_either_role():
    request = _request()
    assert _sanitize_model_overrides(request, is_admin=True).model_overrides is None
    assert _sanitize_model_overrides(request, is_admin=False).model_overrides is None


def test_admin_all_invalid_collapses_to_none():
    request = _request(model_overrides={"not_a_role": "gpt-4.1", "also_fake": "claude-sonnet-4-6"})
    cleaned = _sanitize_model_overrides(request, is_admin=True)
    assert cleaned.model_overrides is None
