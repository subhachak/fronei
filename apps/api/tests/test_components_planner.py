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
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import get_settings
from app.schemas import RouteDecision
from app.services.llm_gateway import LLMResult
from app.services.components import (
    ContentBlock,
    DesignPlan,
    DocPlan,
    EvidencePack,
    SectionPlan,
    compose_docplan_to_pptx_render_plan,
    generate_agentdeck_v2_plan,
    generate_design_plan,
    generate_doc_plan,
    generate_presentation_plan,
)
from app.services.components import NarrativePlan, generate_narrative_plan
from app.services.components.planner import (
    _coerce_evidence_need,
    _coerce_narrative_plan,
    _coerce_outline,
    _coerce_presentation_plan,
    _coerce_story_beat,
    _fallback_design_plan,
    _extract_json_candidate,
    _minimal_narrative_plan,
    _minimal_presentation_plan,
)
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


def test_agentdeck_usage_stats_weighting_defaults_off():
    assert get_settings().agentdeck_usage_stats_weighting_enabled is False


def test_generate_doc_plan_ignores_usage_stats_when_weighting_disabled(monkeypatch):
    outline_json = json.dumps({
        "title": "AI Strategy Review",
        "sections": [
            {
                "slide_layout": "CONTENT_1COL",
                "section_title": "Adoption is accelerating",
                "content_brief": "Summarize adoption momentum.",
                "content_tags": ["narrative"],
            },
        ],
    })
    captured = {}

    def _fake_invoke_llm(*args, **kwargs):
        return _llm_result(outline_json)

    def _fake_build_blocks_user_message(outline, *, usage_stats_map=None):
        captured["usage_stats_map"] = usage_stats_map
        return json.dumps({"deck_title": outline["title"], "slides": []}), []

    monkeypatch.setattr("app.services.components.planner.invoke_llm", _fake_invoke_llm)
    monkeypatch.setattr("app.services.components.planner._build_blocks_user_message", _fake_build_blocks_user_message)
    monkeypatch.setattr(
        "app.services.components.planner.get_settings",
        lambda: SimpleNamespace(agentdeck_usage_stats_weighting_enabled=False),
    )

    class ExplodingDb:
        def query(self, *args, **kwargs):
            raise AssertionError("usage stats should not be loaded when weighting is disabled")

    generate_doc_plan("Summarize adoption", _route(), db=ExplodingDb())

    assert captured["usage_stats_map"] == {}


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
# generate_narrative_plan (#141)
# ---------------------------------------------------------------------------


def test_coerce_evidence_need_defaults_priority_when_invalid():
    need = _coerce_evidence_need(
        {"id": "e1", "question": "What is the market size?", "priority": "urgent"},
        fallback_id="e-fallback",
    )
    assert need is not None
    assert need.id == "e1"
    assert need.priority == "medium"


def test_coerce_evidence_need_drops_empty_question():
    assert _coerce_evidence_need({"id": "e1", "question": "   "}, fallback_id="e-fallback") is None


def test_coerce_story_beat_defaults_id_and_purpose():
    beat = _coerce_story_beat(
        {"title": "Context", "message": "Setting the stage", "purpose": "not_a_real_purpose"},
        index=0,
    )
    assert beat is not None
    assert beat.id == "beat-1"
    assert beat.purpose == "analysis"


def test_coerce_story_beat_drops_missing_title_or_message():
    assert _coerce_story_beat({"title": "Only Title"}, index=0) is None
    assert _coerce_story_beat({"message": "Only Message"}, index=0) is None
    assert _coerce_story_beat({"title": "", "message": "x"}, index=0) is None


def test_coerce_narrative_plan_filters_invalid_beats_and_keeps_valid():
    raw = {
        "title": "Q3 Strategy",
        "audience": "Execs",
        "objective": "Decide on rollout",
        "executive_summary": "Summary here",
        "storyline": [
            {
                "id": "b1",
                "title": "Context",
                "message": "Setting the stage",
                "purpose": "context",
                "evidence_needs": [{"id": "e1", "question": "What is market size?", "priority": "high"}],
            },
            {"title": "Bad purpose", "message": "msg", "purpose": "not_a_purpose"},
            {"title": "", "message": "should be dropped"},
            {"message": "no title - dropped"},
        ],
    }
    plan = _coerce_narrative_plan(raw, fallback_title="fallback")
    assert plan.title == "Q3 Strategy"
    assert plan.audience == "Execs"
    assert len(plan.storyline) == 2
    assert plan.storyline[0].evidence_needs[0].priority == "high"
    assert plan.storyline[1].id == "beat-2"
    assert plan.storyline[1].purpose == "analysis"


def test_coerce_narrative_plan_empty_input_falls_back_to_title():
    plan = _coerce_narrative_plan({}, fallback_title="Fallback Title")
    assert plan.title == "Fallback Title"
    assert plan.storyline == []


def test_minimal_narrative_plan_has_three_beat_spine():
    plan = _minimal_narrative_plan("Test Deck")
    assert plan.title == "Test Deck"
    assert [beat.purpose for beat in plan.storyline] == ["context", "recommendation", "closing"]


def test_generate_narrative_plan_total_failure_falls_back(monkeypatch):
    def _fake_invoke_llm(*args, **kwargs):
        raise RuntimeError("LLM unavailable")

    monkeypatch.setattr("app.services.components.planner.invoke_llm", _fake_invoke_llm)

    plan, result = generate_narrative_plan("Quarterly business review for the board", _route())
    assert isinstance(plan, NarrativePlan)
    assert plan.title
    assert len(plan.storyline) == 3
    assert result.fallback_errors


def test_generate_narrative_plan_unparseable_json_falls_back(monkeypatch):
    monkeypatch.setattr(
        "app.services.components.planner.invoke_llm",
        lambda *a, **k: _llm_result("not json at all"),
    )

    plan, _ = generate_narrative_plan("Quarterly business review for the board", _route())
    assert isinstance(plan, NarrativePlan)
    assert len(plan.storyline) == 3


def test_generate_narrative_plan_happy_path(monkeypatch):
    narrative_json = json.dumps({
        "title": "AI Adoption Strategy",
        "audience": "Executive Steering Committee",
        "objective": "Approve Phase 2 investment",
        "executive_summary": "Adoption is ahead of plan; recommend doubling down.",
        "storyline": [
            {
                "id": "context",
                "title": "Where We Stand",
                "message": "Adoption has accelerated across three business units.",
                "purpose": "context",
            },
            {
                "id": "evidence",
                "title": "What the Data Shows",
                "message": "Unit A throughput is up 35% quarter over quarter.",
                "purpose": "evidence",
                "evidence_needs": [
                    {"id": "ev1", "question": "What is Unit A's throughput trend?", "priority": "high"},
                ],
            },
            {
                "id": "decision",
                "title": "The Ask",
                "message": "Approve funding for Phase 2 rollout.",
                "purpose": "decision",
            },
        ],
    })

    monkeypatch.setattr(
        "app.services.components.planner.invoke_llm",
        lambda *a, **k: _llm_result(narrative_json),
    )

    plan, result = generate_narrative_plan("Summarize our AI adoption progress", _route())
    assert plan.title == "AI Adoption Strategy"
    assert plan.audience == "Executive Steering Committee"
    assert len(plan.storyline) == 3
    assert plan.storyline[1].evidence_needs[0].question.startswith("What is Unit A's")
    assert result.fallback_errors == []


# ---------------------------------------------------------------------------
# AgentDeck v2 planner additions (#142/#157/#143)
# ---------------------------------------------------------------------------


def _narrative() -> NarrativePlan:
    return NarrativePlan(
        title="AI Platform Consolidation",
        audience="Steering Committee",
        storyline=[
            {
                "id": "context",
                "title": "Context",
                "message": "Fragmented AI tooling is slowing delivery.",
                "purpose": "context",
                "audience_question": "Why change now?",
            },
            {
                "id": "decision",
                "title": "Decision",
                "message": "Approve the consolidated platform path.",
                "purpose": "decision",
                "audience_question": "What should we approve?",
            },
        ],
    )


def test_minimal_presentation_plan_maps_story_beats_to_slides():
    plan = _minimal_presentation_plan(_narrative(), theme="dark")
    assert plan.title == "AI Platform Consolidation"
    assert [section.slide_id for section in plan.sections] == ["context", "decision"]
    assert plan.sections[0].slide_layout == "CONTENT_1COL"
    assert plan.sections[1].slide_layout == "CONTENT_SPLIT_DECISIONS"


def test_coerce_presentation_plan_preserves_v2_fields():
    raw = {
        "title": "AI Platform Consolidation",
        "sections": [
            {
                "slide_id": "s1",
                "slide_layout": "CONTENT_1COL",
                "section_title": "Fragmentation slows delivery",
                "dek": "Multiple tools create duplicate governance.",
                "purpose": "analysis",
                "audience_question": "Where is the drag?",
                "message": "The current landscape creates avoidable cycle time.",
                "evidence": [{"evidence_id": "e1", "confidence": "high"}],
            }
        ],
    }
    plan = _coerce_presentation_plan(raw, _narrative(), theme="dark")
    assert plan.sections[0].slide_id == "s1"
    assert plan.sections[0].dek == "Multiple tools create duplicate governance."
    assert plan.sections[0].evidence[0].evidence_id == "e1"


def test_generate_presentation_plan_falls_back_on_llm_failure(monkeypatch):
    monkeypatch.setattr("app.services.components.planner.invoke_llm", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    plan, result = generate_presentation_plan(_narrative(), EvidencePack(), _route())
    assert isinstance(plan, DocPlan)
    assert len(plan.sections) == 2
    assert result.fallback_errors


def test_generate_design_plan_fallback_has_treatments(monkeypatch):
    monkeypatch.setattr("app.services.components.planner.invoke_llm", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    presentation = _minimal_presentation_plan(_narrative())
    design, result = generate_design_plan(presentation, _route())
    assert isinstance(design, DesignPlan)
    assert len(design.slide_treatments) == len(presentation.sections)
    assert design.slide_treatments[0].component_choices
    assert result.fallback_errors


def test_fallback_design_plan_matches_presentation_sections():
    presentation = _minimal_presentation_plan(_narrative())
    design = _fallback_design_plan(presentation)
    assert [t.slide_id for t in design.slide_treatments] == [s.slide_id for s in presentation.sections]


def test_generate_agentdeck_v2_plan_additive_wrapper(monkeypatch):
    responses = [
        json.dumps(_narrative().model_dump(mode="json")),
        json.dumps({
            "title": "AI Platform Consolidation",
            "sections": [
                {
                    "slide_id": "context",
                    "slide_layout": "CONTENT_1COL",
                    "section_title": "Fragmentation slows delivery",
                    "dek": "Multiple tools create duplicate governance.",
                    "purpose": "analysis",
                    "audience_question": "Where is the drag?",
                    "message": "The current landscape creates avoidable cycle time.",
                }
            ],
        }),
        json.dumps({
            "design_system": "agentdeck_v1",
            "theme": "dark",
            "slide_treatments": [
                {
                    "slide_id": "context",
                    "visual_role": "analysis",
                    "layout_id": "CONTENT_1COL",
                    "component_choices": {"body": ["bullet_list"]},
                }
            ],
        }),
        json.dumps({
            "title": "AI Platform Consolidation",
            "sections": [
                {
                    "slide_layout": "CONTENT_1COL",
                    "section_title": "Fragmentation slows delivery",
                    "content_brief": "Explain why duplicate tooling slows delivery.",
                    "content_tags": ["narrative"],
                }
            ],
        }),
        json.dumps({
            "sections": [
                {
                    "index": 0,
                    "blocks": [
                        {
                            "zone": "body",
                            "component_id": "bullet_list",
                            "data": {"items": [{"text": "Duplicate tools create duplicate reviews."}]},
                        }
                    ],
                }
            ]
        }),
    ]

    def _fake_invoke_llm(*args, **kwargs):
        return _llm_result(responses.pop(0))

    monkeypatch.setattr("app.services.components.planner.invoke_llm", _fake_invoke_llm)

    doc_plan, design_plan, result = generate_agentdeck_v2_plan("Build a steering committee deck", _route())
    assert doc_plan.sections[0].slide_id == "context"
    assert doc_plan.sections[0].dek == "Multiple tools create duplicate governance."
    assert doc_plan.sections[0].blocks[0].component_id == "bullet_list"
    assert design_plan.slide_treatments[0].slide_id == "context"
    assert result.fallback_errors == []


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
