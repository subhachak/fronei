from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

QAIssueType = Literal[
    "dangling_punctuation",
    "duplicate_label",
    "missing_title",
    "missing_dek",
    "missing_decision_ask",
    "too_many_items",
    "unsupported_component_zone",
    "fit_overflow",
    "blank",
    "dense_text",
    "dense_ink",
    "tiny_text_risk",
    "empty_zone",
    "excessive_whitespace",
]


class QAIssue(BaseModel):
    type: QAIssueType
    severity: Literal["info", "warning", "error"] = "warning"
    stage: Literal["plan", "render"] = "plan"
    slide: Optional[int] = None
    slide_id: Optional[str] = None
    block_id: Optional[str] = None
    zone: Optional[str] = None
    component_id: Optional[str] = None
    detail: str

    def to_render_qa_issue(self) -> dict:
        payload = self.model_dump(exclude_none=True)
        if self.slide is not None:
            payload["slide"] = self.slide
        return payload
