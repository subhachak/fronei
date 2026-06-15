"""Two-step structured-output planner producing `DocPlan` (Phase 3, #122 of
agentdeck_framework_architecture.md §4).

Replaces the old single free-text "write DeckPlan JSON" pass with two
narrower, more reliable LLM calls:

  1. **Outline** — choose a `slide_layout` (from `SLIDE_LAYOUTS`) and a
     headline/title/section role for each slide, plus `content_tags` per
     generic content slide that drive component selection in step 2.
  2. **Blocks** — for each generic content slide, fill `blocks` (zone ->
     `ContentBlock`) using a *pre-ranked, pre-filtered* candidate list from
     `selection.rank_components` so the model only ever sees component_ids
     that are valid for that slide_layout, plus each candidate's
     `content_schema` (as JSON Schema) so `data` round-trips through
     `ContentBlock`'s validator.

Both steps degrade gracefully: invalid/unparseable JSON, unknown
slide_layouts, or blocks that fail `content_schema` validation are dropped
or replaced with a deterministic minimal fallback rather than raising — the
caller always gets back a valid `DocPlan`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import re

from app.schemas import RouteDecision
from app.services.llm_gateway import LLMResult, invoke_llm

from ..design_systems.registry import get_design_system
from .registry import get_component
from .render_plan import (
    SLIDE_LAYOUTS,
    ContentBlock,
    DocPlan,
    SectionPlan,
    Theme,
    _GENERIC_CONTENT_LAYOUTS,
)
from .selection import rank_components

logger = logging.getLogger(__name__)

_MAX_CANDIDATES_PER_ZONE = 3
_DEDICATED_LAYOUTS = {"TITLE", "SECTION_HEADER", "CLOSING"}


# ---------------------------------------------------------------------------
# Step 1 — outline (section/layout selection)
# ---------------------------------------------------------------------------

OUTLINE_SYSTEM_PROMPT = """You are Fronei's presentation outliner. Given the user's request (and any document/\
web context provided), produce a JSON outline for an executive slide deck. Output ONLY a JSON object — no \
Markdown, no commentary.

Schema:
{
  "title": "Deck title",
  "subtitle": "Audience, client, or context (optional)",
  "theme": "dark | light (optional, default dark — dark suits board/executive decks, light suits print handouts)",
  "sections": [ <section>, ... ]
}

Each <section> is one slide, in deck order. Choose `slide_layout` from this fixed list:
- "SECTION_HEADER": a section divider. Include "section_number" (e.g. "01"), "section_title", \
"section_subtitle".
- "TITLE": an additional title-style slide (rare — the deck already gets a title slide automatically from the \
top-level title/subtitle; only use this for e.g. a closing "thank you" restated as a title, otherwise avoid).
- "CLOSING": a closing/ask slide. Include "closing_text" (the headline ask/recommendation, short) and \
"closing_body" (one supporting sentence).
- "CONTENT_1COL", "CONTENT_2COL", "CONTENT_3COL", "CONTENT_4COL", "CONTENT_HERO_STAT", "CONTENT_TABLE_SIDEBAR", \
"CONTENT_SPLIT_DECISIONS": generic content slides. For these, include:
  - "section_title": short assertion-style slide title (40-80 chars) — make a point, don't just label a topic.
  - "content_brief": 1-3 sentences describing exactly what data/content this slide should contain (specific \
facts, numbers, options, phases, decisions, etc. drawn from the user's request/context — never invented).
  - "content_tags": 2-5 lowercase keywords describing the content's nature, used to pick visual components. \
Useful tags include: kpi, metric, financial, comparison, narrative, summary, key_points, timeline, roadmap, \
phases, decision, recommendation, governance, structured_data, risk_register, matrix, insight, takeaway.
  - "notes": optional speaker notes / talk track for this slide.

Layout choice guide:
- CONTENT_HERO_STAT: 1-4 headline KPIs/metrics as the slide's main point.
- CONTENT_TABLE_SIDEBAR: a comparison table or risk/financial register, with supporting commentary.
- CONTENT_SPLIT_DECISIONS: a set of decisions/recommendations the audience must approve.
- CONTENT_2COL / CONTENT_3COL / CONTENT_4COL: side-by-side comparisons, options, or pillars (2/3/4 of them).
- CONTENT_1COL: a single narrative block — bullet list, timeline/roadmap, or one large card. Default for \
narrative/summary slides and for phased roadmaps.

Build a deck with a clear story spine: SECTION_HEADER slides to open major parts, content slides that build the \
narrative (context -> analysis/options -> recommendation), and a CLOSING slide at the end. Aim for 5-10 slides \
total. Use the most specific layout that fits the content — do not default everything to CONTENT_1COL.

Do not invent facts, figures, or names not present in the user's request/context.
"""


def _build_outline_user_message(prompt_text: str, extra_context: str | None) -> str:
    if extra_context:
        return f"{prompt_text}\n\n{extra_context}"
    return prompt_text


def _extract_json_candidate(content: str) -> str | None:
    """Pull a JSON object out of an LLM response that may be fenced in a
    Markdown code block or have stray surrounding text."""
    stripped = content.strip()
    if not stripped:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        return fence.group(1)
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    # Tolerant fallback: first '{' to last '}' (handles minor leading/trailing prose).
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        return stripped[start:end + 1]
    return None


def _parse_json_object(content: str) -> dict[str, Any] | None:
    candidate = _extract_json_candidate(content)
    if not candidate:
        return None
    try:
        data = json.loads(candidate)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _coerce_outline(data: dict[str, Any]) -> dict[str, Any]:
    """Tolerant cleanup of the step-1 JSON: drop unknown slide_layouts,
    coerce list/str fields, cap section count."""
    title = str(data.get("title") or "").strip() or "Untitled Deck"
    subtitle = data.get("subtitle")
    subtitle = str(subtitle).strip() if subtitle else None
    theme = data.get("theme") if data.get("theme") in ("dark", "light") else "dark"

    sections_out: list[dict[str, Any]] = []
    for raw in data.get("sections") or []:
        if not isinstance(raw, dict):
            continue
        layout = str(raw.get("slide_layout") or "").strip().upper()
        if layout not in SLIDE_LAYOUTS:
            continue
        section: dict[str, Any] = {"slide_layout": layout}
        for key in (
            "section_title", "content_brief", "notes",
            "hero_title", "subtitle", "presenter", "deck_type_label", "date_label", "confidentiality",
            "section_number", "section_subtitle", "closing_text", "closing_body",
        ):
            val = raw.get(key)
            if val is not None:
                section[key] = str(val).strip()
        tags = raw.get("content_tags")
        if isinstance(tags, list):
            section["content_tags"] = [str(t).strip() for t in tags if str(t).strip()]
        sections_out.append(section)

    return {"title": title, "subtitle": subtitle, "theme": theme, "sections": sections_out[:14]}


# ---------------------------------------------------------------------------
# Step 2 — component/block selection
# ---------------------------------------------------------------------------

BLOCKS_SYSTEM_PROMPT = """You are Fronei's presentation content planner, step 2 of 2. You are given a deck \
outline (from step 1) and, for each content slide, a pre-filtered list of candidate visual components — each with \
its `component_id` and a JSON Schema describing the `data` payload it requires.

For each slide listed, choose ONE candidate component per zone (the best fit for that slide's `content_brief`) \
and produce the `data` payload for it, conforming exactly to that component's JSON Schema (required fields, \
correct types, allowed enum values). Populate `data` using ONLY information from the slide's `content_brief`, the \
deck's overall context, and the user's original request — never invent facts, figures, or names.

Output ONLY a JSON object — no Markdown, no commentary:
{
  "sections": [
    {
      "index": <0-based index into outline.sections>,
      "blocks": [ {"zone": "<zone name>", "component_id": "<from candidates>", "data": { ... }} ],
      "header_bar": {"section_number": "...", "section_title": "...", "variant": "dark_navy"} | null,
      "callout": {"text": "...", "variant": "insight"} | null
    }
  ]
}

Rules:
- Only include `index` values for slides that were given a zone/candidate list below.
- Fill every listed zone with exactly one block (pick the single best candidate — do not list multiple).
- `header_bar` and `callout` are optional extras, not zones — include them only when they add value (e.g. a \
section label or a key-takeaway strip); otherwise set to null.
- If you cannot confidently populate a schema for a zone, choose the candidate component whose schema is \
simplest to satisfy with the available content (usually the bullet/text-oriented one) rather than omitting the \
zone.
"""


def _layout_zones(slide_layout: str) -> list[str]:
    spec = get_design_system("agentdeck_v1")
    try:
        layout = spec.slide_layout(slide_layout)
    except Exception:
        return []
    return list(layout.zones)


def _candidates_payload(
    slide_layout: str,
    tags: list[str],
    *,
    usage_stats_map: dict[tuple[str, str, str, str], float] | None = None,
    theme: Theme = "dark",
) -> list[dict[str, Any]]:
    ranked = rank_components(
        slide_layout, tags, usage_stats_map=usage_stats_map, design_system="agentdeck_v1", theme=theme
    )[:_MAX_CANDIDATES_PER_ZONE]
    out = []
    for c in ranked:
        try:
            schema = c.content_schema.model_json_schema()
        except Exception:
            schema = {}
        out.append({"component_id": c.id, "content_schema": schema})
    return out


def _build_blocks_user_message(
    outline: dict[str, Any],
    *,
    usage_stats_map: dict[tuple[str, str, str, str], float] | None = None,
) -> tuple[str, list[int]]:
    """Returns (message, indices) where `indices` are the outline section
    indices that have a generic content layout (and thus need blocks)."""
    theme: Theme = "light" if outline.get("theme") == "light" else "dark"
    slides_payload = []
    indices: list[int] = []
    for i, section in enumerate(outline["sections"]):
        layout = section["slide_layout"]
        if layout not in _GENERIC_CONTENT_LAYOUTS:
            continue
        indices.append(i)
        zones = _layout_zones(layout)
        tags = section.get("content_tags") or []
        slides_payload.append({
            "index": i,
            "slide_layout": layout,
            "section_title": section.get("section_title"),
            "content_brief": section.get("content_brief"),
            "zones": {
                zone: _candidates_payload(layout, tags, usage_stats_map=usage_stats_map, theme=theme)
                for zone in zones
            },
        })
    message = json.dumps({"deck_title": outline["title"], "slides": slides_payload}, ensure_ascii=False)
    return message, indices


# ---------------------------------------------------------------------------
# Assembly + fallbacks
# ---------------------------------------------------------------------------


def _fallback_block(zone: str, section_title: str | None) -> ContentBlock | None:
    """Deterministic minimal block: an (empty or single-item) bullet list,
    if `bullet_list` is valid for this zone's data shape. Falls back to None
    (zone left empty) if even that fails."""
    items = [{"text": section_title, "level": 0}] if section_title else []
    try:
        return ContentBlock(zone=zone, component_id="bullet_list", data={"items": items})
    except Exception:
        return None


def _build_content_block(zone: str, raw: dict[str, Any]) -> ContentBlock | None:
    component_id = str(raw.get("component_id") or "").strip()
    data = raw.get("data")
    if not component_id or not isinstance(data, dict):
        return None
    try:
        get_component(component_id)
    except KeyError:
        return None
    try:
        return ContentBlock(zone=zone, component_id=component_id, data=data)
    except Exception as exc:
        logger.info("Dropping invalid block for zone %r/%s: %s", zone, component_id, exc)
        return None


def _build_section_plan(outline_section: dict[str, Any], blocks_section: dict[str, Any] | None) -> SectionPlan:
    layout = outline_section["slide_layout"]
    base_kwargs: dict[str, Any] = {"slide_layout": layout, "notes": outline_section.get("notes")}

    if layout in _DEDICATED_LAYOUTS:
        for key in (
            "hero_title", "subtitle", "presenter", "deck_type_label", "date_label", "confidentiality",
            "section_number", "section_title", "section_subtitle", "closing_text", "closing_body",
        ):
            if key in outline_section:
                base_kwargs[key] = outline_section[key]
        try:
            return SectionPlan(**base_kwargs)
        except Exception as exc:
            logger.warning("Invalid %s section, using minimal fallback: %s", layout, exc)
            return SectionPlan(slide_layout=layout)

    base_kwargs["section_title"] = outline_section.get("section_title")

    blocks: list[ContentBlock] = []
    seen_zones: set[str] = set()
    if blocks_section:
        for raw in blocks_section.get("blocks") or []:
            if not isinstance(raw, dict):
                continue
            zone = str(raw.get("zone") or "").strip()
            if not zone or zone in seen_zones:
                continue
            block = _build_content_block(zone, raw)
            if block is not None:
                blocks.append(block)
                seen_zones.add(zone)
        for extra_key, target_kwarg in (("header_bar", "header_bar"), ("callout", "callout")):
            val = blocks_section.get(extra_key)
            if isinstance(val, dict):
                base_kwargs[target_kwarg] = val

    try:
        return SectionPlan(blocks=blocks, **base_kwargs)
    except Exception as exc:
        logger.info("Section blocks failed validation (%s), retrying with valid subset only: %s", layout, exc)

    # Retry, dropping blocks one at a time until valid.
    while blocks:
        try:
            return SectionPlan(blocks=blocks, **base_kwargs)
        except Exception:
            blocks = blocks[:-1]

    # Final fallback: try a single deterministic bullet_list in the first
    # zone of this layout; otherwise empty blocks (always valid).
    zones = _layout_zones(layout)
    if zones:
        fallback = _fallback_block(zones[0], outline_section.get("section_title"))
        if fallback is not None:
            try:
                return SectionPlan(blocks=[fallback], **base_kwargs)
            except Exception:
                pass
    base_kwargs.pop("header_bar", None)
    base_kwargs.pop("callout", None)
    try:
        return SectionPlan(blocks=[], **base_kwargs)
    except Exception:
        return SectionPlan(slide_layout=layout)


def generate_doc_plan(
    prompt_text: str,
    route: RouteDecision,
    *,
    theme: Theme = "dark",
    extra_context: str | None = None,
    db: Any | None = None,
) -> tuple[DocPlan, LLMResult]:
    """Run the two-step structured-output planner and return a validated
    `DocPlan` plus the combined `LLMResult` (for cost/latency accounting).

    Always returns a valid `DocPlan`, even if one or both LLM calls fail or
    return unparseable/invalid JSON — failures degrade to a minimal deck.

    `db`, when provided (Phase 3, #130), is used to load real
    `component_usage_stats` history (`usage_stats.load_usage_stats_map`) so
    the block-selection step's candidate ordering reflects past render/QA
    outcomes rather than the static neutral prior. `None` (the default)
    preserves prior behavior exactly.
    """
    # ── Step 1: outline ─────────────────────────────────────────────────
    outline_result = invoke_llm(
        _build_outline_user_message(prompt_text, extra_context),
        route,
        deep_research=False,
        artifact_context=OUTLINE_SYSTEM_PROMPT,
    )
    outline_data = _parse_json_object(outline_result.answer) or {}
    outline = _coerce_outline(outline_data)

    total_prompt_tokens = outline_result.prompt_tokens or 0
    total_completion_tokens = outline_result.completion_tokens or 0
    total_cost = outline_result.estimated_cost_usd or 0.0
    total_latency = outline_result.latency_ms
    fallback_errors = list(outline_result.fallback_errors)
    model_used = outline_result.model_used

    # ── Step 2: component/block selection ──────────────────────────────
    from .usage_stats import load_usage_stats_map

    usage_stats_map = load_usage_stats_map(db)

    blocks_by_index: dict[int, dict[str, Any]] = {}
    blocks_message, content_indices = _build_blocks_user_message(outline, usage_stats_map=usage_stats_map)
    if content_indices:
        try:
            blocks_result = invoke_llm(
                blocks_message,
                route,
                deep_research=False,
                artifact_context=BLOCKS_SYSTEM_PROMPT,
            )
            total_prompt_tokens += blocks_result.prompt_tokens or 0
            total_completion_tokens += blocks_result.completion_tokens or 0
            total_cost += blocks_result.estimated_cost_usd or 0.0
            total_latency += blocks_result.latency_ms
            fallback_errors += blocks_result.fallback_errors
            model_used = blocks_result.model_used

            blocks_data = _parse_json_object(blocks_result.answer) or {}
            for raw in blocks_data.get("sections") or []:
                if not isinstance(raw, dict):
                    continue
                try:
                    idx = int(raw.get("index"))
                except (TypeError, ValueError):
                    continue
                blocks_by_index[idx] = raw
        except Exception as exc:
            logger.warning("Block-selection LLM call failed, using fallback blocks: %s", exc)

    # ── Assemble DocPlan ─────────────────────────────────────────────────
    sections = [
        _build_section_plan(section, blocks_by_index.get(i))
        for i, section in enumerate(outline["sections"])
    ]
    if not sections:
        sections = [SectionPlan(slide_layout="CLOSING", closing_text="Thank You")]

    doc_plan = DocPlan(
        doc_type="presentation",
        design_system="agentdeck_v1",
        theme=outline.get("theme", theme),
        title=outline["title"],
        subtitle=outline.get("subtitle"),
        sections=sections,
    )

    combined_result = LLMResult(
        answer=outline_result.answer,
        model_used=model_used,
        latency_ms=total_latency,
        prompt_tokens=total_prompt_tokens or None,
        completion_tokens=total_completion_tokens or None,
        estimated_cost_usd=total_cost or None,
        fallback_errors=fallback_errors,
    )
    return doc_plan, combined_result
