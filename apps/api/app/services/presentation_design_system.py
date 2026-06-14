"""Presentation design-system registry for generated PPTX decks.

This module is intentionally deterministic and dependency-free. It is the
control plane between a content-oriented DeckPlan and the PptxGenJS renderer:
tokens, canonical layout aliases, slide templates, primitive component names,
and compact component trees.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


THEME_TOKENS: dict[str, dict[str, Any]] = {
    "warm-editorial": {
        "bg": "F6F0E6",
        "card": "FFFDF8",
        "card_line": "D8CDC6",
        "fg": "1F2937",
        "muted": "6B5E52",
        "accent": "B45009",
        "accent2": "0F766E",
        "warn": "D9544D",
        "success": "0F766E",
        "heading_font": "Georgia",
        "body_font": "Calibri",
        "chart_palette": ["B45009", "0F766E", "7C3AED", "C9A14A", "64748B"],
        "table_header_fill": "1F2937",
        "table_header_text": "FFFDF8",
    },
    "modern-tech": {
        "bg": "080C11",
        "card": "121A24",
        "card_line": "24303D",
        "fg": "EFF6FF",
        "muted": "AAB8C7",
        "accent": "22D3EE",
        "accent2": "A3E635",
        "warn": "FB7185",
        "success": "A3E635",
        "heading_font": "Calibri",
        "body_font": "Calibri",
        "chart_palette": ["22D3EE", "A3E635", "818CF8", "F59E0B", "94A3B8"],
        "table_header_fill": "24303D",
        "table_header_text": "EFF6FF",
    },
    "executive-navy": {
        "bg": "101827",
        "card": "172033",
        "card_line": "2A3752",
        "fg": "F8FAFC",
        "muted": "A7B2C5",
        "accent": "38BDF8",
        "accent2": "7C3AED",
        "warn": "F87171",
        "success": "34D399",
        "heading_font": "Calibri",
        "body_font": "Calibri",
        "chart_palette": ["38BDF8", "7C3AED", "34D399", "F59E0B", "CBD5E1"],
        "table_header_fill": "172033",
        "table_header_text": "F8FAFC",
    },
    "data-product-os": {
        "bg": "0B1220",
        "card": "111827",
        "card_line": "1E293B",
        "fg": "F1F5F9",
        "muted": "CBD5E1",
        "accent": "34D399",
        "accent2": "F59E0B",
        "warn": "F97316",
        "success": "34D399",
        "heading_font": "Calibri",
        "body_font": "Calibri",
        "chart_palette": ["34D399", "F59E0B", "38BDF8", "A78BFA", "94A3B8"],
        "table_header_fill": "1E293B",
        "table_header_text": "F1F5F9",
    },
    "clean-light": {
        "bg": "F8FAFC",
        "card": "FFFFFF",
        "card_line": "E2E8F0",
        "fg": "0F172A",
        "muted": "475569",
        "accent": "2563EB",
        "accent2": "10B981",
        "warn": "DC2626",
        "success": "10B981",
        "heading_font": "Calibri",
        "body_font": "Calibri",
        "chart_palette": ["2563EB", "10B981", "F59E0B", "7C3AED", "64748B"],
        "table_header_fill": "2563EB",
        "table_header_text": "FFFFFF",
    },
}

TYPOGRAPHY_TOKENS: dict[str, dict[str, Any]] = {
    "kicker": {"size": 12, "bold": True, "uppercase": True, "letter_spacing": 1.1},
    "title": {"size_min": 20, "size_max": 32, "bold": True},
    "title_hero": {"size_min": 36, "size_max": 44, "bold": True},
    "headline": {"size_min": 28, "size_max": 34, "bold": True},
    "body": {"size": 15, "bold": False},
    "body_sub": {"size": 13, "bold": False},
    "stat_value": {"size_min": 36, "size_max": 48, "bold": True},
    "stat_label": {"size": 12, "bold": False},
    "caption": {"size": 10, "bold": False},
    "table_header": {"size": 12, "bold": True},
    "table_cell": {"size": 11, "bold": False},
}

GRID_TOKENS: dict[str, float] = {
    "slide_width": 13.333,
    "slide_height": 7.5,
    "margin_x": 0.6,
    "title_y": 0.42,
    "accent_rule_y": 1.32,
    "content_top_y": 1.65,
    "content_bottom_y": 6.9,
    "gutter": 0.3,
    "card_radius": 0.06,
}

FIT_CONTRACTS: dict[str, dict[str, Any]] = {
    "TitleBlock": {"chars": 90, "max_lines": 2, "overflow": "speaker_notes"},
    "KickerLabel": {"chars": 34, "max_lines": 1, "overflow": "truncate"},
    "BodyBulletList": {"chars_per_item": 90, "max_items": 6, "overflow": "speaker_notes"},
    "AppendixBulletList": {"chars_per_item": 90, "max_items": 10, "overflow": "speaker_notes"},
    "StatCard": {"value_chars": 16, "label_chars": 60, "source_chars": 60, "overflow": "speaker_notes"},
    "CalloutBox": {"chars": 200, "max_lines": 3, "overflow": "speaker_notes"},
    "ComparisonCard": {"heading_chars": 50, "bullet_chars": 90, "max_items": 5, "overflow": "speaker_notes"},
    "Chart": {"max_categories": 12, "max_series": 4, "legend_chars": 22, "overflow": "speaker_notes"},
    "Table": {"max_rows": 8, "max_columns": 5, "cell_chars": 80, "overflow": "speaker_notes"},
    "Timeline": {"max_phases": 6, "phase_title_chars": 80, "phase_detail_chars": 160, "overflow": "speaker_notes"},
    "RiskMatrix": {"max_items": 5, "label_chars": 42, "overflow": "speaker_notes"},
    "RiskRegisterTable": {"max_rows": 8, "cell_chars": 90, "overflow": "speaker_notes"},
    "ArchitectureDiagram": {"max_nodes": 6, "node_chars": 48, "overflow": "speaker_notes"},
    "OperatingModelGrid": {"max_lanes": 6, "lane_chars": 90, "overflow": "speaker_notes"},
    "Footer": {"chars": 40, "overflow": "truncate"},
}

PRIMITIVE_COMPONENTS = {
    "TitleBlock",
    "KickerLabel",
    "BodyBulletList",
    "StatCard",
    "CalloutBox",
    "ComparisonCard",
    "Chart",
    "Table",
    "Timeline",
    "RiskMatrix",
    "RiskRegisterTable",
    "ArchitectureDiagram",
    "OperatingModelGrid",
    "InvestmentCaseBlock",
    "SectionDividerBlock",
    "Footer",
    "SourceCitation",
}

LAYOUT_ALIASES: dict[str, str] = {
    "cover": "section",
    "hero_cover": "section",
    "divider": "section",
    "section_break": "section",
    "intro": "section",
    "chapter": "section",
    "decision": "recommendation",
    "decision_slide": "recommendation",
    "decision_recommendation": "recommendation",
    "closing": "recommendation",
    "thank_you": "recommendation",
    "next_steps": "recommendation",
    "roadmap": "timeline",
    "process": "timeline",
    "process_steps": "timeline",
    "architecture_map": "architecture",
    "system_map": "architecture",
    "financial_exhibit": "financial_model",
    "data_exhibit": "financial_model",
    "three_card_system": "comparison",
    "governance_grid": "comparison",
    "principles_grid": "comparison",
    "takeaways": "executive_summary",
    "stats": "stat_cards",
    "metrics": "stat_cards",
    "kpi": "stat_cards",
    "kpi_grid": "stat_cards",
    "market_context": "stat_cards",
    "by_the_numbers": "stat_cards",
    "risk_matrix": "risk_matrix",
    "risk_heatmap": "risk_matrix",
    "risk_register": "risk_register",
    "agenda": "agenda",
    "toc": "agenda",
    "outline": "agenda",
    "quote": "callout",
    "testimonial": "callout",
}

SLIDE_TEMPLATES: dict[str, dict[str, Any]] = {
    "title": {"components": ["TitleBlock", "KickerLabel", "Footer"], "required": ["title"]},
    "section": {"components": ["SectionDividerBlock"], "required": ["title"]},
    "agenda": {"components": ["TitleBlock", "BodyBulletList"], "required": ["bullets"]},
    "content": {"components": ["TitleBlock", "BodyBulletList", "Footer"], "required": ["title"]},
    "bullets": {"components": ["TitleBlock", "BodyBulletList", "Footer"], "required": ["title"]},
    "two_content": {"components": ["TitleBlock", "ComparisonCard", "Footer"], "required": ["columns"]},
    "comparison": {"components": ["TitleBlock", "ComparisonCard", "Footer"], "required": ["columns"]},
    "stat_cards": {"components": ["TitleBlock", "StatCard", "CalloutBox", "Footer"], "required": ["stats"]},
    "callout": {"components": ["TitleBlock", "CalloutBox", "BodyBulletList", "Footer"], "required": ["title"]},
    "executive_summary": {"components": ["TitleBlock", "CalloutBox", "BodyBulletList", "Footer"], "required": ["bullets"]},
    "recommendation": {"components": ["TitleBlock", "CalloutBox", "BodyBulletList", "Footer"], "required": ["bullets"]},
    "chart": {"components": ["TitleBlock", "Chart", "SourceCitation", "Footer"], "required": ["chart"]},
    "financial_model": {"components": ["TitleBlock", "Chart", "StatCard", "Footer"], "required": ["chart"]},
    "table": {"components": ["TitleBlock", "Table", "SourceCitation", "Footer"], "required": ["table"]},
    "timeline": {"components": ["TitleBlock", "Timeline", "Footer"], "required": ["phases"]},
    "architecture": {"components": ["TitleBlock", "ArchitectureDiagram", "CalloutBox", "Footer"], "required": ["bullets"]},
    "risk_matrix": {"components": ["TitleBlock", "RiskMatrix", "RiskRegisterTable", "Footer"], "required": ["heatmap"]},
    "risk_register": {"components": ["TitleBlock", "RiskRegisterTable", "Footer"], "required": ["columns"]},
    "operating_model": {"components": ["TitleBlock", "OperatingModelGrid", "Footer"], "required": ["columns"]},
    "investment_case": {"components": ["TitleBlock", "InvestmentCaseBlock", "StatCard", "Footer"], "required": ["stats"]},
    "appendix": {"components": ["KickerLabel", "TitleBlock", "AppendixBulletList", "Footer"], "required": ["title"]},
}

ARCHETYPE_TO_TEMPLATE: dict[str, str] = {
    "section_divider": "section",
    "board_decision": "recommendation",
    "metric_scorecard": "stat_cards",
    "risk_register": "risk_register",
    "risk_heatmap": "risk_matrix",
    "operating_model": "operating_model",
    "architecture_map": "architecture",
    "investment_case": "investment_case",
    "roadmap": "timeline",
    "comparison_matrix": "comparison",
    "executive_summary": "executive_summary",
}


def canonical_layout(raw_layout: object) -> tuple[str, str | None]:
    layout = str(raw_layout or "content").strip().lower()
    if not layout:
        return "content", "unknown_layout:"
    if layout in SLIDE_TEMPLATES:
        return layout, None
    if layout in LAYOUT_ALIASES:
        return LAYOUT_ALIASES[layout], None
    return "content", f"unknown_layout:{layout}"


def template_for_slide(slide: dict[str, Any]) -> str:
    archetype = str(slide.get("archetype") or "")
    if archetype in ARCHETYPE_TO_TEMPLATE:
        return ARCHETYPE_TO_TEMPLATE[archetype]
    layout, _ = canonical_layout(slide.get("layout"))
    return layout


def component_tree_for_slide(slide: dict[str, Any]) -> dict[str, Any]:
    template_name = template_for_slide(slide)
    template = SLIDE_TEMPLATES.get(template_name, SLIDE_TEMPLATES["content"])
    components = [
        _component_payload(component_name, slide)
        for component_name in template["components"]
        if _component_has_content(component_name, slide)
    ]
    return {
        "template": template_name,
        "components": components,
        "required": list(template.get("required") or []),
    }


def design_system_payload(theme_name: str = "warm-editorial") -> dict[str, Any]:
    theme = THEME_TOKENS.get(theme_name) or THEME_TOKENS["warm-editorial"]
    return {
        "name": "fronei_pptx_design_system",
        "version": 1,
        "theme": theme_name,
        "tokens": {
            "theme": deepcopy(theme),
            "typography": deepcopy(TYPOGRAPHY_TOKENS),
            "grid": deepcopy(GRID_TOKENS),
            "fit": deepcopy(FIT_CONTRACTS),
        },
        "templates": {
            name: {"components": spec["components"], "required": spec.get("required", [])}
            for name, spec in SLIDE_TEMPLATES.items()
        },
        "primitive_components": sorted(PRIMITIVE_COMPONENTS),
    }


def _component_has_content(component_name: str, slide: dict[str, Any]) -> bool:
    if component_name in {"TitleBlock", "Footer", "SectionDividerBlock", "KickerLabel"}:
        return True
    if component_name in {"BodyBulletList", "AppendixBulletList"}:
        return bool(slide.get("bullets"))
    if component_name == "StatCard":
        return bool(slide.get("stats"))
    if component_name == "CalloutBox":
        return bool(slide.get("callout") or slide.get("bullets"))
    if component_name == "ComparisonCard":
        return bool(slide.get("columns"))
    if component_name == "Chart":
        return bool(slide.get("chart"))
    if component_name == "Table":
        return bool(slide.get("table"))
    if component_name == "Timeline":
        return bool(slide.get("phases"))
    if component_name == "RiskMatrix":
        return bool(slide.get("heatmap"))
    if component_name == "RiskRegisterTable":
        return bool(slide.get("heatmap") or slide.get("columns") or slide.get("table"))
    if component_name == "ArchitectureDiagram":
        return bool(slide.get("bullets") or slide.get("columns"))
    if component_name == "OperatingModelGrid":
        return bool(slide.get("columns") or slide.get("bullets"))
    if component_name == "InvestmentCaseBlock":
        return bool(slide.get("stats"))
    if component_name == "SourceCitation":
        return bool(_sources_for_slide(slide))
    return True


def _component_payload(component_name: str, slide: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": component_name}
    if component_name == "TitleBlock":
        payload["text"] = slide.get("title") or "Untitled"
    elif component_name == "KickerLabel":
        payload["text"] = "Appendix" if slide.get("layout") == "appendix" else str(slide.get("archetype") or slide.get("layout") or "")
    elif component_name in {"BodyBulletList", "AppendixBulletList"}:
        payload["items"] = slide.get("bullets") or []
    elif component_name == "StatCard":
        payload["items"] = slide.get("stats") or []
    elif component_name == "CalloutBox":
        payload["callout"] = slide.get("callout")
        payload["headline"] = (slide.get("bullets") or [None])[0]
    elif component_name == "ComparisonCard":
        payload["columns"] = slide.get("columns") or []
    elif component_name == "Chart":
        payload["chart"] = slide.get("chart")
    elif component_name == "Table":
        payload["rows"] = slide.get("table") or []
    elif component_name == "Timeline":
        payload["phases"] = slide.get("phases") or []
    elif component_name == "RiskMatrix":
        payload["items"] = slide.get("heatmap") or []
    elif component_name == "RiskRegisterTable":
        payload["items"] = slide.get("heatmap") or slide.get("columns") or slide.get("table") or []
    elif component_name == "ArchitectureDiagram":
        payload["nodes"] = slide.get("bullets") or []
        payload["columns"] = slide.get("columns") or []
    elif component_name == "OperatingModelGrid":
        payload["lanes"] = slide.get("columns") or slide.get("bullets") or []
    elif component_name == "InvestmentCaseBlock":
        payload["stats"] = slide.get("stats") or []
        payload["callout"] = slide.get("callout")
    elif component_name == "SourceCitation":
        payload["sources"] = _sources_for_slide(slide)
    return payload


def _sources_for_slide(slide: dict[str, Any]) -> list[str]:
    sources: list[str] = []
    for stat in slide.get("stats") or []:
        if isinstance(stat, dict) and stat.get("source"):
            sources.append(str(stat["source"]))
    return sources
