"""Deterministic post-render QA checks (#147).

This wraps the existing LibreOffice/poppler render-QA output in the shared v2
`QAIssue` taxonomy and adds a small whitespace signal from rendered metrics.
"""

from __future__ import annotations

from app.services.design_systems.registry import get_design_system

from .types import QAIssue

_PASSTHROUGH_TYPES = {"blank", "dense_text", "dense_ink", "tiny_text_risk"}


def run_render_checks(render_qa: dict | None, *, design_system_id: str = "agentdeck_v1") -> list[QAIssue]:
    if not render_qa or not render_qa.get("available"):
        return []

    issues: list[QAIssue] = []
    for raw in render_qa.get("issues") or []:
        raw_type = raw.get("type")
        if raw_type in _PASSTHROUGH_TYPES:
            issues.append(
                QAIssue(
                    type=raw_type,
                    severity="warning",
                    stage="render",
                    slide=raw.get("slide"),
                    detail=raw.get("detail") or raw_type,
                )
            )

    thresholds = get_design_system(design_system_id).qa_thresholds
    min_fill = thresholds.whitespace.min_zone_fill_pct
    for metric in render_qa.get("metrics") or []:
        slide = metric.get("slide")
        ink_ratio = metric.get("ink_ratio")
        char_count = metric.get("char_count") or 0
        if ink_ratio is not None and ink_ratio < min_fill and char_count > 0:
            issues.append(
                QAIssue(
                    type="excessive_whitespace",
                    severity="info",
                    stage="render",
                    slide=slide,
                    detail=(
                        f"Rendered slide uses only ~{ink_ratio:.0%} visible ink; "
                        "consider richer layout or larger content treatment."
                    ),
                )
            )
    return issues
