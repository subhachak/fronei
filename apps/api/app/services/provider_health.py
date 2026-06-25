"""Shared provider classification and in-process circuit-breaker state."""

import threading
import time

PROVIDER_MODEL_PREFIXES: dict[str, tuple[str, ...]] = {
    "OpenAI": ("gpt", "o1", "o3", "o4"),
    "Anthropic": ("claude",),
    "Gemini": ("gemini/", "gemini-"),
    "OpenRouter": ("openrouter/",),
}

CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_COOLDOWN_SECONDS = 60

_circuit_state: dict[str, dict] = {}
_circuit_lock = threading.Lock()


def provider_for_model(model: str | None) -> str:
    if not model:
        return "unknown"
    for provider, prefixes in PROVIDER_MODEL_PREFIXES.items():
        if model.startswith(prefixes):
            return provider
    return "Other"


def provider_attempt_allowed(provider: str) -> bool:
    """Return whether dispatch may call this provider now.

    Once cooldown elapses, exactly one caller receives the half-open probe;
    concurrent callers continue skipping until that probe succeeds or fails.
    Unknown/custom providers are not grouped into a shared circuit.
    """
    if provider not in PROVIDER_MODEL_PREFIXES:
        return True
    with _circuit_lock:
        state = _circuit_state.get(provider)
        if not state or state["failures"] < CIRCUIT_FAILURE_THRESHOLD:
            return True
        if time.time() - state["opened_at"] < CIRCUIT_COOLDOWN_SECONDS:
            return False
        if state.get("half_open_in_flight"):
            return False
        state["half_open_in_flight"] = True
        return True


def record_provider_success(provider: str) -> None:
    if provider not in PROVIDER_MODEL_PREFIXES:
        return
    with _circuit_lock:
        _circuit_state.pop(provider, None)


def record_provider_failure(provider: str) -> None:
    if provider not in PROVIDER_MODEL_PREFIXES:
        return
    with _circuit_lock:
        state = _circuit_state.setdefault(
            provider,
            {"failures": 0, "opened_at": 0.0, "half_open_in_flight": False},
        )
        state["failures"] += 1
        if state["failures"] >= CIRCUIT_FAILURE_THRESHOLD:
            state["opened_at"] = time.time()
        state["half_open_in_flight"] = False


def reset_circuit_state() -> None:
    with _circuit_lock:
        _circuit_state.clear()


def get_circuit_status() -> dict[str, dict]:
    """Return the provider circuit state used by the admin Providers tab."""
    now = time.time()
    status: dict[str, dict] = {}
    with _circuit_lock:
        snapshot = {
            provider: dict(state)
            for provider, state in _circuit_state.items()
        }
    for provider, state in snapshot.items():
        cooldown_remaining = max(
            0,
            int(CIRCUIT_COOLDOWN_SECONDS - (now - state["opened_at"])),
        )
        open_ = (
            state["failures"] >= CIRCUIT_FAILURE_THRESHOLD
            and cooldown_remaining > 0
        )
        status[provider] = {
            "consecutive_failures": state["failures"],
            "open": open_,
            "half_open": bool(
                state["failures"] >= CIRCUIT_FAILURE_THRESHOLD
                and cooldown_remaining == 0
            ),
            "probe_in_flight": bool(state.get("half_open_in_flight")),
            "cooldown_remaining_s": cooldown_remaining if open_ else 0,
        }
    return status
