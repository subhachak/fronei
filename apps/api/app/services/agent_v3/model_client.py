from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

from app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class ModelResponse:
    text: str
    model_used: str
    latency_ms: int
    cost_usd: float = 0.0


def _configure_keys() -> None:
    settings = get_settings()
    if settings.openai_api_key:
        os.environ.setdefault("OPENAI_API_KEY", settings.openai_api_key)
    if settings.anthropic_api_key:
        os.environ.setdefault("ANTHROPIC_API_KEY", settings.anthropic_api_key)
    if settings.gemini_api_key:
        os.environ.setdefault("GEMINI_API_KEY", settings.gemini_api_key)
    if settings.openrouter_api_key:
        os.environ.setdefault("OPENROUTER_API_KEY", settings.openrouter_api_key)


def _candidate_models(preferred: str | None = None) -> list[str]:
    settings = get_settings()
    models: list[str] = []
    for model in [preferred, settings.planner_model, *settings.planner_fallback_model_list]:
        if model and model not in models:
            models.append(model)
    return models or ["gpt-4.1-mini"]


def complete(
    messages: list[dict[str, str]],
    *,
    preferred_model: str | None = None,
    timeout_s: int = 30,
    max_tokens: int = 1200,
) -> ModelResponse:
    """Call an LLM without touching the legacy gateway or hybrid routing stack."""

    _configure_keys()
    from litellm import completion

    last_error: Exception | None = None
    for model in _candidate_models(preferred_model):
        started = time.perf_counter()
        try:
            response = completion(
                model=model,
                messages=messages,
                timeout=timeout_s,
                max_tokens=max_tokens,
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            choice = response.choices[0]
            text = getattr(choice.message, "content", None) or ""
            usage = getattr(response, "_hidden_params", {}) or {}
            cost = float(usage.get("response_cost") or 0.0)
            return ModelResponse(
                text=str(text).strip(),
                model_used=str(getattr(response, "model", None) or model),
                latency_ms=latency_ms,
                cost_usd=cost,
            )
        except Exception as exc:  # pragma: no cover - exact provider failures vary.
            last_error = exc
            logger.warning("agent_v3 model call failed for %s: %s", model, exc)
            continue
    raise RuntimeError(f"agent_v3 model call failed for all candidates: {last_error}")


def simple_completion(system: str, user: str, *, max_tokens: int = 1200) -> ModelResponse:
    return complete(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=max_tokens,
    )
