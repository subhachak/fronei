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

from __future__ import annotations

from ..design_systems.registry import get_design_system
from .fit_validation import format_fit_issues, validate_component_fit
from .render_plan import DocPlan, PptxRenderPlan, PptxSlidePlan, SectionPlan, Theme, ZoneInstance
from .registry import get_component


def _section_to_slide(section: SectionPlan) -> PptxSlidePlan:
    spec = get_design_system("agentdeck_v1")
    layout = spec.slide_layout(section.slide_layout)
    fit_results = []
    zones = {
        block.zone: ZoneInstance(
            component_id=block.component_id,
            component_version=block.component_version,
            props=block.data,
            notes=block.notes,
        )
        for block in section.blocks
    }
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
    return PptxRenderPlan.build(slides, theme=resolved_theme, design_system_id=doc_plan.design_system)
