import logging
import os
import time

from litellm import completion

from app.config import get_settings
from app.services.provider_health import (
    record_provider_failure,
    record_provider_success,
)

logger = logging.getLogger(__name__)

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
        record_provider_success(provider)
        return {"success": True, "model": model, "latency_ms": latency_ms, "response": text}
    except Exception as exc:
        record_provider_failure(provider)
        latency_ms = int((time.perf_counter() - started) * 1000)
        return {"success": False, "model": model, "latency_ms": latency_ms, "error": str(exc)[:500]}
