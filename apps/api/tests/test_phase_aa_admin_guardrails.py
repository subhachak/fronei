from __future__ import annotations

import pytest

from app.services.agent_runtime.guardrails import GuardrailContext, GuardrailService
from app.services.agent_runtime.native_backends import register_all
from app.services.agent_runtime.registry import _load_from_files
from app.services.agent_runtime.tool_runner import ToolNotPermittedError, ToolRunner
from app.services.turn_graph.state import TurnGraphState


def _runner(registry=None, guardrails=None):
    registry = registry or _load_from_files()
    return ToolRunner(
        registry,
        "orchestrator",
        guardrails or GuardrailService(registry),
    )


def test_admin_tool_blocked_for_user_role():
    register_all()
    with pytest.raises(ToolNotPermittedError):
        _runner().run(
            "admin_override_judge",
            {"turn_id": "t1", "new_status": "pass", "reason": "test"},
            state=TurnGraphState(user_message="override", user_id="u1", user_role="user"),
        )


def test_admin_tool_allowed_for_admin_role():
    register_all()
    result = _runner().run(
        "admin_override_judge",
        {"turn_id": "t1", "new_status": "pass", "reason": "test"},
        state=TurnGraphState(user_message="override", user_id="u1", user_role="admin"),
    )

    assert result.output == {"overridden": True, "turn_id": "t1"}


def test_role_check_fires_before_guardrail_evaluation():
    register_all()
    registry = _load_from_files()

    class ExplodingGuardrails(GuardrailService):
        def evaluate_boundary(self, boundary, context):
            raise AssertionError("guardrails should not run")

    with pytest.raises(ToolNotPermittedError):
        _runner(registry, ExplodingGuardrails(registry)).run(
            "admin_override_judge",
            {"turn_id": "t1", "new_status": "pass", "reason": "test"},
            state=TurnGraphState(user_message="override", user_id="u1", user_role="user"),
        )


def test_user_role_default_is_user():
    assert TurnGraphState(user_message="hi").user_role == "user"


def test_state_user_role_propagates_to_guardrail_context():
    register_all()
    registry = _load_from_files()
    seen: list[str] = []

    class CapturingGuardrails(GuardrailService):
        def evaluate_boundary(self, boundary, context):
            seen.append(context.user_role)
            return []

    _runner(registry, CapturingGuardrails(registry)).run(
        "admin_override_judge",
        {"turn_id": "t1", "new_status": "pass", "reason": "test"},
        state=TurnGraphState(user_message="override", user_id="u1", user_role="admin"),
    )

    assert seen == ["admin", "admin"]


def test_tool_not_permitted_error_raised_on_role_mismatch():
    register_all()
    with pytest.raises(ToolNotPermittedError) as exc:
        _runner().run(
            "admin_override_judge",
            {"turn_id": "t1", "new_status": "pass", "reason": "test"},
            state=TurnGraphState(user_message="override", user_id="u1", user_role="service"),
        )
    assert "requires role" in str(exc.value)


def test_multiple_required_roles_any_match_allowed():
    register_all()
    registry = _load_from_files()
    registry.tools["admin_override_judge"].required_user_roles = ["admin", "service"]

    result = _runner(registry).run(
        "admin_override_judge",
        {"turn_id": "t2", "new_status": "pass", "reason": "test"},
        state=TurnGraphState(user_message="override", user_id="u1", user_role="service"),
    )

    assert result.output["overridden"] is True


def test_service_role_allowed_when_in_required_roles():
    register_all()
    registry = _load_from_files()
    registry.tools["admin_override_judge"].required_user_roles = ["service"]

    result = _runner(registry).run(
        "admin_override_judge",
        {"turn_id": "t3", "new_status": "pass", "reason": "test"},
        state=TurnGraphState(user_message="override", user_id="u1", user_role="service"),
    )

    assert result.output["turn_id"] == "t3"
