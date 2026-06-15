"""Component-selection ranking (Phase 3, #121 / #130 of
agentdeck_framework_architecture.md §3-4).

Given a `slide_layout` and a zone within it, rank the `ComponentDef`s that
are valid `blocks[].component_id` choices for that zone, ordered by:

  1. `selection_tags` overlap with the content tags the planner/LLM
     associates with the section's data (e.g. ["financial", "comparison"]).
  2. `usage_stats.success_rate` — a neutral 0.5 prior for components with no
     recorded history (#127-129 populate real history into this field at
     registry-load time / after each generation).

This module has no LLM dependency: it's a deterministic scoring/filtering
utility used by (a) the planner prompt (#122) to present the structured
component-selection step with a *pre-filtered, ranked* candidate list rather
than the full registry, and (b) as a deterministic fallback/validation if the
LLM picks a component_id that isn't valid for the zone.
"""

from __future__ import annotations

from .registry import COMPONENT_REGISTRY, ComponentDef, components_for_layout

# Components handled via dedicated SectionPlan fields (header_bar, callout)
# rather than as zone-filling `blocks[].component_id` choices, even though
# their `applicable_slide_layouts` is non-empty.
_NON_BLOCK_COMPONENT_IDS = {"header_bar", "callout_bar"}

# Tag-overlap weight vs. usage_stats.success_rate weight in the combined
# ranking score. Tag relevance dominates; success_rate nudges between
# otherwise-similar candidates and gradually deprioritizes components that
# render poorly for a given layout (#129/#130).
_TAG_WEIGHT = 1.0
_SUCCESS_RATE_WEIGHT = 0.5


def candidates_for_zone(slide_layout: str) -> list[ComponentDef]:
    """Components that may legally fill a `blocks[]` zone on `slide_layout`.

    Zone geometry in `spec.json` doesn't differentiate candidate components
    beyond the slide_layout itself, so this is `components_for_layout` minus
    the components rendered via dedicated SectionPlan fields.
    """
    return [c for c in components_for_layout(slide_layout) if c.id not in _NON_BLOCK_COMPONENT_IDS]


def _tag_overlap_score(component: ComponentDef, tags: set[str]) -> float:
    if not tags or not component.selection_tags:
        return 0.0
    overlap = set(component.selection_tags) & tags
    if not overlap:
        return 0.0
    return len(overlap) / len(component.selection_tags)


def score_component(
    component: ComponentDef,
    tags: set[str],
    *,
    success_rate: float | None = None,
) -> float:
    """Combined ranking score for `component` given content `tags`.

    `success_rate`, when provided, overrides `component.usage_stats.success_rate`
    (#130) — typically a real value loaded from the `component_usage_stats`
    DB table via `usage_stats.load_usage_stats_map`, keyed by
    (component_id, slide_layout, design_system, theme). Falls back to the
    static 0.5 neutral prior when no history exists for that combination.
    """
    rate = component.usage_stats.success_rate if success_rate is None else success_rate
    return _TAG_WEIGHT * _tag_overlap_score(component, tags) + _SUCCESS_RATE_WEIGHT * rate


def rank_components(
    slide_layout: str,
    tags: list[str] | None = None,
    *,
    usage_stats_map: dict[tuple[str, str, str, str], float] | None = None,
    design_system: str = "agentdeck_v1",
    theme: str = "dark",
) -> list[ComponentDef]:
    """Rank components valid for `slide_layout`'s blocks by relevance to
    `tags`, highest score first. Stable for equal scores (registry order).

    `usage_stats_map` (Phase 3, #130), when provided, is consulted for each
    candidate's (component_id, slide_layout, design_system, theme) success
    rate in place of the static neutral prior; see
    `usage_stats.load_usage_stats_map`.
    """
    tag_set = {t.strip().lower() for t in (tags or []) if str(t).strip()}
    candidates = candidates_for_zone(slide_layout)

    def _key(component: ComponentDef) -> float:
        success_rate = None
        if usage_stats_map:
            success_rate = usage_stats_map.get((component.id, slide_layout, design_system, theme))
        return score_component(component, tag_set, success_rate=success_rate)

    return sorted(candidates, key=_key, reverse=True)


def best_component_for_zone(slide_layout: str, tags: list[str] | None = None) -> ComponentDef | None:
    """Deterministic fallback: highest-ranked candidate for `slide_layout`,
    or `None` if no component is valid for this layout's blocks (e.g. TITLE,
    SECTION_HEADER, CLOSING, which don't use `blocks`)."""
    ranked = rank_components(slide_layout, tags)
    return ranked[0] if ranked else None


def is_valid_component_for_zone(component_id: str, slide_layout: str) -> bool:
    """True if `component_id` is a legal `blocks[].component_id` choice for
    `slide_layout` (used to validate/repair LLM-proposed component_ids)."""
    if component_id not in COMPONENT_REGISTRY:
        return False
    if component_id in _NON_BLOCK_COMPONENT_IDS:
        return False
    return any(c.id == component_id for c in candidates_for_zone(slide_layout))
