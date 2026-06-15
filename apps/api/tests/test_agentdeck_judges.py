from app.services.components import ContentBlock, DesignPlan, DocPlan, EvidencePack, NarrativePlan, SectionPlan
from app.services.qa import QAIssue, judge_deck, judge_slide


def test_slide_judge_passes_when_no_issues():
    result = judge_slide(slide_id="s1", slide_number=2, issues=[])
    assert result.status == "pass"
    assert result.score == 1.0
    assert result.severity == "none"


def test_slide_judge_fails_on_fit_overflow():
    issue = QAIssue(
        type="fit_overflow",
        severity="error",
        stage="plan",
        slide=2,
        slide_id="s1",
        detail="estimated height exceeds zone height",
    )

    result = judge_slide(slide_id="s1", slide_number=2, issues=[issue])

    assert result.status == "fail"
    assert result.repair_strategy == "reduce_copy_or_split_slide"


def test_deck_judge_scores_deck_and_recommends_repairs():
    doc_plan = DocPlan(
        title="Deck",
        sections=[
            SectionPlan(
                slide_id="s1",
                slide_layout="CONTENT_1COL",
                section_title="Decision",
                purpose="decision",
                message="Approve the plan.",
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
    narrative = NarrativePlan(title="Deck", storyline=[{"id": "s1", "title": "Decision", "message": "Approve the plan."}])
    design = DesignPlan(slide_treatments=[{"slide_id": "s1", "layout_id": "CONTENT_1COL"}])
    evidence = EvidencePack()
    slide_result = judge_slide(slide_id="s1", slide_number=2, issues=[])

    result = judge_deck(
        doc_plan=doc_plan,
        slide_results=[slide_result],
        narrative_plan=narrative,
        evidence_pack=evidence,
        design_plan=design,
    )

    assert result.status in {"pass", "warn"}
    assert result.storyline_score > 0
    assert result.design_score == 1.0
