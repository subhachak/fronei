"""Composer (Phase 3, #123 of agentdeck_framework_architecture.md §5).

Maps a validated `DocPlan` (the structured-output planner's target, §4) to a
`PptxRenderPlan` ready for `generate_agentdeck_pptx_bytes`. Unlike the Phase 2
bridge composer (`compose.py`, which maps the *legacy* free-text DeckPlan
JSON), this composer is largely mechanical: `DocPlan`/`SectionPlan`/
`ContentBlock` were designed (#120) to mirror `PptxRenderPlan`/`PptxSlidePlan`/
`ZoneInstance` field-for-field, so this is a near 1:1 reshape plus a synthesized
leading TITLE slide (matching `compose_pptx_render_plan`'s convention so
`repair_deck_plan_for_qa`'s `slide_offset=2` continues to hold).
"""

import logging

from pydantic import ValidationError

from ..design_systems.registry import get_design_system
from .fit_validation import format_fit_issues, validate_component_fit
from .render_plan import DocPlan, PptxRenderPlan, PptxSlidePlan, SectionPlan, Theme, ZoneInstance
from .registry import get_component

logger = logging.getLogger(__name__)


def _build_slide_plan(section: SectionPlan, blocks: list, *, notes: str | None) -> PptxSlidePlan:
    zones = {
        block.zone: ZoneInstance(
            component_id=block.component_id,
            component_version=block.component_version,
            props=block.data,
            notes=block.notes,
        )
        for block in blocks
    }

    # `SectionPlan.section_title` is double-duty: for generic content layouts
    # it's the slide's `title`; for SECTION_HEADER it's the section heading
    # (`PptxSlidePlan.section_title`).
    title = section.section_title if section.slide_layout != "SECTION_HEADER" else None
    section_title = section.section_title if section.slide_layout == "SECTION_HEADER" else None

    return PptxSlidePlan(
        slide_layout=section.slide_layout,
        header_bar=section.header_bar,
        title=title,
        zones=zones,
        callout=section.callout,
        hero_title=section.hero_title,
        subtitle=section.subtitle,
        presenter=section.presenter,
        deck_type_label=section.deck_type_label,
        date_label=section.date_label,
        confidentiality=section.confidentiality,
        section_number=section.section_number,
        section_title=section_title,
        section_subtitle=section.section_subtitle,
        closing_text=section.closing_text,
        closing_body=section.closing_body,
        notes=notes,
    )


def _section_to_slide(section: SectionPlan) -> PptxSlidePlan:
    spec = get_design_system("agentdeck_v1")
    layout = spec.slide_layout(section.slide_layout)
    fit_results = []
    for block in section.blocks:
        fit_results.append(
            validate_component_fit(
                slide_layout=section.slide_layout,
                layout=layout,
                zone=block.zone,
                component=get_component(block.component_id),
                props=block.data,
            )
        )
    fit_issue_lines = format_fit_issues(fit_results)
    notes = section.notes
    if fit_issue_lines:
        fit_note = "Fit validation:\n" + "\n".join(f"- {line}" for line in fit_issue_lines)
        notes = f"{notes}\n\n{fit_note}" if notes else fit_note

    blocks = list(section.blocks)
    try:
        return _build_slide_plan(section, blocks, notes=notes)
    except ValidationError as exc:
        logger.info(
            "Slide plan for layout %s failed PptxSlidePlan validation (%s); "
            "retrying with a reduced set of blocks.",
            section.slide_layout,
            exc,
        )

    # Retry, dropping blocks one at a time (from the end) until the slide
    # plan validates. This mirrors `_build_section_plan`'s repair ladder in
    # planner.py: a block whose (zone, component_id) combo is invalid for
    # `PptxSlidePlan`/`ZoneInstance` (but slipped past `SectionPlan`'s own
    # validation) degrades the slide instead of failing the whole deck.
    repair_note = "Note: some slide content was dropped due to a composition error."
    fallback_notes = f"{notes}\n\n{repair_note}" if notes else repair_note
    while blocks:
        blocks = blocks[:-1]
        try:
            return _build_slide_plan(section, blocks, notes=fallback_notes)
        except ValidationError:
            continue

    # Last resort: drop all zone content but keep the slide's dedicated
    # fields (title/hero/closing/etc.) and record what happened.
    return _build_slide_plan(section, [], notes=fallback_notes)


def compose_docplan_to_pptx_render_plan(doc_plan: DocPlan, *, theme: Theme | None = None) -> PptxRenderPlan:
    """Map a validated `DocPlan` to a `PptxRenderPlan`.

    A TITLE slide synthesized from `doc_plan.title`/`doc_plan.subtitle` is
    always rendered first, followed by `doc_plan.sections` 1:1 — mirroring
    `compose_pptx_render_plan` so `repair_deck_plan_for_qa`'s default
    `slide_offset=2` is correct for both composers.

    If a section is itself a TITLE slide (e.g. the planner explicitly
    produced one), it is kept as-is — `doc_plan`-level title/subtitle still
    produce the leading slide, so planners should generally not emit a
    second TITLE section.
    """
    resolved_theme: Theme = theme or doc_plan.theme

    title_slide = PptxSlidePlan(
        slide_layout="TITLE",
        hero_title=doc_plan.title or None,
        subtitle=doc_plan.subtitle or None,
    )
    slides = [title_slide] + [_section_to_slide(section) for section in doc_plan.sections]
    try:
        return PptxRenderPlan.build(slides, theme=resolved_theme, design_system_id=doc_plan.design_system)
    except KeyError:
        # `doc_plan.design_system` may reference a brand design system that
        # failed to persist (or was generated for a template that's since
        # been replaced/deleted). Fall back to the base design system rather
        # than failing the whole generation.
        logger.warning(
            "design_system %r not found; falling back to agentdeck_v1",
            doc_plan.design_system,
        )
        return PptxRenderPlan.build(slides, theme=resolved_theme, design_system_id="agentdeck_v1")
