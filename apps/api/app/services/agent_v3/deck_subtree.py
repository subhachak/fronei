from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.config import get_settings
from app.db.models import SessionLocal
from app.services.agent_v3 import model_client
from app.services.agent_v3.document_subtree import DocumentPlan
from app.services.agent_v3.models import AgentV3Request, Source
from app.services.agent_v3.pptx_design import (
    _design_ledger,
    _design_system_id_for_template,
    _repair_slide_specs,
    _slide_to_plan,
    SlideSpec,
)
from app.services.agent_v3.research_subtree import EvidencePack, infer_research_profile, source_context_from_evidence
from app.services.agent_v3.tools import source_context
from app.services.components.compose_docplan import compose_docplan_to_pptx_render_plan
from app.services.components.render_plan import ContentBlock, DocPlan, PptxRenderPlan, SectionPlan, ZoneInstance
from app.services.document_templates import template_design_context, template_grammar_for_selection

logger = logging.getLogger(__name__)


class DeckPlanResult(BaseModel):
    title: str
    subtitle: str | None = None
    audience: str = "general business audience"
    slides: list[dict[str, Any]] = Field(default_factory=list)


@dataclass
class DeckDraft:
    title: str
    doc_plan: DocPlan
    render_plan: PptxRenderPlan
    summary_markdown: str
    design_system_id: str
    template_grammar: dict[str, Any]
    design_ledger: list[dict[str, Any]]
    repair_actions: list[dict[str, Any]]
    model_used: str = ""
    latency_ms: int = 0
    cost_usd: float = 0.0
    model_role: str = "deck_planner"
    preferred_model: str = ""
    attempted_models: list[str] = field(default_factory=list)
    failed_model_attempts: list[dict[str, str]] = field(default_factory=list)


DECK_PLANNER_PROMPT = """You are Agent v3's presentation architect.

Create a native structured deck plan, not markdown and not prose. Return only JSON:
{
  "title": "deck title",
  "subtitle": "optional subtitle",
  "audience": "target audience",
  "slides": [
    {
      "title": "assertion-style slide title",
      "purpose": "context|analysis|comparison|recommendation|decision|roadmap|evidence|closing",
      "layout": "bullets|cards|table|stat|timeline|decision|closing",
      "message": "one sentence explaining the slide's job",
      "bullets": ["short visible slide text"],
      "notes": "presenter notes with evidence and citations",
      "table": [["Header", "Header"], ["row", "row"]],
      "stats": [{"value": "42%", "label": "adoption", "caption": "optional"}],
      "left": ["cards/options for left side"],
      "right": ["cards/options for right side"]
    }
  ]
}

Design rules:
- Build a real presentation flow: title, context, evidence/analysis, implications, recommendation, next steps.
- Use assertion-style slide titles. Do not use generic labels like "Overview" unless the slide truly introduces the deck.
- Keep visible slide text sparse. Put details, caveats, and citations in notes.
- Use tables for comparisons, timelines for phases/workflows, stat slides for important metrics, and decision slides for recommendations/options.
- If research evidence is provided, cite key claims in notes using [S#].
- For executive decks, favor fewer stronger visual moves over dense bullet slides.
"""


def plan_deck(
    request: AgentV3Request,
    document_plan: DocumentPlan,
    *,
    sources: list[Source],
    research_answer: str | None = None,
    evidence: EvidencePack | None = None,
    user_id: str | None = None,
) -> DeckDraft:
    design_system_id = _design_system_id_for_template(request.template_id, user_id)
    template_grammar = _template_grammar_for_request(request, user_id=user_id, document_plan=document_plan)
    template_context = template_design_context(template_grammar)
    context = source_context_from_evidence(evidence) if evidence is not None else source_context(sources)
    prompt_payload = {
        "message": request.message,
        "conversation_context": request.conversation_context[-4000:] if request.conversation_context else "",
        "quality_mode": request.quality_mode,
        "target_output": "pptx",
        "document_plan": document_plan.model_dump(mode="json"),
        "research_summary": (research_answer or "")[:9000],
        "sources": context[:9000],
        "recommended_slide_count": _recommended_slide_count(request, document_plan, bool(research_answer)),
        "profile": infer_research_profile(request.message),
        "template_grammar": template_grammar,
        "template_design_context": template_context,
        "allowed_visual_layouts": template_grammar.get("preferred_v3_layouts") or ["cards", "bullets", "table", "timeline", "decision", "stat"],
    }
    try:
        response = model_client.complete(
            [
                {"role": "system", "content": DECK_PLANNER_PROMPT},
                {"role": "user", "content": json.dumps(prompt_payload, ensure_ascii=False)},
            ],
            role="document_planner",
            quality_mode=request.quality_mode,
            overrides=request.model_overrides,
            max_tokens=_deck_planner_token_budget(request, has_research=bool(research_answer)),
            timeout_s=max(30, int(get_settings().agent_v3_longform_timeout_s or 180)),
        )
        payload = _parse_json(response.text)
        result = DeckPlanResult.model_validate(payload)
        draft = _deck_from_result(
            request,
            document_plan,
            result,
            design_system_id=design_system_id,
            template_grammar=template_grammar,
            model_used=response.model_used,
            latency_ms=response.latency_ms,
            cost_usd=response.cost_usd,
            preferred_model=getattr(response, "preferred_model", "") or model_client.model_for_role("document_planner", quality_mode=request.quality_mode, overrides=request.model_overrides) or "",
            attempted_models=list(getattr(response, "attempted_models", []) or []),
            failed_model_attempts=list(getattr(response, "failed_model_attempts", []) or []),
        )
        return draft
    except Exception as exc:
        logger.warning("agent_v3 deck planning failed; using deterministic deck fallback: %s", exc)
        return _fallback_deck(request, document_plan, design_system_id=design_system_id, reason=str(exc))


def _deck_from_result(
    request: AgentV3Request,
    document_plan: DocumentPlan,
    result: DeckPlanResult,
    *,
    design_system_id: str,
    template_grammar: dict[str, Any],
    model_used: str,
    latency_ms: int,
    cost_usd: float,
    preferred_model: str,
    attempted_models: list[str],
    failed_model_attempts: list[dict[str, str]],
) -> DeckDraft:
    sections: list[SectionPlan] = []
    repair_actions: list[dict[str, Any]] = []
    slides = result.slides[:22] or []
    for index, raw in enumerate(slides, start=1):
        section, repairs = _section_from_raw_slide(raw, index=index, template_grammar=template_grammar)
        sections.append(section)
        repair_actions.extend(repairs)
    if not sections:
        fallback = _fallback_sections(document_plan.sections or ["Overview", "Findings", "Next steps"])
        sections.extend(fallback)
        repair_actions.append({"type": "fallback_sections", "reason": "planner_returned_no_slides"})
    if not any(section.slide_layout == "CLOSING" for section in sections):
        sections.append(_closing_section(len(sections) + 1))

    doc_plan = DocPlan(
        doc_type="presentation",
        design_system=design_system_id,
        theme="dark",
        title=result.title or document_plan.title,
        subtitle=result.subtitle,
        sections=sections[:24],
    )
    render_plan = compose_docplan_to_pptx_render_plan(doc_plan, theme="dark")
    return DeckDraft(
        title=doc_plan.title,
        doc_plan=doc_plan,
        render_plan=render_plan,
        summary_markdown=_deck_summary_markdown(doc_plan),
        design_system_id=design_system_id,
        template_grammar=template_grammar,
        design_ledger=_design_ledger(render_plan),
        repair_actions=repair_actions,
        model_used=model_used,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        preferred_model=preferred_model,
        attempted_models=attempted_models,
        failed_model_attempts=failed_model_attempts,
    )


def _section_from_raw_slide(raw: dict[str, Any], *, index: int, template_grammar: dict[str, Any] | None = None) -> tuple[SectionPlan, list[dict[str, Any]]]:
    title = _clean_text(raw.get("title") or raw.get("section_title") or f"Slide {index}", 90)
    requested_layout_hint = str(raw.get("layout") or raw.get("visual") or "").lower()
    layout_hint = _coerce_layout_hint(requested_layout_hint, template_grammar)
    purpose = _purpose(raw.get("purpose"))
    message = _clean_text(raw.get("message") or "", 220) or None
    notes = _clean_text(raw.get("notes") or raw.get("speaker_notes") or "", 2000) or None
    bullets = [_clean_text(item, 180) for item in _string_list(raw.get("bullets") or raw.get("points")) if _clean_text(item, 180)]
    table = _table_rows(raw.get("table"))
    stats = _stats(raw.get("stats"))
    left = [_clean_text(item, 170) for item in _string_list(raw.get("left") or raw.get("options") or raw.get("recommendations")) if _clean_text(item, 170)]
    right = [_clean_text(item, 170) for item in _string_list(raw.get("right") or raw.get("risks") or raw.get("mitigations")) if _clean_text(item, 170)]

    repairs: list[dict[str, Any]] = []
    try:
        if layout_hint != requested_layout_hint and requested_layout_hint:
            repairs.append(
                {
                    "type": "template_layout_coercion",
                    "slide": index,
                    "requested": requested_layout_hint,
                    "used": layout_hint,
                }
            )
        if layout_hint == "closing" or purpose == "closing":
            return _closing_section(index, title=title, body=notes or message or "Align on owners, timing, and next steps."), repairs
        if table:
            return _table_section(index, title, table, bullets=bullets, notes=notes, purpose=purpose, message=message), repairs
        if stats:
            return _stat_section(index, title, stats, bullets=bullets, notes=notes, purpose=purpose, message=message), repairs
        if layout_hint == "timeline" or purpose == "roadmap":
            return _timeline_section(index, title, bullets, notes=notes, purpose=purpose, message=message), repairs
        if layout_hint == "decision" or purpose in {"decision", "recommendation"} or (left and right):
            return _decision_section(index, title, left or bullets[:3], right or bullets[3:6], notes=notes, purpose=purpose, message=message), repairs
        if layout_hint == "cards" or 2 <= len(bullets) <= 4:
            return _cards_section(index, title, bullets, notes=notes, purpose=purpose, message=message), repairs
        return _bullet_section(index, title, bullets, notes=notes, purpose=purpose, message=message), repairs
    except (ValidationError, ValueError) as exc:
        repairs.append({"type": "section_validation_fallback", "slide": index, "title": title, "error": str(exc)[:240]})
        slide_specs, spec_repairs = _repair_slide_specs([SlideSpec(title=title, bullets=bullets, notes=notes or "", table=table, source_index=index)])
        repairs.extend(spec_repairs)
        slide_plan = _slide_to_plan(slide_specs[0], index=index)
        return _section_from_pptx_slide(slide_plan, index=index, purpose=purpose, message=message), repairs


def _section_from_pptx_slide(slide, *, index: int, purpose: str, message: str | None) -> SectionPlan:
    blocks = [
        ContentBlock(zone=zone_name, component_id=zone.component_id, data=zone.props)
        for zone_name, zone in (slide.zones or {}).items()
        if isinstance(zone, ZoneInstance)
    ]
    return SectionPlan(
        slide_id=f"slide_{index}",
        slide_layout=slide.slide_layout,
        section_title=slide.title,
        header_bar=slide.header_bar,
        blocks=blocks,
        purpose=purpose,  # type: ignore[arg-type]
        message=message,
        notes=slide.notes,
    )


def _bullet_section(index: int, title: str, bullets: list[str], *, notes: str | None, purpose: str, message: str | None) -> SectionPlan:
    items = [{"text": item, "level": 0} for item in (bullets[:6] or ["Key point to discuss."])]
    return SectionPlan(
        slide_id=f"slide_{index}",
        slide_layout="CONTENT_1COL",
        section_title=title,
        header_bar=_header(index),
        purpose=purpose,  # type: ignore[arg-type]
        message=message,
        blocks=[ContentBlock(zone="body", component_id="bullet_list", data={"items": items})],
        notes=notes,
    )


def _cards_section(index: int, title: str, bullets: list[str], *, notes: str | None, purpose: str, message: str | None) -> SectionPlan:
    cleaned = bullets[:4] or ["Key point"]
    if len(cleaned) == 4:
        layout = "CONTENT_4COL"
        zones = ["col_1", "col_2", "col_3", "col_4"]
    elif len(cleaned) == 3:
        layout = "CONTENT_3COL"
        zones = ["col_1", "col_2", "col_3"]
    elif len(cleaned) == 2:
        layout = "CONTENT_2COL"
        zones = ["col_left", "col_right"]
    else:
        return _bullet_section(index, title, cleaned, notes=notes, purpose=purpose, message=message)
    blocks = [
        ContentBlock(zone=zone, component_id="card", data=_card_data(item, i))
        for i, (zone, item) in enumerate(zip(zones, cleaned))
    ]
    return SectionPlan(
        slide_id=f"slide_{index}",
        slide_layout=layout,  # type: ignore[arg-type]
        section_title=title,
        header_bar=_header(index),
        purpose=purpose,  # type: ignore[arg-type]
        message=message,
        blocks=blocks,
        notes=notes,
    )


def _table_section(index: int, title: str, table: list[list[str]], *, bullets: list[str], notes: str | None, purpose: str, message: str | None) -> SectionPlan:
    headers = table[0][:5] if table else ["Topic", "Implication"]
    rows = [row[: len(headers)] for row in table[1:8]]
    sidebar_items = [{"text": item, "level": 0} for item in bullets[:5]]
    return SectionPlan(
        slide_id=f"slide_{index}",
        slide_layout="CONTENT_TABLE_SIDEBAR",
        section_title=title,
        header_bar=_header(index),
        purpose=purpose,  # type: ignore[arg-type]
        message=message,
        blocks=[
            ContentBlock(zone="table", component_id="table", data={"headers": headers, "rows": rows or [["TBD", "Confirm detail"]]}),
            ContentBlock(zone="sidebar", component_id="bullet_list", data={"items": sidebar_items or [{"text": "Use this table to compare the key dimensions.", "level": 0}]}),
        ],
        notes=notes,
    )


def _stat_section(index: int, title: str, stats: list[dict[str, str]], *, bullets: list[str], notes: str | None, purpose: str, message: str | None) -> SectionPlan:
    hero = stats[0]
    blocks = [ContentBlock(zone="hero", component_id="stat_card", data=hero)]
    if len(stats) > 1:
        blocks.append(ContentBlock(zone="supporting_row", component_id="stat_strip", data={"stats": stats[1:4]}))
    return SectionPlan(
        slide_id=f"slide_{index}",
        slide_layout="CONTENT_HERO_STAT",
        section_title=title,
        header_bar=_header(index),
        purpose=purpose,  # type: ignore[arg-type]
        message=message,
        blocks=blocks,
        notes=notes or "Use this slide to emphasize the most important quantitative signal.",
    )


def _timeline_section(index: int, title: str, bullets: list[str], *, notes: str | None, purpose: str, message: str | None) -> SectionPlan:
    nodes = []
    for node_index, item in enumerate((bullets or ["Plan", "Execute", "Review"])[:5], start=1):
        heading, body = _split_title_body(item)
        nodes.append({"step_label": str(node_index), "title": heading, "body": body})
    return SectionPlan(
        slide_id=f"slide_{index}",
        slide_layout="CONTENT_1COL",
        section_title=title,
        header_bar=_header(index),
        purpose=purpose,  # type: ignore[arg-type]
        message=message,
        blocks=[ContentBlock(zone="body", component_id="timeline", data={"nodes": nodes, "orientation": "horizontal"})],
        notes=notes,
    )


def _decision_section(index: int, title: str, left: list[str], right: list[str], *, notes: str | None, purpose: str, message: str | None) -> SectionPlan:
    left_cards = [_card_data(item, i, variant="filled") for i, item in enumerate((left or ["Recommended action"])[:4])]
    right_cards = [_card_data(item, i, variant="outlined") for i, item in enumerate((right or ["Risk to manage"])[:4])]
    return SectionPlan(
        slide_id=f"slide_{index}",
        slide_layout="CONTENT_SPLIT_DECISIONS",
        section_title=title,
        header_bar=_header(index),
        purpose=purpose,  # type: ignore[arg-type]
        message=message,
        blocks=[
            ContentBlock(zone="left_panel", component_id="decision_list", data={"title": "Do", "cards": left_cards}),
            ContentBlock(zone="right_panel", component_id="decision_list", data={"title": "Watch", "cards": right_cards}),
        ],
        notes=notes,
    )


def _closing_section(index: int, title: str = "Next steps", body: str = "Confirm priorities, assign owners, and move into execution.") -> SectionPlan:
    return SectionPlan(
        slide_id=f"slide_{index}",
        slide_layout="CLOSING",
        purpose="closing",
        closing_text=title,
        closing_body=body,
    )


def _fallback_deck(request: AgentV3Request, document_plan: DocumentPlan, *, design_system_id: str, reason: str) -> DeckDraft:
    template_grammar = _template_grammar_for_request(request, user_id=None, document_plan=document_plan)
    sections = _fallback_sections(document_plan.sections or ["Executive summary", "Key findings", "Next steps"])
    sections.append(_closing_section(len(sections) + 1))
    doc_plan = DocPlan(
        doc_type="presentation",
        design_system=design_system_id,
        theme="dark",
        title=document_plan.title or "Presentation",
        subtitle="Generated by Agent v3",
        sections=sections,
    )
    render_plan = compose_docplan_to_pptx_render_plan(doc_plan, theme="dark")
    return DeckDraft(
        title=doc_plan.title,
        doc_plan=doc_plan,
        render_plan=render_plan,
        summary_markdown=_deck_summary_markdown(doc_plan),
        design_system_id=design_system_id,
        template_grammar=template_grammar,
        design_ledger=_design_ledger(render_plan),
        repair_actions=[{"type": "deterministic_deck_fallback", "reason": reason[:300]}],
        preferred_model=model_client.model_for_role("document_planner", quality_mode=request.quality_mode, overrides=request.model_overrides) or "",
    )


def _fallback_sections(headings: list[str]) -> list[SectionPlan]:
    sections: list[SectionPlan] = []
    for index, heading in enumerate(headings[:10], start=1):
        sections.append(
            _cards_section(
                index,
                _clean_text(heading, 88),
                [f"What matters about {heading}", "Evidence to confirm", "Decision or action required"],
                notes=f"Fallback slide for {heading}. Replace with richer presenter notes after review.",
                purpose="analysis",
                message=f"Explain {heading}.",
            )
        )
    return sections


def _deck_summary_markdown(doc_plan: DocPlan) -> str:
    lines = [f"# {doc_plan.title}", ""]
    if doc_plan.subtitle:
        lines.extend([doc_plan.subtitle, ""])
    for index, section in enumerate(doc_plan.sections, start=1):
        title = section.section_title or section.closing_text or section.hero_title or f"Slide {index}"
        lines.append(f"## {index}. {title}")
        if section.message:
            lines.append(section.message)
        if section.notes:
            lines.append(section.notes)
        lines.append("")
    return "\n".join(lines).strip()


def _recommended_slide_count(request: AgentV3Request, plan: DocumentPlan, has_research: bool) -> int:
    if request.quality_mode == "executive" or request.research_level == "deep":
        return max(8, min(16, len(plan.sections or []) + (3 if has_research else 1)))
    return max(5, min(10, len(plan.sections or []) + 1))


def _deck_planner_token_budget(request: AgentV3Request, *, has_research: bool) -> int:
    if request.quality_mode == "executive" or request.research_level == "deep":
        return 6500 if has_research else 4500
    return 3800 if has_research else 2800


def _template_grammar_for_request(
    request: AgentV3Request,
    *,
    user_id: str | None,
    document_plan: DocumentPlan,
) -> dict[str, Any]:
    brief = {
        "title": document_plan.title,
        "audience": document_plan.audience,
        "sections": document_plan.sections,
        "output_format": "pptx",
    }
    try:
        with SessionLocal() as db:
            grammar = template_grammar_for_selection(db, user_id or "", request.template_id, brief)
            return dict(grammar or {})
    except Exception as exc:
        logger.warning("agent_v3 template grammar fetch failed; using default grammar: %s", exc)
        return {
            "mode": "fronei_premium_freehand",
            "template_id": request.template_id or "fronei-default",
            "fallback_reason": str(exc),
            "preferred_v3_layouts": ["cards", "bullets", "table", "timeline", "decision", "stat"],
        }


def _coerce_layout_hint(layout_hint: str, template_grammar: dict[str, Any] | None) -> str:
    normalized = (layout_hint or "cards").strip().lower()
    aliases = {
        "bullet": "bullets",
        "card": "cards",
        "comparison": "table",
        "matrix": "table",
        "data": "table",
        "metric": "stat",
        "roadmap": "timeline",
        "process": "timeline",
        "recommendation": "decision",
    }
    normalized = aliases.get(normalized, normalized)
    supported_all = {"bullets", "cards", "table", "stat", "timeline", "decision", "closing"}
    if normalized not in supported_all:
        normalized = "cards"
    if normalized == "closing":
        return normalized
    preferred = [str(item).lower() for item in (template_grammar or {}).get("preferred_v3_layouts", [])]
    if not preferred or normalized in preferred:
        return normalized
    for candidate in preferred:
        if candidate in supported_all and candidate != "closing":
            return candidate
    return "cards"


def _parse_json(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


def _header(index: int) -> dict[str, Any]:
    return {"section_number": f"{index:02d}", "section_title": "Fronei work product", "variant": "surface"}


def _purpose(value: Any) -> str:
    candidate = str(value or "analysis").strip().lower()
    allowed = {"title", "section", "context", "analysis", "comparison", "recommendation", "decision", "roadmap", "evidence", "closing"}
    return candidate if candidate in allowed else "analysis"


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    if isinstance(value, str) and value.strip():
        return [line.strip(" -") for line in value.splitlines() if line.strip(" -")]
    return []


def _table_rows(value: Any) -> list[list[str]]:
    if not isinstance(value, list):
        return []
    rows: list[list[str]] = []
    for row in value:
        if isinstance(row, list):
            cleaned = [_clean_text(cell, 80) for cell in row[:6]]
            if any(cleaned):
                rows.append(cleaned)
    return rows[:8] if len(rows) >= 2 else []


def _stats(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    stats: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        val = _clean_text(item.get("value"), 24)
        label = _clean_text(item.get("label"), 70)
        if val and label:
            stat: dict[str, str] = {"value": val, "label": label}
            caption = _clean_text(item.get("caption"), 90)
            if caption:
                stat["caption"] = caption
            stats.append(stat)
    return stats[:4]


def _card_data(text: str, index: int, *, variant: str = "outlined") -> dict[str, Any]:
    title, body = _split_title_body(text)
    return {
        "title": title,
        "body": body,
        "variant": variant,
        "color_variant": ["blue", "teal", "gold", "surface"][index % 4],
    }


def _split_title_body(text: str) -> tuple[str, str]:
    cleaned = _clean_text(text, 240)
    parts = re.split(r"\s+[—:-]\s+", cleaned, maxsplit=1)
    if len(parts) == 2:
        return _clean_text(parts[0], 46), _clean_text(parts[1], 150)
    words = cleaned.split()
    if len(words) > 9:
        return _clean_text(" ".join(words[:6]), 46), _clean_text(" ".join(words[6:]), 150)
    return _clean_text(cleaned, 46), _clean_text(cleaned, 150)


def _clean_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    clipped = text[: limit - 1].rsplit(" ", 1)[0].strip()
    return clipped or text[:limit].strip()
