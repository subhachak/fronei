"""Deterministic pre-render QA checks for AgentDeck plans (#147)."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from app.services.components import DocPlan, PptxRenderPlan
from app.services.components.fit_validation import validate_component_fit
from app.services.components.registry import get_component
from app.services.design_systems.registry import get_design_system

from .types import QAIssue

_DANGLING_PUNCT_RE = re.compile(r"[\s\-–—:;]+$")


def run_plan_checks(plan: DocPlan | PptxRenderPlan) -> list[QAIssue]:
    if isinstance(plan, PptxRenderPlan):
        return _check_render_plan(plan)
    return _check_doc_plan(plan)


def _check_doc_plan(plan: DocPlan) -> list[QAIssue]:
    issues: list[QAIssue] = []
    spec = get_design_system(plan.design_system)
    for idx, section in enumerate(plan.sections, start=2):
        title = section.section_title or section.hero_title or section.closing_text
        _check_text_field(issues, title, "title", idx, section.slide_id)
        if section.slide_layout.startswith("CONTENT_"):
            if not title:
                issues.append(_issue("missing_title", idx, "Content slide is missing a title.", slide_id=section.slide_id))
            if not section.dek and not section.subtitle:
                issues.append(_issue("missing_dek", idx, "Content slide is missing a one-line dek/subtitle.", slide_id=section.slide_id, severity="info"))
        if section.purpose in {"decision", "recommendation"} and not _has_decision_ask(section):
            issues.append(_issue("missing_decision_ask", idx, "Decision/recommendation slide is missing an explicit ask or action.", slide_id=section.slide_id))

        seen_zones: set[str] = set()
        layout = spec.slide_layout(section.slide_layout)
        for block in section.blocks:
            if block.zone in seen_zones:
                issues.append(_issue("duplicate_label", idx, f"Zone {block.zone!r} is assigned more than once.", slide_id=section.slide_id, block_id=block.block_id, zone=block.zone, component_id=block.component_id))
            seen_zones.add(block.zone)
            comp = get_component(block.component_id)
            fit = validate_component_fit(
                slide_layout=section.slide_layout,
                layout=layout,
                zone=block.zone,
                component=comp,
                props=block.data,
            )
            for fit_issue in fit.issues:
                issue_type = "too_many_items" if "exceeds max" in fit_issue.message and fit_issue.field not in {"estimated_height_in", "zone_width_in", "zone_height_in"} else "fit_overflow"
                issues.append(
                    QAIssue(
                        type=issue_type,
                        severity="error" if fit_issue.severity == "error" else "warning",
                        stage="plan",
                        slide=idx,
                        slide_id=section.slide_id,
                        block_id=block.block_id,
                        zone=block.zone,
                        component_id=block.component_id,
                        detail=fit_issue.message,
                    )
                )
            _check_props_text(issues, block.data, idx, section.slide_id, block.block_id, block.zone, block.component_id)
            _check_duplicate_labels(issues, block.data, idx, section.slide_id, block.block_id, block.zone, block.component_id)
    return issues


def _check_render_plan(plan: PptxRenderPlan) -> list[QAIssue]:
    issues: list[QAIssue] = []
    for idx, slide in enumerate(plan.slides, start=1):
        title = slide.title or slide.hero_title or slide.section_title or slide.closing_text
        _check_text_field(issues, title, "title", idx, None)
        if slide.slide_layout.startswith("CONTENT_") and not title:
            issues.append(_issue("missing_title", idx, "Content slide is missing a title."))
        for zone, assignment in slide.zones.items():
            instances = assignment if isinstance(assignment, list) else [assignment]
            for inst in instances:
                _check_props_text(issues, inst.props, idx, None, None, zone, inst.component_id)
                _check_duplicate_labels(issues, inst.props, idx, None, None, zone, inst.component_id)
    return issues


def _check_text_field(issues: list[QAIssue], value: str | None, field: str, slide: int, slide_id: str | None) -> None:
    if value and _DANGLING_PUNCT_RE.search(value):
        issues.append(_issue("dangling_punctuation", slide, f"{field} ends with dangling punctuation: {value!r}", slide_id=slide_id))


def _check_props_text(
    issues: list[QAIssue],
    props: Any,
    slide: int,
    slide_id: str | None,
    block_id: str | None,
    zone: str | None,
    component_id: str | None,
) -> None:
    for text in _iter_strings(props):
        if _DANGLING_PUNCT_RE.search(text):
            issues.append(
                _issue(
                    "dangling_punctuation",
                    slide,
                    f"Text ends with dangling punctuation: {text!r}",
                    slide_id=slide_id,
                    block_id=block_id,
                    zone=zone,
                    component_id=component_id,
                )
            )


def _check_duplicate_labels(
    issues: list[QAIssue],
    props: dict[str, Any],
    slide: int,
    slide_id: str | None,
    block_id: str | None,
    zone: str | None,
    component_id: str | None,
) -> None:
    labels: list[str] = []
    for key in ("headers", "title", "label", "value", "step_label"):
        labels.extend(_collect_key_strings(props, key))
    duplicates = [label for label, count in Counter(labels).items() if label and count > 1]
    for label in duplicates:
        issues.append(
            _issue(
                "duplicate_label",
                slide,
                f"Duplicate label detected: {label!r}",
                slide_id=slide_id,
                block_id=block_id,
                zone=zone,
                component_id=component_id,
            )
        )


def _has_decision_ask(section) -> bool:
    text = " ".join(_iter_strings(section.model_dump(mode="json", exclude_none=True))).lower()
    return any(marker in text for marker in ("approve", "decision", "authorize", "ask", "next step", "owner"))


def _iter_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for child in value.values():
            result.extend(_iter_strings(child))
        return result
    if isinstance(value, list):
        result: list[str] = []
        for child in value:
            result.extend(_iter_strings(child))
        return result
    return []


def _collect_key_strings(value: Any, key: str) -> list[str]:
    if isinstance(value, dict):
        result: list[str] = []
        for child_key, child in value.items():
            if child_key == key:
                result.extend(_iter_strings(child))
            result.extend(_collect_key_strings(child, key))
        return result
    if isinstance(value, list):
        result: list[str] = []
        for child in value:
            result.extend(_collect_key_strings(child, key))
        return result
    return []


def _issue(
    issue_type,
    slide: int,
    detail: str,
    *,
    slide_id: str | None = None,
    block_id: str | None = None,
    zone: str | None = None,
    component_id: str | None = None,
    severity: str = "warning",
) -> QAIssue:
    return QAIssue(
        type=issue_type,
        severity=severity,
        stage="plan",
        slide=slide,
        slide_id=slide_id,
        block_id=block_id,
        zone=zone,
        component_id=component_id,
        detail=detail,
    )
