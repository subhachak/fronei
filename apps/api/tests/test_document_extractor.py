from types import SimpleNamespace

import pytest

from app.services import document_extractor, provider_health
from app.services.document_extractor import (
    MAX_CHARS,
    ExtractionError,
    extract_text,
)


@pytest.fixture(autouse=True)
def clean_provider_circuits():
    provider_health.reset_circuit_state()
    yield
    provider_health.reset_circuit_state()


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


def test_vision_extraction_skips_provider_with_open_circuit(monkeypatch):
    for _ in range(provider_health.CIRCUIT_FAILURE_THRESHOLD):
        provider_health.record_provider_failure("Gemini")
    calls: list[str] = []

    def fake_completion(*, model, **_kwargs):
        calls.append(model)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="image text"))]
        )

    monkeypatch.setattr(document_extractor, "completion", fake_completion)

    text, _, _, method = extract_text("image.png", b"not-a-real-image")

    assert calls == [document_extractor._FALLBACK_MODEL]
    assert text == "image text"
    assert method == "vision"
