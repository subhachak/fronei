import pytest
from app.services.document_extractor import (
    MAX_CHARS,
    ExtractionError,
    _extract_csv,
    _extract_xlsx,
    extract_text,
)


def test_extract_txt_plain():
    text, pages, truncated, method = extract_text("note.txt", b"Hello world")
    assert text == "Hello world"
    assert method == "parser"
    assert not truncated


def test_extract_csv_produces_markdown_table():
    content = b"name,role\nSubh,Architect\nJane,Engineer"
    text, _, _, _ = extract_text("data.csv", content)
    assert "| name | role |" in text
    assert "Subh" in text


def test_unsupported_type_raises():
    with pytest.raises(ExtractionError, match="Unsupported"):
        extract_text("archive.zip", b"fake")


def test_empty_file_raises():
    with pytest.raises(ExtractionError, match="No readable text"):
        extract_text("empty.txt", b"   ")


def test_truncation_at_max_chars():
    big = ("word " * 15000).encode()   # ~75k chars
    text, _, truncated, _ = extract_text("big.txt", big)
    assert truncated
    assert "truncated" in text.lower()
    assert len(text) <= MAX_CHARS + 200


def test_md_preserves_content():
    md = b"# Heading\n\nSome **bold** text and a list:\n- item 1\n- item 2"
    text, _, _, method = extract_text("doc.md", md)
    assert "Heading" in text
    assert method == "parser"
