import base64
import zipfile
from io import BytesIO

from docx import Document

from app.services.agent.document_ast import document_ast_from_markdown, qa_document_ast, render_docx_from_markdown
from app.services.agent.tool_registry import ToolRegistry
from app.services.agent.tools import Tools


def test_document_ast_parses_sections_lists_and_tables():
    markdown = """# Vendor memo

## Executive summary

This is the opening paragraph with **bold** evidence [S1].

- First implication
- Second implication

## Comparison

| Vendor | Fit |
| --- | --- |
| Tavily | Search |
| You.com | Search |
"""

    ast = document_ast_from_markdown("Vendor memo", markdown)

    assert [section.heading for section in ast.sections] == ["Executive summary", "Comparison"]
    assert ast.sections[0].blocks[0].kind == "paragraph"
    assert ast.sections[0].blocks[1].kind == "bullets"
    assert ast.sections[1].blocks[0].kind == "table"
    assert ast.sections[1].blocks[0].rows[0] == ["Vendor", "Fit"]


def test_document_ast_qa_detects_missing_planned_section():
    ast = document_ast_from_markdown("Plan", "## Executive summary\n\nDone.")

    issues = qa_document_ast(ast, expected_sections=["Executive summary", "Risk register"])

    assert [issue.code for issue in issues] == ["missing_planned_sections"]


def test_render_docx_from_markdown_creates_real_word_document():
    markdown = """# Architecture report

## Executive summary

The system uses an orchestrated worker flow [S1].

## Components

1. Lead agent
2. Search worker

| Component | Role |
| --- | --- |
| Lead agent | Plans and reflects |
| Evidence binder | Maps claims to sources |
"""

    payload, issues = render_docx_from_markdown(
        "Architecture report",
        markdown,
        expected_sections=["Executive summary", "Components"],
    )

    assert issues == []
    with zipfile.ZipFile(BytesIO(payload)) as package:
        assert "word/document.xml" in package.namelist()
        document_xml = package.read("word/document.xml").decode("utf-8")
    doc = Document(BytesIO(payload))
    visible_text = "\n".join(paragraph.text for paragraph in doc.paragraphs)
    assert "Architecture report" in visible_text
    assert "Executive summary" in visible_text
    assert "Lead agent" in visible_text
    assert "w:tbl" in document_xml


def test_make_docx_artifact_uses_ast_renderer_and_reports_qa():
    registry = ToolRegistry(tools=Tools())

    artifact, call = registry.run(
        "make_docx_artifact",
        {
            "title": "Architecture report",
            "markdown": "## Executive summary\n\nDone.",
            "expected_sections": ["Executive summary", "Risk register"],
        },
    )

    assert call.ok
    assert artifact.kind == "docx"
    assert artifact.filename.endswith(".docx")
    assert "missing_planned_sections" in call.output["qa_issue_codes"]
    payload = base64.b64decode(artifact.base64_data)
    with zipfile.ZipFile(BytesIO(payload)) as package:
        assert "word/document.xml" in package.namelist()
