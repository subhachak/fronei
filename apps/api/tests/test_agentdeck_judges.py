from app.services.components import ContentBlock, DesignPlan, DocPlan, EvidencePack, NarrativePlan, SectionPlan
from app.services.qa import QAIssue, judge_deck, judge_slide
from app.services.qa import vision_judge


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


def test_deck_judge_thresholds_follow_quality_mode():
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
    slide_result = judge_slide(slide_id="s1", slide_number=2, issues=[])

    draft = judge_deck(doc_plan=doc_plan, slide_results=[slide_result], quality_mode="draft")
    executive = judge_deck(doc_plan=doc_plan, slide_results=[slide_result], quality_mode="executive")

    assert draft.status == "pass"
    assert executive.status == "warn"


def test_vision_judge_runs_slide_calls_in_parallel_and_preserves_order(monkeypatch):
    doc_plan = DocPlan(
        title="Deck",
        sections=[
            SectionPlan(slide_id="s1", slide_layout="CONTENT_1COL", section_title="One"),
            SectionPlan(slide_id="s2", slide_layout="CONTENT_1COL", section_title="Two"),
            SectionPlan(slide_id="s3", slide_layout="CONTENT_1COL", section_title="Three"),
        ],
    )

    calls: list[int] = []

    def fake_call(_model, image, _context):
        slide = int(image["slide"])
        calls.append(slide)
        return {
            "status": "pass",
            "score": 0.9,
            "summary": f"slide {slide}",
            "issues": [],
            "_usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.001},
        }

    monkeypatch.setattr(vision_judge, "_call_vision_model", fake_call)

    issues, slide_results, result = vision_judge.judge_rendered_slides(
        doc_plan=doc_plan,
        render_qa={
            "images": [
                {"slide": 3, "mime_type": "image/png", "base64": "three"},
                {"slide": 1, "mime_type": "image/png", "base64": "one"},
                {"slide": 2, "mime_type": "image/png", "base64": "two"},
            ]
        },
    )

    assert issues == []
    assert sorted(calls) == [1, 2, 3]
    assert [item["slide"] for item in slide_results] == [3, 1, 2]
    assert result.prompt_tokens == 3
    assert result.completion_tokens == 3
