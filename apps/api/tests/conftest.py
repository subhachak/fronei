from __future__ import annotations

from types import SimpleNamespace

import pytest


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
