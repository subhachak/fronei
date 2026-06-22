from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field

from app.config import get_settings
from app.services.agent_v3 import model_policy

logger = logging.getLogger(__name__)


@dataclass
class ModelResponse:
    text: str
    model_used: str
    latency_ms: int
    cost_usd: float = 0.0
    model_role: str = ""
    preferred_model: str = ""
    attempted_models: list[str] = field(default_factory=list)
    failed_model_attempts: list[dict[str, str]] = field(default_factory=list)


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
    policy = model_policy.get_effective_model_policy()
    models: list[str] = []
    for model in [preferred, *policy["fallback_models"]]:
        if model and model not in models:
            models.append(model)
    return models or ["gpt-4.1-mini"]


def model_for_role(
    role: str | None,
    *,
    quality_mode: str = "standard",
    overrides: dict[str, str] | None = None,
) -> str | None:
    """Return the preferred Agent v3 model for a role.

    Backed by the DB model policy (see app/services/agent_v3/model_policy.py)
    rather than `.env` — admin-editable without a restart, with hardcoded
    Python defaults as the fallback when nothing has been overridden.

    `overrides` is the admin-only per-turn override
    (AgentV3Request.model_overrides) and takes precedence over the stored
    policy when present for this role, without changing anyone else's
    default. Non-admins never reach here with a populated `overrides` dict —
    routers/agent_v3.py strips it server-side before the request is used.

    Roles with no real LLM call behind them (judge / research_judge /
    document_judge — these are pure rule-based scoring) intentionally return
    None here; see model_policy.py for why.
    """
    canonical = model_policy.canonical_role(role)
    if canonical is None:
        return None
    if canonical == "synthesis" and quality_mode == "executive":
        canonical = "synthesis_executive"
    if overrides:
        override_value = overrides.get(canonical)
        if override_value:
            return override_value
    policy = model_policy.get_effective_model_policy()
    return policy["roles"].get(canonical) or None


def telemetry_for_role(
    role: str | None,
    *,
    quality_mode: str = "standard",
    model_used: str | None = None,
    overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    """Small payload used by progress events to expose model routing."""

    preferred = model_for_role(role, quality_mode=quality_mode, overrides=overrides) or ""
    payload = {
        "model_role": role or "",
        "preferred_model": preferred,
    }
    if model_used:
        payload["actual_model"] = model_used
    return payload


def telemetry_for_response(response: object, *, overrides: dict[str, str] | None = None) -> dict[str, object]:
    """Model route details safe to attach to progress events."""

    model_role = str(getattr(response, "model_role", "") or "")
    model_used = str(getattr(response, "model_used", "") or "")
    preferred_model = str(getattr(response, "preferred_model", "") or "")
    attempted_models = list(getattr(response, "attempted_models", []) or [])
    failed_model_attempts = list(getattr(response, "failed_model_attempts", []) or [])
    payload: dict[str, object] = telemetry_for_role(
        model_role,
        model_used=model_used,
        overrides=overrides,
    )
    if preferred_model:
        payload["preferred_model"] = preferred_model
    if attempted_models:
        payload["attempted_models"] = attempted_models
    if failed_model_attempts:
        payload["failed_model_attempts"] = failed_model_attempts
    payload["model_fallback_used"] = bool(
        preferred_model
        and model_used
        and preferred_model not in model_used
        and model_used not in preferred_model
    )
    return payload


def _safe_error_text(exc: Exception) -> str:
    text = str(exc)
    text = re.sub(r"sk-[A-Za-z0-9_\-]{12,}", "sk-***", text)
    text = re.sub(r"(api[_-]?key['\"]?\s*[:=]\s*)['\"]?[^,'\"\s]+", r"\1***", text, flags=re.IGNORECASE)
    return text[:500]


def complete(
    messages: list[dict[str, str]],
    *,
    preferred_model: str | None = None,
    role: str | None = None,
    quality_mode: str = "standard",
    overrides: dict[str, str] | None = None,
    timeout_s: int = 30,
    max_tokens: int = 1200,
) -> ModelResponse:
    """Call an LLM without touching the legacy gateway or hybrid routing stack."""

    _configure_keys()
    from litellm import completion

    last_error: Exception | None = None
    preferred = preferred_model or model_for_role(role, quality_mode=quality_mode, overrides=overrides)
    attempted_models: list[str] = []
    failed_model_attempts: list[dict[str, str]] = []
    for model in _candidate_models(preferred):
        attempted_models.append(model)
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
                model_role=role or "",
                preferred_model=preferred or "",
                attempted_models=attempted_models,
                failed_model_attempts=failed_model_attempts,
            )
        except Exception as exc:  # pragma: no cover - exact provider failures vary.
            last_error = exc
            failed_model_attempts.append(
                {
                    "model": model,
                    "error_type": exc.__class__.__name__,
                    "error": _safe_error_text(exc),
                }
            )
            logger.warning("agent_v3 model call failed for %s role=%s: %s", model, role or "default", exc)
            continue
    raise RuntimeError(f"agent_v3 model call failed for all candidates: {last_error}")


def simple_completion(
    system: str,
    user: str,
    *,
    max_tokens: int = 1200,
    preferred_model: str | None = None,
    role: str | None = None,
    quality_mode: str = "standard",
    overrides: dict[str, str] | None = None,
    timeout_s: int = 30,
) -> ModelResponse:
    return complete(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=max_tokens,
        preferred_model=preferred_model,
        role=role,
        quality_mode=quality_mode,
        overrides=overrides,
        timeout_s=timeout_s,
    )
