from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field

from app.config import get_settings

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
    settings = get_settings()
    models: list[str] = []
    for model in [preferred, settings.planner_model, *settings.planner_fallback_model_list]:
        if model and model not in models:
            models.append(model)
    return models or ["gpt-4.1-mini"]


def model_for_role(role: str | None, *, quality_mode: str = "standard") -> str | None:
    """Return the preferred Agent v3 model for a role.

    This is intentionally small and config-backed: the runtime can route
    quality-critical stages to stronger models while still falling back through
    the global planner chain when a provider is unavailable.
    """

    if not role:
        return None
    settings = get_settings()
    normalized = role.strip().lower().replace("-", "_")
    if normalized == "synthesis":
        if quality_mode == "executive":
            return settings.agent_v3_synthesis_model_executive or settings.agent_v3_synthesis_model
        return settings.agent_v3_synthesis_model
    role_to_setting = {
        "orchestrator": "agent_v3_orchestrator_model",
        "direct": "agent_v3_direct_model",
        "direct_answer": "agent_v3_direct_model",
        "research_brief": "agent_v3_brief_model",
        "brief": "agent_v3_brief_model",
        "coverage_contract": "agent_v3_contract_model",
        "contract": "agent_v3_contract_model",
        "research_planner": "agent_v3_research_planner_model",
        "lead_research": "agent_v3_research_planner_model",
        "reflection": "agent_v3_reflection_model",
        "citation_verifier": "agent_v3_citation_verifier_model",
        "claim_verifier": "agent_v3_citation_verifier_model",
        "judge": "agent_v3_judge_model",
        "research_judge": "agent_v3_judge_model",
        "document_judge": "agent_v3_judge_model",
        "repair": "agent_v3_repair_model",
        "repair_agent": "agent_v3_repair_model",
        "document_planner": "agent_v3_document_planner_model",
        "document_writer": "agent_v3_document_writer_model",
    }
    setting_name = role_to_setting.get(normalized)
    return str(getattr(settings, setting_name, "") or "") if setting_name else None


def telemetry_for_role(
    role: str | None,
    *,
    quality_mode: str = "standard",
    model_used: str | None = None,
) -> dict[str, str]:
    """Small payload used by progress events to expose model routing."""

    preferred = model_for_role(role, quality_mode=quality_mode) or ""
    payload = {
        "model_role": role or "",
        "preferred_model": preferred,
    }
    if model_used:
        payload["actual_model"] = model_used
    return payload


def telemetry_for_response(response: object) -> dict[str, object]:
    """Model route details safe to attach to progress events."""

    model_role = str(getattr(response, "model_role", "") or "")
    model_used = str(getattr(response, "model_used", "") or "")
    preferred_model = str(getattr(response, "preferred_model", "") or "")
    attempted_models = list(getattr(response, "attempted_models", []) or [])
    failed_model_attempts = list(getattr(response, "failed_model_attempts", []) or [])
    payload: dict[str, object] = telemetry_for_role(
        model_role,
        model_used=model_used,
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
    timeout_s: int = 30,
    max_tokens: int = 1200,
) -> ModelResponse:
    """Call an LLM without touching the legacy gateway or hybrid routing stack."""

    _configure_keys()
    from litellm import completion

    last_error: Exception | None = None
    preferred = preferred_model or model_for_role(role, quality_mode=quality_mode)
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
        timeout_s=timeout_s,
    )
