import logging
import os
import time

logger = logging.getLogger(__name__)

from litellm import completion

from app.config import get_settings

# ── Provider setup ────────────────────────────────────────────────────────────
# configure_provider_keys() must be called once at application startup (via the
# lifespan hook in main.py). Do not call it per-request or from threads.

def configure_provider_keys() -> None:
    import litellm as _litellm
    s = get_settings()
    if s.openai_api_key:
        os.environ["OPENAI_API_KEY"] = s.openai_api_key
    if s.anthropic_api_key:
        os.environ["ANTHROPIC_API_KEY"] = s.anthropic_api_key
    if s.gemini_api_key:
        os.environ["GEMINI_API_KEY"] = s.gemini_api_key
    if s.openrouter_api_key:
        os.environ["OPENROUTER_API_KEY"] = s.openrouter_api_key
    # Silently drop params that specific models don't support (e.g. temperature
    # for o3/o1-series and claude-opus-4-8, tools for non-Gemini models, etc.)
    _litellm.drop_params = True


# Cheapest/fastest model per provider, used only for the admin "test connection" ping.
PROVIDER_TEST_MODELS = {
    "OpenAI": "gpt-4.1-mini",
    "Anthropic": "claude-haiku-4-5-20251001",
    "Gemini": "gemini/gemini-2.5-flash",
    "OpenRouter": "openrouter/deepseek/deepseek-chat",
}

# Maps a model identifier prefix (as stored in model_used) to the
# admin-facing provider name. Shared by the circuit breaker and the admin Providers tab.
PROVIDER_MODEL_PREFIXES: dict[str, tuple[str, ...]] = {
    "OpenAI": ("gpt", "o1", "o3", "o4"),
    "Anthropic": ("claude",),
    "Gemini": ("gemini/", "gemini-"),
    "OpenRouter": ("openrouter/",),
}


def provider_for_model(model: str | None) -> str:
    if not model:
        return "unknown"
    for provider, prefixes in PROVIDER_MODEL_PREFIXES.items():
        if model.startswith(prefixes):
            return provider
    return "Other"


# ── Circuit breaker ───────────────────────────────────────────────────────────
# In-memory, per-process. After CIRCUIT_FAILURE_THRESHOLD consecutive failures for
# a provider, that provider's models are skipped in fallback chains for
# CIRCUIT_COOLDOWN_SECONDS (a "half-open" retry is allowed once the cooldown elapses).
# This avoids burning latency retrying a provider that is fully down on every request.
CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_COOLDOWN_SECONDS = 60

_circuit_state: dict[str, dict] = {}


def _provider_circuit_open(provider: str) -> bool:
    state = _circuit_state.get(provider)
    if not state or state["failures"] < CIRCUIT_FAILURE_THRESHOLD:
        return False
    if time.time() - state["opened_at"] >= CIRCUIT_COOLDOWN_SECONDS:
        return False  # cooldown elapsed — half-open, allow a retry
    return True


def record_provider_success(provider: str) -> None:
    _circuit_state.pop(provider, None)


def record_provider_failure(provider: str) -> None:
    state = _circuit_state.setdefault(provider, {"failures": 0, "opened_at": 0.0})
    state["failures"] += 1
    if state["failures"] >= CIRCUIT_FAILURE_THRESHOLD:
        state["opened_at"] = time.time()


def get_circuit_status() -> dict[str, dict]:
    """Snapshot of circuit-breaker state per provider, for the admin Providers tab."""
    now = time.time()
    status: dict[str, dict] = {}
    for provider, state in _circuit_state.items():
        open_ = state["failures"] >= CIRCUIT_FAILURE_THRESHOLD and (
            now - state["opened_at"] < CIRCUIT_COOLDOWN_SECONDS
        )
        status[provider] = {
            "consecutive_failures": state["failures"],
            "open": open_,
            "cooldown_remaining_s": (
                max(0, int(CIRCUIT_COOLDOWN_SECONDS - (now - state["opened_at"]))) if open_ else 0
            ),
        }
    return status


def test_provider_connection(provider: str) -> dict:
    """Make a minimal (1-token) completion call to verify a provider key is live.

    Used by the admin Providers tab. Costs a negligible fraction of a cent.
    """
    model = PROVIDER_TEST_MODELS.get(provider)
    if not model:
        return {"success": False, "error": f"No test model configured for provider '{provider}'."}
    started = time.perf_counter()
    try:
        response = completion(
            model=model,
            messages=[{"role": "user", "content": "Reply with one word: pong"}],
            max_tokens=5,
            temperature=0,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        text = (response.choices[0].message.content or "").strip()
        return {"success": True, "model": model, "latency_ms": latency_ms, "response": text}
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        return {"success": False, "model": model, "latency_ms": latency_ms, "error": str(exc)[:500]}
