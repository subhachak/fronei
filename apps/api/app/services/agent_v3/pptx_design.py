from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services.components.render_plan import PptxRenderPlan, PptxSlidePlan, ZoneInstance
from app.services.design_systems.brand_generator import design_system_id_for_template
from app.services.design_systems.registry import DEFAULT_DESIGN_SYSTEM, get_design_system


@dataclass
class SlideSpec:
    title: str
    bullets: list[str] = field(default_factory=list)
    notes: str = ""
    table: list[list[str]] = field(default_factory=list)
    source_index: int = 0


@dataclass
class PptxDesignResult:
    payload: bytes
    design_system_id: str
    theme: str
    slide_count: int
    layout_counts: dict[str, int]
    design_ledger: list[dict[str, Any]]
    repair_actions: list[dict[str, Any]]


class AgentDeckRenderError(RuntimeError):
    pass


def render_agentdeck_pptx_from_markdown(
    title: str,
    markdown: str,
    *,
    theme: str = "dark",
    template_id: str | None = None,
    user_id: str | None = None,
) -> PptxDesignResult:
    design_system_id = _design_system_id_for_template(template_id, user_id)
    render_plan, repair_actions = agentdeck_render_plan_from_markdown(
        title,
        markdown,
        theme=theme,
        design_system_id=design_system_id,
        return_repairs=True,
    )
    payload = _render_agentdeck_plan(render_plan)
    layout_counts: dict[str, int] = {}
    for slide in render_plan.slides:
        layout_counts[slide.slide_layout] = layout_counts.get(slide.slide_layout, 0) + 1
    return PptxDesignResult(
        payload=payload,
        design_system_id=design_system_id,
        theme=theme,
        slide_count=len(render_plan.slides),
        layout_counts=layout_counts,
        design_ledger=_design_ledger(render_plan),
        repair_actions=repair_actions,
    )


def agentdeck_render_plan_from_markdown(
    title: str,
    markdown: str,
    *,
    theme: str = "dark",
    design_system_id: str = "agentdeck_v1",
    return_repairs: bool = False,
) -> PptxRenderPlan | tuple[PptxRenderPlan, list[dict[str, Any]]]:
    deck_title, deck_subtitle, slides = _parse_deck_markdown(title, markdown)
    slides, repair_actions = _repair_slide_specs(slides)
    slide_plans: list[PptxSlidePlan] = [
        PptxSlidePlan(
            slide_layout="TITLE",
            hero_title=deck_title,
            subtitle=deck_subtitle,
            deck_type_label="Agent v3 deck",
            confidentiality="Generated work product",
        )
    ]
    for index, slide in enumerate(slides, start=1):
        slide_plans.append(_slide_to_plan(slide, index=index))
    if len(slide_plans) == 1:
        slide_plans.append(
            PptxSlidePlan(
                slide_layout="CONTENT_1COL",
                title="Overview",
                header_bar=_header_bar(1),
                zones={"body": ZoneInstance(component_id="bullet_list", props={"items": _bullets_to_items([markdown or "No content generated."])})},
            )
        )
    slide_plans.append(
        PptxSlidePlan(
            slide_layout="CLOSING",
            closing_text="Next steps",
            closing_body="Use this deck as a working artifact: refine the narrative, confirm source-backed claims, and adapt the visuals to the audience.",
        )
    )
    resolved_theme = "light" if theme == "light" else "dark"
    render_plan = PptxRenderPlan.build(slide_plans[:26], theme=resolved_theme, design_system_id=design_system_id)  # title + 24 content + closing
    if return_repairs:
        return render_plan, repair_actions
    return render_plan


def _parse_deck_markdown(title: str, markdown: str) -> tuple[str, str | None, list[SlideSpec]]:
    deck_title = title.strip() or "Agent v3 presentation"
    deck_subtitle: str | None = None
    slides: list[SlideSpec] = []
    current: SlideSpec | None = None
    in_notes = False
    table_buffer: list[str] = []

    def flush_table() -> None:
        nonlocal table_buffer
        if current is not None and len(table_buffer) >= 2:
            parsed = _parse_markdown_table(table_buffer)
            if parsed:
                current.table = parsed
        table_buffer = []

    for raw_line in (markdown or "").splitlines():
        line = raw_line.strip()
        if not line:
            flush_table()
            continue
        if line.startswith("# "):
            deck_title = _clean_inline(line[2:]) or deck_title
            continue
        if line.startswith("## "):
            flush_table()
            current = SlideSpec(title=_clean_inline(line[3:]) or "Slide", source_index=len(slides) + 1)
            slides.append(current)
            in_notes = False
            continue
        if current is None:
            if deck_subtitle is None and not line.startswith("#"):
                deck_subtitle = _clean_inline(line)
            continue
        if "|" in line and line.startswith("|"):
            table_buffer.append(line)
            continue
        flush_table()
        lowered = line.lower()
        if lowered.startswith(("notes:", "speaker notes:", "presenter notes:")):
            current.notes = _clean_inline(line.split(":", 1)[1])
            in_notes = True
            continue
        if line.startswith(("- ", "* ")):
            current.bullets.append(_clean_inline(line[2:]))
            in_notes = False
            continue
        if re.match(r"^\d+[.)]\s+", line):
            current.bullets.append(_clean_inline(re.sub(r"^\d+[.)]\s+", "", line)))
            in_notes = False
            continue
        if in_notes:
            current.notes = (current.notes + " " + _clean_inline(line)).strip()
        elif len(current.bullets) < 5:
            current.bullets.append(_clean_inline(line))
        else:
            current.notes = (current.notes + " " + _clean_inline(line)).strip()
    flush_table()
    return deck_title, deck_subtitle, slides


def _slide_to_plan(slide: SlideSpec, *, index: int) -> PptxSlidePlan:
    title = _shorten(slide.title, 82)
    notes = slide.notes or None
    header = _header_bar(index)
    if slide.table:
        sidebar = ZoneInstance(component_id="bullet_list", props={"items": _bullets_to_items(slide.bullets[:5])})
        return PptxSlidePlan(
            slide_layout="CONTENT_TABLE_SIDEBAR",
            title=title,
            header_bar=header,
            zones={
                "table": ZoneInstance(component_id="table", props=_table_props(slide.table)),
                "sidebar": sidebar,
            },
            notes=notes,
        )
    stats = _extract_stats(slide.bullets)
    if stats:
        zones: dict[str, Any] = {"hero": ZoneInstance(component_id="stat_card", props=stats[0])}
        if len(stats) > 1:
            zones["supporting_row"] = ZoneInstance(component_id="stat_strip", props={"stats": stats[1:4]})
        return PptxSlidePlan(
            slide_layout="CONTENT_HERO_STAT",
            title=title,
            header_bar=header,
            zones=zones,
            notes=notes,
        )
    if _looks_like_timeline(slide):
        return PptxSlidePlan(
            slide_layout="CONTENT_1COL",
            title=title,
            header_bar=header,
            zones={"body": ZoneInstance(component_id="timeline", props={"nodes": _timeline_nodes(slide.bullets), "orientation": "horizontal"})},
            notes=notes,
        )
    if _looks_like_decision_slide(slide):
        cards = [_card_from_text(item, variant="filled") for item in slide.bullets[:6]]
        midpoint = max(1, (len(cards) + 1) // 2)
        return PptxSlidePlan(
            slide_layout="CONTENT_SPLIT_DECISIONS",
            title=title,
            header_bar=header,
            zones={
                "left_panel": ZoneInstance(component_id="decision_list", props={"cards": cards[:midpoint]}),
                "right_panel": ZoneInstance(component_id="decision_list", props={"cards": cards[midpoint:] or cards[:1]}),
            },
            notes=notes,
        )
    if 2 <= len(slide.bullets) <= 3:
        zone_names = ["col_left", "col_right"] if len(slide.bullets) == 2 else ["col_1", "col_2", "col_3"]
        layout = "CONTENT_2COL" if len(slide.bullets) == 2 else "CONTENT_3COL"
        zones = {
            zone_names[i]: ZoneInstance(component_id="card", props=_card_from_text(item, color_variant=_color_variant(i)))
            for i, item in enumerate(slide.bullets)
        }
        return PptxSlidePlan(slide_layout=layout, title=title, header_bar=header, zones=zones, notes=notes)
    return PptxSlidePlan(
        slide_layout="CONTENT_1COL",
        title=title,
        header_bar=header,
        zones={"body": ZoneInstance(component_id="bullet_list", props={"items": _bullets_to_items(slide.bullets[:6])})},
        notes=notes,
    )


def _render_agentdeck_plan(render_plan: PptxRenderPlan) -> bytes:
    node = shutil.which("node")
    if not node:
        raise AgentDeckRenderError("Node.js is not available for AgentDeck rendering")
    api_root = Path(__file__).resolve().parents[3]
    renderer = api_root / "pptx_render" / "agentdeck" / "render_agentdeck.js"
    if not renderer.exists():
        raise AgentDeckRenderError(f"AgentDeck renderer missing at {renderer}")
    try:
        proc = subprocess.run(
            [node, str(renderer)],
            input=json.dumps(render_plan.to_payload()).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=90,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AgentDeckRenderError("AgentDeck rendering timed out") from exc
    if proc.returncode != 0 or not proc.stdout.startswith(b"PK"):
        error = proc.stderr.decode("utf-8", errors="ignore")[-1200:]
        raise AgentDeckRenderError(error or f"AgentDeck renderer exited with {proc.returncode}")
    return proc.stdout


def _design_system_id_for_template(template_id: str | None, user_id: str | None = None) -> str:
    """Resolve a PPTX template selection to a registered design-system id.

    Uploaded templates generate user-scoped design systems named from
    (user_id, template_id). Built-in template ids may also be passed directly
    if they are already registered design-system ids. Any miss falls back to
    the base AgentDeck system instead of failing the whole artifact.
    """
    normalized = str(template_id or "").strip()
    if not normalized or normalized == "fronei-default":
        return DEFAULT_DESIGN_SYSTEM

    candidates: list[str] = []
    if user_id:
        candidates.append(design_system_id_for_template(user_id, normalized))
    candidates.append(normalized)

    for candidate in candidates:
        try:
            get_design_system(candidate)
            return candidate
        except Exception:
            continue
    return DEFAULT_DESIGN_SYSTEM


def _repair_slide_specs(slides: list[SlideSpec]) -> tuple[list[SlideSpec], list[dict[str, Any]]]:
    repaired: list[SlideSpec] = []
    actions: list[dict[str, Any]] = []

    for slide in slides:
        bullets = [_shorten_bullet_for_slide(item) for item in slide.bullets if item]
        long_bullet_count = sum(1 for original, shortened in zip(slide.bullets, bullets) if original != shortened)
        if long_bullet_count:
            actions.append(
                {
                    "type": "compact_long_bullets",
                    "source_slide": slide.source_index,
                    "title": slide.title,
                    "count": long_bullet_count,
                }
            )

        table = slide.table
        if table and (len(table) > 8 or any(len(row) > 5 for row in table)):
            table = [row[:5] for row in table[:8]]
            actions.append(
                {
                    "type": "trim_table",
                    "source_slide": slide.source_index,
                    "title": slide.title,
                    "max_rows": 8,
                    "max_columns": 5,
                }
            )

        if len(bullets) <= 6:
            repaired.append(SlideSpec(title=slide.title, bullets=bullets, notes=slide.notes, table=table, source_index=slide.source_index))
            continue

        chunks = [bullets[i : i + 5] for i in range(0, len(bullets), 5)]
        actions.append(
            {
                "type": "split_dense_slide",
                "source_slide": slide.source_index,
                "title": slide.title,
                "chunks": len(chunks),
            }
        )
        for index, chunk in enumerate(chunks, start=1):
            suffix = f" ({index}/{len(chunks)})" if len(chunks) > 1 else ""
            repaired.append(
                SlideSpec(
                    title=f"{slide.title}{suffix}",
                    bullets=chunk,
                    notes=slide.notes if index == len(chunks) else "",
                    table=table if index == 1 else [],
                    source_index=slide.source_index,
                )
            )

    return repaired, actions


def _design_ledger(render_plan: PptxRenderPlan) -> list[dict[str, Any]]:
    ledger: list[dict[str, Any]] = []
    for index, slide in enumerate(render_plan.slides, start=1):
        zones = slide.zones or {}
        components: list[dict[str, Any]] = []
        for zone_name, zone in zones.items():
            if isinstance(zone, list):
                component_ids = [item.component_id for item in zone]
            else:
                component_ids = [zone.component_id]
            components.append({"zone": zone_name, "components": component_ids})
        ledger.append(
            {
                "slide": index,
                "layout": slide.slide_layout,
                "visual_role": _visual_role_for_slide(slide),
                "title": slide.hero_title or slide.title or slide.closing_text or "",
                "components": components,
                "notes": bool(slide.notes),
            }
        )
    return ledger


def _visual_role_for_slide(slide: PptxSlidePlan) -> str:
    if slide.slide_layout == "TITLE":
        return "opening"
    if slide.slide_layout == "CLOSING":
        return "close"
    if slide.slide_layout == "CONTENT_HERO_STAT":
        return "metric_focus"
    if slide.slide_layout == "CONTENT_TABLE_SIDEBAR":
        return "comparison"
    if slide.slide_layout == "CONTENT_SPLIT_DECISIONS":
        return "decision"
    return "explanation"


def _shorten_bullet_for_slide(text: str) -> str:
    return _shorten(text, 210)


def _header_bar(index: int) -> dict[str, Any]:
    return {"section_number": f"{index:02d}", "section_title": "Fronei work product", "variant": "surface"}


def _bullets_to_items(items: list[str]) -> list[dict[str, Any]]:
    cleaned = [_shorten(item, 150) for item in items if item]
    if not cleaned:
        cleaned = ["Key point not specified."]
    return [{"text": item, "level": 0} for item in cleaned[:6]]


def _card_from_text(text: str, *, variant: str = "outlined", color_variant: str | None = None) -> dict[str, Any]:
    parts = re.split(r"\s+[—:-]\s+", text, maxsplit=1)
    title = _shorten(parts[0], 46)
    body = _shorten(parts[1] if len(parts) > 1 else text, 190)
    card: dict[str, Any] = {"title": title, "body": body, "variant": variant}
    if color_variant:
        card["color_variant"] = color_variant
    return card


def _color_variant(index: int) -> str:
    return ["blue", "teal", "gold"][index % 3]


def _table_props(table: list[list[str]]) -> dict[str, Any]:
    headers = table[0][:5] if table else []
    rows = [row[: len(headers)] for row in table[1:7]]
    return {"headers": headers, "rows": rows}


def _parse_markdown_table(lines: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in lines:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not cells or all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells):
            continue
        rows.append([_clean_inline(cell) for cell in cells])
    return rows


def _extract_stats(items: list[str]) -> list[dict[str, Any]]:
    stats: list[dict[str, Any]] = []
    for item in items:
        match = re.search(r"(?<![A-Za-z])(?:\$[\d,.]+|\d+(?:\.\d+)?%|\d+(?:\.\d+)?x)(?![A-Za-z])", item, flags=re.IGNORECASE)
        if not match:
            continue
        if not re.search(r"[%$]|\bx\b|revenue|cost|growth|latency|budget|share|rate|score|spend|users|sources", item, flags=re.IGNORECASE):
            continue
        value = match.group(0)
        label = _shorten(item.replace(value, "").strip(" -:—"), 70) or "Metric"
        stats.append({"value": value, "label": label, "caption": None})
    return stats[:4]


def _looks_like_timeline(slide: SlideSpec) -> bool:
    text = " ".join([slide.title, *slide.bullets]).lower()
    return any(term in text for term in ("timeline", "roadmap", "phase", "milestone", "sequence", "workflow"))


def _timeline_nodes(items: list[str]) -> list[dict[str, Any]]:
    nodes = []
    for idx, item in enumerate(items[:5], start=1):
        parts = re.split(r"\s+[—:-]\s+", item, maxsplit=1)
        nodes.append(
            {
                "step_label": f"{idx}",
                "title": _shorten(parts[0], 36),
                "body": _shorten(parts[1] if len(parts) > 1 else item, 110),
            }
        )
    return nodes or [{"step_label": "1", "title": "Start", "body": "Begin execution."}]


def _looks_like_decision_slide(slide: SlideSpec) -> bool:
    text = " ".join([slide.title, *slide.bullets]).lower()
    return bool(
        re.search(
            r"\b(recommend|recommendation|decision|next steps?|actions?|risks?|mitigations?|options?|trade-offs?)\b",
            text,
        )
    )


def _clean_inline(text: str) -> str:
    value = text.strip()
    value = re.sub(r"\*\*([^*]+)\*\*", r"\1", value)
    value = re.sub(r"\*([^*]+)\*", r"\1", value)
    value = re.sub(r"`([^`]+)`", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    return " ".join(value.split())


def _shorten(text: str, limit: int) -> str:
    value = _clean_inline(text)
    if len(value) <= limit:
        return value
    clipped = value[: limit - 1].rsplit(" ", 1)[0].strip()
    return clipped or value[:limit].strip()
