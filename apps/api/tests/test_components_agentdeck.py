"""Tests for the Phase 2 agentdeck_v1 contract (#112) and bridge composer
(#114): `PptxRenderPlan`/`ZoneInstance` validation, `compose_pptx_render_plan`
mapping from normalized DeckPlan dicts, and end-to-end rendering via
`generate_agentdeck_pptx_bytes` (#118).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.components import (
    DEFAULT_COMPONENT_RUNTIME,
    COMPONENT_REGISTRY,
    ContentBlock,
    DesignPlan,
    DocPlan,
    EvidencePack,
    NarrativePlan,
    PresentationPlan,
    PresentationSlidePlan,
    PptxRenderPlan,
    PptxSlidePlan,
    SectionPlan,
    SlideDesignTreatment,
    StoryBeat,
    ZoneInstance,
    compose_docplan_to_pptx_render_plan,
    compose_pptx_render_plan,
)
from app.services.components.content_schemas import BulletListContent, TableContent
from app.services.components.fit_contract import FIT_CONTRACTS
from app.services.qa import run_plan_checks, run_render_checks
from app.services import document_generator
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


def test_v2_plan_aliases_preserve_docplan_compatibility():
    assert DocPlan is PresentationPlan
    assert SectionPlan is PresentationSlidePlan

    plan = DocPlan(
        title="Board Brief",
        sections=[
            SectionPlan(
                slide_id="s1",
                purpose="decision",
                audience_question="What decision is needed?",
                message="Approve the platform direction.",
                slide_layout="CONTENT_1COL",
                section_title="Decision",
                dek="Approve the operating model.",
                evidence=[{"evidence_id": "e1", "confidence": "high"}],
                blocks=[
                    ContentBlock(
                        block_id="b1",
                        zone="body",
                        component_id="bullet_list",
                        data={"items": [{"text": "Authorize Phase 1"}]},
                    )
                ],
            )
        ],
    )

    assert isinstance(plan, PresentationPlan)
    assert plan.sections[0].blocks[0].block_id == "b1"
    assert plan.sections[0].evidence[0].evidence_id == "e1"


def test_v2_narrative_evidence_and_design_plan_stubs_validate():
    narrative = NarrativePlan(
        title="AI Platform",
        storyline=[
            StoryBeat(
                id="beat-1",
                title="Why now",
                message="Fragmented tools are slowing delivery.",
                evidence_needs=[{"id": "need-1", "question": "What is the cost impact?"}],
            )
        ],
    )
    evidence = EvidencePack(items=[{"id": "e1", "title": "Internal cost model"}])
    design = DesignPlan(
        slide_treatments=[
            SlideDesignTreatment(
                slide_id="s1",
                visual_role="decision",
                layout_id="CONTENT_SPLIT_DECISIONS",
                component_choices={"left_panel": ["decision_list"], "right_panel": ["decision_list"]},
                repair_constraints=[{"type": "preserve_message"}],
            )
        ]
    )

    assert narrative.storyline[0].evidence_needs[0].id == "need-1"
    assert evidence.items[0].id == "e1"
    assert design.slide_treatments[0].repair_constraints[0].type == "preserve_message"


# ---------------------------------------------------------------------------
# Component runtime / FitContract foundation (#139/#140)
# ---------------------------------------------------------------------------


def test_all_registered_components_have_fit_contracts():
    assert set(COMPONENT_REGISTRY) == set(FIT_CONTRACTS)
    assert all(component.fit_contract is FIT_CONTRACTS[component_id] for component_id, component in COMPONENT_REGISTRY.items())


def test_default_runtime_validates_fit_and_estimates_density():
    content = DEFAULT_COMPONENT_RUNTIME.normalize(
        {"items": [{"text": "Point A"}, {"text": "Point B"}]},
        BulletListContent,
    )

    result = DEFAULT_COMPONENT_RUNTIME.validate_fit(
        content,
        FIT_CONTRACTS["bullet_list"],
        zone_width_in=3.0,
        zone_height_in=2.0,
    )

    assert result.ok is True
    assert 0 < result.density < 1
    assert result.estimated_height_in is not None
    assert result.issues == []


def test_default_runtime_flags_item_and_height_overflow():
    content = DEFAULT_COMPONENT_RUNTIME.normalize(
        {
            "headers": ["A", "B", "C", "D", "E", "F", "G"],
            "rows": [["x", "y", "z", "w", "v", "u", "t"] for _ in range(10)],
        },
        TableContent,
    )

    result = DEFAULT_COMPONENT_RUNTIME.validate_fit(
        content,
        FIT_CONTRACTS["table"],
        zone_width_in=3.0,
        zone_height_in=1.2,
    )

    assert result.ok is False
    assert any(issue.field == "rows" for issue in result.issues)
    assert any(issue.field == "estimated_height_in" for issue in result.issues)


def test_docplan_composer_attaches_fit_validation_notes():
    plan = DocPlan(
        title="Deck",
        sections=[
            SectionPlan(
                slide_layout="CONTENT_TABLE_SIDEBAR",
                section_title="Overloaded Table",
                blocks=[
                    ContentBlock(
                        block_id="table-1",
                        zone="table",
                        component_id="table",
                        data={
                            "headers": ["A", "B"],
                            "rows": [["1", "2"] for _ in range(10)],
                        },
                    )
                ],
            )
        ],
    )

    render_plan = compose_docplan_to_pptx_render_plan(plan)

    assert "Fit validation:" in (render_plan.slides[1].notes or "")


def test_docplan_composer_repairs_section_with_block_invalid_for_layout():
    """A `SectionPlan` whose `blocks` validated fine on their own but whose
    (zone, component_id) combo is invalid for `PptxSlidePlan`/`ZoneInstance`
    on this `slide_layout` must not raise out of the composer (#192) — the
    offending block should be dropped and the slide degraded instead.
    """
    section = SectionPlan(
        slide_layout="CONTENT_1COL",
        section_title="Decision Time",
        blocks=[
            ContentBlock(
                block_id="b1",
                zone="body",
                component_id="bullet_list",
                data={"items": [{"text": "Authorize Phase 1"}]},
            )
        ],
    )
    # Bypass PresentationSlidePlan's own `_validate_blocks_against_layout` to
    # simulate a block whose component isn't applicable to CONTENT_1COL's
    # "body" zone (e.g. `decision_list`, which is only valid for
    # CONTENT_SPLIT_DECISIONS) slipping through into the composer.
    bad_block = ContentBlock(
        block_id="b2",
        zone="body",
        component_id="decision_list",
        data={"cards": []},
    )
    plan = DocPlan(title="Deck", sections=[section])
    object.__setattr__(plan.sections[0], "blocks", [bad_block])

    render_plan = compose_docplan_to_pptx_render_plan(plan)

    slide = render_plan.slides[1]
    assert slide.slide_layout == "CONTENT_1COL"
    assert slide.zones == {}
    assert "composition error" in (slide.notes or "")


def test_plan_checks_report_dangling_punctuation_and_fit_overflow():
    plan = DocPlan(
        title="Deck",
        sections=[
            SectionPlan(
                slide_id="s1",
                slide_layout="CONTENT_TABLE_SIDEBAR",
                section_title="Financial impact —",
                blocks=[
                    ContentBlock(
                        block_id="table-1",
                        zone="table",
                        component_id="table",
                        data={
                            "headers": ["Metric", "Metric"],
                            "rows": [["a", "b"] for _ in range(10)],
                        },
                    )
                ],
            )
        ],
    )

    issue_types = {issue.type for issue in run_plan_checks(plan)}

    assert "dangling_punctuation" in issue_types
    assert "duplicate_label" in issue_types
    assert "too_many_items" in issue_types


def test_render_checks_normalize_existing_render_qa_and_whitespace_signal():
    issues = run_render_checks(
        {
            "available": True,
            "issues": [{"slide": 2, "type": "dense_text", "detail": "Too much text"}],
            "metrics": [{"slide": 3, "char_count": 120, "ink_ratio": 0.01}],
        }
    )

    issue_types = {issue.type for issue in issues}
    assert "dense_text" in issue_types
    assert "excessive_whitespace" in issue_types


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


def test_generate_agentdeck_pptx_bytes_prefers_warm_renderer(monkeypatch):
    plan = PptxRenderPlan.build(
        [PptxSlidePlan(slide_layout="TITLE", hero_title="Deck")],
        theme="dark",
    )
    calls: list[dict] = []

    monkeypatch.setattr(document_generator, "_agentdeck_renderer_available", lambda: True)
    monkeypatch.setattr(
        document_generator._WARM_AGENTDECK_RENDERER,
        "render",
        lambda payload: calls.append(payload) or b"warm-pptx",
    )
    monkeypatch.setattr(
        document_generator,
        "_render_agentdeck_pptx_one_shot",
        lambda _payload: (_ for _ in ()).throw(AssertionError("one-shot renderer should not run")),
    )

    assert generate_agentdeck_pptx_bytes(plan) == b"warm-pptx"
    assert calls and calls[0]["slides"][0]["slide_layout"] == "TITLE"


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


@pytest.mark.skipif(not _agentdeck_renderer_available(), reason="agentdeck node renderer not installed")
def test_agentdeck_renderer_retries_without_bad_brand_logo():
    plan = PptxRenderPlan.build(
        [
            PptxSlidePlan(
                slide_layout="CONTENT_1COL",
                title="Uploaded template smoke test",
                zones={
                    "body": ZoneInstance(
                        component_id="bullet_list",
                        props={"items": [{"text": "Bad decorative logo data must not block the deck.", "level": 0}]},
                    )
                },
            )
        ],
        theme="dark",
    )
    payload = plan.to_payload()
    payload["design_system"]["meta"]["brand_logo"] = {
        "content_type": "image/png",
        "data_base64": "not-a-valid-image",
        "width_in": 1.2,
        "height_in": 0.6,
    }

    content = document_generator._render_agentdeck_pptx_one_shot(payload)

    assert isinstance(content, bytes)
    assert content[:2] == b"PK"
