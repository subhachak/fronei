"""Structural repair loop for AgentDeck v2 (#150)."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from app.services.components import ContentBlock, DocPlan, SectionPlan
from app.services.qa.types import QAIssue

_DANGLING_RE = re.compile(r"[\s\-–—:;]+$")


def repair_docplan_for_qa(doc_plan: DocPlan, issues: list[QAIssue]) -> tuple[DocPlan, bool]:
    """Apply small deterministic structural repairs to a DocPlan.

    This is intentionally conservative: it repairs common mechanical defects
    and returns `(plan, changed)`. The judge/LLM repair loop can later replace
    this with richer targeted rewrites while preserving the same contract.
    """
    repaired = doc_plan.model_copy(deep=True)
    changed = False
    section_by_slide: dict[int, SectionPlan] = {
        idx + 2: section for idx, section in enumerate(repaired.sections)
    }
    section_by_id = {section.slide_id: section for section in repaired.sections if section.slide_id}

    for issue in issues:
        section = section_by_id.get(issue.slide_id) if issue.slide_id else None
        if section is None and issue.slide is not None:
            section = section_by_slide.get(issue.slide)
        if section is None:
            continue

        if issue.type == "missing_title" and section.slide_layout.startswith("CONTENT_"):
            section.section_title = section.message or section.dek or "Key decision"
            changed = True
        elif issue.type == "missing_dek" and section.slide_layout.startswith("CONTENT_"):
            section.dek = section.message or section.audience_question or section.section_title
            changed = True
        elif issue.type == "missing_decision_ask":
            section.message = section.message or "Approve the recommended next step."
            if section.slide_layout == "CLOSING":
                section.closing_text = section.closing_text or section.message
            changed = True
        elif issue.type in {"dangling_punctuation", "duplicate_label"}:
            changed = _repair_text(section) or changed
        elif issue.type in {"too_many_items", "fit_overflow", "dense_text", "tiny_text_risk"}:
            changed = _reduce_section_density(section, issue) or changed
        elif issue.type in {"excessive_whitespace", "empty_zone"}:
            changed = _increase_visual_weight(section) or changed

    if changed:
        return DocPlan.model_validate(repaired.model_dump(mode="json", exclude_none=True)), True
    return repaired, False


def _repair_text(section: SectionPlan) -> bool:
    changed = False
    for attr in ("section_title", "dek", "message", "closing_text", "closing_body"):
        value = getattr(section, attr, None)
        if isinstance(value, str):
            cleaned = _DANGLING_RE.sub("", value).strip()
            if cleaned and cleaned != value:
                setattr(section, attr, cleaned)
                changed = True
    for block in section.blocks:
        data, data_changed = _clean_data_text(block.data)
        if data_changed:
            block.data = data
            changed = True
    return changed


def _clean_data_text(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str):
        cleaned = _DANGLING_RE.sub("", value).strip()
        return cleaned, cleaned != value
    if isinstance(value, list):
        changed = False
        out = []
        for item in value:
            cleaned, item_changed = _clean_data_text(item)
            changed = changed or item_changed
            out.append(cleaned)
        return out, changed
    if isinstance(value, dict):
        changed = False
        out = {}
        for key, item in value.items():
            cleaned, item_changed = _clean_data_text(item)
            changed = changed or item_changed
            out[key] = cleaned
        return out, changed
    return value, False


def _reduce_section_density(section: SectionPlan, issue: QAIssue) -> bool:
    candidates = [block for block in section.blocks if not issue.block_id or block.block_id == issue.block_id]
    if not candidates:
        candidates = section.blocks
    for block in candidates:
        reduced, changed = _reduce_block_data(block.data)
        if changed:
            block.data = reduced
            _append_note(section, f"Reduced visible content in {block.zone or 'block'} during QA repair.")
            return True
    return False


def _reduce_block_data(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    out = deepcopy(data)
    for key, keep in (("items", 5), ("rows", 7), ("cards", 3), ("stats", 4), ("nodes", 5), ("bullets", 5)):
        value = out.get(key)
        if isinstance(value, list) and len(value) > keep:
            removed = value[keep:]
            out[key] = value[:keep]
            notes = out.setdefault("speaker_notes", [])
            if isinstance(notes, list):
                notes.append(f"QA repair moved {len(removed)} overflow item(s) out of visible slide content.")
            return out, True
    for key in ("body", "text"):
        value = out.get(key)
        if isinstance(value, str) and len(value) > 220:
            out[key] = value[:217].rsplit(" ", 1)[0].rstrip(" -–—:;,.") + "..."
            return out, True
    return out, False


def _increase_visual_weight(section: SectionPlan) -> bool:
    if section.dek or not section.message:
        return False
    section.dek = section.message
    return True


def _append_note(section: SectionPlan, note: str) -> None:
    section.notes = f"{section.notes}\n{note}" if section.notes else note
