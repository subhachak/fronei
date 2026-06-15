"""Typed PptxRenderPlan contract (Phase 2, §5 of agentdeck_framework_architecture.md).

This is the Pydantic mirror of the informal JSON contract
`pptx_render/agentdeck/render_agentdeck.js` consumes:

    { "design_system": <spec.json dict>, "theme": "dark"|"light", "slides": [...] }

Validating here means a `PptxRenderPlan` that references an unknown
slide_layout, an unknown zone, or a component that isn't applicable to a
zone's slide_layout never reaches the Node renderer — it fails fast in
Python with a clear error.

Also includes the format-agnostic `DocPlan`/`SectionPlan`/`ContentBlock`
base classes from §4 of the architecture doc. These aren't consumed by the
Phase-2 bridge composer (which maps the existing DeckPlan dict straight to
`PptxRenderPlan`), but they're the shape the Phase-3 planner will target, and
having them now keeps the contract documented in one place.
"""

from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator

from ..design_systems.registry import design_system_payload, get_design_system
from .registry import get_component

Theme = Literal["dark", "light"]

# Keys of spec.json.slide_layouts (excluding the `description`/`common_rules`
# metadata keys handled by SlideLayouts).
SLIDE_LAYOUTS: tuple[str, ...] = (
    "TITLE",
    "SECTION_HEADER",
    "CONTENT_1COL",
    "CONTENT_2COL",
    "CONTENT_3COL",
    "CONTENT_4COL",
    "CONTENT_HERO_STAT",
    "CONTENT_TABLE_SIDEBAR",
    "CONTENT_SPLIT_DECISIONS",
    "CLOSING",
)

SlideLayoutName = Literal[
    "TITLE",
    "SECTION_HEADER",
    "CONTENT_1COL",
    "CONTENT_2COL",
    "CONTENT_3COL",
    "CONTENT_4COL",
    "CONTENT_HERO_STAT",
    "CONTENT_TABLE_SIDEBAR",
    "CONTENT_SPLIT_DECISIONS",
    "CLOSING",
]


# ---------------------------------------------------------------------------
# PptxRenderPlan (§5)
# ---------------------------------------------------------------------------


class ZoneInstance(BaseModel):
    """A single component placed into a slide_layout zone."""

    component_id: str
    component_version: str = "1.0.0"
    props: dict[str, Any] = Field(default_factory=dict)
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _component_must_exist(self) -> "ZoneInstance":
        try:
            get_component(self.component_id)
        except KeyError as exc:
            raise ValueError(str(exc)) from exc
        return self


# Some zones (e.g. CONTENT_HERO_STAT.supporting_row, CONTENT_SPLIT_DECISIONS.*)
# hold a repeated group of instances rather than a single one.
ZoneAssignment = Union[ZoneInstance, list[ZoneInstance]]


class PptxSlidePlan(BaseModel):
    """One slide. Field set mirrors `layouts.js`'s `SLIDE_LAYOUT_RENDERERS`:
    generic content layouts use `zones` (+ optional `header_bar`/`title`/
    `callout`); TITLE/SECTION_HEADER/CLOSING use their own dedicated fields.
    """

    slide_layout: SlideLayoutName

    # generic content layouts (CONTENT_*COL, CONTENT_HERO_STAT,
    # CONTENT_TABLE_SIDEBAR, CONTENT_SPLIT_DECISIONS)
    header_bar: Optional[dict[str, Any]] = None
    title: Optional[str] = None
    zones: dict[str, ZoneAssignment] = Field(default_factory=dict)
    callout: Optional[dict[str, Any]] = None

    # TITLE
    hero_title: Optional[str] = None
    subtitle: Optional[str] = None
    presenter: Optional[str] = None
    deck_type_label: Optional[str] = None
    date_label: Optional[str] = None
    confidentiality: Optional[str] = None

    # SECTION_HEADER
    section_number: Optional[str] = None
    section_title: Optional[str] = None
    section_subtitle: Optional[str] = None

    # CLOSING
    closing_text: Optional[str] = None
    closing_body: Optional[str] = None

    # all layouts
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _validate_zones_against_layout(self) -> "PptxSlidePlan":
        spec = get_design_system("agentdeck_v1")
        layout = spec.slide_layout(self.slide_layout)  # raises KeyError -> below
        valid_zones = set(layout.zones)
        for zone_name, assignment in self.zones.items():
            if zone_name not in valid_zones:
                raise ValueError(
                    f"Zone {zone_name!r} is not defined for slide_layout "
                    f"{self.slide_layout!r} (valid zones: {sorted(valid_zones)})"
                )
            instances = assignment if isinstance(assignment, list) else [assignment]
            for inst in instances:
                comp = get_component(inst.component_id)
                if comp.applicable_slide_layouts and self.slide_layout not in comp.applicable_slide_layouts:
                    raise ValueError(
                        f"Component {inst.component_id!r} is not applicable to "
                        f"slide_layout {self.slide_layout!r} "
                        f"(applicable: {comp.applicable_slide_layouts})"
                    )
        return self


class PptxRenderPlan(BaseModel):
    """The JSON document handed to `render_agentdeck.js` on stdin."""

    design_system: dict[str, Any]
    theme: Theme = "dark"
    slides: list[PptxSlidePlan]

    @classmethod
    def build(
        cls,
        slides: list[PptxSlidePlan] | list[dict[str, Any]],
        *,
        theme: Theme = "dark",
        design_system_id: str = "agentdeck_v1",
    ) -> "PptxRenderPlan":
        """Convenience constructor: resolves `design_system` from the
        registry by id so callers don't have to load spec.json themselves."""
        return cls(
            design_system=design_system_payload(design_system_id),
            theme=theme,
            slides=slides,
        )

    def to_payload(self) -> dict[str, Any]:
        """JSON-serializable dict, ready for `json.dumps()` on the
        render_agentdeck.js stdin pipe."""
        return self.model_dump(mode="json", exclude_none=True)


# ---------------------------------------------------------------------------
# DocPlan (§4) — format-agnostic planner output, Phase 3 target.
# ---------------------------------------------------------------------------


# Slide layouts whose content is driven by `blocks` (zone -> component_id ->
# content_schema-validated data), as opposed to the dedicated fields below.
_GENERIC_CONTENT_LAYOUTS = {
    "CONTENT_1COL",
    "CONTENT_2COL",
    "CONTENT_3COL",
    "CONTENT_4COL",
    "CONTENT_HERO_STAT",
    "CONTENT_TABLE_SIDEBAR",
    "CONTENT_SPLIT_DECISIONS",
}


class ContentBlock(BaseModel):
    """One zone's content. `data` is validated against the component's
    `content_schema` (registry.py) — a planner that can't populate the
    schema for a zone's data should pick a different `component_id`.
    """

    zone: str
    component_id: str
    component_version: str = "1.0.0"
    data: dict[str, Any] = Field(default_factory=dict)
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _component_must_exist_and_validate_data(self) -> "ContentBlock":
        try:
            comp = get_component(self.component_id)
        except KeyError as exc:
            raise ValueError(str(exc)) from exc
        try:
            validated = comp.content_schema.model_validate(self.data)
        except Exception as exc:  # pydantic.ValidationError
            raise ValueError(
                f"data for component {self.component_id!r} (zone {self.zone!r}) "
                f"failed content_schema validation: {exc}"
            ) from exc
        # Normalize: round-trip through the schema so defaults are filled in
        # and the payload handed to the composer/renderer is canonical.
        self.data = validated.model_dump(mode="json", exclude_none=True)
        return self


class SectionPlan(BaseModel):
    """One slide. Mirrors `PptxSlidePlan`'s field set (§5) but is
    format-agnostic: `blocks` (zone -> ContentBlock) replaces `zones`
    (zone -> ZoneInstance) as the planner-facing representation — the
    composer (#123) resolves `blocks` into `zones`.
    """

    slide_layout: SlideLayoutName

    # generic content layouts (CONTENT_*COL, CONTENT_HERO_STAT,
    # CONTENT_TABLE_SIDEBAR, CONTENT_SPLIT_DECISIONS)
    section_title: Optional[str] = None  # rendered as the slide's `title`
    header_bar: Optional[dict[str, Any]] = None
    blocks: list[ContentBlock] = Field(default_factory=list)
    callout: Optional[dict[str, Any]] = None

    # TITLE
    hero_title: Optional[str] = None
    subtitle: Optional[str] = None
    presenter: Optional[str] = None
    deck_type_label: Optional[str] = None
    date_label: Optional[str] = None
    confidentiality: Optional[str] = None

    # SECTION_HEADER
    section_number: Optional[str] = None
    section_subtitle: Optional[str] = None

    # CLOSING
    closing_text: Optional[str] = None
    closing_body: Optional[str] = None

    # all layouts
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _validate_blocks_against_layout(self) -> "SectionPlan":
        if self.slide_layout not in _GENERIC_CONTENT_LAYOUTS:
            if self.blocks:
                raise ValueError(
                    f"slide_layout {self.slide_layout!r} does not use `blocks` "
                    "(use the dedicated TITLE/SECTION_HEADER/CLOSING fields instead)"
                )
            return self

        spec = get_design_system("agentdeck_v1")
        layout = spec.slide_layout(self.slide_layout)
        valid_zones = set(layout.zones)
        seen_zones: set[str] = set()
        for block in self.blocks:
            if block.zone not in valid_zones:
                raise ValueError(
                    f"Zone {block.zone!r} is not defined for slide_layout "
                    f"{self.slide_layout!r} (valid zones: {sorted(valid_zones)})"
                )
            if block.zone in seen_zones:
                raise ValueError(f"Zone {block.zone!r} assigned more than once")
            seen_zones.add(block.zone)
            comp = get_component(block.component_id)
            if comp.applicable_slide_layouts and self.slide_layout not in comp.applicable_slide_layouts:
                raise ValueError(
                    f"Component {block.component_id!r} is not applicable to "
                    f"slide_layout {self.slide_layout!r} "
                    f"(applicable: {comp.applicable_slide_layouts})"
                )
        return self


class DocPlan(BaseModel):
    """Format-agnostic planner output (§4). The structured-output planner
    (#122) produces this; the composer (#123) maps it to `PptxRenderPlan`.
    """

    doc_type: Literal["presentation", "document", "spreadsheet"] = "presentation"
    design_system: str = "agentdeck_v1"
    theme: Theme = "dark"
    title: str
    subtitle: Optional[str] = None
    sections: list[SectionPlan] = Field(default_factory=list)
