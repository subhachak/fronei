from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import provider_health
from app.services.agent import model_client


@pytest.fixture(autouse=True)
def clean_circuits():
    provider_health.reset_circuit_state()
    yield
    provider_health.reset_circuit_state()


def _response(model: str, text: str = "ok"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        model=model,
        _hidden_params={"response_cost": 0.001},
    )


def test_open_provider_is_skipped_before_fallback(monkeypatch):
    for _ in range(provider_health.CIRCUIT_FAILURE_THRESHOLD):
        provider_health.record_provider_failure("OpenAI")
    monkeypatch.setattr(
        model_client,
        "_candidate_models",
        lambda _preferred=None: ["gpt-4.1-mini", "claude-sonnet-4-6"],
    )
    calls: list[str] = []

    def fake_completion(*, model, **_kwargs):
        calls.append(model)
        return _response(model)

    monkeypatch.setattr("litellm.completion", fake_completion)

    response = model_client.complete([{"role": "user", "content": "hello"}])

    assert calls == ["claude-sonnet-4-6"]
    assert response.attempted_models == ["claude-sonnet-4-6"]
    assert response.skipped_model_attempts == [{
        "model": "gpt-4.1-mini",
        "provider": "OpenAI",
        "reason": "provider circuit is open",
    }]


def test_model_failures_open_provider_and_later_calls_skip_it(monkeypatch):
    monkeypatch.setattr(
        model_client,
        "_candidate_models",
        lambda _preferred=None: ["gpt-4.1-mini", "claude-sonnet-4-6"],
    )
    calls: list[str] = []

    def fake_completion(*, model, **_kwargs):
        calls.append(model)
        if model.startswith("gpt"):
            raise TimeoutError("provider timeout")
        return _response(model)

    monkeypatch.setattr("litellm.completion", fake_completion)

    for _ in range(provider_health.CIRCUIT_FAILURE_THRESHOLD):
        model_client.complete([{"role": "user", "content": "hello"}])
    calls.clear()

    response = model_client.complete([{"role": "user", "content": "hello"}])

    assert calls == ["claude-sonnet-4-6"]
    assert response.skipped_model_attempts[0]["provider"] == "OpenAI"
    assert provider_health.get_circuit_status()["OpenAI"]["open"] is True


def test_half_open_allows_one_probe_and_success_closes_circuit(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr(provider_health.time, "time", lambda: clock["now"])
    for _ in range(provider_health.CIRCUIT_FAILURE_THRESHOLD):
        provider_health.record_provider_failure("OpenAI")

    clock["now"] += provider_health.CIRCUIT_COOLDOWN_SECONDS + 1

    assert provider_health.provider_attempt_allowed("OpenAI") is True
    assert provider_health.provider_attempt_allowed("OpenAI") is False
    assert provider_health.get_circuit_status()["OpenAI"]["probe_in_flight"] is True

    provider_health.record_provider_success("OpenAI")

    assert "OpenAI" not in provider_health.get_circuit_status()
    assert provider_health.provider_attempt_allowed("OpenAI") is True


def test_failed_half_open_probe_reopens_circuit(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr(provider_health.time, "time", lambda: clock["now"])
    for _ in range(provider_health.CIRCUIT_FAILURE_THRESHOLD):
        provider_health.record_provider_failure("OpenAI")
    clock["now"] += provider_health.CIRCUIT_COOLDOWN_SECONDS + 1

    assert provider_health.provider_attempt_allowed("OpenAI") is True
    provider_health.record_provider_failure("OpenAI")

    status = provider_health.get_circuit_status()["OpenAI"]
    assert status["open"] is True
    assert status["probe_in_flight"] is False


def test_all_open_candidates_fail_without_provider_calls(monkeypatch):
    for provider in ("OpenAI", "Anthropic"):
        for _ in range(provider_health.CIRCUIT_FAILURE_THRESHOLD):
            provider_health.record_provider_failure(provider)
    monkeypatch.setattr(
        model_client,
        "_candidate_models",
        lambda _preferred=None: ["gpt-4.1-mini", "claude-sonnet-4-6"],
    )
    monkeypatch.setattr(
        "litellm.completion",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("provider should not be called")),
    )

    with pytest.raises(RuntimeError, match="provider circuits are open"):
        model_client.complete([{"role": "user", "content": "hello"}])
