"""Deck-level judge contract for AgentDeck v2 (#149)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.services.components import DesignPlan, DocPlan, EvidencePack, NarrativePlan

from .slide_judge import SlideJudgeResult
from .types import QAIssue


class DeckJudgeResult(BaseModel):
    status: Literal["pass", "warn", "fail"]
    score: float = Field(ge=0.0, le=1.0)
    storyline_score: float = Field(ge=0.0, le=1.0)
    design_score: float = Field(ge=0.0, le=1.0)
    evidence_score: float = Field(ge=0.0, le=1.0)
    executive_readiness_score: float = Field(ge=0.0, le=1.0)
    issues: list[QAIssue] = Field(default_factory=list)
    slide_results: list[SlideJudgeResult] = Field(default_factory=list)
    recommended_repairs: list[str] = Field(default_factory=list)


def judge_deck(
    *,
    doc_plan: DocPlan,
    slide_results: list[SlideJudgeResult],
    narrative_plan: NarrativePlan | None = None,
    evidence_pack: EvidencePack | None = None,
    design_plan: DesignPlan | None = None,
) -> DeckJudgeResult:
    issues: list[QAIssue] = []
    for slide in slide_results:
        issues.extend(slide.issues)

    storyline_score = _storyline_score(doc_plan, narrative_plan)
    design_score = _design_score(doc_plan, design_plan, slide_results)
    evidence_score = _evidence_score(doc_plan, evidence_pack)
    slide_score = _avg([slide.score for slide in slide_results], default=1.0)
    executive_readiness = _avg([storyline_score, design_score, evidence_score, slide_score])
    status = "pass"
    if any(slide.status == "fail" for slide in slide_results) or executive_readiness < 0.65:
        status = "fail"
    elif any(slide.status == "warn" for slide in slide_results) or executive_readiness < 0.82:
        status = "warn"

    repairs = _recommended_repairs(slide_results, storyline_score, design_score, evidence_score)
    return DeckJudgeResult(
        status=status,
        score=executive_readiness,
        storyline_score=storyline_score,
        design_score=design_score,
        evidence_score=evidence_score,
        executive_readiness_score=executive_readiness,
        issues=issues,
        slide_results=slide_results,
        recommended_repairs=repairs,
    )


def _storyline_score(doc_plan: DocPlan, narrative_plan: NarrativePlan | None) -> float:
    if not doc_plan.sections:
        return 0.0
    has_decision = any(section.purpose in {"decision", "recommendation", "closing"} for section in doc_plan.sections)
    has_messages = sum(1 for section in doc_plan.sections if section.message or section.section_title or section.closing_text)
    base = min(1.0, has_messages / max(1, len(doc_plan.sections)))
    if narrative_plan and narrative_plan.storyline:
        base = (base + min(1.0, len(narrative_plan.storyline) / max(1, len(doc_plan.sections)))) / 2
    return min(1.0, base + (0.1 if has_decision else 0.0))


def _design_score(doc_plan: DocPlan, design_plan: DesignPlan | None, slide_results: list[SlideJudgeResult]) -> float:
    if not design_plan or not design_plan.slide_treatments:
        return 0.65
    treatment_ratio = min(1.0, len(design_plan.slide_treatments) / max(1, len(doc_plan.sections)))
    issue_penalty = 0.1 * sum(1 for slide in slide_results if slide.status == "fail")
    return max(0.0, treatment_ratio - issue_penalty)


def _evidence_score(doc_plan: DocPlan, evidence_pack: EvidencePack | None) -> float:
    refs = [ref for section in doc_plan.sections for ref in section.evidence]
    if not refs:
        return 0.7
    evidence_ids = {item.id for item in (evidence_pack.items if evidence_pack else [])}
    resolved = sum(1 for ref in refs if ref.evidence_id in evidence_ids)
    return resolved / len(refs) if refs else 0.7


def _recommended_repairs(
    slide_results: list[SlideJudgeResult],
    storyline_score: float,
    design_score: float,
    evidence_score: float,
) -> list[str]:
    repairs = []
    if storyline_score < 0.75:
        repairs.append("strengthen_storyline")
    if design_score < 0.75:
        repairs.append("revise_design_treatments")
    if evidence_score < 0.75:
        repairs.append("resolve_or_remove_weak_evidence_refs")
    for slide in slide_results:
        if slide.repair_strategy and slide.repair_strategy not in repairs:
            repairs.append(slide.repair_strategy)
    return repairs


def _avg(values: list[float], *, default: float = 0.0) -> float:
    return sum(values) / len(values) if values else default
