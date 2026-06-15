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
import typing
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

import re

from app.config import get_settings
from app.schemas import RouteDecision
from app.services.brand import BrandProfile, UserDocumentProfile
from app.services.llm_gateway import LLMResult, invoke_llm

from ..design_systems.registry import get_design_system
from .design_plan import DesignPlan, RepairConstraint, SlideDesignTreatment
from .quality_mode import (
    DEFAULT_QUALITY_MODE,
    QualityMode,
    brand_strictness_for_quality,
    density_target_for_quality,
    normalize_quality_mode,
)
from .registry import get_component
from .render_plan import (
    SLIDE_LAYOUTS,
    ContentBlock,
    DocPlan,
    EvidencePack,
    EvidenceNeed,
    EvidenceRef,
    NarrativePlan,
    SectionPlan,
    SlidePurpose,
    StoryBeat,
    Theme,
    _GENERIC_CONTENT_LAYOUTS,
)
from .selection import rank_components

logger = logging.getLogger(__name__)

_MAX_CANDIDATES_PER_ZONE = 3
_DEDICATED_LAYOUTS = {"TITLE", "SECTION_HEADER", "CLOSING"}
_SLIDE_PURPOSES: tuple[str, ...] = typing.get_args(SlidePurpose)
_EVIDENCE_PRIORITIES = ("low", "medium", "high")
_GENERIC_BY_PURPOSE: dict[str, str] = {
    "context": "CONTENT_1COL",
    "analysis": "CONTENT_2COL",
    "comparison": "CONTENT_TABLE_SIDEBAR",
    "recommendation": "CONTENT_SPLIT_DECISIONS",
    "decision": "CONTENT_SPLIT_DECISIONS",
    "roadmap": "CONTENT_1COL",
    "evidence": "CONTENT_TABLE_SIDEBAR",
}


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


def _user_document_profile_context(profile: "UserDocumentProfile | None") -> str:
    """Render a `UserDocumentProfile` (#156) as compact prompt context.

    Returns "" when there is no signal worth injecting so callers can
    cheaply concatenate this onto existing `extra_context` strings.
    """
    if profile is None:
        return ""
    lines: list[str] = []
    if profile.preferred_tone:
        lines.append(f"- preferred tone: {profile.preferred_tone}")
    if profile.preferred_depth:
        lines.append(f"- preferred depth: {profile.preferred_depth}")
    if profile.preferred_slide_density:
        lines.append(f"- preferred slide density: {profile.preferred_slide_density}")
    if profile.industry_context:
        lines.append(f"- industry context: {profile.industry_context}")
    if profile.writing_style:
        lines.append(f"- writing style: {profile.writing_style}")
    if profile.common_audiences:
        lines.append(f"- common audiences: {', '.join(profile.common_audiences)}")
    if profile.past_rejected_patterns:
        lines.append(f"- avoid (from past feedback): {', '.join(profile.past_rejected_patterns)}")
    if not lines:
        return ""
    return "USER DOCUMENT PREFERENCES:\n" + "\n".join(lines)


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
    design_system: str = "agentdeck_v1",
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
    # The active theme/design system is a user/template selection, not a
    # planner choice. Keep it authoritative even if the LLM emits a stale
    # default such as "dark" in its outline JSON.
    outline["theme"] = theme

    total_prompt_tokens = outline_result.prompt_tokens or 0
    total_completion_tokens = outline_result.completion_tokens or 0
    total_cost = outline_result.estimated_cost_usd or 0.0
    total_latency = outline_result.latency_ms
    fallback_errors = list(outline_result.fallback_errors)
    model_used = outline_result.model_used

    # ── Step 2: component/block selection ──────────────────────────────
    usage_stats_map: dict[tuple[str, str, str, str], float] = {}
    if get_settings().agentdeck_usage_stats_weighting_enabled:
        from .usage_stats import load_usage_stats_map

        usage_stats_map = load_usage_stats_map(db)

    blocks_by_index: dict[int, dict[str, Any]] = {}
    blocks_message, content_indices = _build_blocks_user_message(outline, usage_stats_map=usage_stats_map)
    if content_indices:
        try:
            block_results: list[LLMResult] = []
            if len(content_indices) <= 1:
                block_results = [invoke_llm(
                    blocks_message,
                    route,
                    deep_research=False,
                    artifact_context=BLOCKS_SYSTEM_PROMPT,
                )]
            else:
                blocks_payload = json.loads(blocks_message)
                slide_payloads = blocks_payload.get("slides") or []

                def _run_slide_blocks(slide_payload: dict[str, Any]) -> LLMResult:
                    message = json.dumps(
                        {"deck_title": outline["title"], "slides": [slide_payload]},
                        ensure_ascii=False,
                    )
                    return invoke_llm(
                        message,
                        route,
                        deep_research=False,
                        artifact_context=BLOCKS_SYSTEM_PROMPT,
                    )

                max_workers = min(len(slide_payloads), max(1, get_settings().max_document_workers))
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    futures = [pool.submit(_run_slide_blocks, slide) for slide in slide_payloads]
                    for future in as_completed(futures):
                        block_results.append(future.result())

            for blocks_result in block_results:
                blocks_data = _parse_json_object(blocks_result.answer) or {}
                for raw in blocks_data.get("sections") or []:
                    if not isinstance(raw, dict):
                        continue
                    try:
                        idx = int(raw.get("index"))
                    except (TypeError, ValueError):
                        continue
                    blocks_by_index[idx] = raw

                total_prompt_tokens += blocks_result.prompt_tokens or 0
                total_completion_tokens += blocks_result.completion_tokens or 0
                total_cost += blocks_result.estimated_cost_usd or 0.0
                total_latency += blocks_result.latency_ms
                fallback_errors += blocks_result.fallback_errors
                model_used = blocks_result.model_used
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
        design_system=design_system,
        theme=theme,
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


# ---------------------------------------------------------------------------
# Narrative planning (Phase 3, #141) — first of the 4-step v2 pipeline.
#
# `generate_narrative_plan` is additive: it does not replace
# `generate_doc_plan` (which remains the active path until #143's
# `generate_agentdeck_v2_plan()` is wired up and proven). It produces a
# `NarrativePlan` — thesis, audience, and a `storyline` of `StoryBeat`s —
# that #142's slide-planning step consumes to populate each
# `PresentationSlidePlan.purpose`/`audience_question`/`message`/`evidence`.
#
# Per v2 §20.3, the "Story Editor" sharpening pass is folded into this step:
# the prompt asks the model to both draft and self-critique the storyline in
# one structured-output call rather than as a separate task.
# ---------------------------------------------------------------------------

NARRATIVE_SYSTEM_PROMPT = """You are Fronei's presentation narrative planner, step 1 of a 4-step pipeline (\
narrative -> slides -> design -> render). Given the user's request (and any document/web context provided), \
produce the deck's underlying argument BEFORE any slide layout or visuals are chosen. Output ONLY a JSON object \
— no Markdown, no commentary.

Schema:
{
  "title": "Deck title",
  "audience": "Who this deck is for (role/seniority), if inferrable (optional)",
  "objective": "What the audience should believe or do after seeing this deck (one sentence)",
  "executive_summary": "2-3 sentence summary of the overall argument/thesis (optional)",
  "storyline": [ <beat>, ... ]
}

Each <beat> is one step in the deck's argument (roughly, but not exactly, one beat per slide — some beats may \
span multiple slides later):
{
  "id": "beat-1",
  "title": "Short label for this beat (e.g. 'The Problem', 'Why Now', 'Our Recommendation')",
  "message": "The single point this beat makes — a complete sentence, specific and assertion-style, not a topic \
label.",
  "purpose": one of "title" | "section" | "context" | "analysis" | "comparison" | "recommendation" | "decision" \
| "roadmap" | "evidence" | "closing",
  "audience_question": "The question this beat answers from the audience's point of view (optional, but include \
for analysis/recommendation/decision beats — e.g. 'Why should we act now?')",
  "evidence_needs": [ <evidence_need>, ... ]
}

Each <evidence_need> describes a fact/figure/citation this beat's message depends on (omit if the beat is purely \
framing, e.g. a section divider):
{
  "id": "ev-1",
  "question": "What fact or data point would substantiate this beat's message?",
  "claim_type": "e.g. 'market_size', 'cost_savings', 'benchmark', 'timeline', 'risk' (optional)",
  "preferred_source_role": "e.g. 'industry_report', 'internal_data', 'case_study' (optional)",
  "priority": "low" | "medium" | "high"
}

Build a coherent argument with a clear spine: open with context/problem, build through analysis/evidence/\
comparison, land on a recommendation/decision, and close with next steps. Aim for 5-10 beats. Self-check before \
responding: does each beat's `message` follow logically from the previous beat, and does the storyline overall \
support `objective`? Tighten any beat whose message is vague or redundant with another beat.

Do not invent facts, figures, or names not present in the user's request/context — `evidence_needs` should \
describe what evidence is *needed*, not assert unverified facts as the `message` itself.
"""


def _coerce_evidence_need(raw: dict[str, Any], fallback_id: str) -> EvidenceNeed | None:
    question = str(raw.get("question") or "").strip()
    if not question:
        return None
    priority = raw.get("priority")
    if priority not in _EVIDENCE_PRIORITIES:
        priority = "medium"
    kwargs: dict[str, Any] = {
        "id": str(raw.get("id") or fallback_id).strip() or fallback_id,
        "question": question,
        "priority": priority,
    }
    for key in ("claim_type", "preferred_source_role"):
        val = raw.get(key)
        if val:
            kwargs[key] = str(val).strip()
    try:
        return EvidenceNeed(**kwargs)
    except Exception:
        return None


def _coerce_story_beat(raw: dict[str, Any], index: int) -> StoryBeat | None:
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()
    message = str(raw.get("message") or "").strip()
    if not title or not message:
        return None
    beat_id = str(raw.get("id") or f"beat-{index + 1}").strip() or f"beat-{index + 1}"
    purpose = raw.get("purpose")
    if purpose not in _SLIDE_PURPOSES:
        purpose = "analysis"
    kwargs: dict[str, Any] = {
        "id": beat_id,
        "title": title,
        "message": message,
        "purpose": purpose,
    }
    audience_question = raw.get("audience_question")
    if audience_question:
        kwargs["audience_question"] = str(audience_question).strip()

    evidence_needs: list[EvidenceNeed] = []
    for j, need_raw in enumerate(raw.get("evidence_needs") or []):
        if not isinstance(need_raw, dict):
            continue
        need = _coerce_evidence_need(need_raw, f"{beat_id}-ev-{j + 1}")
        if need is not None:
            evidence_needs.append(need)
    kwargs["evidence_needs"] = evidence_needs

    try:
        return StoryBeat(**kwargs)
    except Exception as exc:
        logger.info("Dropping invalid story beat %r: %s", beat_id, exc)
        return None


def _coerce_narrative_plan(data: dict[str, Any], *, fallback_title: str) -> NarrativePlan:
    title = str(data.get("title") or "").strip() or fallback_title
    audience = data.get("audience")
    objective = data.get("objective")
    executive_summary = data.get("executive_summary")

    storyline: list[StoryBeat] = []
    for i, raw in enumerate(data.get("storyline") or []):
        beat = _coerce_story_beat(raw, i)
        if beat is not None:
            storyline.append(beat)

    return NarrativePlan(
        title=title,
        audience=str(audience).strip() if audience else None,
        objective=str(objective).strip() if objective else None,
        executive_summary=str(executive_summary).strip() if executive_summary else None,
        storyline=storyline,
    )


def _minimal_narrative_plan(fallback_title: str) -> NarrativePlan:
    return NarrativePlan(
        title=fallback_title,
        storyline=[
            StoryBeat(id="beat-1", title="Overview", message=fallback_title, purpose="context"),
            StoryBeat(id="beat-2", title="Recommendation", message="Recommended next steps.", purpose="recommendation"),
            StoryBeat(id="beat-3", title="Close", message="Thank you.", purpose="closing"),
        ],
    )


def generate_narrative_plan(
    prompt_text: str,
    route: RouteDecision,
    *,
    extra_context: str | None = None,
) -> tuple[NarrativePlan, LLMResult]:
    """Run the narrative-planning LLM step and return a validated
    `NarrativePlan` plus its `LLMResult` (for cost/latency accounting).

    Always returns a valid `NarrativePlan`, even if the LLM call fails or
    returns unparseable/invalid JSON — failures degrade to a minimal
    3-beat storyline (context -> recommendation -> close) derived from the
    prompt text, mirroring `generate_doc_plan`'s fallback philosophy.

    This is purely additive (#141): it is not called by `generate_doc_plan`
    or wired into the existing pipeline. #142/#143 (Phase 3) consume it to
    build `generate_agentdeck_v2_plan()`.
    """
    fallback_title = (prompt_text or "Untitled Deck").strip().splitlines()[0][:120] or "Untitled Deck"

    try:
        result = invoke_llm(
            _build_outline_user_message(prompt_text, extra_context),
            route,
            deep_research=False,
            artifact_context=NARRATIVE_SYSTEM_PROMPT,
        )
    except Exception as exc:
        logger.warning("Narrative-planning LLM call failed, using minimal fallback: %s", exc)
        return _minimal_narrative_plan(fallback_title), LLMResult(
            answer="",
            model_used="fallback",
            latency_ms=0,
            prompt_tokens=None,
            completion_tokens=None,
            estimated_cost_usd=None,
            fallback_errors=[str(exc)],
        )

    data = _parse_json_object(result.answer) or {}
    try:
        plan = _coerce_narrative_plan(data, fallback_title=fallback_title)
    except Exception as exc:
        logger.warning("NarrativePlan validation failed, using minimal fallback: %s", exc)
        plan = _minimal_narrative_plan(fallback_title)

    if not plan.storyline:
        plan = _minimal_narrative_plan(fallback_title) if not plan.title else NarrativePlan(
            title=plan.title,
            audience=plan.audience,
            objective=plan.objective,
            executive_summary=plan.executive_summary,
            storyline=_minimal_narrative_plan(fallback_title).storyline,
        )

    return plan, result

# ---------------------------------------------------------------------------
# Slide planning + Designer stage (Phase 3, #142/#157/#143)
# ---------------------------------------------------------------------------

SLIDE_PLANNING_SYSTEM_PROMPT = """You are Fronei's presentation slide planner, step 2 of a 4-step AgentDeck v2 \
pipeline. Convert a narrative plan into a slide-level presentation plan. Output ONLY a JSON object.

Schema:
{
  "title": "Deck title",
  "subtitle": "Optional subtitle",
  "theme": "dark|light",
  "sections": [
    {
      "slide_id": "stable-id",
      "slide_layout": "SECTION_HEADER|CONTENT_1COL|CONTENT_2COL|CONTENT_3COL|CONTENT_4COL|CONTENT_HERO_STAT|CONTENT_TABLE_SIDEBAR|CONTENT_SPLIT_DECISIONS|CLOSING",
      "section_title": "Assertion-style slide title",
      "dek": "One-sentence subtitle/dek under the slide title",
      "purpose": "context|analysis|comparison|recommendation|decision|roadmap|evidence|closing",
      "audience_question": "Question this slide answers",
      "message": "Single sentence takeaway",
      "content_brief": "What content/data should be rendered here",
      "content_tags": ["narrative", "comparison", "decision"],
      "notes": "Optional speaker-note guidance",
      "evidence": [{"evidence_id": "e1", "confidence": "high"}]
    }
  ]
}

Rules:
- Use the narrative beats as the storyline spine. One beat usually becomes one slide.
- Use SECTION_HEADER sparingly; use CLOSING for the final ask/next step.
- Every content slide must have a `dek`, `message`, and `audience_question`.
- Do not create component blocks. The next step binds components.
"""

DESIGN_SYSTEM_PROMPT = """You are Fronei's presentation designer, step 3 of AgentDeck v2. Given a presentation \
plan and the available design-system layouts/components, choose the visual treatment for each slide. Output ONLY \
a JSON object.

Schema:
{
  "design_system": "agentdeck_v1",
  "theme": "dark|light",
  "visual_direction": "Short design direction",
  "density_strategy": "sparse|balanced|dense",
  "slide_treatments": [
    {
      "slide_id": "matching slide_id",
      "visual_role": "hero|section_divider|analysis|comparison|evidence|decision|roadmap|closing",
      "layout_id": "slide layout id",
      "component_choices": {"zone": ["component_id", "..."]},
      "hierarchy_notes": "What should dominate visually",
      "density_target": "sparse|balanced|dense",
      "repair_constraints": [{"type": "preserve_message", "note": "optional"}]
    }
  ]
}

Rules:
- Choose component ids that are valid for the layout/zone.
- Prefer fewer, stronger visual moves over dense generic cards.
- Preserve the slide message and any evidence references during future repairs.
"""


def _storybeat_to_section(beat: StoryBeat, index: int) -> SectionPlan:
    purpose = beat.purpose
    if purpose == "section":
        return SectionPlan(
            slide_id=beat.id,
            slide_layout="SECTION_HEADER",
            section_number=f"{index + 1:02d}",
            section_title=beat.title,
            section_subtitle=beat.message,
            purpose="section",
            message=beat.message,
            audience_question=beat.audience_question,
        )
    if purpose == "closing":
        return SectionPlan(
            slide_id=beat.id,
            slide_layout="CLOSING",
            closing_text=beat.message,
            closing_body=beat.audience_question,
            purpose="closing",
            message=beat.message,
            audience_question=beat.audience_question,
        )

    layout = _GENERIC_BY_PURPOSE.get(purpose, "CONTENT_1COL")
    return SectionPlan(
        slide_id=beat.id,
        slide_layout=layout,
        section_title=beat.message or beat.title,
        dek=beat.audience_question or beat.title,
        purpose=purpose,
        audience_question=beat.audience_question,
        message=beat.message,
        notes=beat.title,
    )


def _minimal_presentation_plan(
    narrative: NarrativePlan, *, theme: Theme = "dark", design_system: str = "agentdeck_v1"
) -> DocPlan:
    sections = [_storybeat_to_section(beat, i) for i, beat in enumerate(narrative.storyline)]
    if not sections:
        sections = [SectionPlan(slide_layout="CLOSING", closing_text="Thank You", purpose="closing")]
    return DocPlan(
        title=narrative.title,
        subtitle=narrative.audience,
        theme=theme,
        design_system=design_system,
        sections=sections,
    )


def _evidence_refs(raw: Any) -> list[EvidenceRef]:
    refs: list[EvidenceRef] = []
    if not isinstance(raw, list):
        return refs
    for item in raw:
        if not isinstance(item, dict):
            continue
        evidence_id = str(item.get("evidence_id") or "").strip()
        if not evidence_id:
            continue
        confidence = item.get("confidence")
        if confidence not in ("low", "medium", "high"):
            confidence = None
        refs.append(EvidenceRef(evidence_id=evidence_id, note=item.get("note"), confidence=confidence))
    return refs


def _coerce_presentation_plan(
    data: dict[str, Any], narrative: NarrativePlan, *, theme: Theme = "dark", design_system: str = "agentdeck_v1"
) -> DocPlan:
    outline = _coerce_outline(data)
    sections: list[SectionPlan] = []
    raw_sections = [raw for raw in (data.get("sections") or []) if isinstance(raw, dict)]
    for i, coerced in enumerate(outline.get("sections") or []):
        raw = raw_sections[i] if i < len(raw_sections) else coerced
        layout = coerced.get("slide_layout")
        beat = narrative.storyline[min(i, len(narrative.storyline) - 1)] if narrative.storyline else None
        kwargs: dict[str, Any] = {
            "slide_id": str(raw.get("slide_id") or (beat.id if beat else f"slide-{i + 1}")),
            "slide_layout": layout,
            "dek": str(raw.get("dek") or raw.get("subtitle") or (beat.audience_question if beat else "") or "").strip() or None,
            "purpose": raw.get("purpose") if raw.get("purpose") in _SLIDE_PURPOSES else (beat.purpose if beat else "analysis"),
            "audience_question": raw.get("audience_question") or (beat.audience_question if beat else None),
            "message": raw.get("message") or (beat.message if beat else None),
            "evidence": _evidence_refs(raw.get("evidence")),
            "notes": raw.get("notes"),
        }
        for key in (
            "section_title", "section_number", "section_subtitle", "hero_title", "subtitle",
            "presenter", "deck_type_label", "date_label", "confidentiality",
            "closing_text", "closing_body",
        ):
            if coerced.get(key) or raw.get(key):
                kwargs[key] = coerced.get(key) or raw.get(key)
        if layout in _GENERIC_CONTENT_LAYOUTS and not kwargs.get("section_title"):
            kwargs["section_title"] = kwargs.get("message") or (beat.message if beat else "Overview")
        try:
            sections.append(SectionPlan(**kwargs))
        except Exception as exc:
            logger.info("Dropping invalid slide-planning section %s: %s", i, exc)
    if not sections:
        return _minimal_presentation_plan(narrative, theme=theme, design_system=design_system)
    return DocPlan(
        title=outline.get("title") or narrative.title,
        subtitle=outline.get("subtitle") or narrative.audience,
        theme=theme,
        design_system=design_system,
        sections=sections,
    )


def generate_presentation_plan(
    narrative: NarrativePlan,
    evidence_pack: EvidencePack | None,
    route: RouteDecision,
    *,
    prompt_text: str = "",
    theme: Theme = "dark",
    design_system: str = "agentdeck_v1",
    user_document_profile: "UserDocumentProfile | None" = None,
) -> tuple[DocPlan, LLMResult]:
    payload = {
        "prompt": prompt_text,
        "narrative": narrative.model_dump(mode="json", exclude_none=True),
        "evidence_pack": (evidence_pack or EvidencePack()).model_dump(mode="json", exclude_none=True),
    }
    if user_document_profile is not None:
        payload["user_document_profile"] = user_document_profile.model_dump(mode="json", exclude_none=True)
    try:
        result = invoke_llm(
            json.dumps(payload, ensure_ascii=False),
            route,
            deep_research=False,
            artifact_context=SLIDE_PLANNING_SYSTEM_PROMPT,
        )
    except Exception as exc:
        logger.warning("Slide-planning LLM call failed, using fallback: %s", exc)
        return _minimal_presentation_plan(narrative, theme=theme, design_system=design_system), LLMResult(
            answer="",
            model_used="fallback",
            latency_ms=0,
            prompt_tokens=None,
            completion_tokens=None,
            estimated_cost_usd=None,
            fallback_errors=[str(exc)],
        )
    data = _parse_json_object(result.answer) or {}
    return _coerce_presentation_plan(data, narrative, theme=theme, design_system=design_system), result


def _visual_role_for_section(section: SectionPlan) -> str:
    if section.slide_layout == "SECTION_HEADER":
        return "section_divider"
    if section.slide_layout == "CLOSING":
        return "closing"
    if section.purpose in {"recommendation", "decision"}:
        return "decision"
    if section.purpose == "roadmap":
        return "roadmap"
    if section.purpose == "evidence":
        return "evidence"
    if section.purpose == "comparison":
        return "comparison"
    return "analysis"


def _component_choices_for_layout(slide_layout: str) -> dict[str, list[str]]:
    if slide_layout not in _GENERIC_CONTENT_LAYOUTS:
        return {}
    choices: dict[str, list[str]] = {}
    for zone in _layout_zones(slide_layout):
        ranked = rank_components(slide_layout, [])[:_MAX_CANDIDATES_PER_ZONE]
        choices[zone] = [component.id for component in ranked]
    return choices


def _fallback_design_plan(
    presentation_plan: DocPlan,
    *,
    quality_mode: QualityMode | str = DEFAULT_QUALITY_MODE,
) -> DesignPlan:
    mode = normalize_quality_mode(quality_mode)
    density_target = density_target_for_quality(mode)
    brand_strictness = brand_strictness_for_quality(mode)
    treatments: list[SlideDesignTreatment] = []
    for section in presentation_plan.sections:
        treatments.append(
            SlideDesignTreatment(
                slide_id=section.slide_id,
                visual_role=_visual_role_for_section(section),
                layout_id=section.slide_layout,
                component_choices=_component_choices_for_layout(section.slide_layout),
                density_target=density_target,
                repair_constraints=[
                    RepairConstraint(type="preserve_message"),
                    RepairConstraint(type="may_reduce_copy"),
                ],
            )
        )
    return DesignPlan(
        design_system=presentation_plan.design_system,
        theme=presentation_plan.theme,
        quality_mode=mode,
        visual_direction=(
            "Executive-ready AgentDeck layout using semantic tokens and sparse hierarchy. "
            f"Brand strictness: {brand_strictness}."
        ),
        density_strategy=density_target,
        slide_treatments=treatments,
    )


def _apply_brand_context(
    plan: DesignPlan,
    brand_profile: "BrandProfile | None",
    user_document_profile: "UserDocumentProfile | None",
) -> DesignPlan:
    """Stamp #155/#156 context onto a `DesignPlan`'s Phase-4 stub fields."""
    if brand_profile is not None:
        plan.brand_profile_id = brand_profile.id
        plan.brand_profile = brand_profile.model_dump(mode="json", exclude_none=True)
    if user_document_profile is not None:
        plan.user_document_profile = user_document_profile.model_dump(mode="json", exclude_none=True)
    return plan


def generate_design_plan(
    presentation_plan: DocPlan,
    route: RouteDecision,
    *,
    quality_mode: QualityMode | str = DEFAULT_QUALITY_MODE,
    brand_profile: "BrandProfile | None" = None,
    user_document_profile: "UserDocumentProfile | None" = None,
) -> tuple[DesignPlan, LLMResult]:
    mode = normalize_quality_mode(quality_mode)
    payload = {
        "presentation_plan": presentation_plan.model_dump(mode="json", exclude_none=True),
        "design_system": presentation_plan.design_system,
        "available_components": {
            section.slide_id or f"slide-{i + 1}": _component_choices_for_layout(section.slide_layout)
            for i, section in enumerate(presentation_plan.sections)
        },
        "quality_mode": mode,
        "density_target": density_target_for_quality(mode),
        "brand_strictness": brand_strictness_for_quality(mode),
    }
    if brand_profile is not None:
        payload["brand_profile"] = brand_profile.model_dump(mode="json", exclude_none=True)
    if user_document_profile is not None:
        payload["user_document_profile"] = user_document_profile.model_dump(mode="json", exclude_none=True)
    try:
        result = invoke_llm(
            json.dumps(payload, ensure_ascii=False),
            route,
            deep_research=False,
            artifact_context=DESIGN_SYSTEM_PROMPT,
        )
    except Exception as exc:
        logger.warning("Design-planning LLM call failed, using fallback: %s", exc)
        fallback = _apply_brand_context(
            _fallback_design_plan(presentation_plan, quality_mode=mode),
            brand_profile,
            user_document_profile,
        )
        return fallback, LLMResult(
            answer="",
            model_used="fallback",
            latency_ms=0,
            prompt_tokens=None,
            completion_tokens=None,
            estimated_cost_usd=None,
            fallback_errors=[str(exc)],
        )
    data = _parse_json_object(result.answer) or {}
    try:
        plan = DesignPlan.model_validate(data)
    except Exception:
        plan = _fallback_design_plan(presentation_plan, quality_mode=mode)
    if not plan.slide_treatments:
        plan = _fallback_design_plan(presentation_plan, quality_mode=mode)
    plan.design_system = presentation_plan.design_system
    plan.theme = presentation_plan.theme
    plan.quality_mode = mode
    plan = _apply_brand_context(plan, brand_profile, user_document_profile)
    return plan, result


def _combine_llm_results(results: list[LLMResult], *, answer: str = "") -> LLMResult:
    model_used = next((r.model_used for r in reversed(results) if r.model_used), "unknown")
    fallback_errors: list[str] = []
    prompt_tokens = 0
    completion_tokens = 0
    cost = 0.0
    latency = 0
    any_prompt = any(r.prompt_tokens is not None for r in results)
    any_completion = any(r.completion_tokens is not None for r in results)
    any_cost = any(r.estimated_cost_usd is not None for r in results)
    for result in results:
        fallback_errors.extend(result.fallback_errors)
        prompt_tokens += result.prompt_tokens or 0
        completion_tokens += result.completion_tokens or 0
        cost += result.estimated_cost_usd or 0.0
        latency += result.latency_ms
    return LLMResult(
        answer=answer,
        model_used=model_used,
        latency_ms=latency,
        prompt_tokens=prompt_tokens if any_prompt else None,
        completion_tokens=completion_tokens if any_completion else None,
        estimated_cost_usd=cost if any_cost else None,
        fallback_errors=fallback_errors,
    )


def _overlay_presentation_intent(doc_plan: DocPlan, presentation_plan: DocPlan) -> DocPlan:
    for i, section in enumerate(doc_plan.sections):
        if i >= len(presentation_plan.sections):
            continue
        source = presentation_plan.sections[i]
        section.slide_id = section.slide_id or source.slide_id
        section.dek = section.dek or source.dek
        section.purpose = source.purpose
        section.audience_question = source.audience_question
        section.message = source.message
        section.evidence = source.evidence
    return doc_plan


def generate_agentdeck_v2_plan(
    prompt_text: str,
    route: RouteDecision,
    *,
    theme: Theme = "dark",
    extra_context: str | None = None,
    db: Any | None = None,
    quality_mode: QualityMode | str = DEFAULT_QUALITY_MODE,
    brand_profile: "BrandProfile | None" = None,
    user_document_profile: "UserDocumentProfile | None" = None,
    design_system: str = "agentdeck_v1",
) -> tuple[DocPlan, DesignPlan, LLMResult]:
    mode = normalize_quality_mode(quality_mode)
    udp_context = _user_document_profile_context(user_document_profile)
    narrative_context = "\n\n".join(part for part in (extra_context, udp_context) if part)
    narrative, narrative_result = generate_narrative_plan(
        prompt_text, route, extra_context=narrative_context or None
    )
    evidence_pack = EvidencePack()
    presentation, presentation_result = generate_presentation_plan(
        narrative,
        evidence_pack,
        route,
        prompt_text=prompt_text,
        theme=theme,
        design_system=design_system,
        user_document_profile=user_document_profile,
    )
    design, design_result = generate_design_plan(
        presentation,
        route,
        quality_mode=mode,
        brand_profile=brand_profile,
        user_document_profile=user_document_profile,
    )

    doc_plan, doc_result = generate_doc_plan(
        prompt_text,
        route,
        theme=presentation.theme,
        extra_context=json.dumps(
            {
                "narrative": narrative.model_dump(mode="json", exclude_none=True),
                "presentation_plan": presentation.model_dump(mode="json", exclude_none=True),
                "design_plan": design.model_dump(mode="json", exclude_none=True),
            },
            ensure_ascii=False,
        ),
        db=db,
        design_system=design_system,
    )
    doc_plan = _overlay_presentation_intent(doc_plan, presentation)
    combined = _combine_llm_results(
        [narrative_result, presentation_result, design_result, doc_result],
        answer=doc_plan.model_dump_json(),
    )
    return doc_plan, design, combined
