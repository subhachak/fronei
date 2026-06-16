from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from app.schemas import RouteDecision
from app.services.agent_runtime.adapters import model_policy_to_route
from app.services.agent_runtime.models import ModelPolicy


class ModelPolicyViolation(Exception):
    """Raised when a model call violates its registry policy."""


def validate_model_policy(policy: ModelPolicy) -> None:
    if not policy.enabled:
        raise ModelPolicyViolation(f"Model policy {policy.id!r} is disabled")
    if policy.allowed_models and policy.primary_model not in policy.allowed_models:
        raise ModelPolicyViolation(
            f"Primary model {policy.primary_model!r} is not in allowed_models for {policy.id!r}"
        )
    for model in policy.fallback_models:
        if policy.allowed_models and model not in policy.allowed_models:
            raise ModelPolicyViolation(
                f"Fallback model {model!r} is not in allowed_models for {policy.id!r}"
            )


def route_for_model(policy: ModelPolicy, model: str, *, remaining_fallbacks: list[str] | None = None) -> RouteDecision:
    route = model_policy_to_route(policy)
    try:
        return route.model_copy(update={"primary_model": model, "fallbacks": remaining_fallbacks or []})
    except AttributeError:
        return replace(route, primary_model=model, fallbacks=remaining_fallbacks or [])


def invoke_with_policy_fallback(
    policy: ModelPolicy,
    invoke_fn: Callable[[RouteDecision], Any],
) -> Any:
    """Invoke with primary and policy fallbacks, validating every candidate."""

    validate_model_policy(policy)
    candidates = [policy.primary_model, *policy.fallback_models]
    last_error: Exception | None = None
    for index, model in enumerate(candidates):
        try:
            return invoke_fn(route_for_model(policy, model, remaining_fallbacks=candidates[index + 1 :]))
        except Exception as exc:
            last_error = exc
            if index == len(candidates) - 1:
                break
    if last_error is not None:
        raise last_error
    raise ModelPolicyViolation(f"Model policy {policy.id!r} has no candidate models")
