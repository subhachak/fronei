import json
from pathlib import Path

from app.routers.documents import build_document_artifact
from app.services.brand import brand_profile_from_template_grammar, user_document_profile_from_memory
from app.services.components import ContentBlock, DesignPlan, DocPlan, EvidencePack, NarrativePlan, SectionPlan
from app.services.qa import QAIssue, judge_deck, judge_slide, repair_docplan_for_qa


def test_brand_profile_from_template_grammar_extracts_design_signals():
    grammar = {
        "template_id": "tpl-123",
        "colors": ["112233", "FFFFFF"],
        "fonts": ["Aptos", "Georgia"],
        "available_slide_types": ["cover", "decision"],
        "observed_slide_roles": ["hero_cover", "data_exhibit"],
    }

    profile = brand_profile_from_template_grammar(grammar, user_id="user-1")

    assert profile.id == "tpl-123"
    assert profile.source_template_id == "tpl-123"
    assert profile.color_tokens == ["112233", "FFFFFF"]
    assert "decision" in profile.layout_preferences
    assert "hero_cover" in profile.extracted_components


def test_user_document_profile_from_memory_uses_existing_profile_json():
    profile = user_document_profile_from_memory(
        "user-1",
        {
            "preferred_tone": "executive",
            "preferred_slide_density": "sparse",
            "common_audiences": ["CIO", "board"],
            "communication_style": "Concise, direct, specific.",
            "key_preferences": ["Avoid generic AI slop"],
        },
    )

    assert profile.preferred_tone == "executive"
    assert profile.preferred_slide_density == "sparse"
    assert profile.common_audiences == ["CIO", "board"]
    assert profile.writing_style == "Concise, direct, specific."
    assert profile.past_rejected_patterns == ["Avoid generic AI slop"]


def test_repair_docplan_trims_overflow_and_strips_dangling_separator():
    plan = DocPlan(
        title="Deck",
        sections=[
            SectionPlan(
                slide_id="s1",
                slide_layout="CONTENT_1COL",
                section_title="Decision —",
                blocks=[
                    ContentBlock(
                        block_id="b1",
                        zone="body",
                        component_id="bullet_list",
                        data={"items": [{"text": f"Point {i} —"} for i in range(8)]},
                    )
                ],
            )
        ],
    )
    issues = [
        QAIssue(type="dangling_punctuation", slide=2, slide_id="s1", block_id="b1", detail="dangling"),
        QAIssue(type="too_many_items", severity="error", slide=2, slide_id="s1", block_id="b1", detail="too many"),
    ]

    repaired, changed = repair_docplan_for_qa(plan, issues)

    assert changed is True
    assert repaired.sections[0].section_title == "Decision"
    assert len(repaired.sections[0].blocks[0].data["items"]) == 5
    assert repaired.sections[0].blocks[0].data["items"][0]["text"] == "Point 0"


def test_lighthouse_fixture_strict_acceptance_gate():
    fixture_path = Path(__file__).parent / "golden" / "lighthouse" / "enterprise_ai_platform_consolidation_smoke.json"
    fixture = json.loads(fixture_path.read_text())

    doc_plan = DocPlan(
        title="Enterprise AI Platform Consolidation",
        subtitle="Steering Committee Decision Brief",
        theme=fixture["expected_theme"],
        sections=[
            SectionPlan(
                slide_id="s1",
                slide_layout="CONTENT_1COL",
                section_title="Fragmented AI tooling is costing us speed and control",
                dek="Duplicate platforms create governance drag, shadow spend, and slower delivery.",
                purpose="context",
                message="Fragmentation is now an operating-model risk.",
                blocks=[
                    ContentBlock(
                        block_id="b1",
                        zone="body",
                        component_id="bullet_list",
                        data={"items": [{"text": "Multiple AI/ML stacks duplicate reviews"}, {"text": "Platform teams repeat controls and enablement"}]},
                    )
                ],
            ),
            SectionPlan(
                slide_id="s2",
                slide_layout="CONTENT_SPLIT_DECISIONS",
                section_title="Approve managed consolidation with federated ownership",
                dek="Centralize the foundation while keeping domains accountable for outcomes.",
                purpose="decision",
                message="Approve the consolidated platform path.",
                blocks=[
                    ContentBlock(
                        block_id="b2",
                        zone="left_panel",
                        component_id="decision_list",
                        data={"cards": [{"title": "Decision", "body": "Approve preferred platform standard"}]},
                    ),
                    ContentBlock(
                        block_id="b3",
                        zone="right_panel",
                        component_id="decision_list",
                        data={"cards": [{"title": "Next step", "body": "Authorize Phase 1 migration plan"}]},
                    ),
                ],
            ),
        ],
    )
    design = DesignPlan(slide_treatments=[{"slide_id": s.slide_id, "layout_id": s.slide_layout} for s in doc_plan.sections])
    narrative = NarrativePlan(title=doc_plan.title, storyline=[{"id": "s1", "title": "Context", "message": "Fragmentation is a risk."}])
    slide_results = [judge_slide(slide_id=s.slide_id, slide_number=i + 2, issues=[]) for i, s in enumerate(doc_plan.sections)]
    deck_result = judge_deck(
        doc_plan=doc_plan,
        slide_results=slide_results,
        narrative_plan=narrative,
        evidence_pack=EvidencePack(),
        design_plan=design,
    )
    preview = build_document_artifact(doc_plan.title, doc_plan.model_dump_json(), "presentation", fmt="pptx")

    assert deck_result.status in {"pass", "warn"}
    assert deck_result.executive_readiness_score >= 0.75
    assert preview["format"] == "pptx"
    assert preview.get("pptx_base64")
