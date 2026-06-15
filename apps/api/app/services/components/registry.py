"""Component library registry.

Each `ComponentDef` binds:
  - an `id` the planner emits in `ContentBlock.component_id` / `ZoneInstance.component_id`
  - a `content_schema` the planner's `data` payload is validated against
  - which design-system spec entries (`spec.json.components`) it renders via
  - which `slide_layouts` zones it may be placed into
  - `selection_tags` the planner uses for ranking candidates against input data
  - `usage_stats`, updated by the QA/feedback loop (§3 of the architecture doc)
    to bias future selection toward components that render cleanly.

11 components map 1:1 to `spec.json.components` (atomic). 3 composites
(`stat_strip`, `decision_list`, `timeline`) are multi-instance zone fillers
built from atomics — they exist because some slide_layout zones
(`CONTENT_HERO_STAT.supporting_row`, `CONTENT_SPLIT_DECISIONS.*`) hold a
repeated group of atoms, not a single one.
"""

from __future__ import annotations

from enum import Enum
from typing import Type

from pydantic import BaseModel, Field

from . import content_schemas as cs


class LayoutPrimitive(str, Enum):
    HEADER = "header"
    CARD_GRID = "card_grid"
    TABLE = "table"
    DIAGRAM = "diagram"
    STAT_STRIP = "stat_strip"
    TEXT = "text"
    TIMELINE = "timeline"
    DIVIDER = "divider"
    BADGE = "badge"
    PROGRESS = "progress"


class ComponentUsageStats(BaseModel):
    """Running quality signal for a component, updated by the QA gate (§6)
    and implicit user-edit feedback (§3). Starts neutral for new components.
    """

    uses: int = 0
    qa_failures: int = 0
    user_rejections: int = 0

    @property
    def success_rate(self) -> float:
        if self.uses == 0:
            return 0.5  # neutral prior for unseen components
        failures = self.qa_failures + self.user_rejections
        return max(0.0, (self.uses - failures) / self.uses)


class ComponentDef(BaseModel):
    id: str
    version: str = "1.0.0"
    primitive: LayoutPrimitive
    content_schema: Type[BaseModel]
    design_system_refs: list[str]
    applicable_slide_layouts: list[str]
    selection_tags: list[str] = Field(default_factory=list)
    usage_stats: ComponentUsageStats = Field(default_factory=ComponentUsageStats)

    model_config = {"arbitrary_types_allowed": True}


# ---------------------------------------------------------------------------
# Registry — 11 atomic + 3 composite components
# ---------------------------------------------------------------------------

_DEFS: list[ComponentDef] = [
    ComponentDef(
        id="header_bar",
        primitive=LayoutPrimitive.HEADER,
        content_schema=cs.HeaderBarContent,
        design_system_refs=["header_bar"],
        applicable_slide_layouts=[
            "CONTENT_1COL", "CONTENT_2COL", "CONTENT_3COL", "CONTENT_4COL",
            "CONTENT_HERO_STAT", "CONTENT_TABLE_SIDEBAR", "CONTENT_SPLIT_DECISIONS",
            "SECTION_HEADER",
        ],
        selection_tags=["header", "navigation", "section_label"],
    ),
    ComponentDef(
        id="card",
        primitive=LayoutPrimitive.CARD_GRID,
        content_schema=cs.CardContent,
        design_system_refs=["card"],
        applicable_slide_layouts=[
            "CONTENT_1COL", "CONTENT_2COL", "CONTENT_3COL", "CONTENT_4COL",
            "CONTENT_TABLE_SIDEBAR",
        ],
        selection_tags=[
            "generic", "summary", "framework", "pillar", "category",
            "comparison", "narrative",
        ],
    ),
    ComponentDef(
        id="stat_card",
        primitive=LayoutPrimitive.STAT_STRIP,
        content_schema=cs.StatCardContent,
        design_system_refs=["stat_card"],
        applicable_slide_layouts=["CONTENT_HERO_STAT", "CONTENT_4COL"],
        selection_tags=["kpi", "metric", "financial", "highlight"],
    ),
    ComponentDef(
        id="badge",
        primitive=LayoutPrimitive.BADGE,
        content_schema=cs.BadgeContent,
        design_system_refs=["badge"],
        applicable_slide_layouts=[],  # inline-only; not independently zone-fillable
        selection_tags=["status", "category", "tag"],
    ),
    ComponentDef(
        id="divider",
        primitive=LayoutPrimitive.DIVIDER,
        content_schema=cs.DividerContent,
        design_system_refs=["divider"],
        applicable_slide_layouts=[],  # inline-only
        selection_tags=["separator", "visual_rhythm"],
    ),
    ComponentDef(
        id="bullet_list",
        primitive=LayoutPrimitive.TEXT,
        content_schema=cs.BulletListContent,
        design_system_refs=["bullet_list"],
        applicable_slide_layouts=[
            "CONTENT_1COL", "CONTENT_2COL", "CONTENT_3COL", "CONTENT_TABLE_SIDEBAR",
        ],
        selection_tags=["narrative", "summary", "key_points", "text"],
    ),
    ComponentDef(
        id="table",
        primitive=LayoutPrimitive.TABLE,
        content_schema=cs.TableContent,
        design_system_refs=["table"],
        applicable_slide_layouts=["CONTENT_1COL", "CONTENT_TABLE_SIDEBAR"],
        selection_tags=["comparison", "structured_data", "risk_register", "matrix"],
    ),
    ComponentDef(
        id="callout_bar",
        primitive=LayoutPrimitive.TEXT,
        content_schema=cs.CalloutContent,
        design_system_refs=["callout_bar"],
        # Fixed position per spec (x=0.4, full-width band near bottom) —
        # composable onto any content layout, not tied to a named zone.
        applicable_slide_layouts=[
            "CONTENT_1COL", "CONTENT_2COL", "CONTENT_3COL", "CONTENT_4COL",
            "CONTENT_HERO_STAT", "CONTENT_TABLE_SIDEBAR", "CONTENT_SPLIT_DECISIONS",
        ],
        selection_tags=["insight", "takeaway", "recommendation", "warning"],
    ),
    ComponentDef(
        id="progress_bar",
        primitive=LayoutPrimitive.PROGRESS,
        content_schema=cs.ProgressContent,
        design_system_refs=["progress_bar"],
        applicable_slide_layouts=[],  # inline-only (within cards)
        selection_tags=["completion", "status", "progress"],
    ),
    ComponentDef(
        id="icon_circle",
        primitive=LayoutPrimitive.BADGE,
        content_schema=cs.IconCircleContent,
        design_system_refs=["icon_circle"],
        applicable_slide_layouts=[],  # inline-only (within cards/timeline_node)
        selection_tags=["icon", "step_number", "feature"],
    ),
    ComponentDef(
        id="timeline_node",
        primitive=LayoutPrimitive.TIMELINE,
        content_schema=cs.TimelineNodeContent,
        design_system_refs=["timeline_node"],
        applicable_slide_layouts=[],  # used via the `timeline` composite
        selection_tags=["timeline_step", "milestone"],
    ),
    # -- composites ---------------------------------------------------------
    ComponentDef(
        id="stat_strip",
        primitive=LayoutPrimitive.STAT_STRIP,
        content_schema=cs.StatStripContent,
        design_system_refs=["stat_card"],
        applicable_slide_layouts=["CONTENT_HERO_STAT"],
        selection_tags=["kpi", "metric", "financial", "supporting_metrics"],
    ),
    ComponentDef(
        id="decision_list",
        primitive=LayoutPrimitive.CARD_GRID,
        content_schema=cs.DecisionListContent,
        design_system_refs=["card", "badge"],
        applicable_slide_layouts=["CONTENT_SPLIT_DECISIONS"],
        selection_tags=["decision", "action_plan", "recommendation", "governance"],
    ),
    ComponentDef(
        id="timeline",
        primitive=LayoutPrimitive.TIMELINE,
        content_schema=cs.TimelineContent,
        design_system_refs=["timeline_node", "divider"],
        applicable_slide_layouts=["CONTENT_1COL", "CONTENT_2COL"],
        selection_tags=["timeline", "roadmap", "process", "phases"],
    ),
]

COMPONENT_REGISTRY: dict[str, ComponentDef] = {c.id: c for c in _DEFS}


def list_components() -> list[str]:
    return sorted(COMPONENT_REGISTRY)


def get_component(component_id: str) -> ComponentDef:
    try:
        return COMPONENT_REGISTRY[component_id]
    except KeyError as exc:
        raise KeyError(
            f"Unknown component {component_id!r}. Valid: {list_components()}"
        ) from exc


def components_for_layout(slide_layout: str) -> list[ComponentDef]:
    """Zone-fillable components applicable to a given slide_layout."""
    return [c for c in _DEFS if slide_layout in c.applicable_slide_layouts]
