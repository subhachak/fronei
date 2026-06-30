"""Step 5 document route content extraction (scoring_spec.md §1.7).

Live-validated: a real document-route run (business memo query) produced a
docx artifact whose extracted text was the real 5,257-char memo content
(title, TO/FROM/DATE/RE header, actual policy text) — not the chat
confirmation stub ("Done. I created X.docx") that v1 graded instead.
format_correct=True confirmed end-to-end against that real artifact.

Also fixes a real bug found while building this: research_document was
previously dispatched identically to plain "research" — _run_document was
never called, so research_document cases never actually produced a
document artifact to grade at all. See _run_research_document_blocking.
"""
import base64
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_API_ROOT = Path(__file__).resolve().parents[1]
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from app.routers.evals import _extract_artifact_text, score_format_correct  # noqa: E402


def _docx_bytes(paragraph_text: str) -> bytes:
    """Build a minimal real .docx file in memory for extraction tests —
    document_extractor._extract_docx uses python-docx, so a synthetic
    base64 string won't round-trip; this needs to be an actual docx."""
    import io
    from docx import Document

    doc = Document()
    doc.add_paragraph(paragraph_text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _artifact(kind="docx", filename="test.docx", base64_data=""):
    return SimpleNamespace(kind=kind, filename=filename, base64_data=base64_data)


# ── _extract_artifact_text ───────────────────────────────────────────────────

def test_extract_artifact_text_none_without_base64_data():
    assert _extract_artifact_text(_artifact(base64_data="")) is None


def test_extract_artifact_text_none_on_garbage_data():
    assert _extract_artifact_text(_artifact(base64_data="not-valid-base64-docx-content")) is None


def test_extract_artifact_text_real_docx_roundtrip():
    content = _docx_bytes("This is the real memo content, not a confirmation stub.")
    artifact = _artifact(base64_data=base64.b64encode(content).decode())
    text = _extract_artifact_text(artifact)
    assert text is not None
    assert "real memo content" in text


# ── score_format_correct ────────────────────────────────────────────────────

def test_format_correct_none_without_expected_format():
    assert score_format_correct({"v2_spec": {}}, []) is None


def test_format_correct_false_without_artifacts():
    case = {"v2_spec": {"document_requirements": {"expected_format": "docx"}}}
    assert score_format_correct(case, []) is False


def test_format_correct_false_on_format_mismatch():
    case = {"v2_spec": {"document_requirements": {"expected_format": "pptx"}}}
    assert score_format_correct(case, [_artifact(kind="docx")]) is False


def test_format_correct_true_without_page_count_check():
    case = {"v2_spec": {"document_requirements": {"expected_format": "docx"}}}
    assert score_format_correct(case, [_artifact(kind="docx")]) is True


def test_format_correct_with_page_count_within_tolerance():
    # ~500 words = 1 page; this paragraph is short, well under 500*2 words,
    # so estimated_pages=1, matching expected_page_count=1.
    content = _docx_bytes("Short memo. " * 50)
    case = {"v2_spec": {"document_requirements": {"expected_format": "docx", "expected_page_count": 1}}}
    artifact = _artifact(base64_data=base64.b64encode(content).decode())
    assert score_format_correct(case, [artifact]) is True


def test_format_correct_false_when_page_count_way_off():
    content = _docx_bytes("word " * 20)  # ~20 words, still estimates to 1 page minimum
    case = {"v2_spec": {"document_requirements": {"expected_format": "docx", "expected_page_count": 10}}}
    artifact = _artifact(base64_data=base64.b64encode(content).decode())
    # estimated_pages=1 (max(1, round(20/500))), expected=10, diff=9 > 1 tolerance
    assert score_format_correct(case, [artifact]) is False


def test_format_correct_false_when_extraction_fails():
    case = {"v2_spec": {"document_requirements": {"expected_format": "docx", "expected_page_count": 1}}}
    artifact = _artifact(base64_data="garbage-not-a-real-docx")
    assert score_format_correct(case, [artifact]) is False
