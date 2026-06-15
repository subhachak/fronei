"""Bridge composer (Phase 2, #114 of agentdeck_framework_architecture.md §8).

Maps the *existing* normalized DeckPlan dict produced by
`document_generator.parse_deck_plan` (fields: layout, archetype, density,
visual_object, title, subtitle, bullets, table, columns, phases, chart,
stats, callout, options, units, bars, decisions, platform, speaker_notes)
into a validated `PptxRenderPlan` for `generate_agentdeck_pptx_bytes`.

This is a transitional shim: it lets the current LLM-authored DeckPlan JSON
flow through the new agentdeck_v1 renderer before Phase 3 replaces the
planner itself with structured `DocPlan` output (§4). `chart`/`options`/
`units`/`bars`/`platform` are not yet representable in agentdeck_v1's
component set and are intentionally dropped (their content, if any, has
already been folded into `bullets`/`callout` by `parse_deck_plan` or is
simply not rendered) — Phase 3's planner will pick components that *can*
represent this data instead of relying on this best-effort mapping.
"""

from __future__ import annotations

from typing import Any

from .render_plan import PptxRenderPlan, PptxSlidePlan, Theme, ZoneInstance

# Keep zone bullet lists from overflowing CONTENT_TABLE_SIDEBAR's narrow
# sidebar — agentdeck_v1's sidebar zone is sized for ~6 short lines.
_SIDEBAR_BULLET_CAP = 6


def _bullets_to_items(bullets: list[Any]) -> list[dict[str, Any]]:
    return [{"text": str(b), "level": 0} for b in bullets if str(b or "").strip()]


def _card_from_column(col: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": col.get("heading") or None,
        "bullets": _bullets_to_items(col.get("bullets") or []),
        "variant": "outlined",
    }


def _card_from_decision(dec: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": dec.get("label") or None,
        "body": dec.get("text") or None,
        "variant": "outlined",
    }


def _table_props(table_rows: list[list[Any]]) -> dict[str, Any]:
    if not table_rows:
        return {"headers": [], "rows": []}
    headers = [str(c) for c in table_rows[0]]
    rows = [[str(c) for c in row] for row in table_rows[1:]]
    return {"headers": headers, "rows": rows}


def _stat_props(stat: dict[str, Any]) -> dict[str, Any]:
    return {
        "value": stat.get("value") or "",
        "label": stat.get("label") or "",
        "caption": stat.get("source") or stat.get("period") or None,
    }


def _callout_props(callout: dict[str, Any] | None) -> dict[str, Any] | None:
    if not callout:
        return None
    text = callout.get("text") or ""
    if not text:
        return None
    return {"text": text, "variant": "insight"}


def _compose_slide(slide: dict[str, Any], index: int) -> PptxSlidePlan:
    layout = str(slide.get("layout") or "content")
    title = str(slide.get("title") or "").strip()
    subtitle = str(slide.get("subtitle") or "").strip()
    bullets = slide.get("bullets") or []
    table = slide.get("table") or []
    columns = slide.get("columns") or []
    phases = slide.get("phases") or []
    stats = slide.get("stats") or []
    decisions = slide.get("decisions") or []
    callout = _callout_props(slide.get("callout"))
    notes = str(slide.get("speaker_notes") or "").strip() or None

    if layout == "title":
        return PptxSlidePlan(
            slide_layout="TITLE",
            hero_title=title or None,
            subtitle=subtitle or None,
            notes=notes,
        )

    if layout == "section":
        return PptxSlidePlan(
            slide_layout="SECTION_HEADER",
            section_number=str(index) if index else None,
            section_title=title,
            section_subtitle=subtitle or None,
            notes=notes,
        )

    # Table-bearing slides -> CONTENT_TABLE_SIDEBAR
    if table:
        sidebar_zone = ZoneInstance(component_id="bullet_list", props={"items": []})
        if bullets:
            sidebar_zone = ZoneInstance(
                component_id="bullet_list",
                props={"items": _bullets_to_items(bullets)[:_SIDEBAR_BULLET_CAP]},
            )
        elif callout:
            sidebar_zone = ZoneInstance(component_id="callout_bar", props=callout)
            callout = None
        return PptxSlidePlan(
            slide_layout="CONTENT_TABLE_SIDEBAR",
            title=title or None,
            zones={
                "table": ZoneInstance(component_id="table", props=_table_props(table)),
                "sidebar": sidebar_zone,
            },
            callout=callout,
            notes=notes,
        )

    # Decision slides -> CONTENT_SPLIT_DECISIONS
    if decisions:
        cards = [_card_from_decision(d) for d in decisions]
        mid = (len(cards) + 1) // 2
        left, right = cards[:mid], cards[mid:]
        return PptxSlidePlan(
            slide_layout="CONTENT_SPLIT_DECISIONS",
            title=title or None,
            zones={
                "left_panel": ZoneInstance(component_id="decision_list", props={"cards": left}),
                "right_panel": ZoneInstance(component_id="decision_list", props={"cards": right}),
            },
            callout=callout,
            notes=notes,
        )

    # Stat-heavy slides -> CONTENT_HERO_STAT
    if stats:
        hero, rest = stats[0], stats[1:4]
        zones: dict[str, Any] = {
            "hero": ZoneInstance(component_id="stat_card", props=_stat_props(hero)),
        }
        if rest:
            zones["supporting_row"] = ZoneInstance(
                component_id="stat_strip",
                props={"stats": [_stat_props(s) for s in rest]},
            )
        return PptxSlidePlan(
            slide_layout="CONTENT_HERO_STAT",
            title=title or None,
            zones=zones,
            callout=callout,
            notes=notes,
        )

    # Phased/roadmap slides -> CONTENT_1COL with the timeline composite
    if phases:
        nodes = [
            {
                "step_label": ph.get("label") or "",
                "title": ph.get("title") or "",
                "body": ph.get("description") or None,
            }
            for ph in phases
        ]
        return PptxSlidePlan(
            slide_layout="CONTENT_1COL",
            title=title or None,
            zones={"body": ZoneInstance(component_id="timeline", props={"nodes": nodes, "orientation": "horizontal"})},
            callout=callout,
            notes=notes,
        )

    # Column/comparison/operating-model slides -> CONTENT_2COL / CONTENT_3COL
    if columns:
        cards = [_card_from_column(c) for c in columns[:3]]
        if len(cards) <= 2:
            zone_names, slide_layout = ["col_left", "col_right"], "CONTENT_2COL"
        else:
            zone_names, slide_layout = ["col_1", "col_2", "col_3"], "CONTENT_3COL"
        zones = {
            zone_names[i]: ZoneInstance(component_id="card", props=cards[i])
            for i in range(len(cards))
        }
        return PptxSlidePlan(
            slide_layout=slide_layout,
            title=title or None,
            zones=zones,
            callout=callout,
            notes=notes,
        )

    # Closing-style slides (closing/thank_you/next_steps alias to
    # "recommendation" with no bullets/table by the time they reach here)
    if layout == "recommendation" and not bullets:
        return PptxSlidePlan(
            slide_layout="CLOSING",
            closing_text=title or None,
            closing_body=subtitle or None,
            notes=notes,
        )

    # Default: bullet-list content -> CONTENT_1COL
    return PptxSlidePlan(
        slide_layout="CONTENT_1COL",
        title=title or None,
        zones={"body": ZoneInstance(component_id="bullet_list", props={"items": _bullets_to_items(bullets)})},
        callout=callout,
        notes=notes,
    )


def compose_pptx_render_plan(deck_plan: dict[str, Any], *, theme: Theme = "dark") -> PptxRenderPlan:
    """Map a normalized DeckPlan dict (from `parse_deck_plan`) to a validated
    `PptxRenderPlan`, ready for `generate_agentdeck_pptx_bytes`.

    Mirrors the legacy renderer's deck structure: a TITLE slide synthesized
    from the deck-level `title`/`subtitle` is always rendered first, followed
    by `deck_plan["slides"]` 1:1. This keeps `repair_deck_plan_for_qa`'s
    default `slide_offset=2` (rendered slide N -> `plan["slides"][N-2]`)
    correct for both renderers.
    """
    deck_title = str(deck_plan.get("title") or "").strip()
    deck_subtitle = str(deck_plan.get("subtitle") or "").strip()
    title_slide = PptxSlidePlan(
        slide_layout="TITLE",
        hero_title=deck_title or None,
        subtitle=deck_subtitle or None,
    )
    slides_raw = deck_plan.get("slides") or []
    slides = [title_slide] + [_compose_slide(slide, idx) for idx, slide in enumerate(slides_raw)]
    return PptxRenderPlan.build(slides, theme=theme)
