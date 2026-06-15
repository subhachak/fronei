import json

from app.routers import documents
from app.config import get_settings
from app.services.components import ContentBlock, DocPlan, SectionPlan


def test_build_document_artifact_generates_pptx_payload():
    preview = documents.build_document_artifact(
        "Client AI Strategy",
        "# Client AI Strategy\n\n## Recommendation\n- Move in phases\n",
        "presentation",
        "pptx",
    )

    assert preview["format"] == "pptx"
    assert preview["requested_format"] == "pptx"
    assert preview["filename"].endswith(".pptx")
    assert preview["pptx_base64"]
    assert "generation_error" not in preview


def test_build_document_artifact_reports_unsupported_format():
    preview = documents.build_document_artifact(
        "Client AI Strategy",
        "# Client AI Strategy\n\nBody",
        "executive_report",
        "pdf",
    )

    assert preview["format"] == "markdown"
    assert preview["requested_format"] == "pdf"
    assert "not supported" in preview["generation_error"]


def test_build_document_artifact_reports_render_failure(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("template unavailable")

    monkeypatch.setattr(documents, "generate_pptx_bytes", fail)
    preview = documents.build_document_artifact(
        "Client AI Strategy",
        "# Client AI Strategy\n\n## Recommendation\n- Move in phases\n",
        "presentation",
        "pptx",
    )

    assert preview["format"] == "failed"
    assert preview["requested_format"] == "pptx"
    assert preview["markdown"] == ""
    assert preview["generation_failure"]["stage"] == "renderer"
    assert preview["generation_failure"]["retryable"] is True
    assert "PowerPoint rendering failed" in preview["generation_error"]


def test_build_document_artifact_agentdeck_render_failure_returns_generation_failure(monkeypatch):
    doc_plan = DocPlan(
        title="AI Strategy Review",
        sections=[
            SectionPlan(
                slide_layout="CONTENT_1COL",
                section_title="Adoption is accelerating",
                dek="Early teams are already moving from experimentation to production.",
                blocks=[
                    ContentBlock(
                        zone="body",
                        component_id="bullet_list",
                        data={"items": [{"text": "Unit A live", "level": 0}]},
                    )
                ],
            ),
        ],
    )

    def fail_agentdeck(*args, **kwargs):
        raise RuntimeError("agentdeck unavailable")

    def legacy_must_not_run(*args, **kwargs):
        raise AssertionError("legacy renderer should not be called after AgentDeck failure")

    monkeypatch.setattr(documents, "generate_agentdeck_pptx_bytes", fail_agentdeck)
    monkeypatch.setattr(documents, "generate_pptx_bytes", legacy_must_not_run)

    preview = documents.build_document_artifact("", doc_plan.model_dump_json(), "presentation", "pptx")

    # Per #152: a renderer failure must surface as a DocumentGenerationFailure,
    # not silently degrade to the legacy python-pptx fallback (which would
    # reintroduce V1-era artifacts like risk_register mislabeling).
    assert preview["format"] == documents.DOCUMENT_FAILURE_FORMAT
    assert preview["generation_failure"]["stage"] == "renderer"
    assert "agentdeck unavailable" in preview["generation_failure"]["debug_info"]


def test_build_document_artifact_agentdeck_compose_failure_does_not_use_legacy_renderer(monkeypatch):
    doc_plan = DocPlan(
        title="AI Strategy Review",
        sections=[SectionPlan(slide_layout="CLOSING", closing_text="Approve Phase 2 funding")],
    )

    def fail_compose(*args, **kwargs):
        raise RuntimeError("bad plan")

    def legacy_must_not_run(*args, **kwargs):
        raise AssertionError("legacy renderer should not be called after AgentDeck compose failure")

    monkeypatch.setattr(documents, "compose_docplan_to_pptx_render_plan", fail_compose)
    monkeypatch.setattr(documents, "generate_pptx_bytes", legacy_must_not_run)

    preview = documents.build_document_artifact("", doc_plan.model_dump_json(), "presentation", "pptx")

    assert preview["format"] == "failed"
    assert preview["requested_format"] == "pptx"
    assert "pptx_base64" not in preview
    assert preview["generation_failure"]["stage"] == "composer"
    assert "bad plan" in preview["generation_failure"]["debug_info"]


def test_build_document_artifact_missing_brand_design_system_falls_back():
    doc_plan = DocPlan(
        title="AI Strategy Review",
        design_system="brand_u1_missing_uploaded_template",
        sections=[
            SectionPlan(
                slide_layout="CONTENT_1COL",
                section_title="Adoption is accelerating",
                dek="Early teams are already moving from experimentation to production.",
                blocks=[
                    ContentBlock(
                        zone="body",
                        component_id="bullet_list",
                        data={"items": [{"text": "Unit A live", "level": 0}]},
                    )
                ],
            ),
        ],
    )

    preview = documents.build_document_artifact("", doc_plan.model_dump_json(), "presentation", "pptx")

    assert preview["format"] == "pptx"
    assert preview["pptx_base64"]
    assert "generation_failure" not in preview


def test_build_document_artifact_repairs_dense_slide_via_render_qa(monkeypatch):
    assert get_settings().pptx_render_qa_enabled is False

    deck_plan = json.dumps({
        "title": "Client AI Strategy",
        "slides": [
            {
                "layout": "bullets",
                "title": "Roadmap",
                "bullets": [
                    "Bullet number 1 with some supporting detail",
                    "Bullet number 2 with some supporting detail",
                    "Bullet number 3 with some supporting detail",
                    "Bullet number 4 with some supporting detail",
                    "Bullet number 5 with some supporting detail",
                    "Bullet number 6 with some supporting detail",
                ],
            },
        ],
    })

    qa_results = [
        {
            "available": True,
            "slide_count": 2,
            "issues": [{"slide": 2, "type": "dense_text", "detail": "too much text"}],
        },
        {"available": True, "slide_count": 2, "issues": []},
    ]

    def fake_run_qa(content, *args, **kwargs):
        return qa_results.pop(0)

    monkeypatch.setattr(documents, "run_pptx_render_qa", fake_run_qa)

    preview = documents.build_document_artifact("", deck_plan, "presentation", "pptx", quality_mode="executive")

    assert preview["format"] == "pptx"
    assert "generation_error" not in preview
    assert preview["render_qa"]["repair_iterations"] == 1
    assert preview["render_qa"]["issues"] == []
    # The repaired plan should have one fewer visible bullet, with the
    # dropped bullet's full text preserved in speaker notes rather than lost.
    assert "- Bullet number 6" not in preview["markdown"]
    assert "- Bullet number 5" in preview["markdown"]
    assert "Trimmed for slide density: Bullet number 6" in preview["markdown"]


def test_build_document_artifact_draft_quality_skips_repair_loop(monkeypatch):
    assert get_settings().pptx_render_qa_enabled is False

    deck_plan = json.dumps({
        "title": "Client AI Strategy",
        "slides": [
            {
                "layout": "bullets",
                "title": "Roadmap",
                "bullets": [
                    "Bullet number 1 with some supporting detail",
                    "Bullet number 2 with some supporting detail",
                    "Bullet number 3 with some supporting detail",
                    "Bullet number 4 with some supporting detail",
                    "Bullet number 5 with some supporting detail",
                    "Bullet number 6 with some supporting detail",
                ],
            },
        ],
    })

    def fake_run_qa(content, *args, **kwargs):
        return {
            "available": True,
            "slide_count": 2,
            "issues": [{"slide": 2, "type": "dense_text", "detail": "too much text"}],
        }

    monkeypatch.setattr(documents, "run_pptx_render_qa", fake_run_qa)

    preview = documents.build_document_artifact("", deck_plan, "presentation", "pptx", quality_mode="draft")

    assert preview["format"] == "pptx"
    assert preview["quality_mode"] == "draft"
    assert "render_qa" not in preview
    assert "- Bullet number 6" in preview["markdown"]


def test_build_document_artifact_executive_quality_runs_vision_judge(monkeypatch):
    doc_plan = DocPlan(
        title="AI Strategy Review",
        sections=[
            SectionPlan(
                slide_layout="CONTENT_1COL",
                section_title="Adoption is accelerating",
                dek="Early teams are already moving from experimentation to production.",
                blocks=[
                    ContentBlock(
                        zone="body",
                        component_id="bullet_list",
                        data={"items": [{"text": "Unit A live", "level": 0}]},
                    )
                ],
            ),
        ],
    )
    include_images_values: list[bool] = []

    def fake_run_qa(content, *args, **kwargs):
        include_images_values.append(bool(kwargs.get("include_images")))
        return {
            "available": True,
            "slide_count": 2,
            "issues": [],
            "images": [{"slide": 1, "mime_type": "image/png", "base64": "abc"}] if kwargs.get("include_images") else [],
        }

    def fake_judge(*, doc_plan, render_qa):
        return [], [{"slide": 1, "status": "pass", "score": 0.95, "issues": []}], type(
            "JudgeResult",
            (),
            {"model_used": "vision-test", "latency_ms": 12, "estimated_cost_usd": 0.001},
        )()

    monkeypatch.setattr(documents, "run_pptx_render_qa", fake_run_qa)
    monkeypatch.setattr(documents, "judge_rendered_slides", fake_judge)

    preview = documents.build_document_artifact(
        "", doc_plan.model_dump_json(), "presentation", "pptx", quality_mode="executive"
    )

    assert preview["format"] == "pptx"
    assert include_images_values == [True]
    assert preview["render_qa"]["vision_judge"]["available"] is True
    assert preview["render_qa"]["vision_judge"]["model"] == "vision-test"
    assert "images" not in preview["render_qa"]


def test_build_document_artifact_deferred_executive_qa_returns_queued_status(monkeypatch):
    doc_plan = DocPlan(
        title="AI Strategy Review",
        sections=[
            SectionPlan(
                slide_layout="CONTENT_1COL",
                section_title="Adoption is accelerating",
                dek="Early teams are already moving from experimentation to production.",
                blocks=[
                    ContentBlock(
                        zone="body",
                        component_id="bullet_list",
                        data={"items": [{"text": "Unit A live", "level": 0}]},
                    )
                ],
            ),
        ],
    )

    def fake_run_qa(*args, **kwargs):
        raise AssertionError("render QA should be deferred")

    monkeypatch.setattr(documents, "run_pptx_render_qa", fake_run_qa)

    preview = documents.build_document_artifact(
        "",
        doc_plan.model_dump_json(),
        "presentation",
        "pptx",
        quality_mode="executive",
        defer_render_qa=True,
    )

    assert preview["format"] == "pptx"
    assert preview["pptx_base64"]
    assert preview["render_qa"]["status"] == "queued"
    assert preview["render_qa"]["available"] is False


def test_build_document_artifact_standard_quality_skips_vision_judge(monkeypatch):
    doc_plan = DocPlan(
        title="AI Strategy Review",
        sections=[
            SectionPlan(
                slide_layout="CONTENT_1COL",
                section_title="Adoption is accelerating",
                dek="Early teams are already moving from experimentation to production.",
                blocks=[
                    ContentBlock(
                        zone="body",
                        component_id="bullet_list",
                        data={"items": [{"text": "Unit A live", "level": 0}]},
                    )
                ],
            ),
        ],
    )
    include_images_values: list[bool] = []

    def fake_run_qa(content, *args, **kwargs):
        include_images_values.append(bool(kwargs.get("include_images")))
        return {"available": True, "slide_count": 2, "issues": []}

    def fake_judge(*args, **kwargs):
        raise AssertionError("vision judge should not run for standard quality")

    monkeypatch.setattr(documents, "run_pptx_render_qa", fake_run_qa)
    monkeypatch.setattr(documents, "judge_rendered_slides", fake_judge)

    preview = documents.build_document_artifact(
        "", doc_plan.model_dump_json(), "presentation", "pptx", quality_mode="standard"
    )

    assert preview["format"] == "pptx"
    assert include_images_values == []
    assert "render_qa" not in preview


def test_build_document_artifact_uses_readable_preview_for_deck_plan_json():
    deck_plan = json.dumps({
        "title": "Client AI Strategy",
        "slides": [
            {"layout": "bullets", "title": "The decision is timing-sensitive", "bullets": ["Move in phases"]},
        ],
    })

    preview = documents.build_document_artifact("", deck_plan, "presentation", "pptx")

    assert preview["title"] == "Client AI Strategy"
    assert preview["format"] == "pptx"
    assert preview["filename"] == "client-ai-strategy.pptx"
    assert preview["markdown"].startswith("# Client AI Strategy")
    assert "## The decision is timing-sensitive" in preview["markdown"]
    assert not preview["markdown"].lstrip().startswith("{")


def test_build_document_artifact_exposes_parallel_composition_for_deck_plan_json():
    deck_plan = json.dumps({
        "title": "Board Briefing",
        "slides": [
            {"layout": "section", "title": "Context"},
            {"layout": "bullets", "title": "Savings proof", "stats": [{"value": "$4.2M", "label": "Savings"}]},
            {
                "layout": "bullets",
                "title": "Phased rollout",
                "phases": [{"label": "Q1", "title": "Foundation"}, {"label": "Q2", "title": "Scale"}],
            },
        ],
    })

    preview = documents.build_document_artifact("", deck_plan, "presentation", "markdown")

    assert preview["format"] == "pptx"
    assert preview["requested_format"] == "pptx"
    assert preview["filename"] == "board-briefing.pptx"
    assert preview["pptx_base64"]
    assert preview["composition"]["parallel"] is True
    assert preview["composition"]["slide_count"] == 3
    assert preview["composition"]["workers"] >= 2
    assert preview["composition"]["changed_slides"] == [1, 2, 3]
    assert preview["composition"]["archetypes"] == ["section_divider", "investment_case", "roadmap"]
    assert "**$4.2M**" in preview["markdown"]
    assert "Q1: Foundation" in preview["markdown"]


def test_build_document_preview_detected_presentation_generates_pptx():
    preview = documents.build_document_preview(
        "Create a PowerPoint presentation for the steering committee.",
        "# Steering Committee Update\n\n## Recommendation\n- Approve phase 1\n",
    )

    assert preview is not None
    assert preview["doc_type"] == "presentation"
    assert preview["format"] == "pptx"
    assert preview["requested_format"] == "pptx"
    assert preview["filename"].endswith(".pptx")
    assert preview["pptx_base64"]
