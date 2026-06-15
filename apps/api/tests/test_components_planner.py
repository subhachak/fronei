"""Tests for the Phase 3 structured-output planner stack (#120-126 of
agentdeck_framework_architecture.md §4):

  - `DocPlan`/`SectionPlan`/`ContentBlock` validation (#120)
  - component-selection ranking (#121)
  - `generate_doc_plan` two-step planner with graceful degradation (#122)
  - `compose_docplan_to_pptx_render_plan` (#123)
  - end-to-end `generate_document_output` -> `build_document_artifact`
    wiring for presentations (#124)
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.schemas import RouteDecision
from app.services.llm_gateway import LLMResult
from app.services.components import (
    ContentBlock,
    DocPlan,
    SectionPlan,
    compose_docplan_to_pptx_render_plan,
    generate_doc_plan,
)
from app.services.components.planner import _coerce_outline, _extract_json_candidate
from app.services.components.selection import rank_components


# ---------------------------------------------------------------------------
# DocPlan / SectionPlan / ContentBlock validation (#120)
# ---------------------------------------------------------------------------


def test_docplan_requires_only_title():
    plan = DocPlan(title="Just a Title")
    assert plan.sections == []
    assert plan.doc_type == "presentation"
    assert plan.design_system == "agentdeck_v1"
    assert plan.theme == "dark"


def test_content_block_validates_against_component_schema():
    block = ContentBlock(
        zone="body",
        component_id="bullet_list",
        data={"items": [{"text": "Hello", "level": 0}]},
    )
    assert block.data["items"][0]["text"] == "Hello"


def test_content_block_rejects_invalid_data_for_component():
    with pytest.raises(ValidationError):
        ContentBlock(zone="body", component_id="bullet_list", data={"items": "not-a-list"})


def test_section_plan_rejects_blocks_on_dedicated_layout():
    with pytest.raises(ValidationError):
        SectionPlan(
            slide_layout="TITLE",
            blocks=[ContentBlock(zone="body", component_id="bullet_list", data={"items": []})],
        )


def test_section_plan_accepts_valid_generic_content_block():
    section = SectionPlan(
        slide_layout="CONTENT_1COL",
        section_title="Overview",
        blocks=[ContentBlock(zone="body", component_id="bullet_list", data={"items": [{"text": "A", "level": 0}]})],
    )
    assert section.blocks[0].component_id == "bullet_list"


# ---------------------------------------------------------------------------
# Component-selection ranking (#121)
# ---------------------------------------------------------------------------


def test_rank_components_prefers_tag_overlap():
    ranked = rank_components("CONTENT_1COL", ["financial", "comparison"])
    ranked_ids = [c.id for c in ranked]
    # `table` has selection_tags including "comparison"; `bullet_list` has none
    # of the requested tags. Tag-matching components should rank above it.
    assert ranked_ids.index("table") < ranked_ids.index("bullet_list")


def test_rank_components_with_no_tags_is_stable_and_nonempty():
    ranked = rank_components("CONTENT_1COL", [])
    assert ranked
    # No tags -> all scores driven by usage_stats only; still a valid ordering.
    assert all(hasattr(c, "id") for c in ranked)


def test_rank_components_excludes_non_block_components():
    ranked = rank_components("CONTENT_1COL", ["section_label"])
    ids = {c.id for c in ranked}
    assert "header_bar" not in ids
    assert "callout_bar" not in ids


# ---------------------------------------------------------------------------
# JSON extraction / outline coercion helpers (#122)
# ---------------------------------------------------------------------------


def test_extract_json_candidate_handles_fenced_block():
    content = "Here you go:\n```json\n{\"title\": \"Deck\"}\n```\nThanks."
    assert json.loads(_extract_json_candidate(content)) == {"title": "Deck"}


def test_extract_json_candidate_handles_raw_object():
    assert json.loads(_extract_json_candidate('{"title": "Deck"}')) == {"title": "Deck"}


def test_extract_json_candidate_tolerant_fallback():
    content = "Sure, here's the outline: {\"title\": \"Deck\"} -- let me know what you think!"
    assert json.loads(_extract_json_candidate(content)) == {"title": "Deck"}


def test_extract_json_candidate_returns_none_for_no_json():
    assert _extract_json_candidate("no json here") is None


def test_coerce_outline_drops_unknown_layout_and_caps_sections():
    raw = {
        "title": "Strategy Review",
        "subtitle": "Q3",
        "theme": "light",
        "sections": [{"slide_layout": "NOT_REAL", "section_title": "x"}]
        + [{"slide_layout": "CONTENT_1COL", "section_title": f"S{i}"} for i in range(20)],
    }
    outline = _coerce_outline(raw)
    assert outline["title"] == "Strategy Review"
    assert outline["theme"] == "light"
    assert len(outline["sections"]) == 14
    assert all(s["slide_layout"] == "CONTENT_1COL" for s in outline["sections"])


def test_coerce_outline_defaults_missing_title_and_theme():
    outline = _coerce_outline({"sections": []})
    assert outline["title"] == "Untitled Deck"
    assert outline["theme"] == "dark"


# ---------------------------------------------------------------------------
# generate_doc_plan (#122) — graceful degradation
# ---------------------------------------------------------------------------


def _route() -> RouteDecision:
    return RouteDecision(
        task_type="writing",
        complexity="high",
        profile="balanced",
        primary_model="test-model",
        fallbacks=[],
        reason="test",
    )


def _llm_result(answer: str) -> LLMResult:
    return LLMResult(
        answer=answer,
        model_used="test-model",
        latency_ms=1,
        prompt_tokens=10,
        completion_tokens=10,
        estimated_cost_usd=0.0,
    )


def test_generate_doc_plan_total_failure_falls_back_to_closing(monkeypatch):
    def _fake_invoke_llm(*args, **kwargs):
        return _llm_result("not json at all")

    monkeypatch.setattr("app.services.components.planner.invoke_llm", _fake_invoke_llm)

    doc_plan, result = generate_doc_plan("Write a deck about nothing", _route())
    assert isinstance(doc_plan, DocPlan)
    assert doc_plan.title  # falls back to "Untitled Deck"
    assert len(doc_plan.sections) == 1
    assert doc_plan.sections[0].slide_layout == "CLOSING"
    assert doc_plan.sections[0].closing_text == "Thank You"
    assert result.model_used == "test-model"


def test_generate_doc_plan_happy_path(monkeypatch):
    outline_json = json.dumps({
        "title": "AI Strategy Review",
        "subtitle": "Executive Briefing",
        "theme": "dark",
        "sections": [
            {
                "slide_layout": "SECTION_HEADER",
                "section_number": "01",
                "section_title": "Context",
                "section_subtitle": "Where we stand",
            },
            {
                "slide_layout": "CONTENT_1COL",
                "section_title": "Adoption is accelerating across the portfolio",
                "content_brief": "Summarize adoption momentum across three business units.",
                "content_tags": ["narrative", "summary", "key_points"],
            },
            {
                "slide_layout": "CLOSING",
                "closing_text": "Approve Phase 2 funding",
                "closing_body": "Unlocks the next wave of automation.",
            },
        ],
    })
    blocks_json = json.dumps({
        "sections": [
            {
                "index": 1,
                "blocks": [
                    {
                        "zone": "body",
                        "component_id": "bullet_list",
                        "data": {"items": [{"text": "Unit A live in production", "level": 0}]},
                    }
                ],
                "header_bar": None,
                "callout": None,
            }
        ]
    })

    calls = {"n": 0}

    def _fake_invoke_llm(*args, **kwargs):
        calls["n"] += 1
        return _llm_result(outline_json if calls["n"] == 1 else blocks_json)

    monkeypatch.setattr("app.services.components.planner.invoke_llm", _fake_invoke_llm)

    doc_plan, result = generate_doc_plan("Summarize our AI adoption progress", _route())

    assert doc_plan.title == "AI Strategy Review"
    assert doc_plan.subtitle == "Executive Briefing"
    assert doc_plan.theme == "dark"
    assert [s.slide_layout for s in doc_plan.sections] == ["SECTION_HEADER", "CONTENT_1COL", "CLOSING"]

    content_section = doc_plan.sections[1]
    assert content_section.blocks[0].component_id == "bullet_list"
    assert content_section.blocks[0].data["items"][0]["text"] == "Unit A live in production"

    closing = doc_plan.sections[2]
    assert closing.closing_text == "Approve Phase 2 funding"

    assert calls["n"] == 2  # outline + blocks calls
    assert result.fallback_errors == []


def test_generate_doc_plan_invalid_block_falls_back_gracefully(monkeypatch):
    outline_json = json.dumps({
        "title": "Deck",
        "sections": [
            {
                "slide_layout": "CONTENT_1COL",
                "section_title": "Some point",
                "content_brief": "Anything",
                "content_tags": ["narrative"],
            },
        ],
    })
    # Blocks response references a component_id that isn't valid data for bullet_list.
    blocks_json = json.dumps({
        "sections": [
            {"index": 0, "blocks": [{"zone": "body", "component_id": "bullet_list", "data": {"items": "oops"}}]},
        ]
    })

    calls = {"n": 0}

    def _fake_invoke_llm(*args, **kwargs):
        calls["n"] += 1
        return _llm_result(outline_json if calls["n"] == 1 else blocks_json)

    monkeypatch.setattr("app.services.components.planner.invoke_llm", _fake_invoke_llm)

    doc_plan, _ = generate_doc_plan("Anything", _route())
    section = doc_plan.sections[0]
    assert section.slide_layout == "CONTENT_1COL"
    # The invalid block is dropped; either empty blocks or a fallback bullet_list remains valid.
    for block in section.blocks:
        assert block.component_id  # round-tripped through validation successfully


# ---------------------------------------------------------------------------
# compose_docplan_to_pptx_render_plan (#123)
# ---------------------------------------------------------------------------


def test_compose_docplan_prepends_title_slide():
    doc_plan = DocPlan(
        title="Client AI Strategy",
        subtitle="Q3 Review",
        sections=[SectionPlan(slide_layout="CLOSING", closing_text="Thank You")],
    )
    render_plan = compose_docplan_to_pptx_render_plan(doc_plan, theme="dark")
    layouts = [s.slide_layout for s in render_plan.slides]
    assert layouts == ["TITLE", "CLOSING"]
    assert render_plan.slides[0].hero_title == "Client AI Strategy"
    assert render_plan.slides[0].subtitle == "Q3 Review"
    assert render_plan.slides[1].closing_text == "Thank You"
    assert render_plan.theme == "dark"


def test_compose_docplan_maps_blocks_to_zones():
    doc_plan = DocPlan(
        title="Deck",
        sections=[
            SectionPlan(
                slide_layout="CONTENT_1COL",
                section_title="Overview",
                blocks=[
                    ContentBlock(
                        zone="body",
                        component_id="bullet_list",
                        data={"items": [{"text": "Point A", "level": 0}]},
                    )
                ],
            )
        ],
    )
    render_plan = compose_docplan_to_pptx_render_plan(doc_plan)
    slide = render_plan.slides[1]
    assert slide.slide_layout == "CONTENT_1COL"
    assert slide.title == "Overview"
    assert slide.zones["body"].component_id == "bullet_list"
    assert slide.zones["body"].props["items"][0]["text"] == "Point A"


def test_compose_docplan_section_header_uses_section_title_field():
    doc_plan = DocPlan(
        title="Deck",
        sections=[
            SectionPlan(slide_layout="SECTION_HEADER", section_number="01", section_title="Part One", section_subtitle="Overview")
        ],
    )
    render_plan = compose_docplan_to_pptx_render_plan(doc_plan)
    slide = render_plan.slides[1]
    assert slide.slide_layout == "SECTION_HEADER"
    assert slide.section_title == "Part One"
    assert slide.section_subtitle == "Overview"
    assert slide.title is None


def test_compose_docplan_output_valid_for_both_themes():
    doc_plan = DocPlan(
        title="Full Deck",
        subtitle="All branches",
        sections=[
            SectionPlan(slide_layout="SECTION_HEADER", section_number="01", section_title="Intro"),
            SectionPlan(
                slide_layout="CONTENT_1COL",
                section_title="Point",
                blocks=[ContentBlock(zone="body", component_id="bullet_list", data={"items": [{"text": "A", "level": 0}]})],
            ),
            SectionPlan(slide_layout="CLOSING", closing_text="Thanks", closing_body="Questions?"),
        ],
    )
    for theme in ("dark", "light"):
        render_plan = compose_docplan_to_pptx_render_plan(doc_plan, theme=theme)
        assert render_plan.theme == theme
        assert len(render_plan.slides) == len(doc_plan.sections) + 1
        payload = render_plan.to_payload()
        assert payload["slides"][0]["slide_layout"] == "TITLE"


# ---------------------------------------------------------------------------
# End-to-end: DocPlan JSON -> build_document_artifact (#124)
# ---------------------------------------------------------------------------


def test_build_document_artifact_renders_docplan_json():
    from app.routers import documents

    doc_plan = DocPlan(
        title="AI Strategy Review",
        subtitle="Executive Briefing",
        sections=[
            SectionPlan(slide_layout="SECTION_HEADER", section_number="01", section_title="Context"),
            SectionPlan(
                slide_layout="CONTENT_1COL",
                section_title="Adoption is accelerating",
                blocks=[ContentBlock(zone="body", component_id="bullet_list", data={"items": [{"text": "Unit A live", "level": 0}]})],
            ),
            SectionPlan(slide_layout="CLOSING", closing_text="Approve Phase 2 funding"),
        ],
    )
    body = doc_plan.model_dump_json()

    preview = documents.build_document_artifact("", body, "presentation", "pptx")

    assert preview["title"] == "AI Strategy Review"
    assert preview["format"] == "pptx"
    assert preview["markdown"].startswith("# AI Strategy Review")
    assert "## 01 — Context" in preview["markdown"] or "Context" in preview["markdown"]
    assert "Adoption is accelerating" in preview["markdown"]
    assert "Approve Phase 2 funding" in preview["markdown"]


def test_build_document_artifact_still_supports_legacy_deckplan_json():
    from app.routers import documents

    legacy = json.dumps({
        "title": "Client AI Strategy",
        "slides": [
            {"layout": "bullets", "title": "The decision is timing-sensitive", "bullets": ["Move in phases"]},
        ],
    })
    preview = documents.build_document_artifact("", legacy, "presentation", "pptx")
    assert preview["title"] == "Client AI Strategy"
    assert preview["markdown"].startswith("# Client AI Strategy")
    assert "## The decision is timing-sensitive" in preview["markdown"]
