"""Tests for the Phase 2 agentdeck_v1 contract (#112) and bridge composer
(#114): `PptxRenderPlan`/`ZoneInstance` validation, `compose_pptx_render_plan`
mapping from normalized DeckPlan dicts, and end-to-end rendering via
`generate_agentdeck_pptx_bytes` (#118).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.components import (
    PptxRenderPlan,
    PptxSlidePlan,
    ZoneInstance,
    compose_pptx_render_plan,
)
from app.services.document_generator import (
    _agentdeck_renderer_available,
    generate_agentdeck_pptx_bytes,
)


# ---------------------------------------------------------------------------
# PptxRenderPlan / PptxSlidePlan / ZoneInstance validation (#112)
# ---------------------------------------------------------------------------


def test_zone_instance_rejects_unknown_component():
    with pytest.raises(ValidationError):
        ZoneInstance(component_id="not_a_real_component", props={})


def test_slide_plan_rejects_unknown_slide_layout():
    with pytest.raises(ValidationError):
        PptxSlidePlan(slide_layout="NOT_A_LAYOUT")  # type: ignore[arg-type]


def test_slide_plan_rejects_unknown_zone_for_layout():
    with pytest.raises(ValidationError):
        PptxSlidePlan(
            slide_layout="CONTENT_1COL",
            zones={"not_a_zone": ZoneInstance(component_id="bullet_list", props={"items": []})},
        )


def test_slide_plan_rejects_component_not_applicable_to_layout():
    # decision_list is only applicable to CONTENT_SPLIT_DECISIONS.
    with pytest.raises(ValidationError):
        PptxSlidePlan(
            slide_layout="CONTENT_1COL",
            zones={"body": ZoneInstance(component_id="decision_list", props={"cards": []})},
        )


def test_slide_plan_accepts_valid_zone_assignment():
    slide = PptxSlidePlan(
        slide_layout="CONTENT_1COL",
        title="Example",
        zones={"body": ZoneInstance(component_id="bullet_list", props={"items": [{"text": "Hello", "level": 0}]})},
    )
    assert slide.slide_layout == "CONTENT_1COL"


def test_render_plan_build_resolves_design_system_and_serializes():
    plan = PptxRenderPlan.build(
        [PptxSlidePlan(slide_layout="TITLE", hero_title="Deck", subtitle="Sub")],
        theme="dark",
    )
    assert plan.theme == "dark"
    assert isinstance(plan.design_system, dict)
    assert plan.design_system  # non-empty, resolved from registry

    payload = plan.to_payload()
    assert payload["theme"] == "dark"
    assert payload["slides"][0]["slide_layout"] == "TITLE"
    assert payload["slides"][0]["hero_title"] == "Deck"
    # exclude_none: subtitle was set, but unset optional fields should be absent
    assert "section_title" not in payload["slides"][0]


# ---------------------------------------------------------------------------
# compose_pptx_render_plan mapping (#114)
# ---------------------------------------------------------------------------


def _slide_layouts(render_plan: PptxRenderPlan) -> list[str]:
    return [s.slide_layout for s in render_plan.slides]


def test_compose_prepends_title_slide_from_deck_level_fields():
    deck_plan = {
        "title": "Client AI Strategy",
        "subtitle": "Q3 Review",
        "slides": [
            {"layout": "bullets", "title": "Roadmap", "bullets": ["A", "B", "C"]},
        ],
    }
    plan = compose_pptx_render_plan(deck_plan, theme="dark")
    assert _slide_layouts(plan) == ["TITLE", "CONTENT_1COL"]
    assert plan.slides[0].hero_title == "Client AI Strategy"
    assert plan.slides[0].subtitle == "Q3 Review"
    assert plan.slides[1].title == "Roadmap"


def test_compose_section_slide():
    deck_plan = {
        "title": "Deck",
        "slides": [{"layout": "section", "title": "Part One", "subtitle": "Overview"}],
    }
    plan = compose_pptx_render_plan(deck_plan)
    assert _slide_layouts(plan) == ["TITLE", "SECTION_HEADER"]
    section = plan.slides[1]
    assert section.section_title == "Part One"
    assert section.section_subtitle == "Overview"


def test_compose_table_slide_with_bullets_sidebar():
    deck_plan = {
        "title": "Deck",
        "slides": [
            {
                "layout": "table",
                "title": "Risk Register",
                "table": [["Risk", "Owner"], ["Outage", "SRE"], ["Budget", "Finance"]],
                "bullets": ["Mitigation A", "Mitigation B"],
            }
        ],
    }
    plan = compose_pptx_render_plan(deck_plan)
    slide = plan.slides[1]
    assert slide.slide_layout == "CONTENT_TABLE_SIDEBAR"
    assert slide.zones["table"].component_id == "table"
    assert slide.zones["table"].props["headers"] == ["Risk", "Owner"]
    assert slide.zones["table"].props["rows"] == [["Outage", "SRE"], ["Budget", "Finance"]]
    assert slide.zones["sidebar"].component_id == "bullet_list"
    assert len(slide.zones["sidebar"].props["items"]) == 2


def test_compose_table_slide_without_bullets_uses_callout_sidebar():
    deck_plan = {
        "title": "Deck",
        "slides": [
            {
                "layout": "table",
                "title": "Risk Register",
                "table": [["Risk", "Owner"], ["Outage", "SRE"]],
                "callout": {"label": "Note", "text": "Escalate immediately"},
            }
        ],
    }
    plan = compose_pptx_render_plan(deck_plan)
    slide = plan.slides[1]
    assert slide.zones["sidebar"].component_id == "callout_bar"
    assert slide.zones["sidebar"].props["text"] == "Escalate immediately"
    # callout was consumed by the sidebar, so the slide-level callout is unset
    assert slide.callout is None


def test_compose_table_sidebar_caps_bullets():
    bullets = [f"Bullet {i}" for i in range(10)]
    deck_plan = {
        "title": "Deck",
        "slides": [
            {
                "layout": "table",
                "title": "Risk Register",
                "table": [["A", "B"], ["1", "2"]],
                "bullets": bullets,
            }
        ],
    }
    plan = compose_pptx_render_plan(deck_plan)
    items = plan.slides[1].zones["sidebar"].props["items"]
    assert len(items) == 6  # _SIDEBAR_BULLET_CAP


def test_compose_decisions_slide_splits_cards_across_panels():
    deck_plan = {
        "title": "Deck",
        "slides": [
            {
                "layout": "recommendation",
                "title": "Decisions",
                "decisions": [{"label": f"D{i}", "text": f"Text {i}"} for i in range(5)],
            }
        ],
    }
    plan = compose_pptx_render_plan(deck_plan)
    slide = plan.slides[1]
    assert slide.slide_layout == "CONTENT_SPLIT_DECISIONS"
    left = slide.zones["left_panel"]
    right = slide.zones["right_panel"]
    assert len(left.props["cards"]) + len(right.props["cards"]) == 5
    assert len(left.props["cards"]) == 3  # (5+1)//2


def test_compose_stats_slide_with_hero_and_strip():
    deck_plan = {
        "title": "Deck",
        "slides": [
            {
                "layout": "stat_cards",
                "title": "By the Numbers",
                "stats": [
                    {"value": "42%", "label": "Adoption", "source": "Internal"},
                    {"value": "3x", "label": "Throughput"},
                    {"value": "$2M", "label": "Savings"},
                ],
            }
        ],
    }
    plan = compose_pptx_render_plan(deck_plan)
    slide = plan.slides[1]
    assert slide.slide_layout == "CONTENT_HERO_STAT"
    assert slide.zones["hero"].component_id == "stat_card"
    assert slide.zones["hero"].props["value"] == "42%"
    assert slide.zones["supporting_row"].component_id == "stat_strip"
    assert len(slide.zones["supporting_row"].props["stats"]) == 2


def test_compose_stats_slide_single_stat_has_no_supporting_row():
    deck_plan = {
        "title": "Deck",
        "slides": [
            {"layout": "stat_cards", "title": "Headline", "stats": [{"value": "100%", "label": "Coverage"}]}
        ],
    }
    plan = compose_pptx_render_plan(deck_plan)
    slide = plan.slides[1]
    assert "supporting_row" not in slide.zones


def test_compose_phases_slide_uses_timeline_component():
    deck_plan = {
        "title": "Deck",
        "slides": [
            {
                "layout": "timeline",
                "title": "Roadmap",
                "phases": [
                    {"label": "Q1", "title": "Foundation", "description": "Set up platform"},
                    {"label": "Q2", "title": "Scale", "description": "Expand usage"},
                ],
            }
        ],
    }
    plan = compose_pptx_render_plan(deck_plan)
    slide = plan.slides[1]
    assert slide.slide_layout == "CONTENT_1COL"
    body = slide.zones["body"]
    assert body.component_id == "timeline"
    assert body.props["orientation"] == "horizontal"
    assert [n["step_label"] for n in body.props["nodes"]] == ["Q1", "Q2"]


def test_compose_two_columns_slide():
    deck_plan = {
        "title": "Deck",
        "slides": [
            {
                "layout": "comparison",
                "title": "Build vs Buy",
                "columns": [
                    {"heading": "Build", "bullets": ["Custom", "Slow"]},
                    {"heading": "Buy", "bullets": ["Fast", "Vendor lock-in"]},
                ],
            }
        ],
    }
    plan = compose_pptx_render_plan(deck_plan)
    slide = plan.slides[1]
    assert slide.slide_layout == "CONTENT_2COL"
    assert set(slide.zones.keys()) == {"col_left", "col_right"}
    assert slide.zones["col_left"].props["title"] == "Build"


def test_compose_three_columns_slide():
    deck_plan = {
        "title": "Deck",
        "slides": [
            {
                "layout": "operating_model",
                "title": "Operating Model",
                "columns": [
                    {"heading": "People", "bullets": ["A"]},
                    {"heading": "Process", "bullets": ["B"]},
                    {"heading": "Technology", "bullets": ["C"]},
                ],
            }
        ],
    }
    plan = compose_pptx_render_plan(deck_plan)
    slide = plan.slides[1]
    assert slide.slide_layout == "CONTENT_3COL"
    assert set(slide.zones.keys()) == {"col_1", "col_2", "col_3"}


def test_compose_recommendation_without_bullets_is_closing():
    deck_plan = {
        "title": "Deck",
        "slides": [
            {
                "layout": "recommendation",
                "title": "Thank You",
                "subtitle": "Questions welcome",
            }
        ],
    }
    plan = compose_pptx_render_plan(deck_plan)
    slide = plan.slides[1]
    assert slide.slide_layout == "CLOSING"
    assert slide.closing_text == "Thank You"
    assert slide.closing_body == "Questions welcome"


def test_compose_default_bullet_slide():
    deck_plan = {
        "title": "Deck",
        "slides": [{"layout": "content", "title": "Overview", "bullets": ["One", "Two"]}],
    }
    plan = compose_pptx_render_plan(deck_plan)
    slide = plan.slides[1]
    assert slide.slide_layout == "CONTENT_1COL"
    assert slide.zones["body"].component_id == "bullet_list"
    assert [b["text"] for b in slide.zones["body"].props["items"]] == ["One", "Two"]


def test_compose_handles_empty_slides_list():
    plan = compose_pptx_render_plan({"title": "Just a Title", "slides": []})
    assert _slide_layouts(plan) == ["TITLE"]


def test_compose_drops_unrepresentable_fields_without_error():
    deck_plan = {
        "title": "Deck",
        "slides": [
            {
                "layout": "content",
                "title": "Has Extras",
                "bullets": ["Keep me"],
                "chart": {"type": "bar", "data": []},
                "options": ["x", "y"],
                "units": "USD",
                "bars": [1, 2, 3],
                "platform": "web",
            }
        ],
    }
    plan = compose_pptx_render_plan(deck_plan)
    slide = plan.slides[1]
    assert slide.slide_layout == "CONTENT_1COL"
    assert [b["text"] for b in slide.zones["body"].props["items"]] == ["Keep me"]


def test_compose_output_is_a_valid_render_plan_for_both_themes():
    deck_plan = {
        "title": "Full Deck",
        "subtitle": "All layout branches",
        "slides": [
            {"layout": "section", "title": "Intro"},
            {"layout": "content", "title": "Overview", "bullets": ["A", "B"], "callout": {"text": "Heads up"}},
            {
                "layout": "comparison",
                "title": "Compare",
                "columns": [{"heading": "X", "bullets": ["a"]}, {"heading": "Y", "bullets": ["b"]}],
            },
            {"layout": "table", "title": "Data", "table": [["H1", "H2"], ["1", "2"]]},
            {
                "layout": "timeline",
                "title": "Roadmap",
                "phases": [{"label": "Q1", "title": "Start"}],
            },
            {"layout": "stat_cards", "title": "Stats", "stats": [{"value": "1", "label": "Metric"}]},
            {
                "layout": "recommendation",
                "title": "Decisions",
                "decisions": [{"label": "D1", "text": "Do it"}],
            },
            {"layout": "recommendation", "title": "Thanks", "subtitle": "Goodbye"},
        ],
    }
    for theme in ("dark", "light"):
        plan = compose_pptx_render_plan(deck_plan, theme=theme)
        assert plan.theme == theme
        assert len(plan.slides) == len(deck_plan["slides"]) + 1
        payload = plan.to_payload()
        assert payload["slides"][0]["slide_layout"] == "TITLE"


# ---------------------------------------------------------------------------
# End-to-end agentdeck rendering (#118)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _agentdeck_renderer_available(), reason="agentdeck node renderer not installed")
def test_generate_agentdeck_pptx_bytes_end_to_end():
    deck_plan = {
        "title": "End to End Deck",
        "subtitle": "Generated by tests",
        "slides": [
            {"layout": "content", "title": "Overview", "bullets": ["First point", "Second point"]},
            {"layout": "recommendation", "title": "Thank You", "subtitle": "Questions?"},
        ],
    }
    plan = compose_pptx_render_plan(deck_plan, theme="dark")
    content = generate_agentdeck_pptx_bytes(plan)
    assert isinstance(content, bytes)
    assert len(content) > 1000
    # PPTX files are zip archives.
    assert content[:2] == b"PK"
