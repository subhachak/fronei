"""FitContract — per-component sizing/density envelope (#139).

Each `ComponentDef` (registry.py) carries a `FitContract` describing how a
component's footprint and content volume relate to the zone it is placed
into. This is the data `ComponentRuntime.validate_fit`/`estimate_density`
(#140) and the composer's `_validate_fit` (#145) operate on — it formalizes
the handful of ad-hoc numbers that previously existed only as
`spec.json.components.stat_card.{min_width_in,min_height_in}` or as
hardcoded geometry in `pptx_render/agentdeck/components.js`.

Sizing model
------------
For a zone of size `(zone_w, zone_h)` inches and a content payload with
`n` repeated items (bullets, cards, table rows, stats, timeline nodes —
whichever axis the component repeats along):

    required_height_in ≈ base_height_in + n * per_item_height_in

`max_items` is the hard cap on `n` (mirrors `qa_thresholds.max_items` in
spec.json, but expressed per-component here so runtime code doesn't need to
re-derive the mapping). `max_chars` caps individual text fields by content
schema field name (mirrors `qa_thresholds.max_chars`).

`min_width_in`/`min_height_in`/`max_width_in`/`max_height_in` bound the zone
itself — a zone smaller than `min_*` cannot host this component at all
regardless of content volume (e.g. stat_card needs >= 2.4in x 1.6in).

All fields are optional: components with fixed, content-independent geometry
(divider, progress_bar, icon_circle, header_bar, callout_bar) only set the
`min_*`/`max_*` zone bounds (or nothing at all) and leave density fields
unset.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class FitContract(BaseModel):
    # -- zone-size bounds (inches) -------------------------------------------
    min_width_in: Optional[float] = None
    min_height_in: Optional[float] = None
    max_width_in: Optional[float] = None
    max_height_in: Optional[float] = None

    # -- content-density model -----------------------------------------------
    # required_height_in ≈ base_height_in + n_items * per_item_height_in
    base_height_in: Optional[float] = None
    per_item_height_in: Optional[float] = None
    # Which content-schema field `n_items` is taken from, e.g. "bullets",
    # "cards", "rows", "stats", "nodes". None if the component has no
    # repeated-item axis (e.g. stat_card, callout_bar).
    item_field: Optional[str] = None
    max_items: Optional[int] = None

    # Optional secondary cap for table-like components (columns/headers).
    max_columns: Optional[int] = None

    # -- per-field text caps (chars), keyed by content-schema field name -----
    max_chars: dict[str, int] = Field(default_factory=dict)

    notes: Optional[str] = None

    def estimate_height_in(self, n_items: int = 0) -> Optional[float]:
        """Rough required height for `n_items` repeated items, or None if
        this component has no height-density model (fixed-geometry
        components)."""
        if self.base_height_in is None:
            return None
        per_item = self.per_item_height_in or 0.0
        return self.base_height_in + n_items * per_item

    def exceeds_max_items(self, n_items: int) -> bool:
        return self.max_items is not None and n_items > self.max_items


# ---------------------------------------------------------------------------
# Per-component contracts
# ---------------------------------------------------------------------------
# Geometry references (agentdeck_v1 spec.json):
#   - slide is 13.3in x 7.5in; content area starts at y=1.1, 12.3in wide
#   - CONTENT_1COL.body: w=12.3 h=5.2 | CONTENT_2COL col: w=5.95 h=5.2
#   - CONTENT_3COL col: w=3.9 h=5.2 | CONTENT_4COL col: w=2.875 h=5.2
#   - CONTENT_HERO_STAT.supporting_row: h=2.6, 4 columns
#   - CONTENT_TABLE_SIDEBAR.sidebar: w=3.1 h=5.2
# max_chars/max_items defaults below mirror spec.json `qa_thresholds`.

HEADER_BAR = FitContract(
    min_width_in=2.0,
    min_height_in=1.0,
    max_height_in=1.0,
    max_chars={"section_title": 50, "section_number": 6},
    notes="Fixed-height full-width band; height is not content-driven.",
)

CARD = FitContract(
    min_width_in=2.0,
    min_height_in=1.0,
    base_height_in=0.7,  # padding + optional title/badge band
    per_item_height_in=0.22,  # ~1 wrapped bullet line at body (11pt)
    item_field="bullets",
    max_items=6,
    max_chars={"title": 40, "body": 160, "bullet_item": 110},
)

STAT_CARD = FitContract(
    min_width_in=2.4,
    min_height_in=1.6,
    max_chars={"value": 12, "label": 30, "delta": 16, "caption": 60},
    notes="min_width/min_height mirror spec.json components.stat_card.",
)

BADGE = FitContract(
    min_width_in=0.55,
    min_height_in=0.25,
    max_chars={"text": 24},
    notes="Inline component; width grows with text (see components.js badgeWidth).",
)

DIVIDER = FitContract(
    min_height_in=0.04,
    max_height_in=0.04,
)

BULLET_LIST = FitContract(
    min_width_in=2.0,
    min_height_in=0.8,
    base_height_in=0.2,
    per_item_height_in=0.3,
    item_field="items",
    max_items=6,
    max_chars={"title": 40, "bullet_item": 110},
)

TABLE = FitContract(
    min_width_in=4.0,
    min_height_in=1.0,
    base_height_in=0.5,  # header row
    per_item_height_in=0.48,  # body row
    item_field="rows",
    max_items=8,
    max_columns=6,
    max_chars={"table_cell": 60},
)

CALLOUT_BAR = FitContract(
    min_width_in=4.0,
    min_height_in=0.9,
    max_height_in=0.9,
    max_chars={"text": 140},
)

PROGRESS_BAR = FitContract(
    min_width_in=1.0,
    min_height_in=0.14,
    max_height_in=0.45,  # includes optional label
    max_chars={"label": 40},
)

ICON_CIRCLE = FitContract(
    min_width_in=0.45,
    min_height_in=0.45,
    max_width_in=0.9,
    max_height_in=0.9,
)

TIMELINE_NODE = FitContract(
    min_width_in=1.2,
    min_height_in=0.8,
    max_chars={"step_label": 10, "title": 40, "body": 80},
)

# -- composites ---------------------------------------------------------

STAT_STRIP = FitContract(
    min_width_in=9.0,
    min_height_in=1.6,
    base_height_in=0.0,
    per_item_height_in=1.6,
    item_field="stats",
    max_items=4,
    notes="Row of stat_card instances; each inherits STAT_CARD's min sizing.",
)

DECISION_LIST = FitContract(
    min_width_in=5.0,
    min_height_in=2.0,
    base_height_in=0.4,
    per_item_height_in=1.3,
    item_field="cards",
    max_items=4,
)

TIMELINE = FitContract(
    min_width_in=8.0,
    min_height_in=1.5,
    base_height_in=0.0,
    per_item_height_in=0.0,
    item_field="nodes",
    max_items=6,
    notes="Horizontal/vertical layout; nodes share fixed width, not additive height.",
)


FIT_CONTRACTS: dict[str, FitContract] = {
    "header_bar": HEADER_BAR,
    "card": CARD,
    "stat_card": STAT_CARD,
    "badge": BADGE,
    "divider": DIVIDER,
    "bullet_list": BULLET_LIST,
    "table": TABLE,
    "callout_bar": CALLOUT_BAR,
    "progress_bar": PROGRESS_BAR,
    "icon_circle": ICON_CIRCLE,
    "timeline_node": TIMELINE_NODE,
    "stat_strip": STAT_STRIP,
    "decision_list": DECISION_LIST,
    "timeline": TIMELINE,
}


def get_fit_contract(component_id: str) -> FitContract:
    try:
        return FIT_CONTRACTS[component_id]
    except KeyError as exc:
        raise KeyError(
            f"No FitContract for component {component_id!r}. Valid: {sorted(FIT_CONTRACTS)}"
        ) from exc
