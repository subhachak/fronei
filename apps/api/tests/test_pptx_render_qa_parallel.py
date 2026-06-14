from app.services import pptx_render_qa


def test_parallel_slide_inspection_preserves_slide_order(monkeypatch):
    monkeypatch.setattr(
        pptx_render_qa,
        "_ink_ratio",
        lambda path: {"slide-1.png": 0.01, "slide-2.png": 0.40, "slide-3.png": 0.0}[str(path)],
    )

    results = pptx_render_qa._inspect_rendered_slides_parallel(
        ["short text", "word " * 220, ""],
        ["slide-1.png", "slide-2.png", "slide-3.png"],  # type: ignore[list-item]
        3,
    )

    assert [item["metrics"]["slide"] for item in results] == [1, 2, 3]
    assert results[0]["issues"] == []
    assert any(issue["type"] == "dense_text" for issue in results[1]["issues"])
    assert any(issue["type"] == "dense_ink" for issue in results[1]["issues"])
    assert any(issue["type"] == "blank" for issue in results[2]["issues"])


def test_slide_inspection_flags_extreme_text_as_tiny_text_risk(monkeypatch):
    monkeypatch.setattr(pptx_render_qa, "_ink_ratio", lambda path: 0.10)

    result = pptx_render_qa._inspect_rendered_slide(2, "x" * 1500, "slide-2.png")  # type: ignore[arg-type]

    assert result["metrics"]["char_count"] == 1500
    assert any(issue["type"] == "dense_text" for issue in result["issues"])
    assert any(issue["type"] == "tiny_text_risk" for issue in result["issues"])
