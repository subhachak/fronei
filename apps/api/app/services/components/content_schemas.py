"""Per-component content schemas.

These are the `data` payloads a planner must produce (validated) for a
`ContentBlock`/`ZoneInstance` referencing a given `component_id`. They are
deliberately strict: a planner that can't populate a schema for the data it
has should pick a different component, not stuff mismatched data into one.

Shared atoms (BulletItem, BadgeRef, TableCell) are reused across components.
"""

from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel, Field

BadgeVariant = Literal[
    "default",
    "primary",
    "success",
    "danger",
    "gold",
    "solid_primary",
    "solid_danger",
    "solid_success",
    "solid_gold",
]

CardVariant = Literal["default", "outlined", "filled", "accent"]
CardColorVariant = Literal["blue", "teal", "gold", "danger", "success", "surface"]
DividerVariant = Literal["subtle", "default", "accent", "primary"]
CalloutVariant = Literal["insight", "info", "danger", "success"]
HeaderBarVariant = Literal["dark_navy", "accent_blue", "surface"]
IconCircleSize = Literal["sm", "md", "lg"]
SemanticCell = Literal["critical", "warning", "positive", "muted"]


class BulletItem(BaseModel):
    text: str
    level: int = 0


class BadgeRef(BaseModel):
    text: str
    variant: BadgeVariant = "default"


class TableCell(BaseModel):
    text: str
    semantic: Optional[SemanticCell] = None
    bold: bool = False


# A cell may be given as a plain string (no semantic styling) or a TableCell.
TableCellInput = Union[str, TableCell]


def _normalize_cell(cell: TableCellInput) -> TableCell:
    if isinstance(cell, TableCell):
        return cell
    return TableCell(text=cell)


# ---------------------------------------------------------------------------
# 1. header_bar
# ---------------------------------------------------------------------------


class HeaderBarContent(BaseModel):
    section_number: Optional[str] = None
    section_title: str
    variant: HeaderBarVariant = "dark_navy"


# ---------------------------------------------------------------------------
# 2. card  (primary content building block)
# ---------------------------------------------------------------------------


class CardContent(BaseModel):
    title: Optional[str] = None
    badge: Optional[BadgeRef] = None
    bullets: list[BulletItem] = Field(default_factory=list)
    body: Optional[str] = None
    variant: CardVariant = "default"
    color_variant: Optional[CardColorVariant] = None


# ---------------------------------------------------------------------------
# 3. stat_card
# ---------------------------------------------------------------------------


class StatCardContent(BaseModel):
    icon: Optional[str] = None
    value: str
    label: str
    delta: Optional[str] = None
    delta_direction: Optional[Literal["positive", "negative"]] = None
    caption: Optional[str] = None


# ---------------------------------------------------------------------------
# 4. badge  (usually inline, but addressable as its own zone for emphasis)
# ---------------------------------------------------------------------------


class BadgeContent(BadgeRef):
    pass


# ---------------------------------------------------------------------------
# 5. divider
# ---------------------------------------------------------------------------


class DividerContent(BaseModel):
    variant: DividerVariant = "subtle"
    orientation: Literal["horizontal", "vertical"] = "horizontal"


# ---------------------------------------------------------------------------
# 6. bullet_list
# ---------------------------------------------------------------------------


class BulletListContent(BaseModel):
    items: list[BulletItem]
    title: Optional[str] = None


# ---------------------------------------------------------------------------
# 7. table
# ---------------------------------------------------------------------------


class TableContent(BaseModel):
    headers: list[str]
    rows: list[list[TableCellInput]]

    def normalized_rows(self) -> list[list[TableCell]]:
        return [[_normalize_cell(c) for c in row] for row in self.rows]


# ---------------------------------------------------------------------------
# 8. callout_bar
# ---------------------------------------------------------------------------


class CalloutContent(BaseModel):
    text: str
    variant: CalloutVariant = "insight"
    icon: Optional[str] = None


# ---------------------------------------------------------------------------
# 9. progress_bar
# ---------------------------------------------------------------------------


class ProgressContent(BaseModel):
    value: float = Field(ge=0.0, le=1.0)
    label: Optional[str] = None


# ---------------------------------------------------------------------------
# 10. icon_circle
# ---------------------------------------------------------------------------


class IconCircleContent(BaseModel):
    icon: str
    size: IconCircleSize = "md"
    number: Optional[int] = None


# ---------------------------------------------------------------------------
# 11. timeline_node (atom) / timeline (composite, zone-fillable)
# ---------------------------------------------------------------------------


class TimelineNodeContent(BaseModel):
    step_label: str
    title: str
    body: Optional[str] = None


class TimelineContent(BaseModel):
    nodes: list[TimelineNodeContent]
    orientation: Literal["horizontal", "vertical"] = "horizontal"


# ---------------------------------------------------------------------------
# Composites — multi-instance zone fillers built from atomic components
# ---------------------------------------------------------------------------


class StatStripContent(BaseModel):
    """Fills CONTENT_HERO_STAT.supporting_row (4 stat_card instances)."""

    stats: list[StatCardContent]


class DecisionListContent(BaseModel):
    """Fills CONTENT_SPLIT_DECISIONS.left_panel / right_panel (stack of cards)."""

    title: Optional[str] = None
    cards: list[CardContent]
