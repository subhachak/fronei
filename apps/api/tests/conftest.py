from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

# Most tests authenticate by overriding `get_current_user_id` with a bare
# fake user id (e.g. "u1") and don't care about the admin-approval gate.
# That gate (`get_current_active_user_id` / `CurrentActiveUser` in
# app/auth.py) defaults to *requiring* approval, and would otherwise try to
# bootstrap/query a UserAdminControl row for that fake id against whatever
# the real SessionLocal resolves to in-process. Default it off for the test
# suite as a whole; tests that specifically exercise the approval gate (see
# test_user_approval_gate.py) opt back in locally via monkeypatch + clearing
# the settings cache.
os.environ.setdefault("REQUIRE_USER_APPROVAL", "false")


@pytest.fixture(autouse=True)
def reset_circuit_breakers():
    yield
    try:
        from app.services.agent_runtime.circuit_breaker import CircuitBreakerRegistry

        CircuitBreakerRegistry.get().reset()
    except Exception:
        pass


@pytest.fixture
def make_llm_sequence():
    def _factory(*answers: str):
        iterator = iter(answers)
        last = answers[-1] if answers else ""

        def _invoke(**kwargs):
            nonlocal last
            try:
                answer = next(iterator)
                last = answer
            except StopIteration:
                answer = last
            return SimpleNamespace(answer=answer, model_used="m", latency_ms=1, estimated_cost_usd=0.0)

        return _invoke

    return _factory
