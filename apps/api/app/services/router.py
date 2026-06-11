from functools import lru_cache
from pathlib import Path

import yaml

from app.config import get_settings
from app.schemas import Complexity, Profile, RouteDecision, TaskType
from app.services.classifier import classify_task

POLICY_PATH = Path(__file__).resolve().parents[1] / "policies" / "routing_rules.yaml"

# When web_search is active, prefer models with native search capability.
# Gemini uses Google Search grounding; Perplexity has built-in live web search.
_WEB_SEARCH_PRIMARIES: dict[str, str] = {
    "cost_saver": "gemini/gemini-2.5-flash",
    "balanced": "openrouter/perplexity/sonar",
    "best_quality": "openrouter/perplexity/sonar-pro",
}

# Valid model string prefixes — used to reject planner hallucinations.
_KNOWN_MODEL_PREFIXES = ("claude", "gpt", "gemini/", "o1", "o3", "openrouter/")

# Last-resort fallbacks appended to every chain so that model outages never
# result in a total failure. Ordered by quality so the best available model
# is tried first when preferred models are unavailable.
_SAFETY_NET: list[str] = [
    "claude-sonnet-4-6",        # Anthropic — high quality, reliable paid tier
    "gpt-4.1-mini",             # OpenAI — low cost, reliable
    "gemini/gemini-2.5-flash",  # Google — generous free-tier quota, last resort
]

# Task types not in the YAML are mapped to the nearest equivalent route.
# This prevents every math/reasoning/planning query from silently falling to default.
_TASK_TYPE_ALIASES: dict[str, str] = {
    "math":        "architecture",    # systematic step-by-step reasoning
    "reasoning":   "architecture",    # same
    "document_qa": "summarization",   # read + distil
    "planning":    "writing",         # structured, organised prose
    "email":       "writing",         # prose composition
}


def _is_valid_model_hint(model: str | None) -> bool:
    return bool(model and isinstance(model, str) and any(model.startswith(p) for p in _KNOWN_MODEL_PREFIXES))


def _with_safety_net(primary: str, fallbacks: list[str]) -> list[str]:
    """Append safety-net models not already in the chain."""
    chain = {primary, *fallbacks}
    return [*fallbacks, *[m for m in _SAFETY_NET if m not in chain]]


@lru_cache(maxsize=1)
def load_policy() -> dict:
    with POLICY_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _lookup_route(policy: dict, task_type: str, complexity: str, profile: str) -> dict | None:
    """Find the best matching route, trying aliases when the task type has no direct YAML entry."""
    routes = policy.get("routes", {})
    for candidate in [task_type, _TASK_TYPE_ALIASES.get(task_type)]:
        if not candidate:
            continue
        task_routes = routes.get(candidate, {})
        for tier in [complexity, "medium", "high"]:
            if tier in task_routes and profile in task_routes[tier]:
                return task_routes[tier][profile]
    return None


def choose_route(
    message: str,
    profile: Profile | None = None,
    force_model: str | None = None,
    deep_research: bool = False,
    web_search: bool = False,
    task_override: TaskType | None = None,
    complexity_override: Complexity | None = None,
    preferred_model: str | None = None,
) -> RouteDecision:
    settings = get_settings()
    selected_profile = profile or settings.default_profile
    task_type, complexity, classifier_reason = classify_task(message)

    # Planner overrides beat the keyword classifier
    if task_override and task_override != "unknown":
        task_type = task_override
        classifier_reason = f"task='{task_type}' (planner). " + classifier_reason
    if complexity_override:
        complexity = complexity_override
        classifier_reason = f"complexity='{complexity}' (planner). " + classifier_reason

    # Deep research always forces the research/high slot
    if deep_research:
        task_type, complexity = "research", "high"
        classifier_reason = "Deep research mode. " + classifier_reason

    if force_model:
        return RouteDecision(
            task_type=task_type,
            complexity=complexity,
            profile=selected_profile,
            primary_model=force_model,
            fallbacks=_with_safety_net(force_model, []),
            reason=f"User forced model '{force_model}'. {classifier_reason}",
        )

    policy = load_policy()
    selected = _lookup_route(policy, task_type, complexity, selected_profile)
    if not selected:
        selected = policy["default"][selected_profile]

    primary = selected["primary"]
    fallbacks = selected.get("fallback", [])
    base_reason = f"routing_rules.yaml. {classifier_reason}"

    # Web search override: swap in a search-native model and push the task-selected
    # model to the front of fallbacks so it's tried if the search model fails.
    if web_search and not deep_research:
        native = _WEB_SEARCH_PRIMARIES.get(selected_profile)
        if native and native != primary:
            web_fallbacks = _with_safety_net(native, [primary, *fallbacks])
            return RouteDecision(
                task_type=task_type,
                complexity=complexity,
                profile=selected_profile,
                primary_model=native,
                fallbacks=web_fallbacks,
                reason=f"Web search — native-search model preferred. {base_reason}",
            )

    # Planner-preferred model hint: use as primary, push YAML primary into fallbacks.
    # Validated against known prefixes to reject hallucinated model names.
    if _is_valid_model_hint(preferred_model) and preferred_model != primary:
        hint_fallbacks = _with_safety_net(preferred_model, [primary, *fallbacks])  # type: ignore[arg-type]
        return RouteDecision(
            task_type=task_type,
            complexity=complexity,
            profile=selected_profile,
            primary_model=preferred_model,  # type: ignore[arg-type]
            fallbacks=hint_fallbacks,
            reason=f"Planner-preferred '{preferred_model}'. {base_reason}",
        )

    return RouteDecision(
        task_type=task_type,
        complexity=complexity,
        profile=selected_profile,
        primary_model=primary,
        fallbacks=_with_safety_net(primary, fallbacks),
        reason=base_reason,
    )
