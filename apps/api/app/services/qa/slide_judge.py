"""Slide-level judge contract for AgentDeck v2 (#148).

Phase 3 provides the result model and a deterministic scorer over existing
QA issues. Phase 4 can replace or augment `judge_slide` with a vision-model
critique while preserving this contract.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .types import QAIssue


class SlideJudgeResult(BaseModel):
    slide_id: Optional[str] = None
    slide_number: Optional[int] = None
    status: Literal["pass", "warn", "fail"]
    score: float = Field(ge=0.0, le=1.0)
    severity: Literal["none", "low", "medium", "high"]
    issues: list[QAIssue] = Field(default_factory=list)
    repair_strategy: Optional[str] = None
    summary: str


def judge_slide(
    *,
    slide_id: str | None = None,
    slide_number: int | None = None,
    issues: list[QAIssue] | None = None,
    context: str | None = None,
) -> SlideJudgeResult:
    slide_issues = [
        issue
        for issue in (issues or [])
        if (slide_id and issue.slide_id == slide_id) or (slide_number and issue.slide == slide_number)
    ]
    if not slide_issues:
        return SlideJudgeResult(
            slide_id=slide_id,
            slide_number=slide_number,
            status="pass",
            score=1.0,
            severity="none",
            summary="No deterministic slide issues detected.",
        )

    has_error = any(issue.severity == "error" for issue in slide_issues)
    high_risk_types = {"fit_overflow", "blank", "tiny_text_risk", "missing_title"}
    high_risk = has_error or any(issue.type in high_risk_types for issue in slide_issues)
    score = max(0.0, 1.0 - sum(0.25 if issue.severity == "error" else 0.12 for issue in slide_issues))
    status = "fail" if high_risk else "warn"
    severity = "high" if high_risk else "medium"
    repair_strategy = _repair_strategy(slide_issues)
    summary = f"{len(slide_issues)} issue(s) detected"
    if context:
        summary += f" for {context}"
    return SlideJudgeResult(
        slide_id=slide_id,
        slide_number=slide_number,
        status=status,
        score=score,
        severity=severity,
        issues=slide_issues,
        repair_strategy=repair_strategy,
        summary=summary,
    )


def _repair_strategy(issues: list[QAIssue]) -> str:
    issue_types = {issue.type for issue in issues}
    if issue_types & {"fit_overflow", "too_many_items", "dense_text", "tiny_text_risk"}:
        return "reduce_copy_or_split_slide"
    if issue_types & {"missing_title", "missing_dek", "missing_decision_ask"}:
        return "repair_plan_metadata"
    if issue_types & {"duplicate_label", "dangling_punctuation"}:
        return "repair_text_labels"
    if issue_types & {"excessive_whitespace", "empty_zone"}:
        return "increase_visual_weight_or_change_layout"
    return "review_slide"
