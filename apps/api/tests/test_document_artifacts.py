import json

from app.routers import documents


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
