import json

from app.routers import documents
from app.config import get_settings


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

    assert preview["format"] == "markdown"
    assert preview["requested_format"] == "pptx"
    assert "PowerPoint rendering failed" in preview["generation_error"]


def test_build_document_artifact_repairs_dense_slide_via_render_qa(monkeypatch):
    assert get_settings().pptx_render_qa_enabled is True

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

    preview = documents.build_document_artifact("", deck_plan, "presentation", "pptx")

    assert preview["format"] == "pptx"
    assert "generation_error" not in preview
    assert preview["render_qa"]["repair_iterations"] == 1
    assert preview["render_qa"]["issues"] == []
    # The repaired plan should have one fewer visible bullet, with the
    # dropped bullet's full text preserved in speaker notes rather than lost.
    assert "- Bullet number 6" not in preview["markdown"]
    assert "- Bullet number 5" in preview["markdown"]
    assert "Trimmed for slide density: Bullet number 6" in preview["markdown"]


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

    assert preview["composition"]["parallel"] is True
    assert preview["composition"]["slide_count"] == 3
    assert preview["composition"]["workers"] >= 2
    assert preview["composition"]["changed_slides"] == [1, 2, 3]
    assert preview["composition"]["archetypes"] == ["section_divider", "investment_case", "roadmap"]
    assert "**$4.2M**" in preview["markdown"]
    assert "Q1: Foundation" in preview["markdown"]
