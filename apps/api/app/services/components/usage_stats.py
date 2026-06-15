"""Usage-stats logging (Phase 3, #128 of agentdeck_framework_architecture.md
§3/§6).

After a presentation is generated, `log_doc_plan_usage` records one
`component_usage_stats` row per (component_id, slide_layout, design_system,
theme) combination used in the resulting `DocPlan`, incrementing
`success_count` and stamping `last_used_at`. Render-QA failures (#129) and
ranking (#130) build on these rows.

Best-effort: any failure here (bad JSON, DB error) is logged and swallowed —
usage-stats logging must never break document generation.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone

from app.db.models import ComponentUsageStat

from .render_plan import DocPlan

logger = logging.getLogger(__name__)


def _component_usage_keys(doc_plan: DocPlan) -> Counter[tuple[str, str, str, str]]:
    """(component_id, slide_layout, design_system, theme) -> times used."""
    counts: Counter[tuple[str, str, str, str]] = Counter()
    for section in doc_plan.sections:
        for block in section.blocks:
            counts[(block.component_id, section.slide_layout, doc_plan.design_system, doc_plan.theme)] += 1
    return counts


def load_usage_stats_map(db) -> dict[tuple[str, str, str, str], float]:
    """Load `success_rate` for every `component_usage_stats` row, keyed by
    (component_id, slide_layout, design_system, theme) (Phase 3, #130).

    Used by `selection.rank_components` to weight candidate ordering by real
    render/QA history instead of the static 0.5 neutral prior. Returns an
    empty dict (== "no history yet, use the neutral prior everywhere") on any
    error or when `db` is None; never raises.
    """
    if db is None:
        return {}
    try:
        rows = db.query(ComponentUsageStat).all()
    except Exception:
        logger.exception("Failed to load component usage stats")
        return {}

    result: dict[tuple[str, str, str, str], float] = {}
    for row in rows:
        uses = row.success_count + row.failure_count
        success_rate = 0.5 if uses == 0 else max(0.0, row.success_count / uses)
        result[(row.component_id, row.slide_layout, row.design_system, row.theme)] = success_rate
    return result


_QA_FAILURE_TYPES = {"dense_text", "dense_ink", "tiny_text_risk"}


def log_render_qa_failures(db, doc_type: str, doc_body: str, render_qa: dict | None) -> None:
    """Increment `failure_count` for components on slides flagged by render QA
    (Phase 3, #129).

    `render_qa` is the dict returned by `run_pptx_render_qa` (or the repaired
    version after the repair loop), as stored in `preview["render_qa"]`.
    Slide numbers are 1-based and the composer prepends a synthesized TITLE
    slide, so `doc_plan.sections[i]` renders as slide `i + 2`. Only issue
    types in `_QA_FAILURE_TYPES` (dense_text, dense_ink, tiny_text_risk) count
    as failures. No-op for non-presentation doc types, bodies that don't parse
    as a `DocPlan`, or when render QA produced no flagged issues. Never raises.
    """
    if doc_type != "presentation" or not render_qa:
        return
    issues = render_qa.get("issues") or []
    flagged_slides = {
        i.get("slide") for i in issues if i.get("type") in _QA_FAILURE_TYPES and i.get("slide")
    }
    if not flagged_slides:
        return

    try:
        doc_plan = DocPlan.model_validate_json(doc_body)
    except Exception:
        return

    counts: Counter[tuple[str, str, str, str]] = Counter()
    for idx, section in enumerate(doc_plan.sections):
        slide_number = idx + 2  # slide 1 is the synthesized TITLE slide
        if slide_number not in flagged_slides:
            continue
        for block in section.blocks:
            counts[(block.component_id, section.slide_layout, doc_plan.design_system, doc_plan.theme)] += 1

    if not counts:
        return

    try:
        now = datetime.now(timezone.utc)
        for (component_id, slide_layout, design_system, theme), n in counts.items():
            row = (
                db.query(ComponentUsageStat)
                .filter(
                    ComponentUsageStat.component_id == component_id,
                    ComponentUsageStat.slide_layout == slide_layout,
                    ComponentUsageStat.design_system == design_system,
                    ComponentUsageStat.theme == theme,
                )
                .first()
            )
            if row is None:
                row = ComponentUsageStat(
                    component_id=component_id,
                    slide_layout=slide_layout,
                    design_system=design_system,
                    theme=theme,
                    success_count=0,
                    failure_count=0,
                )
                db.add(row)
            row.failure_count += n
            row.updated_at = now
        db.commit()
    except Exception:
        logger.exception("Failed to log render QA failure stats")
        db.rollback()


def log_doc_plan_usage(db, doc_type: str, doc_body: str) -> None:
    """Record component usage from a generated presentation's `DocPlan` JSON.

    No-op for non-presentation doc types or bodies that don't parse as a
    `DocPlan` (e.g. legacy DeckPlan JSON). Never raises.
    """
    if doc_type != "presentation":
        return
    try:
        doc_plan = DocPlan.model_validate_json(doc_body)
    except Exception:
        return

    counts = _component_usage_keys(doc_plan)
    if not counts:
        return

    try:
        now = datetime.now(timezone.utc)
        for (component_id, slide_layout, design_system, theme), n in counts.items():
            row = (
                db.query(ComponentUsageStat)
                .filter(
                    ComponentUsageStat.component_id == component_id,
                    ComponentUsageStat.slide_layout == slide_layout,
                    ComponentUsageStat.design_system == design_system,
                    ComponentUsageStat.theme == theme,
                )
                .first()
            )
            if row is None:
                row = ComponentUsageStat(
                    component_id=component_id,
                    slide_layout=slide_layout,
                    design_system=design_system,
                    theme=theme,
                    success_count=0,
                    failure_count=0,
                )
                db.add(row)
            row.success_count += n
            row.last_used_at = now
            row.updated_at = now
        db.commit()
    except Exception:
        logger.exception("Failed to log component usage stats")
        db.rollback()
