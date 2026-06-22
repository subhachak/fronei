"""Single source of truth for Agent v3 model selection.

Model assignment used to live in `.env` (AGENT_V3_*_MODEL) with no runtime
control — changing it meant editing the environment and redeploying, and
there was no way to grant a narrower "model selection" capability to admins
without giving them shell/deploy access. This module replaces that: model
assignment is now DB-backed (the existing generic `admin_settings` key/value
store), editable through /admin/agent-v3/model-policy without a restart, and
the `.env` AGENT_V3_*_MODEL variables no longer exist — there is exactly one
place this is configured.

Roles are the same logical stages Agent v3 already had. `direct_answer`
intentionally defaults to a mini-tier model now: it is the highest-volume
role in the system (it's what ordinary chat and quick web lookups route
through via the fast path), so it has the most cost leverage of any single
setting here. Research/document/synthesis-grade roles keep a frontier-tier
default because they are lower-volume and the output quality is what the
user directly judges.
"""
from __future__ import annotations

import time
from typing import Any

MODEL_ROLES: tuple[str, ...] = (
    "fast_router",
    "orchestrator",
    "direct_answer",
    "research_brief",
    "coverage_contract",
    "research_planner",
    "reflection",
    "citation_verifier",
    "repair",
    "document_planner",
    "document_writer",
    "synthesis",
    "synthesis_executive",
)

# Roles deliberately NOT included above: "judge" / "research_judge" /
# "document_judge". judge_research(), judge_document(), and
# judge_research_final() are pure rule-based scoring (citation-regex counts,
# length thresholds, coverage ratios) -- none of them call a model. A
# AGENT_V3_JUDGE_MODEL-style knob would control nothing, so it isn't here.
# If LLM-based judging is added later, give it a real role key then.

DEFAULT_MODEL_POLICY: dict[str, str] = {
    "fast_router": "gpt-4.1-mini",
    "orchestrator": "gpt-4.1-mini",
    "direct_answer": "gpt-4.1-mini",
    "research_brief": "gpt-4.1-mini",
    "coverage_contract": "gpt-4.1-mini",
    "research_planner": "claude-sonnet-4-6",
    "reflection": "claude-sonnet-4-6",
    "citation_verifier": "gpt-4.1-mini",
    "repair": "claude-sonnet-4-6",
    "document_planner": "gpt-4.1-mini",
    "document_writer": "claude-sonnet-4-6",
    "synthesis": "claude-sonnet-4-6",
    "synthesis_executive": "claude-opus-4-8",
}

# litellm needs the provider prefix to route to Gemini; the old
# AGENT_V3_FALLBACK_MODELS env value ("gemini-3.5-flash", no prefix) almost
# certainly failed to route and silently dropped out of the fallback chain.
DEFAULT_FALLBACK_MODELS: list[str] = ["gpt-4.1", "gemini/gemini-2.5-flash", "gpt-4.1-mini"]

MODEL_POLICY_SETTING_KEY = "agent_v3_model_policy"

# Module-process cache so the hot path (every Agent v3 model call) doesn't
# hit the DB every time, while still picking up admin edits without a
# restart -- worst case staleness is one TTL window. set_model_policy() and
# reset_model_policy() also drop the cache immediately for same-process
# reads right after an edit.
_CACHE_TTL_SECONDS = 20.0
_cache: dict[str, Any] | None = None
_cache_at: float = 0.0


def _normalize_role(role: str) -> str:
    return role.strip().lower().replace("-", "_")


# Synonyms used at various Agent v3 call sites that should resolve to the
# same canonical role/policy key.
_ROLE_ALIASES: dict[str, str] = {
    "direct": "direct_answer",
    "brief": "research_brief",
    "contract": "coverage_contract",
    "lead_research": "research_planner",
    "repair_agent": "repair",
}


def canonical_role(role: str | None) -> str | None:
    if not role:
        return None
    normalized = _normalize_role(role)
    normalized = _ROLE_ALIASES.get(normalized, normalized)
    return normalized if normalized in MODEL_ROLES else None


def _load_policy_from_db(db) -> dict[str, Any]:
    from app.db.models import get_admin_setting  # local import: avoid import cycle

    stored = get_admin_setting(db, MODEL_POLICY_SETTING_KEY)
    stored_roles = stored.get("roles") if isinstance(stored.get("roles"), dict) else {}
    roles = {**DEFAULT_MODEL_POLICY}
    for key, value in stored_roles.items():
        if key in MODEL_ROLES and isinstance(value, str) and value.strip():
            roles[key] = value.strip()
    stored_fallbacks = stored.get("fallback_models")
    fallback_models = (
        [str(m).strip() for m in stored_fallbacks if str(m).strip()]
        if isinstance(stored_fallbacks, list) and stored_fallbacks
        else list(DEFAULT_FALLBACK_MODELS)
    )
    return {"roles": roles, "fallback_models": fallback_models}


def get_effective_model_policy(*, fresh: bool = False) -> dict[str, Any]:
    """The policy every Agent v3 model call resolves against. Cached briefly
    (see _CACHE_TTL_SECONDS) so this can sit on the hot path."""
    global _cache, _cache_at
    now = time.monotonic()
    if not fresh and _cache is not None and (now - _cache_at) < _CACHE_TTL_SECONDS:
        return _cache
    from app.db.models import SessionLocal  # local import: avoid import cycle

    db = SessionLocal()
    try:
        policy = _load_policy_from_db(db)
    finally:
        db.close()
    _cache = policy
    _cache_at = now
    return policy


def invalidate_cache() -> None:
    global _cache
    _cache = None


def get_model_policy(db) -> dict[str, Any]:
    """Uncached read against a caller-supplied session, for admin endpoints
    that want a guaranteed-current view (e.g. right after an update)."""
    return _load_policy_from_db(db)


def set_model_policy(
    db,
    *,
    role_overrides: dict[str, str] | None = None,
    fallback_models: list[str] | None = None,
) -> dict[str, Any]:
    """Partial update: only the roles/fallbacks provided are changed. Raises
    ValueError on an unknown role key so the router can turn that into a 422."""
    from app.db.models import get_admin_setting, set_admin_setting  # local import

    if role_overrides:
        unknown = sorted(set(role_overrides) - set(MODEL_ROLES))
        if unknown:
            raise ValueError(f"Unknown model role(s): {', '.join(unknown)}")

    stored = get_admin_setting(db, MODEL_POLICY_SETTING_KEY)
    next_roles = dict(stored.get("roles") if isinstance(stored.get("roles"), dict) else {})
    if role_overrides:
        for key, value in role_overrides.items():
            cleaned = (value or "").strip()
            if cleaned:
                next_roles[key] = cleaned
            else:
                next_roles.pop(key, None)  # empty string clears back to default

    next_fallbacks = stored.get("fallback_models")
    if fallback_models is not None:
        next_fallbacks = [m.strip() for m in fallback_models if m.strip()]

    set_admin_setting(db, MODEL_POLICY_SETTING_KEY, {"roles": next_roles, "fallback_models": next_fallbacks})
    invalidate_cache()
    return _load_policy_from_db(db)


def reset_model_policy(db) -> dict[str, Any]:
    from app.db.models import set_admin_setting  # local import

    set_admin_setting(db, MODEL_POLICY_SETTING_KEY, {"roles": {}, "fallback_models": None})
    invalidate_cache()
    return _load_policy_from_db(db)
