from __future__ import annotations

import copy
import json
import logging
import re
import shutil
import subprocess
from datetime import date
from io import BytesIO
from pathlib import Path

from docx import Document
from docx.enum.text import WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt
from docx.text.paragraph import Paragraph
from markdown_it import MarkdownIt
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn as pptx_qn
from pptx.util import Inches as PptxInches, Pt as PptxPt

from app.services.document_templates import resolve_pptx_template_path

logger = logging.getLogger(__name__)

# PptxGenJS-based renderer for the "no template" (fronei-default) PPTX path —
# see PPTX_RENDER_DIR / "render.js". Decks built from a built-in or
# user-uploaded branded .pptx template still go through python-pptx (below),
# which can read that template's layouts/placeholders directly.
PPTX_RENDER_DIR = Path(__file__).resolve().parents[2] / "pptx_render"
PPTX_RENDER_JS = PPTX_RENDER_DIR / "render.js"
PPTX_RENDER_TIMEOUT_SECONDS = 60


TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
_MARKDOWN_INLINE = MarkdownIt("commonmark")
KNOWN_DOC_TYPES = {
    "executive_report",
    "proposal",
    "memo",
    "technical_spec",
    "meeting_notes",
    "one_pager",
    "letter",
    "resume",
    "presentation",
}
SPEAKER_NOTES_RE = re.compile(r"^speaker notes?\s*:\s*(.*)$", re.IGNORECASE)
MAX_BULLETS_PER_SLIDE = 6
# Slide titles are assertion-style, short headlines. The prompt asks the LLM
# for ~40-60 chars; this is the ceiling beyond which _shorten_title_to_notes
# shortens at a clause/word boundary (never mid-word, never literal "...")
# and routes the full title to speaker_notes.
MAX_SLIDE_TITLE_CHARS = 90
MAX_BULLET_CHARS = 90

# Default theme used by the python-pptx fallback renderer when the uploaded
# template's layouts provide no usable placeholders (common for "design
# scaffold" templates where every slide is hand-laid-out free shapes).
# Mirrors the warm-editorial palette used by pptx_render/render.js.
PPTX_FALLBACK_HEADING_FONT = "Georgia"
PPTX_FALLBACK_BODY_FONT = "Calibri"
PPTX_FALLBACK_TEXT_RGB = RGBColor(0x28, 0x24, 0x21)
PPTX_FALLBACK_ACCENT_RGB = RGBColor(0xE0, 0x4F, 0x00)
PPTX_FALLBACK_NAVY_RGB = RGBColor(0x1F, 0x3B, 0x5C)
PPTX_FALLBACK_CARD_BG_RGB = RGBColor(0xFF, 0xFD, 0xFC)
PPTX_FALLBACK_CARD_LINE_RGB = RGBColor(0xD8, 0xCD, 0xC6)

# Title box/accent-rule geometry for the fallback title textbox — mirrors
# TITLE_BOX_H / TITLE_RULE_Y / CONTENT_TOP_Y in render.js so placeholder-less
# templates get the same layout rhythm as the PptxGenJS renderer.
PPTX_TITLE_BOX_H = 1.0
PPTX_TITLE_RULE_Y = 1.32
PPTX_CONTENT_TOP_Y = 1.65

# Per-template design tokens for the python-pptx fallback renderer. Each of
# the built-in "design scaffold" templates (warm-editorial, modern-tech,
# executive-navy, data-product-os, clean-light) ships its own bg/card/fg/
# muted/accent/accent2 palette and heading/body fonts, extracted from that
# template's "design system snapshot" slide. The fallback renderer (used for
# templates whose layouts have no placeholders) looks up the active theme via
# `_pptx_theme()` so generated decks match the uploaded template's palette
# instead of a single hardcoded warm-editorial look.
PPTX_DEFAULT_THEME: dict = {
    "bg": RGBColor(0xF6, 0xF0, 0xE6),
    "card": PPTX_FALLBACK_CARD_BG_RGB,
    "card_line": PPTX_FALLBACK_CARD_LINE_RGB,
    "fg": PPTX_FALLBACK_TEXT_RGB,
    "muted": RGBColor(0x6B, 0x5E, 0x52),
    "accent": PPTX_FALLBACK_ACCENT_RGB,
    "accent2": RGBColor(0x0F, 0x76, 0x6E),
    "heading_font": PPTX_FALLBACK_HEADING_FONT,
    "body_font": PPTX_FALLBACK_BODY_FONT,
}

PPTX_TEMPLATE_THEMES: dict[str, dict] = {
    "warm-editorial": {
        "bg": RGBColor(0xF6, 0xF0, 0xE6),
        "card": RGBColor(0xFF, 0xFD, 0xF8),
        "card_line": RGBColor(0xD8, 0xCD, 0xC6),
        "fg": RGBColor(0x1F, 0x29, 0x37),
        "muted": RGBColor(0x6B, 0x5E, 0x52),
        "accent": RGBColor(0xB4, 0x50, 0x09),
        "accent2": RGBColor(0x0F, 0x76, 0x6E),
        "heading_font": "Georgia",
        "body_font": "Calibri",
    },
    "modern-tech": {
        "bg": RGBColor(0x08, 0x0C, 0x11),
        "card": RGBColor(0x12, 0x1A, 0x24),
        "card_line": RGBColor(0x24, 0x30, 0x3D),
        "fg": RGBColor(0xEF, 0xF6, 0xFF),
        "muted": RGBColor(0xAA, 0xB8, 0xC7),
        "accent": RGBColor(0x22, 0xD3, 0xEE),
        "accent2": RGBColor(0xA3, 0xE6, 0x35),
        "heading_font": "Calibri",
        "body_font": "Calibri",
    },
    "executive-navy": {
        "bg": RGBColor(0x10, 0x18, 0x27),
        "card": RGBColor(0x17, 0x20, 0x33),
        "card_line": RGBColor(0x2A, 0x37, 0x52),
        "fg": RGBColor(0xF8, 0xFA, 0xFC),
        "muted": RGBColor(0xA7, 0xB2, 0xC5),
        "accent": RGBColor(0x38, 0xBD, 0xF8),
        "accent2": RGBColor(0x7C, 0x3A, 0xED),
        "heading_font": "Calibri",
        "body_font": "Calibri",
    },
    "data-product-os": {
        "bg": RGBColor(0x0B, 0x12, 0x20),
        "card": RGBColor(0x11, 0x18, 0x27),
        "card_line": RGBColor(0x1E, 0x29, 0x3B),
        "fg": RGBColor(0xF1, 0xF5, 0xF9),
        "muted": RGBColor(0xCB, 0xD5, 0xE1),
        "accent": RGBColor(0x34, 0xD3, 0x99),
        "accent2": RGBColor(0xF5, 0x9E, 0x0B),
        "heading_font": "Calibri",
        "body_font": "Calibri",
    },
    "clean-light": {
        "bg": RGBColor(0xF8, 0xFA, 0xFC),
        "card": RGBColor(0xFF, 0xFF, 0xFF),
        "card_line": RGBColor(0xE2, 0xE8, 0xF0),
        "fg": RGBColor(0x0F, 0x17, 0x2A),
        "muted": RGBColor(0x47, 0x55, 0x69),
        "accent": RGBColor(0x25, 0x63, 0xEB),
        "accent2": RGBColor(0x10, 0xB9, 0x81),
        "heading_font": "Calibri",
        "body_font": "Calibri",
    },
}

# Active theme for the current render call (set by
# `_generate_pptx_bytes_python_pptx` before rendering, restored afterward).
# Module-level rather than threaded through every helper signature for
# simplicity — PPTX generation is synchronous/single-call per request.
_ACTIVE_PPTX_THEME: dict = PPTX_DEFAULT_THEME


def _pptx_theme() -> dict:
    return _ACTIVE_PPTX_THEME


def _pptx_set_theme(template_id: str | None) -> dict:
    """Activate the design theme for `template_id` and return the previous
    theme so the caller can restore it after rendering."""
    global _ACTIVE_PPTX_THEME
    previous = _ACTIVE_PPTX_THEME
    _ACTIVE_PPTX_THEME = PPTX_TEMPLATE_THEMES.get(template_id or "", PPTX_DEFAULT_THEME)
    return previous


def _pptx_restore_theme(previous: dict) -> None:
    global _ACTIVE_PPTX_THEME
    _ACTIVE_PPTX_THEME = previous


def _pptx_set_slide_background(slide) -> None:
    """Fill the slide background with the active theme's `bg` color. New
    slides added to a placeholder-less template layout don't inherit a
    background fill, so dark themes would otherwise render on a white page."""
    theme = _pptx_theme()
    bg = theme.get("bg")
    if bg is None:
        return
    try:
        fill = slide.background.fill
        fill.solid()
        fill.fore_color.rgb = bg
    except Exception:
        pass


def _pptx_add_slide(prs: Presentation, role: str):
    """Add a slide for `role` and immediately paint the theme background.

    Without this, slides rendered onto a template layout that *has* a title
    placeholder (the common case for real PowerPoint templates) never call
    `_pptx_set_slide_background` — only the placeholder-less fallback path
    did — so themed decks rendered with plain white backgrounds regardless
    of the active theme's `bg` color. Centralizing add_slide here ensures
    every slide gets the theme background up front; per-shape fills (cards,
    accent rules, etc.) are layered on top by the layout-specific renderers."""
    slide = prs.slides.add_slide(_pptx_layout_for_role(prs, role))
    _pptx_set_slide_background(slide)
    return slide

# Appendix slides are reference material — denser content is acceptable, so
# they get a higher per-slide bullet cap than the standard body slides.
MAX_APPENDIX_BULLETS = 10

# Layout name aliases normalized by parse_deck_plan. Both sides of an alias
# pair are treated identically by the PPTX renderer.
DECK_LAYOUT_ALIASES = {
    "cover": "section",
    "hero_cover": "section",
    "decision": "recommendation",
    "decision_slide": "recommendation",
    "decision_recommendation": "recommendation",
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
}
COVER_DOC_TYPES = {"executive_report", "proposal", "technical_spec"}
COMPACT_HEADER_DOC_TYPES = {"memo", "one_pager", "resume"}
TOC_DOC_TYPES = {"executive_report", "proposal"}


def _inline_tokens(text: str):
    try:
        parsed = _MARKDOWN_INLINE.parseInline(str(text or ""))
    except Exception:
        return None
    if not parsed:
        return []
    return parsed[0].children or []


def _clean_inline(text: str) -> str:
    tokens = _inline_tokens(text)
    if tokens is None:
        text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
        text = re.sub(r"`([^`]+)`", r"\1", text)
        text = re.sub(r"(\*\*|__)(.*?)\1", r"\2", text)
        text = re.sub(r"(\*|_)(.*?)\1", r"\2", text)
        return text.strip()

    parts: list[str] = []
    link_href: str | None = None
    for token in tokens:
        if token.type == "link_open":
            link_href = (token.attrs or {}).get("href")
            continue
        if token.type == "link_close":
            if link_href:
                parts.append(f" ({link_href})")
            link_href = None
            continue
        if token.type in {"text", "code_inline", "image"}:
            parts.append(token.content)
        elif token.type in {"softbreak", "hardbreak"}:
            parts.append("\n")
    return "".join(parts).strip()


def _inline_token_text(token) -> str:
    if token.type == "image":
        return token.content or (token.attrs or {}).get("alt", "")
    if token.type in {"softbreak", "hardbreak"}:
        return "\n"
    return token.content or ""


def _split_table_row(line: str) -> list[str]:
    trimmed = line.strip().strip("|")
    return [cell.strip() for cell in trimmed.split("|")]


def _is_table_start(lines: list[str], idx: int) -> bool:
    return (
        idx + 1 < len(lines)
        and lines[idx].strip().startswith("|")
        and TABLE_SEPARATOR_RE.match(lines[idx + 1]) is not None
    )


def _add_table(doc: Document, rows: list[list[str]]) -> None:
    if not rows:
        return
    width = max(len(row) for row in rows)
    table = doc.add_table(rows=len(rows), cols=width)
    table.style = "Table Grid"
    for r_idx, row in enumerate(rows):
        for c_idx in range(width):
            cell = table.cell(r_idx, c_idx)
            cell.text = ""
            paragraph = cell.paragraphs[0]
            _add_inline_runs(paragraph, row[c_idx] if c_idx < len(row) else "", base_bold=(r_idx == 0))
            if r_idx == 0:
                for paragraph in cell.paragraphs:
                    for run in paragraph.runs:
                        run.bold = True


def _add_code_block(doc: Document, code_lines: list[str]) -> None:
    if not code_lines:
        return
    paragraph = doc.add_paragraph()
    run = paragraph.add_run("\n".join(code_lines))
    run.font.name = "Courier New"
    run.font.size = Pt(9)


def _add_hyperlink(paragraph: Paragraph, text: str, url: str, base_bold: bool, base_italic: bool) -> None:
    part = paragraph.part
    relationship_id = part.relate_to(
        url,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True,
    )
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), relationship_id)

    run_element = OxmlElement("w:r")
    properties = OxmlElement("w:rPr")

    if base_bold:
        properties.append(OxmlElement("w:b"))
    if base_italic:
        properties.append(OxmlElement("w:i"))

    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0563C1")
    properties.append(color)
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    properties.append(underline)

    text_element = OxmlElement("w:t")
    text_element.text = text
    run_element.append(properties)
    run_element.append(text_element)
    hyperlink.append(run_element)
    paragraph._p.append(hyperlink)


def _add_run(
    paragraph: Paragraph,
    text: str,
    *,
    bold: bool = False,
    italic: bool = False,
    code: bool = False,
    base_bold: bool = False,
    base_italic: bool = False,
) -> None:
    if not text:
        return
    run = paragraph.add_run(text)
    run.bold = base_bold or bold
    run.italic = base_italic or italic
    if code:
        run.font.name = "Courier New"
        run.font.size = Pt(9.5)


def _add_inline_runs(
    paragraph: Paragraph,
    text: str,
    *,
    base_bold: bool = False,
    base_italic: bool = False,
) -> None:
    """Add CommonMark inline text as formatted DOCX runs."""
    tokens = _inline_tokens(text)
    if tokens is None:
        _add_run(paragraph, str(text or "").strip(), base_bold=base_bold, base_italic=base_italic)
        return

    bold_depth = 0
    italic_depth = 0
    link_href: str | None = None
    for token in tokens:
        if token.type == "strong_open":
            bold_depth += 1
            continue
        if token.type == "strong_close":
            bold_depth = max(0, bold_depth - 1)
            continue
        if token.type == "em_open":
            italic_depth += 1
            continue
        if token.type == "em_close":
            italic_depth = max(0, italic_depth - 1)
            continue
        if token.type == "link_open":
            link_href = (token.attrs or {}).get("href")
            continue
        if token.type == "link_close":
            link_href = None
            continue

        token_text = _inline_token_text(token)
        if not token_text:
            continue
        is_bold = bold_depth > 0
        is_italic = italic_depth > 0
        if link_href and token.type != "code_inline":
            _add_hyperlink(paragraph, token_text, link_href, base_bold or is_bold, base_italic or is_italic)
            continue
        _add_run(
            paragraph,
            token_text,
            bold=is_bold,
            italic=is_italic,
            code=(token.type == "code_inline"),
            base_bold=base_bold,
            base_italic=base_italic,
        )


def _add_inline_paragraph(
    doc: Document,
    text: str,
    *,
    style: str | None = None,
    base_bold: bool = False,
    base_italic: bool = False,
) -> None:
    paragraph = doc.add_paragraph(style=style)
    _add_inline_runs(paragraph, text, base_bold=base_bold, base_italic=base_italic)


def _add_field(paragraph: Paragraph, instruction: str, placeholder: str = "") -> None:
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    run._r.append(begin)

    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = instruction
    run._r.append(instr)

    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    run._r.append(separate)
    if placeholder:
        text = OxmlElement("w:t")
        text.text = placeholder
        run._r.append(text)

    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(end)


def _add_footer(section) -> None:
    paragraph = section.footer.paragraphs[0]
    paragraph.text = "Fronei"
    paragraph.add_run(" | Page ")
    _add_field(paragraph, "PAGE", "1")


def _apply_type_styles(doc: Document, doc_type: str) -> None:
    section = doc.sections[0]
    if doc_type == "executive_report":
        section.top_margin = Inches(0.85)
        section.bottom_margin = Inches(0.8)
        section.left_margin = Inches(0.9)
        section.right_margin = Inches(0.9)
        sizes = [("Title", 28), ("Heading 1", 20), ("Heading 2", 15), ("Heading 3", 12)]
    elif doc_type == "one_pager":
        section.top_margin = Inches(0.55)
        section.bottom_margin = Inches(0.55)
        section.left_margin = Inches(0.6)
        section.right_margin = Inches(0.6)
        sizes = [("Title", 18), ("Heading 1", 14), ("Heading 2", 12), ("Heading 3", 11)]
        doc.styles["Normal"].font.size = Pt(9.5)
    elif doc_type == "proposal":
        section.top_margin = Inches(0.8)
        section.bottom_margin = Inches(0.8)
        section.left_margin = Inches(0.85)
        section.right_margin = Inches(0.85)
        sizes = [("Title", 26), ("Heading 1", 18), ("Heading 2", 14), ("Heading 3", 12)]
    else:
        sizes = [("Title", 24), ("Heading 1", 18), ("Heading 2", 14), ("Heading 3", 12)]

    for style_name, size in sizes:
        doc.styles[style_name].font.size = Pt(size)


def _add_cover(doc: Document, title: str, subtitle: str | None) -> None:
    _add_inline_paragraph(doc, _clean_inline(title) or "Fronei document", style="Title")
    if subtitle:
        _add_inline_paragraph(doc, subtitle, base_italic=True)
    doc.add_paragraph("")
    prepared = doc.add_paragraph()
    _add_inline_runs(prepared, f"Prepared for Fronei\n{date.today().isoformat()}")
    doc.add_page_break()


def _add_compact_header(doc: Document, title: str, subtitle: str | None) -> None:
    _add_inline_paragraph(doc, _clean_inline(title) or "Fronei document", style="Heading 1")
    meta = doc.add_paragraph()
    meta_text = f"Prepared for Fronei | {date.today().isoformat()}"
    if subtitle:
        meta_text = f"{subtitle} | {meta_text}"
    _add_inline_runs(meta, meta_text, base_italic=True)


def _add_toc(doc: Document, content: str) -> None:
    """Render a static table of contents from the document's headings.

    Deliberately avoids a Word TOC field (w:fldChar / instrText "TOC ...").
    Field codes — even without w:updateFields set — make Word show
    "This document contains fields that may refer to other files. Do you
    want to update the fields in this document?" on open for some users/
    Word configurations. A plain heading list has identical informational
    value without any field codes.
    """
    headings: list[tuple[int, str]] = []
    skipped_first_h1 = False
    for raw in content.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        match = re.match(r"^(#{1,3})\s+(.+)$", raw.strip())
        if not match:
            continue
        level = len(match.group(1))
        text = _clean_inline(match.group(2)).strip()
        if level == 1 and not skipped_first_h1:
            # Skip the document's own title heading (rendered separately).
            skipped_first_h1 = True
            continue
        if text:
            headings.append((level, text))

    if not headings:
        return

    doc.add_heading("Table of Contents", level=1)
    for level, text in headings:
        paragraph = doc.add_paragraph(text)
        paragraph.paragraph_format.left_indent = Inches(0.25 * (level - 1))
    doc.add_page_break()


def _strip_leading_h1(content: str) -> str:
    """Remove a leading top-level Markdown heading, if present.

    Used when the title is already rendered separately (cover page or
    compact header) so it doesn't appear a second time in the body.
    """
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx < len(lines) and re.match(r"^#\s+.+$", lines[idx].strip()):
        return "\n".join(lines[idx + 1:])
    return content


def generate_docx_bytes(title: str, content: str, subtitle: str | None = None, doc_type: str | None = None) -> bytes:
    """Render Markdown-ish text into a simple, professional DOCX document."""
    doc = Document()
    section = doc.sections[0]
    section.top_margin = Inches(0.7)
    section.bottom_margin = Inches(0.7)
    section.left_margin = Inches(0.8)
    section.right_margin = Inches(0.8)

    styles = doc.styles
    styles["Normal"].font.name = "Aptos"
    styles["Normal"].font.size = Pt(10.5)
    for style_name, size in [("Title", 24), ("Heading 1", 18), ("Heading 2", 14), ("Heading 3", 12)]:
        styles[style_name].font.name = "Aptos Display"
        styles[style_name].font.size = Pt(size)

    enhanced_doc_type = doc_type if doc_type in KNOWN_DOC_TYPES else None
    if enhanced_doc_type:
        _apply_type_styles(doc, enhanced_doc_type)
        _add_footer(section)
        if enhanced_doc_type in COVER_DOC_TYPES:
            _add_cover(doc, title, subtitle)
            if enhanced_doc_type in TOC_DOC_TYPES:
                _add_toc(doc, content)
        elif enhanced_doc_type in COMPACT_HEADER_DOC_TYPES:
            _add_compact_header(doc, title, subtitle)
        # The cover / compact header already renders the title, so drop a
        # leading "# Title" line from the body to avoid showing it twice.
        content = _strip_leading_h1(content)
    else:
        _add_inline_paragraph(doc, _clean_inline(title) or "Fronei document", style="Title")
        if subtitle:
            _add_inline_paragraph(doc, subtitle, base_italic=True)

    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    idx = 0
    in_code = False
    code_lines: list[str] = []

    while idx < len(lines):
        raw = lines[idx]
        line = raw.rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            if in_code:
                _add_code_block(doc, code_lines)
                code_lines = []
                in_code = False
            else:
                in_code = True
            idx += 1
            continue

        if in_code:
            code_lines.append(raw)
            idx += 1
            continue

        if not stripped:
            idx += 1
            continue

        if _is_table_start(lines, idx):
            rows = [_split_table_row(lines[idx])]
            idx += 2
            while idx < len(lines) and lines[idx].strip().startswith("|"):
                rows.append(_split_table_row(lines[idx]))
                idx += 1
            _add_table(doc, rows)
            continue

        if stripped in {"---", "***", "___"}:
            paragraph = doc.add_paragraph()
            paragraph.add_run().add_break(WD_BREAK.LINE)
            idx += 1
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading_match:
            level = min(len(heading_match.group(1)), 3)
            _add_inline_paragraph(doc, heading_match.group(2), style=f"Heading {level}")
            idx += 1
            continue

        bullet_match = re.match(r"^[-*+]\s+(.+)$", stripped)
        if bullet_match:
            _add_inline_paragraph(doc, bullet_match.group(1), style="List Bullet")
            idx += 1
            continue

        number_match = re.match(r"^\d+[.)]\s+(.+)$", stripped)
        if number_match:
            _add_inline_paragraph(doc, number_match.group(1), style="List Number")
            idx += 1
            continue

        blockquote_match = re.match(r"^>\s+(.+)$", stripped)
        if blockquote_match:
            _add_inline_paragraph(doc, blockquote_match.group(1), base_italic=True)
            idx += 1
            continue

        _add_inline_paragraph(doc, stripped)
        idx += 1

    if code_lines:
        _add_code_block(doc, code_lines)

    output = BytesIO()
    doc.save(output)
    return output.getvalue()


# --- XLSX generation ------------------------------------------------------

_XLSX_HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
_XLSX_HEADER_FONT = Font(bold=True, color="FFFFFF")
_XLSX_TITLE_FONT = Font(bold=True, size=14)
_XLSX_SUBTITLE_FONT = Font(italic=True, size=10, color="595959")
_XLSX_HEADING_FONTS = {1: Font(bold=True, size=12), 2: Font(bold=True, size=11), 3: Font(bold=True, size=10.5)}
_XLSX_MAX_COL_WIDTH = 60
_XLSX_MIN_COL_WIDTH = 10


def _xlsx_sheet_title(base: str, used: set[str]) -> str:
    """Return a unique, Excel-safe sheet title (<=31 chars, no [] : * ? / \\)."""
    safe = re.sub(r"[\[\]:*?/\\]", " ", base).strip()
    safe = re.sub(r"\s+", " ", safe) or "Sheet"
    safe = safe[:31]
    candidate = safe
    n = 2
    while candidate in used:
        suffix = f" ({n})"
        candidate = safe[: 31 - len(suffix)] + suffix
        n += 1
    used.add(candidate)
    return candidate


def _xlsx_autosize_columns(ws, rows: list[list[str]]) -> None:
    if not rows:
        return
    widths: dict[int, int] = {}
    for row in rows:
        for c_idx, value in enumerate(row, start=1):
            length = len(str(value)) if value is not None else 0
            widths[c_idx] = max(widths.get(c_idx, 0), length)
    for c_idx, width in widths.items():
        ws.column_dimensions[get_column_letter(c_idx)].width = max(
            _XLSX_MIN_COL_WIDTH, min(_XLSX_MAX_COL_WIDTH, width + 2)
        )


def _xlsx_numeric_value(value) -> float | None:
    """Coerce a table cell value to a float for charting, stripping common
    formatting like thousands separators, currency symbols, and percent signs."""
    if value is None:
        return None
    cleaned = str(value).replace(",", "").replace("$", "").strip()
    is_percent = cleaned.endswith("%")
    if is_percent:
        cleaned = cleaned[:-1].strip()
    if not cleaned:
        return None
    try:
        num = float(cleaned)
    except ValueError:
        return None
    return num / 100 if is_percent else num


def _xlsx_add_table_chart(ws, rows: list[list[str]]) -> None:
    """Add a native bar chart next to a table when at least one non-label
    column is fully numeric. Numeric cells are rewritten as real numbers so
    the chart (and any downstream formulas) can reference them directly."""
    if len(rows) < 2:
        return
    width = max(len(row) for row in rows)
    if width < 2:
        return
    numeric_cols: list[int] = []
    for c in range(1, width):
        values = [_xlsx_numeric_value(row[c]) if c < len(row) else None for row in rows[1:]]
        if values and all(v is not None for v in values):
            numeric_cols.append(c)
    if not numeric_cols:
        return
    for r_idx, row in enumerate(rows[1:], start=2):
        for c in numeric_cols:
            if c < len(row):
                ws.cell(row=r_idx, column=c + 1, value=_xlsx_numeric_value(row[c]))
    chart = BarChart()
    chart.type = "col"
    chart.style = 10
    data = Reference(ws, min_col=numeric_cols[0] + 1, max_col=numeric_cols[-1] + 1, min_row=1, max_row=len(rows))
    categories = Reference(ws, min_col=1, min_row=2, max_row=len(rows))
    chart.add_data(data, titles_from_data=True)
    chart.set_categories(categories)
    chart.height = 9
    chart.width = 16
    anchor = f"{get_column_letter(width + 2)}2"
    ws.add_chart(chart, anchor)


def _xlsx_write_table(wb: Workbook, sheet_name: str, rows: list[list[str]], used_sheet_names: set[str]):
    title = _xlsx_sheet_title(sheet_name, used_sheet_names)
    ws = wb.create_sheet(title=title)
    for row in rows:
        ws.append(row)
    if rows:
        width = max(len(row) for row in rows)
        for c_idx in range(1, width + 1):
            cell = ws.cell(row=1, column=c_idx)
            cell.font = _XLSX_HEADER_FONT
            cell.fill = _XLSX_HEADER_FILL
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{get_column_letter(width)}{len(rows)}"
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
    _xlsx_autosize_columns(ws, rows)
    _xlsx_add_table_chart(ws, rows)
    return ws


def generate_xlsx_bytes(title: str, content: str, subtitle: str | None = None, doc_type: str | None = None) -> bytes:
    """Render Markdown-ish text into a multi-sheet XLSX workbook.

    Any Markdown tables in `content` become their own sheets (named after the
    nearest preceding heading, falling back to "Table N"), styled with a bold
    header row, autofilter, and frozen header. All remaining narrative content
    (headings, paragraphs, bullets) is collected into an "Overview" sheet.
    """
    wb = Workbook()
    overview = wb.active
    overview.title = "Overview"
    used_sheet_names = {"Overview"}

    overview_rows: list[list[str]] = []

    title_cell_row = 1
    overview.cell(row=title_cell_row, column=1, value=_clean_inline(title) or "Fronei document").font = _XLSX_TITLE_FONT
    overview_rows.append([title])
    next_row = 2
    if subtitle:
        overview.cell(row=next_row, column=1, value=subtitle).font = _XLSX_SUBTITLE_FONT
        overview_rows.append([subtitle])
        next_row += 1
    meta = f"Prepared for Fronei | {date.today().isoformat()}"
    overview.cell(row=next_row, column=1, value=meta).font = _XLSX_SUBTITLE_FONT
    overview_rows.append([meta])
    next_row += 1
    overview.cell(row=next_row, column=1, value="")
    next_row += 1

    # The title is already rendered above, so drop a leading "# Title" line
    # from the body to avoid showing it twice in the Overview sheet.
    content = _strip_leading_h1(content)

    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    idx = 0
    in_code = False
    code_lines: list[str] = []
    last_heading = ""
    table_count = 0

    def _write_overview(value: str, *, font: Font | None = None) -> None:
        nonlocal next_row
        cell = overview.cell(row=next_row, column=1, value=value)
        if font:
            cell.font = font
        overview_rows.append([value])
        next_row += 1

    while idx < len(lines):
        raw = lines[idx]
        line = raw.rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            in_code = not in_code
            if not in_code and code_lines:
                _write_overview("\n".join(code_lines))
                code_lines = []
            idx += 1
            continue

        if in_code:
            code_lines.append(raw)
            idx += 1
            continue

        if not stripped:
            idx += 1
            continue

        if _is_table_start(lines, idx):
            rows = [_split_table_row(lines[idx])]
            idx += 2
            while idx < len(lines) and lines[idx].strip().startswith("|"):
                rows.append(_split_table_row(lines[idx]))
                idx += 1
            table_count += 1
            sheet_name = last_heading or f"Table {table_count}"
            _xlsx_write_table(wb, sheet_name, rows, used_sheet_names)
            continue

        if stripped in {"---", "***", "___"}:
            idx += 1
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading_match:
            level = min(len(heading_match.group(1)), 3)
            text = _clean_inline(heading_match.group(2))
            _write_overview(text, font=_XLSX_HEADING_FONTS.get(level))
            last_heading = text
            idx += 1
            continue

        bullet_match = re.match(r"^[-*+]\s+(.+)$", stripped)
        if bullet_match:
            _write_overview(f"• {_clean_inline(bullet_match.group(1))}")
            idx += 1
            continue

        number_match = re.match(r"^\d+[.)]\s+(.+)$", stripped)
        if number_match:
            _write_overview(_clean_inline(number_match.group(1)))
            idx += 1
            continue

        blockquote_match = re.match(r"^>\s+(.+)$", stripped)
        if blockquote_match:
            _write_overview(_clean_inline(blockquote_match.group(1)))
            idx += 1
            continue

        _write_overview(_clean_inline(stripped))
        idx += 1

    if code_lines:
        _write_overview("\n".join(code_lines))

    overview.column_dimensions["A"].width = 100
    for row in overview.iter_rows(min_row=1, max_row=next_row - 1, max_col=1):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    output = BytesIO()
    wb.save(output)
    return output.getvalue()


# --- PPTX generation -------------------------------------------------------
#
# Convention expected from the document writer for doc_type="presentation":
#   - The first H1 ("# ...") is the deck title; an immediately-following
#     italic line (e.g. "*Subtitle*") becomes the subtitle on the title slide.
#   - Subsequent H1s are section-divider slides (title only).
#   - H2s are slide titles; the bullets/paragraphs/table beneath an H2 become
#     that slide's body. H3s render as bold sub-headers within the slide body.
#   - A line of the form "Speaker notes: ..." (optionally wrapped in
#     italics/blockquote) becomes that slide's speaker notes and is not
#     rendered on the slide itself.
#   - Markdown tables become their own "Title Only" slide with a real table.
# Slides with more than MAX_BULLETS_PER_SLIDE bullet lines are split into
# "(cont.)" continuation slides to keep density reasonable.

# Semantic slide-layout roles used by the DeckPlan renderer. Rather than
# assuming every template ships PowerPoint's default 12-layout master in the
# default order (true for Fronei's built-in templates, but not guaranteed for
# user-uploaded .pptx templates), layouts are resolved by:
#   1. Matching the layout's display name against known PowerPoint layout
#      names/aliases for the role (works for most templates, including
#      non-default masters that keep conventional English layout names).
#   2. Falling back to a placeholder-shape heuristic (title-only, title +
#      single content, title + two content, title + body-only "section").
#   3. Falling back to the standard PowerPoint default-template index for
#      the role, then to the first layout.
# This lets user-uploaded templates act as real branded layout systems
# instead of just a visual theme applied to a fixed layout order.
_PPTX_ROLE_NAME_KEYWORDS: dict[str, tuple[str, ...]] = {
    "title": ("title slide",),
    "section": ("section header", "section divider", "divider", "agenda"),
    "content": ("title and content",),
    "two_content": ("two content", "comparison", "two column"),
    "title_only": ("title only",),
}

# Standard PowerPoint default-template layout indices, used as a last-resort
# fallback when neither name nor placeholder-shape matching finds a layout.
_PPTX_ROLE_INDEX_FALLBACK: dict[str, int] = {
    "title": 0,
    "content": 1,
    "section": 2,
    "two_content": 3,
    "title_only": 5,
}


def _pptx_layout_placeholder_types(layout) -> list:
    return [ph.placeholder_format.type for ph in layout.placeholders]


def _pptx_classify_layout(layout) -> set[str]:
    """Return the set of roles a layout's placeholder shapes are suited for."""
    from pptx.enum.shapes import PP_PLACEHOLDER_TYPE as PPT

    types = _pptx_layout_placeholder_types(layout)
    has_title = PPT.TITLE in types or PPT.CENTER_TITLE in types
    has_subtitle = PPT.SUBTITLE in types
    object_count = sum(1 for t in types if t == PPT.OBJECT)
    body_count = sum(1 for t in types if t == PPT.BODY)
    content_count = object_count + body_count

    roles: set[str] = set()
    if PPT.CENTER_TITLE in types and has_subtitle:
        roles.add("title")
    if has_title and object_count >= 2:
        roles.add("two_content")
    if has_title and content_count == 1:
        roles.add("content")
        if body_count == 1 and object_count == 0:
            roles.add("section")
    if has_title and content_count == 0:
        roles.add("title_only")
    return roles


def _pptx_layout_for_role(prs: Presentation, role: str):
    """Resolve a slide layout for a semantic role (title/content/section/
    two_content/title_only) — see role-resolution notes above."""
    layouts = list(prs.slide_layouts)
    if not layouts:
        raise ValueError("Presentation has no slide layouts")

    for keyword in _PPTX_ROLE_NAME_KEYWORDS.get(role, ()):
        for layout in layouts:
            if keyword in (layout.name or "").lower():
                return layout

    for layout in layouts:
        if role in _pptx_classify_layout(layout):
            return layout

    idx = _PPTX_ROLE_INDEX_FALLBACK.get(role, 0)
    return layouts[idx] if idx < len(layouts) else layouts[0]


def _presentation_from_template(template_id: str | None = None, template_path: str | Path | None = None) -> Presentation:
    template_path = Path(template_path) if template_path else resolve_pptx_template_path(template_id)
    prs = Presentation(str(template_path)) if template_path else Presentation()
    # Built-in templates carry sample slides so users can inspect them outside
    # the app. Keep masters/layouts/theme, but remove sample content before
    # rendering the generated deck.
    while len(prs.slides) > 0:
        slide_id_list = prs.slides._sldIdLst
        rel_id = slide_id_list[0].rId
        prs.part.drop_rel(rel_id)
        slide_id_list.remove(slide_id_list[0])
    return prs


def _pptx_add_runs(text_frame_or_paragraph, text: str) -> None:
    """Add CommonMark inline text as runs to a pptx paragraph."""
    paragraph = text_frame_or_paragraph
    tokens = _inline_tokens(text)
    if tokens is None:
        paragraph.add_run().text = str(text or "").strip()
        return

    bold_depth = 0
    italic_depth = 0
    link_depth = 0
    for token in tokens:
        if token.type == "strong_open":
            bold_depth += 1
            continue
        if token.type == "strong_close":
            bold_depth = max(0, bold_depth - 1)
            continue
        if token.type == "em_open":
            italic_depth += 1
            continue
        if token.type == "em_close":
            italic_depth = max(0, italic_depth - 1)
            continue
        if token.type == "link_open":
            link_depth += 1
            continue
        if token.type == "link_close":
            link_depth = max(0, link_depth - 1)
            continue

        token_text = _inline_token_text(token)
        if not token_text:
            continue
        run = paragraph.add_run()
        run.text = token_text
        run.font.bold = bold_depth > 0
        run.font.italic = italic_depth > 0
        if token.type == "code_inline":
            run.font.name = "Courier New"
        if link_depth:
            run.font.underline = True


def _pptx_bullet_indent(level: int) -> int:
    return max(0, min(level, 4))


def _shorten(text: str, limit: int) -> str:
    cleaned = re.sub(r"\s+", " ", _clean_inline(str(text or ""))).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 3)].rstrip() + "..."


_TITLE_CLAUSE_BREAK_RE = re.compile(r"[.!?:;—–-]\s")


def _shorten_title_to_notes(text: str, limit: int) -> tuple[str, str | None]:
    """Shorten a slide title without ever cutting mid-word or appending a
    literal "...". Slide titles wrap (word_wrap + TOP anchor), so a long
    title rendered in full just takes an extra line — but assertion-style
    titles read far better when cut at a natural clause boundary than when
    hard-truncated mid-sentence with "...".

    Strategy: if the cleaned title fits within `limit`, return it as-is. If
    not, prefer cutting at the last sentence/clause-ending punctuation
    (. ! ? : ; — - ) within the limit (as long as that keeps at least ~40%
    of the budget, so we don't end up with a one-word title). Otherwise cut
    at the last word boundary before `limit`. The full original title is
    returned as the second element so callers can preserve it in
    speaker_notes."""
    cleaned = re.sub(r"\s+", " ", _clean_inline(str(text or ""))).strip()
    if len(cleaned) <= limit:
        return cleaned, None

    window = cleaned[:limit]
    best_cut = -1
    for m in _TITLE_CLAUSE_BREAK_RE.finditer(window):
        best_cut = m.start() + 1  # keep the punctuation, drop the trailing space
    if best_cut >= limit * 0.4:
        return cleaned[:best_cut].rstrip(), cleaned

    last_space = window.rfind(" ")
    if last_space >= limit * 0.4:
        return cleaned[:last_space].rstrip(), cleaned

    return window.rstrip(), cleaned


def _shorten_to_notes(text: str, limit: int) -> tuple[str, str | None]:
    """Like `_shorten`, but also returns the full original text when it had
    to be truncated, so callers can route the overflow into speaker notes
    instead of silently dropping it (copy/notes separation)."""
    cleaned = re.sub(r"\s+", " ", _clean_inline(str(text or ""))).strip()
    if len(cleaned) <= limit:
        return cleaned, None
    return cleaned[: max(0, limit - 3)].rstrip() + "...", cleaned


def _slide_visual_object(
    table_rows: list, columns: list, phases: list, chart: dict | None, stats: list | None = None
) -> str | None:
    """The single "visual job" a slide is committed to, derived from which
    structured content it carries. Part of the SlideBlueprint commitment
    (archetype + density + visual_object) made before rendering."""
    if chart:
        return "chart"
    if table_rows:
        return "table"
    if phases:
        return "timeline"
    if stats:
        return "stat_cards"
    if columns:
        return "columns"
    return None


def _slide_density(
    bullet_count: int, table_rows: list, columns: list, phases: list, stats: list | None = None
) -> str:
    """Coarse content-density classification ("low" | "medium" | "high") used
    as part of the SlideBlueprint commitment. Density is computed from
    whichever structured content the slide carries, not just bullets."""
    if phases:
        weight = len(phases)
    elif stats:
        weight = len(stats)
    elif columns:
        weight = sum(len(c.get("bullets") or []) for c in columns)
    elif table_rows:
        weight = len(table_rows)
    else:
        weight = bullet_count
    if weight <= 3:
        return "low"
    if weight <= MAX_BULLETS_PER_SLIDE:
        return "medium"
    return "high"


def _extract_json_candidate(content: str) -> str | None:
    stripped = content.strip()
    if not stripped:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        return fence.group(1)
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    return None


def parse_deck_plan(content: str) -> dict | None:
    """Return a tolerant DeckPlan dict from JSON content, or None.

    The LLM-facing schema is intentionally small and product-oriented:
    title/subtitle plus slides with layout, assertion title, bullets, table,
    columns, and speaker notes. The renderer accepts minor variants so model
    output is useful without brittle exact-key dependence.
    """
    candidate = _extract_json_candidate(content)
    if not candidate:
        return None
    try:
        data = json.loads(candidate)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    slides = data.get("slides")
    if not isinstance(slides, list):
        return None
    normalized: dict = {
        "title": _shorten(data.get("title") or data.get("deck_title") or "Fronei deck", 120),
        "subtitle": _shorten(data.get("subtitle") or data.get("audience") or "", 160),
        "slides": [],
    }
    for raw in slides:
        if not isinstance(raw, dict):
            continue
        layout = str(raw.get("layout") or raw.get("type") or "bullets").lower().strip()
        layout = DECK_LAYOUT_ALIASES.get(layout, layout)
        raw_title = raw.get("title") or raw.get("headline") or raw.get("key_message") or "Untitled"
        title, title_overflow = _shorten_title_to_notes(raw_title, MAX_SLIDE_TITLE_CHARS)
        bullets_raw = raw.get("bullets") or raw.get("points") or []
        if isinstance(bullets_raw, str):
            bullets_raw = [bullets_raw]
        bullets_raw = [str(b) for b in bullets_raw if str(b or "").strip()]

        # Copy/notes separation: any text that gets truncated to fit on the
        # slide, and any bullets beyond the per-slide cap, are routed into
        # speaker_notes (full detail preserved) rather than silently lost.
        overflow_notes: list[str] = []
        if title_overflow:
            overflow_notes.append(f"Full title: {title_overflow}")
        if layout in {"executive_summary", "recommendation"} and bullets_raw:
            # The first bullet is the headline/primary assertion rendered in a
            # large font (e.g. _pptx_render_executive_summary's 28pt headline)
            # — give it the same generous budget as slide titles instead of
            # truncating it mid-sentence at the standard bullet length.
            headline, headline_overflow = _shorten_to_notes(bullets_raw[0], MAX_SLIDE_TITLE_CHARS)
            if headline_overflow:
                overflow_notes.append(f"Full headline: {headline_overflow}")
            bullets = [headline]
            rest = bullets_raw[1:]
        else:
            bullets = []
            rest = bullets_raw
        for b in rest:
            shortened, overflow = _shorten_to_notes(b, MAX_BULLET_CHARS)
            bullets.append(shortened)
            if overflow:
                overflow_notes.append(f"Full point: {overflow}")
        bullet_cap = MAX_APPENDIX_BULLETS if layout == "appendix" else MAX_BULLETS_PER_SLIDE
        if len(bullets) > bullet_cap:
            dropped = bullets[bullet_cap:]
            bullets = bullets[:bullet_cap]
            for d in dropped:
                overflow_notes.append(f"Additional point: {d}")
        notes = raw.get("speaker_notes") or raw.get("notes") or ""
        table = raw.get("table")
        if isinstance(table, dict):
            headers = table.get("headers") or table.get("columns") or []
            rows = table.get("rows") or []
            table = [headers] + rows if headers else rows
        if not isinstance(table, list):
            table = []
        table_rows = []
        for row in table:
            if isinstance(row, dict):
                table_rows.append([_shorten(v, 80) for v in row.values()])
            elif isinstance(row, list):
                table_rows.append([_shorten(v, 80) for v in row])
        columns = raw.get("columns") or []
        normalized_columns = []
        if isinstance(columns, list):
            for col in columns[:3]:
                if not isinstance(col, dict):
                    continue
                col_bullets = col.get("bullets") or col.get("points") or []
                if isinstance(col_bullets, str):
                    col_bullets = [col_bullets]
                normalized_columns.append({
                    "heading": _shorten(col.get("heading") or col.get("title") or "", 50),
                    "bullets": [_shorten(b, 90) for b in col_bullets if str(b or "").strip()][:5],
                })
        stats_raw = raw.get("stats") or raw.get("metrics") or raw.get("kpis") or []
        normalized_stats = []
        if isinstance(stats_raw, list):
            for stat in stats_raw[:4]:
                if not isinstance(stat, dict):
                    continue
                value = _shorten(stat.get("value") or stat.get("number") or stat.get("metric") or "", 16)
                label = _shorten(stat.get("label") or stat.get("title") or stat.get("description") or "", 60)
                if not value and not label:
                    continue
                normalized_stats.append({
                    "value": value,
                    "label": label,
                    "source": _shorten(stat.get("source") or stat.get("citation") or "", 60),
                })

        callout_raw = raw.get("callout") or raw.get("key_insight") or raw.get("insight")
        normalized_callout = None
        if isinstance(callout_raw, dict):
            callout_text, callout_overflow = _shorten_to_notes(
                callout_raw.get("text") or callout_raw.get("body") or callout_raw.get("description") or "", 200
            )
            if callout_text:
                normalized_callout = {
                    "label": _shorten(callout_raw.get("label") or callout_raw.get("title") or "Key Insight", 30),
                    "text": callout_text,
                }
                if callout_overflow:
                    overflow_notes.append(f"Full insight: {callout_overflow}")
        elif isinstance(callout_raw, str) and callout_raw.strip():
            callout_text, callout_overflow = _shorten_to_notes(callout_raw, 200)
            normalized_callout = {"label": "Key Insight", "text": callout_text}
            if callout_overflow:
                overflow_notes.append(f"Full insight: {callout_overflow}")

        phases = raw.get("phases") or []
        normalized_phases = []
        if isinstance(phases, list):
            for ph in phases[:6]:
                if isinstance(ph, dict):
                    normalized_phases.append({
                        "label": _shorten(ph.get("label") or ph.get("name") or ph.get("date") or "", 40),
                        "title": _shorten(ph.get("title") or ph.get("headline") or "", 80),
                        "description": _shorten(ph.get("description") or ph.get("detail") or ph.get("summary") or "", 160),
                    })
                elif str(ph or "").strip():
                    normalized_phases.append({"label": "", "title": _shorten(ph, 80), "description": ""})

        chart = raw.get("chart")
        normalized_chart = None
        if isinstance(chart, dict):
            categories = chart.get("categories") or chart.get("labels") or []
            series_raw = chart.get("series") or []
            series = []
            if isinstance(categories, list) and isinstance(series_raw, list):
                for s in series_raw[:4]:
                    if not isinstance(s, dict):
                        continue
                    values = s.get("values") or s.get("data") or []
                    numeric_values = []
                    if isinstance(values, list):
                        for v in values:
                            try:
                                numeric_values.append(float(v))
                            except (TypeError, ValueError):
                                numeric_values = []
                                break
                    if numeric_values:
                        series.append({
                            "name": _shorten(s.get("name") or "Series", 40),
                            "values": numeric_values,
                        })
            if categories and series:
                chart_type = str(chart.get("type") or "bar").lower().strip()
                if chart_type not in {"bar", "line", "pie"}:
                    chart_type = "bar"
                normalized_chart = {
                    "type": chart_type,
                    "categories": [_shorten(c, 30) for c in categories][:12],
                    "series": series,
                }

        # SlideBlueprint commitment: an archetype, content density, and the
        # single "visual job" the slide is doing — computed deterministically
        # from the normalized content so every slide is committed to a shape
        # before rendering, even if the planner didn't supply hints.
        archetype = str(raw.get("archetype") or "").strip() or layout
        visual_object = _slide_visual_object(table_rows, normalized_columns, normalized_phases, normalized_chart, normalized_stats)
        density = _slide_density(len(bullets), table_rows, normalized_columns, normalized_phases, normalized_stats)
        raw_density = str(raw.get("density") or "").lower().strip()
        if raw_density in {"low", "medium", "high"}:
            density = raw_density

        base_notes = _clean_inline(str(notes or "")).strip()
        # Dedupe overflow note lines (e.g. identical "Full title"/"Full headline"
        # entries when headline and title overflow to the same text) while
        # preserving order.
        seen_notes: set[str] = set()
        deduped_overflow: list[str] = []
        for n in overflow_notes:
            if n not in seen_notes:
                seen_notes.add(n)
                deduped_overflow.append(n)
        speaker_notes = "\n".join([n for n in [base_notes, *deduped_overflow] if n])

        normalized["slides"].append({
            "layout": layout,
            "archetype": archetype,
            "density": density,
            "visual_object": visual_object,
            "title": title,
            "bullets": bullets,
            "table": table_rows,
            "columns": normalized_columns,
            "phases": normalized_phases,
            "chart": normalized_chart,
            "stats": normalized_stats,
            "callout": normalized_callout,
            "speaker_notes": speaker_notes,
        })
    return normalized if normalized["slides"] else None


def repair_deck_plan_for_qa(plan: dict, issues: list[dict]) -> tuple[dict, bool]:
    """Apply small, deterministic edits to `plan` aimed at resolving render-QA
    issues (currently: `dense_text` / `dense_ink` — visually crowded slides).

    Render-QA slide numbers are 1-based across the *rendered* deck, where
    slide 1 is always the title slide (not present in `plan["slides"]`).
    So `plan["slides"][i]` corresponds to rendered slide `i + 2`.

    Returns `(plan, changed)` where `changed` is False once no further
    deterministic repair could be made (callers should stop looping).
    """
    plan = copy.deepcopy(plan)
    slides = plan.get("slides") or []
    changed = False

    crowded_slide_nums = {
        issue["slide"]
        for issue in issues
        if issue.get("type") in {"dense_text", "dense_ink"} and isinstance(issue.get("slide"), int)
    }

    for slide_num in crowded_slide_nums:
        idx = slide_num - 2
        if idx < 0 or idx >= len(slides):
            continue
        slide = slides[idx]

        # 1) Drop the last bullet, if there's more than one — cheapest way to
        #    reduce density without losing the slide's core message. The
        #    dropped bullet's full text is preserved in speaker_notes rather
        #    than lost (copy/notes separation).
        bullets = slide.get("bullets") or []
        if len(bullets) > 1:
            dropped = bullets[-1]
            slide["bullets"] = bullets[:-1]
            _append_speaker_note(slide, f"Trimmed for slide density: {dropped}")
            changed = True
            _recompute_slide_blueprint(slide)
            continue

        # 2) Drop the last row of a table (keep the header row).
        table = slide.get("table") or []
        if len(table) > 2:
            slide["table"] = table[:-1]
            changed = True
            _recompute_slide_blueprint(slide)
            continue

        # 3) Drop the last phase of a timeline.
        phases = slide.get("phases") or []
        if len(phases) > 2:
            dropped_phase = phases[-1]
            slide["phases"] = phases[:-1]
            phase_summary = dropped_phase.get("title") or dropped_phase.get("label") or ""
            if phase_summary:
                _append_speaker_note(slide, f"Trimmed phase for slide density: {phase_summary}")
            changed = True
            _recompute_slide_blueprint(slide)
            continue

        # 4) Trim a bullet from whichever column currently has the most.
        columns = slide.get("columns") or []
        if columns:
            longest = max(columns, key=lambda c: len(c.get("bullets") or []))
            if len(longest.get("bullets") or []) > 1:
                dropped_col_bullet = longest["bullets"][-1]
                longest["bullets"] = longest["bullets"][:-1]
                _append_speaker_note(
                    slide, f"Trimmed for slide density ({longest.get('heading') or 'column'}): {dropped_col_bullet}"
                )
                changed = True
                _recompute_slide_blueprint(slide)
                continue

        # 5) Last resort: shorten the single remaining bullet/headline text.
        if bullets and len(bullets[0]) > 60:
            shortened, overflow = _shorten_to_notes(bullets[0], 60)
            slide["bullets"] = [shortened]
            if overflow:
                _append_speaker_note(slide, f"Full point: {overflow}")
            changed = True
            _recompute_slide_blueprint(slide)
            continue

    return plan, changed


def _append_speaker_note(slide: dict, note: str) -> None:
    """Append a note line to speaker_notes, skipping exact-duplicate lines.
    Repair passes can run more than once over the same slide (e.g. repeated
    "Trimmed for slide density" or "Full point" entries), so dedupe on the
    exact line to keep notes from growing unboundedly."""
    existing = slide.get("speaker_notes") or ""
    existing_lines = set(existing.splitlines())
    if note in existing_lines:
        return
    slide["speaker_notes"] = "\n".join([p for p in [existing, note] if p])


def _recompute_slide_blueprint(slide: dict) -> None:
    """Refresh the SlideBlueprint (density/visual_object) after a repair-loop
    edit changes a slide's content shape."""
    slide["visual_object"] = _slide_visual_object(
        slide.get("table") or [], slide.get("columns") or [], slide.get("phases") or [], slide.get("chart"), slide.get("stats") or []
    )
    slide["density"] = _slide_density(
        len(slide.get("bullets") or []), slide.get("table") or [], slide.get("columns") or [], slide.get("phases") or [], slide.get("stats") or []
    )


def deck_plan_to_markdown(content: str) -> str | None:
    plan = parse_deck_plan(content)
    if not plan:
        return None
    lines = [f"# {plan['title']}"]
    if plan.get("subtitle"):
        lines.append(f"*{plan['subtitle']}*")
    for slide in plan["slides"]:
        layout = slide.get("layout")
        if layout in {"section", "section_divider"}:
            lines.extend(["", f"# {slide['title']}"])
            continue
        if layout == "appendix":
            lines.extend(["", f"# Appendix: {slide['title']}"])
        else:
            lines.extend(["", f"## {slide['title']}"])
        bullets = slide.get("bullets") or []
        if layout == "executive_summary" and bullets:
            lines.append(f"**{bullets[0]}**")
            for bullet in bullets[1:]:
                lines.append(f"- {bullet}")
        elif layout == "recommendation" and bullets:
            lines.append(f"**Recommendation: {bullets[0]}**")
            for bullet in bullets[1:]:
                lines.append(f"- {bullet}")
        else:
            for bullet in bullets:
                lines.append(f"- {bullet}")
        for phase in slide.get("phases") or []:
            label = f"{phase['label']}: " if phase.get("label") else ""
            title = phase.get("title") or ""
            desc = f" — {phase['description']}" if phase.get("description") else ""
            lines.append(f"- {label}{title}{desc}")
        for col in slide.get("columns") or []:
            if col.get("heading"):
                lines.append(f"### {col['heading']}")
            for bullet in col.get("bullets") or []:
                lines.append(f"- {bullet}")
        for stat in slide.get("stats") or []:
            source = f" ({stat['source']})" if stat.get("source") else ""
            lines.append(f"- **{stat.get('value', '')}** — {stat.get('label', '')}{source}")
        callout = slide.get("callout")
        if callout:
            lines.append(f"> **{callout.get('label') or 'Key Insight'}:** {callout.get('text', '')}")
        if layout == "architecture" and not slide.get("columns"):
            lines.append("_(architecture diagram placeholder)_")
        table = slide.get("table") or []
        if table:
            width = max(len(r) for r in table)
            rows = [r + [""] * (width - len(r)) for r in table]
            lines.append("| " + " | ".join(rows[0]) + " |")
            lines.append("| " + " | ".join(["---"] * width) + " |")
            for row in rows[1:]:
                lines.append("| " + " | ".join(row) + " |")
        if slide.get("speaker_notes"):
            lines.append(f"Speaker notes: {slide['speaker_notes']}")
    return "\n".join(lines).strip()


def _parse_pptx_slides(content: str) -> tuple[dict | None, list[dict]]:
    """Parse markdown-ish content into (title_slide, [section/content/table slides]).

    title_slide is {"title": str, "subtitle": str | None} or None if the
    content doesn't open with an H1.
    """
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    idx = 0
    n = len(lines)

    def _is_blank(i: int) -> bool:
        return i < n and not lines[i].strip()

    while idx < n and not lines[idx].strip():
        idx += 1

    title_slide: dict | None = None
    if idx < n:
        m = re.match(r"^#\s+(.+)$", lines[idx].strip())
        if m:
            title_slide = {"title": _clean_inline(m.group(1)), "subtitle": None}
            idx += 1
            j = idx
            while j < n and not lines[j].strip():
                j += 1
            if j < n:
                candidate = lines[j].strip()
                italic_m = re.match(r"^(\*|_)(.+)\1$", candidate)
                if italic_m and not re.match(r"^#{1,6}\s", candidate):
                    title_slide["subtitle"] = _clean_inline(italic_m.group(2))
                    idx = j + 1

    slides: list[dict] = []
    current: dict | None = None

    def _start_content(title: str) -> dict:
        slide = {"kind": "content", "title": _clean_inline(title), "bullets": [], "notes": None}
        slides.append(slide)
        return slide

    while idx < n:
        raw = lines[idx]
        line = raw.rstrip()
        stripped = line.strip()

        if not stripped:
            idx += 1
            continue

        h1 = re.match(r"^#\s+(.+)$", stripped)
        if h1:
            current = {"kind": "section", "title": _clean_inline(h1.group(1)), "notes": None}
            slides.append(current)
            idx += 1
            continue

        h2 = re.match(r"^##\s+(.+)$", stripped)
        if h2:
            current = _start_content(h2.group(1))
            idx += 1
            continue

        h3 = re.match(r"^###\s+(.+)$", stripped)
        if h3:
            if current is None or current.get("kind") != "content":
                current = _start_content(h3.group(1))
            else:
                current["bullets"].append((0, f"**{_clean_inline(h3.group(1))}**"))
            idx += 1
            continue

        # Speaker notes — may be wrapped in italics or a blockquote.
        notes_candidate = stripped
        bq = re.match(r"^>\s*(.+)$", notes_candidate)
        if bq:
            notes_candidate = bq.group(1).strip()
        italic_wrap = re.match(r"^(\*|_)(.+)\1$", notes_candidate)
        if italic_wrap:
            notes_candidate = italic_wrap.group(2).strip()
        notes_m = SPEAKER_NOTES_RE.match(notes_candidate)
        if notes_m and current is not None:
            current["notes"] = ((current.get("notes") or "") + " " + notes_m.group(1)).strip()
            idx += 1
            continue

        if _is_table_start(lines, idx):
            rows = [_split_table_row(lines[idx])]
            idx += 2
            while idx < len(lines) and lines[idx].strip().startswith("|"):
                rows.append(_split_table_row(lines[idx]))
                idx += 1
            table_title = current["title"] if current else "Table"
            slides.append({"kind": "table", "title": table_title, "rows": rows, "notes": None})
            continue

        if stripped in {"---", "***", "___"}:
            idx += 1
            continue

        if current is None or current.get("kind") != "content":
            current = _start_content("")

        bullet_m = re.match(r"^(\s*)[-*+]\s+(.+)$", line)
        number_m = re.match(r"^(\s*)\d+[.)]\s+(.+)$", line)
        m = bullet_m or number_m
        if m:
            level = min(len(m.group(1)) // 2, 4)
            current["bullets"].append((level, _clean_inline(m.group(2))))
            idx += 1
            continue

        # Plain paragraph text becomes a top-level bullet.
        current["bullets"].append((0, _clean_inline(stripped)))
        idx += 1

    return title_slide, slides


def _split_dense_slides(slides: list[dict]) -> list[dict]:
    """Split content slides with too many bullets into "(cont.)" slides so a
    rendered deck doesn't end up with dense, unreadable text walls."""
    result: list[dict] = []
    for slide in slides:
        if slide.get("kind") != "content" or len(slide.get("bullets", [])) <= MAX_BULLETS_PER_SLIDE:
            result.append(slide)
            continue
        bullets = slide["bullets"]
        for i in range(0, len(bullets), MAX_BULLETS_PER_SLIDE):
            chunk = bullets[i:i + MAX_BULLETS_PER_SLIDE]
            title = slide["title"] if i == 0 else f"{slide['title']} (cont.)"
            result.append({
                "kind": "content",
                "title": title,
                "bullets": chunk,
                "notes": slide.get("notes") if i == 0 else None,
            })
    return result


def _pptx_title_font_size(text: str) -> int:
    """Scale the title font down for longer titles so it wraps to at most
    ~2 lines within the title placeholder, instead of overflowing into the
    slide body (the cause of title/body overlap on long, LLM-generated
    titles). Thresholds mirror render.js's titleFontSize() so titles render
    consistently across the PptxGenJS and python-pptx renderers."""
    length = len(text or "")
    if length <= 42:
        return 28
    if length <= 64:
        return 24
    return 20


def _pptx_set_title(slide, text: str) -> None:
    title_shape = slide.shapes.title
    if title_shape is not None:
        title_shape.text = ""
        tf = title_shape.text_frame
        tf.word_wrap = True
        try:
            tf.vertical_anchor = MSO_ANCHOR.TOP
        except Exception:
            pass
        p = tf.paragraphs[0]
        _pptx_add_runs(p, text or "Untitled")
        font_size = _pptx_title_font_size(text or "Untitled")
        theme = _pptx_theme()
        for run in p.runs:
            run.font.size = PptxPt(font_size)
            # Now that every slide gets the theme background painted
            # (_pptx_add_slide), the template layout's default placeholder
            # font color may no longer have contrast (e.g. dark text on a
            # dark theme background). Force the theme's foreground color so
            # titles stay legible across all themes.
            run.font.color.rgb = theme["fg"]
        return

    # Some branded templates define slide layouts with no placeholders at
    # all (every slide in the source deck was hand-laid-out free shapes).
    # `slide.shapes.title` is then always None and titles were silently
    # dropped. Fall back to a styled textbox + accent rule so every slide
    # still gets a title.
    _pptx_add_title_textbox(slide, text)


def _pptx_add_title_textbox(slide, text: str) -> None:
    theme = _pptx_theme()
    _pptx_set_slide_background(slide)
    box = slide.shapes.add_textbox(
        PptxInches(0.65), PptxInches(0.42), PptxInches(11.0), PptxInches(PPTX_TITLE_BOX_H)
    )
    tf = box.text_frame
    tf.word_wrap = True
    try:
        tf.vertical_anchor = MSO_ANCHOR.TOP
    except Exception:
        pass
    p = tf.paragraphs[0]
    _pptx_add_runs(p, text or "Untitled")
    font_size = _pptx_title_font_size(text or "Untitled")
    for run in p.runs:
        run.font.size = PptxPt(font_size)
        run.font.bold = True
        run.font.name = theme["heading_font"]
        run.font.color.rgb = theme["fg"]

    rule = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, PptxInches(0.65), PptxInches(PPTX_TITLE_RULE_Y), PptxInches(1.0), PptxInches(0.04)
    )
    rule.fill.solid()
    rule.fill.fore_color.rgb = theme["accent"]
    rule.line.fill.background()


def _pptx_render_table(slide, rows: list[list[str]]) -> None:
    if not rows:
        return
    n_rows = len(rows)
    n_cols = max(len(r) for r in rows)
    left, top = PptxInches(0.5), PptxInches(1.7)
    width, height = PptxInches(9.0), PptxInches(0.5 + 0.4 * n_rows)
    table_shape = slide.shapes.add_table(n_rows, n_cols, left, top, width, height)
    table = table_shape.table
    for r_idx, row in enumerate(rows):
        for c_idx in range(n_cols):
            cell = table.cell(r_idx, c_idx)
            cell.text = _clean_inline(row[c_idx]) if c_idx < len(row) else ""
            for p in cell.text_frame.paragraphs:
                for run in p.runs:
                    run.font.size = PptxPt(12)
                    if r_idx == 0:
                        run.font.bold = True


def _pptx_set_bullet_marker(paragraph, color: RGBColor, char: str = "▪") -> None:
    """Give a paragraph a small colored square bullet (matching the
    ACCENT_BULLET marker used by the pptxgenjs renderer), via raw OOXML
    <a:buClr>/<a:buFont>/<a:buChar> elements. python-pptx has no native API
    for bullet color, so we manipulate the paragraph's <a:pPr> directly."""
    pPr = paragraph._p.get_or_add_pPr()
    for tag in ("a:buNone", "a:buAutoNum", "a:buChar", "a:buFont", "a:buClr"):
        for el in pPr.findall(pptx_qn(tag)):
            pPr.remove(el)
    buClr = pPr.makeelement(pptx_qn("a:buClr"), {})
    srgb = pPr.makeelement(pptx_qn("a:srgbClr"), {"val": str(color)})
    buClr.append(srgb)
    buFont = pPr.makeelement(pptx_qn("a:buFont"), {"typeface": "Arial", "pitchFamily": "34", "charset": "0"})
    buChar = pPr.makeelement(pptx_qn("a:buChar"), {"char": char})
    pPr.append(buClr)
    pPr.append(buFont)
    pPr.append(buChar)


def _pptx_add_text_box(slide, left, top, width, height, heading: str, bullets: list[str]) -> None:
    theme = _pptx_theme()
    box = slide.shapes.add_textbox(left, top, width, height)
    tf = box.text_frame
    tf.word_wrap = True
    tf.clear()
    if heading:
        p = tf.paragraphs[0]
        _pptx_add_runs(p, heading)
        for run in p.runs:
            run.font.bold = True
            run.font.size = PptxPt(15)
            run.font.name = theme["heading_font"]
            run.font.color.rgb = theme["fg"]
    for idx, bullet in enumerate(bullets):
        p = tf.paragraphs[0] if idx == 0 and not heading else tf.add_paragraph()
        p.level = 0
        _pptx_add_runs(p, bullet)
        if bullet:
            _pptx_set_bullet_marker(p, theme["accent"])
        for run in p.runs:
            run.font.size = PptxPt(13)
            run.font.name = theme["body_font"]
            run.font.color.rgb = theme["fg"]


def _pptx_render_executive_summary(slide, bullets: list[str]) -> None:
    """Big 'so what' statement up top, supporting bullets below."""
    theme = _pptx_theme()
    headline, support = (bullets[0], bullets[1:]) if bullets else ("", [])
    if headline:
        box = slide.shapes.add_textbox(PptxInches(0.65), PptxInches(1.5), PptxInches(11.0), PptxInches(1.7))
        tf = box.text_frame
        tf.word_wrap = True
        tf.clear()
        _pptx_add_runs(tf.paragraphs[0], headline)
        for run in tf.paragraphs[0].runs:
            run.font.size = PptxPt(28)
            run.font.bold = True
            run.font.name = theme["heading_font"]
            run.font.color.rgb = theme["fg"]
    if support:
        _pptx_add_text_box(
            slide, PptxInches(0.65), PptxInches(3.3), PptxInches(11.0), PptxInches(3.2),
            "", support[:MAX_BULLETS_PER_SLIDE - 1],
        )


def _pptx_render_recommendation(slide, bullets: list[str]) -> None:
    """Accent card around the recommendation line, remaining bullets as rationale."""
    theme = _pptx_theme()
    primary, rationale = (bullets[0], bullets[1:]) if bullets else ("", [])
    if primary:
        box = slide.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE, PptxInches(0.65), PptxInches(1.5), PptxInches(11.0), PptxInches(1.3)
        )
        box.fill.solid()
        box.fill.fore_color.rgb = theme["accent2"]
        box.line.fill.background()
        tf = box.text_frame
        tf.word_wrap = True
        tf.clear()
        _pptx_add_runs(tf.paragraphs[0], f"Recommendation: {primary}")
        for run in tf.paragraphs[0].runs:
            run.font.size = PptxPt(18)
            run.font.bold = True
            run.font.name = theme["heading_font"]
            run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        tf.paragraphs[0].alignment = PP_ALIGN.LEFT
    if rationale:
        _pptx_add_text_box(
            slide, PptxInches(0.65), PptxInches(3.1), PptxInches(11.0), PptxInches(3.4),
            "Rationale", rationale[:MAX_BULLETS_PER_SLIDE],
        )


def _pptx_render_stat_cards(slide, stats: list[dict], callout: dict | None) -> None:
    """Row of up to 4 metric cards (value + label + optional source), plus an
    optional accent-colored 'Key Insight' callout box beneath them."""
    theme = _pptx_theme()
    stats = [s for s in (stats or []) if isinstance(s, dict) and (s.get("value") or s.get("label"))][:4]
    if not stats:
        return

    top = PptxInches(1.5)
    card_height = PptxInches(2.0)
    gutter = 0.25
    total_width = 11.0
    card_width = (total_width - gutter * (len(stats) - 1)) / len(stats)

    for i, stat in enumerate(stats):
        left = PptxInches(0.65 + i * (card_width + gutter))
        card = slide.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE, left, top, PptxInches(card_width), card_height
        )
        card.fill.solid()
        card.fill.fore_color.rgb = theme["card"]
        card.line.color.rgb = theme["card_line"]
        card.line.width = PptxPt(1)
        tf = card.text_frame
        tf.word_wrap = True
        tf.clear()
        tf.margin_left = PptxInches(0.1)
        tf.margin_right = PptxInches(0.1)

        value_p = tf.paragraphs[0]
        _pptx_add_runs(value_p, stat.get("value") or "")
        for run in value_p.runs:
            run.font.size = PptxPt(28)
            run.font.bold = True
            run.font.name = theme["heading_font"]
            run.font.color.rgb = theme["accent"]
        value_p.alignment = PP_ALIGN.CENTER

        if stat.get("label"):
            label_p = tf.add_paragraph()
            _pptx_add_runs(label_p, stat["label"])
            for run in label_p.runs:
                run.font.size = PptxPt(13)
                run.font.name = theme["body_font"]
                run.font.color.rgb = theme["fg"]
            label_p.alignment = PP_ALIGN.CENTER

        if stat.get("source"):
            source_p = tf.add_paragraph()
            _pptx_add_runs(source_p, stat["source"])
            for run in source_p.runs:
                run.font.size = PptxPt(9)
                run.font.italic = True
                run.font.name = theme["body_font"]
                run.font.color.rgb = theme["muted"]
            source_p.alignment = PP_ALIGN.CENTER

    if callout and (callout.get("text") or "").strip():
        box = slide.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE, PptxInches(0.65), PptxInches(3.85), PptxInches(11.0), PptxInches(1.6)
        )
        box.fill.solid()
        box.fill.fore_color.rgb = theme["accent2"]
        box.line.fill.background()
        tf = box.text_frame
        tf.word_wrap = True
        tf.clear()
        tf.margin_left = PptxInches(0.2)
        tf.margin_right = PptxInches(0.2)
        label_p = tf.paragraphs[0]
        _pptx_add_runs(label_p, callout.get("label") or "Key Insight")
        for run in label_p.runs:
            run.font.size = PptxPt(14)
            run.font.bold = True
            run.font.name = theme["heading_font"]
            run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        text_p = tf.add_paragraph()
        _pptx_add_runs(text_p, callout.get("text") or "")
        for run in text_p.runs:
            run.font.size = PptxPt(14)
            run.font.name = theme["body_font"]
            run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)


def _pptx_render_timeline(slide, phases: list[dict]) -> None:
    """Horizontal timeline of phase markers, each with a label/title/description."""
    phases = [p for p in phases if isinstance(p, dict) and (p.get("title") or p.get("label") or p.get("description"))][:6]
    if not phases:
        return
    theme = _pptx_theme()
    n = len(phases)
    total_w = 12.0
    gap = 0.25
    box_w = (total_w - gap * (n - 1)) / n
    top = 2.0
    for idx, ph in enumerate(phases):
        left = 0.65 + idx * (box_w + gap)
        if idx > 0:
            connector = slide.shapes.add_shape(
                MSO_SHAPE.RECTANGLE, PptxInches(left - gap), PptxInches(top + 0.4),
                PptxInches(gap), PptxInches(0.04),
            )
            connector.fill.solid()
            connector.fill.fore_color.rgb = theme["card_line"]
            connector.line.fill.background()
        marker = slide.shapes.add_shape(
            MSO_SHAPE.OVAL, PptxInches(left + box_w / 2 - 0.15), PptxInches(top + 0.25), PptxInches(0.3), PptxInches(0.3)
        )
        marker.fill.solid()
        marker.fill.fore_color.rgb = theme["accent"]
        marker.line.fill.background()
        lines = []
        if ph.get("label"):
            lines.append(ph["label"])
        if ph.get("title"):
            lines.append(ph["title"])
        if ph.get("description"):
            lines.append(ph["description"])
        _pptx_add_text_box(slide, PptxInches(left), PptxInches(top + 0.7), PptxInches(box_w), PptxInches(3.8), "", lines)


_PPTX_CHART_TYPE_MAP = {
    "bar": XL_CHART_TYPE.COLUMN_CLUSTERED,
    "line": XL_CHART_TYPE.LINE_MARKERS,
    "pie": XL_CHART_TYPE.PIE,
}


def _pptx_chart_palette(theme: dict) -> list[RGBColor]:
    """Derive a small multi-series chart palette from the active theme's
    accent colors, mirroring the [ACCENT, NAVY, "8C6F5D", "C9A14A"] palette
    used by the pptxgenjs renderer so charts read consistently across both
    rendering paths and across templates."""
    accent = theme["accent"]
    accent2 = theme["accent2"]

    def _mix(c1: RGBColor, c2: RGBColor, t: float) -> RGBColor:
        return RGBColor(
            int(c1[0] + (c2[0] - c1[0]) * t),
            int(c1[1] + (c2[1] - c1[1]) * t),
            int(c1[2] + (c2[2] - c1[2]) * t),
        )

    muted = theme["muted"]
    return [accent, accent2, muted, _mix(accent, accent2, 0.5)]


def _pptx_render_chart(slide, chart_spec: dict) -> None:
    """Render a native python-pptx chart from a normalized chart spec
    ({"type": "bar|line|pie", "categories": [...], "series": [{"name", "values"}]}),
    styled with the active template's theme colors and with data labels
    enabled so values are legible without a separate table."""
    theme = _pptx_theme()
    chart_data = CategoryChartData()
    chart_data.categories = chart_spec.get("categories") or []
    series = chart_spec.get("series") or []
    chart_type = chart_spec.get("type") or "bar"
    if chart_type == "pie":
        series = series[:1]
    for s in series:
        chart_data.add_series(s.get("name") or "Series", s.get("values") or [])
    xl_chart_type = _PPTX_CHART_TYPE_MAP.get(chart_type, XL_CHART_TYPE.COLUMN_CLUSTERED)
    graphic_frame = slide.shapes.add_chart(
        xl_chart_type, PptxInches(1.0), PptxInches(1.6), PptxInches(11.3), PptxInches(5.3), chart_data
    )
    chart = graphic_frame.chart
    chart.has_title = False
    palette = _pptx_chart_palette(theme)

    chart.has_legend = len(series) > 1 or chart_type == "pie"
    if chart.has_legend:
        chart.legend.position = 2  # XL_LEGEND_POSITION.BOTTOM
        chart.legend.include_in_layout = False
        chart.legend.font.size = PptxPt(11)
        chart.legend.font.color.rgb = theme["fg"]

    if chart_type == "pie":
        point_count = len(chart_data.categories)
        for idx, point in enumerate(chart.plots[0].series[0].points):
            point.format.fill.solid()
            point.format.fill.fore_color.rgb = palette[idx % len(palette)]
        plot = chart.plots[0]
        plot.has_data_labels = True
        data_labels = plot.data_labels
        data_labels.show_percentage = True
        data_labels.show_value = False
        data_labels.number_format = "0%"
        data_labels.number_format_is_linked = False
        data_labels.font.size = PptxPt(11)
        data_labels.font.color.rgb = theme["fg"]
    else:
        for idx, plot_series in enumerate(chart.plots[0].series):
            color = palette[idx % len(palette)]
            if chart_type == "line":
                plot_series.format.line.color.rgb = color
                plot_series.format.line.width = PptxPt(2.25)
            else:
                plot_series.format.fill.solid()
                plot_series.format.fill.fore_color.rgb = color
        plot = chart.plots[0]
        plot.has_data_labels = True
        data_labels = plot.data_labels
        data_labels.show_value = True
        data_labels.number_format = "0.#"
        data_labels.number_format_is_linked = False
        data_labels.font.size = PptxPt(10)
        data_labels.font.color.rgb = theme["muted"]

    if chart_type != "pie":
        for axis in (chart.category_axis, chart.value_axis):
            axis.tick_labels.font.size = PptxPt(11)
            axis.tick_labels.font.color.rgb = theme["muted"]


def _pptx_render_deck_plan(prs: Presentation, plan: dict, fallback_title: str, subtitle: str | None) -> None:
    title_slide = _pptx_add_slide(prs, "title")
    _pptx_set_title(title_slide, plan.get("title") or fallback_title or "Fronei deck")
    deck_subtitle = plan.get("subtitle") or subtitle
    if deck_subtitle:
        subtitle_placeholder = None
        for shape in title_slide.placeholders:
            if shape.placeholder_format.idx == 1:
                subtitle_placeholder = shape
                break
        if subtitle_placeholder is not None:
            subtitle_placeholder.text_frame.text = ""
            _pptx_add_runs(subtitle_placeholder.text_frame.paragraphs[0], deck_subtitle)
            theme = _pptx_theme()
            for run in subtitle_placeholder.text_frame.paragraphs[0].runs:
                run.font.color.rgb = theme["muted"]
        else:
            # No subtitle placeholder on this template's title layout — drop a
            # styled textbox below the (also-fallback) title textbox.
            box = title_slide.shapes.add_textbox(
                PptxInches(0.65), PptxInches(PPTX_CONTENT_TOP_Y), PptxInches(11.0), PptxInches(0.8)
            )
            tf = box.text_frame
            tf.word_wrap = True
            _pptx_add_runs(tf.paragraphs[0], deck_subtitle)
            theme = _pptx_theme()
            for run in tf.paragraphs[0].runs:
                run.font.size = PptxPt(16)
                run.font.name = theme["body_font"]
                run.font.color.rgb = theme["muted"]

    for spec in plan.get("slides", []):
        layout = spec.get("layout") or "bullets"
        title = spec.get("title") or "Untitled"
        notes = spec.get("speaker_notes")
        bullets = spec.get("bullets") or []
        if layout in {"section", "section_divider"}:
            slide = _pptx_add_slide(prs, "section")
            _pptx_set_title(slide, title)
        elif spec.get("chart"):
            slide = _pptx_add_slide(prs, "title_only")
            _pptx_set_title(slide, title)
            _pptx_render_chart(slide, spec["chart"])
        elif spec.get("table"):
            slide = _pptx_add_slide(prs, "title_only")
            _pptx_set_title(slide, title)
            _pptx_render_table(slide, spec["table"])
        elif layout in {"two_column", "comparison", "architecture"} and spec.get("columns"):
            slide = _pptx_add_slide(prs, "two_content")
            _pptx_set_title(slide, title)
            cols = spec["columns"][:3]
            n = max(len(cols), 1)
            total_w = 12.0
            gap = 0.35
            col_w_in = (total_w - gap * (n - 1)) / n
            col_w = PptxInches(col_w_in)
            top = PptxInches(PPTX_CONTENT_TOP_Y)
            height = PptxInches(4.9)
            theme = _pptx_theme()
            for idx, col in enumerate(cols):
                left = PptxInches(0.65 + idx * (col_w_in + gap))
                card = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, left, top, col_w, height)
                card.fill.solid()
                card.fill.fore_color.rgb = theme["card"]
                card.line.color.rgb = theme["card_line"]
                card.shadow.inherit = False
                inset = PptxInches(0.18)
                _pptx_add_text_box(
                    slide, left + inset, top + inset, col_w - inset * 2, height - inset * 2,
                    col.get("heading") or "", col.get("bullets") or [],
                )
        elif layout == "executive_summary":
            slide = _pptx_add_slide(prs, "title_only")
            _pptx_set_title(slide, title)
            _pptx_render_executive_summary(slide, bullets)
        elif layout == "recommendation":
            slide = _pptx_add_slide(prs, "title_only")
            _pptx_set_title(slide, title)
            _pptx_render_recommendation(slide, bullets)
        elif layout == "stat_cards":
            slide = _pptx_add_slide(prs, "title_only")
            _pptx_set_title(slide, title)
            _pptx_render_stat_cards(slide, spec.get("stats") or [], spec.get("callout"))
        elif layout == "timeline":
            slide = _pptx_add_slide(prs, "title_only")
            _pptx_set_title(slide, title)
            phases = spec.get("phases") or [
                {"label": "", "title": b, "description": ""} for b in bullets
            ]
            _pptx_render_timeline(slide, phases)
        elif layout == "architecture":
            slide = _pptx_add_slide(prs, "title_only")
            _pptx_set_title(slide, title)
            _pptx_add_text_box(
                slide, PptxInches(0.65), PptxInches(1.55), PptxInches(5.6), PptxInches(4.9),
                "Architecture diagram", ["(diagram placeholder — describe components and data flow)"],
            )
            _pptx_add_text_box(
                slide, PptxInches(6.5), PptxInches(1.55), PptxInches(5.8), PptxInches(4.9),
                "", bullets[:MAX_BULLETS_PER_SLIDE] or [""],
            )
        else:
            slide = _pptx_add_slide(prs, "content")
            _pptx_set_title(slide, title)
            cap = MAX_APPENDIX_BULLETS if layout == "appendix" else MAX_BULLETS_PER_SLIDE
            body = slide.placeholders[1] if len(slide.placeholders) > 1 else None
            if body is not None:
                theme = _pptx_theme()
                tf = body.text_frame
                tf.clear()
                for i, bullet in enumerate(bullets[:cap] or [""]):
                    p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                    p.level = 0
                    _pptx_add_runs(p, bullet)
                    for run in p.runs:
                        run.font.color.rgb = theme["fg"]
            else:
                # Template layout has no body placeholder either — fall back
                # to a textbox positioned below the (also-fallback) title.
                _pptx_add_text_box(
                    slide, PptxInches(0.65), PptxInches(PPTX_CONTENT_TOP_Y), PptxInches(11.0), PptxInches(4.9),
                    "", bullets[:cap] or [""],
                )
        if notes:
            slide.notes_slide.notes_text_frame.text = notes


def _js_pptx_renderer_available() -> bool:
    if not PPTX_RENDER_JS.exists() or shutil.which("node") is None:
        return False
    # Guard against a missing `npm install` under apps/api/pptx_render: without
    # this check, every default deck would attempt JS rendering, fail, log an
    # exception, and silently fall back to python-pptx.
    pptxgenjs_dir = PPTX_RENDER_DIR / "node_modules" / "pptxgenjs"
    return pptxgenjs_dir.is_dir()


def _js_slide_from_deck_spec(spec: dict) -> dict:
    """Convert one normalized DeckPlan slide (from parse_deck_plan) into the
    role-based shape consumed by pptx_render/render.js."""
    layout = spec.get("layout") or "bullets"
    title = spec.get("title") or "Untitled"
    notes = spec.get("speaker_notes") or None
    bullets = spec.get("bullets") or []

    def with_blueprint(payload: dict) -> dict:
        payload["blueprint"] = _js_slide_blueprint_from_spec(spec, payload.get("role") or "content")
        return payload

    if layout in {"section", "section_divider"}:
        return with_blueprint({"role": "section", "title": title, "notes": notes})
    if spec.get("chart"):
        return with_blueprint({"role": "chart", "title": title, "chart": spec["chart"], "notes": notes})
    if spec.get("table"):
        return with_blueprint({"role": "table", "title": title, "rows": spec["table"], "notes": notes})
    if layout in {"two_column", "comparison", "architecture"} and spec.get("columns"):
        return with_blueprint({"role": "two_content", "title": title, "columns": spec["columns"][:3], "notes": notes})
    if layout == "executive_summary":
        return with_blueprint({"role": "executive_summary", "title": title, "bullets": bullets, "notes": notes})
    if layout == "recommendation":
        return with_blueprint({"role": "recommendation", "title": title, "bullets": bullets, "notes": notes})
    if layout == "stat_cards":
        return with_blueprint({
            "role": "stat_cards",
            "title": title,
            "stats": spec.get("stats") or [],
            "callout": spec.get("callout"),
            "notes": notes,
        })
    if layout == "timeline":
        phases = spec.get("phases") or [{"label": "", "title": b, "description": ""} for b in bullets]
        return with_blueprint({"role": "timeline", "title": title, "phases": phases, "notes": notes})
    if layout == "architecture":
        return with_blueprint({"role": "architecture", "title": title, "bullets": bullets[:MAX_BULLETS_PER_SLIDE], "notes": notes})

    cap = MAX_APPENDIX_BULLETS if layout == "appendix" else MAX_BULLETS_PER_SLIDE
    return with_blueprint({
        "role": "content",
        "title": title,
        "appendix": layout == "appendix",
        "bullets": [{"level": 0, "text": b} for b in (bullets[:cap] or [""])],
        "notes": notes,
    })


def _infer_slide_emphasis(spec: dict, role: str) -> str:
    stats_text: list[str] = []
    for stat in spec.get("stats") or []:
        if isinstance(stat, dict):
            stats_text.extend(str(stat.get(key) or "") for key in ("value", "label", "source"))
    callout = spec.get("callout") if isinstance(spec.get("callout"), dict) else {}
    text_parts = [
        str(spec.get("layout") or ""),
        str(spec.get("archetype") or ""),
        str(spec.get("title") or ""),
        " ".join(str(b) for b in (spec.get("bullets") or [])),
        " ".join(str(c.get("heading") or "") for c in (spec.get("columns") or []) if isinstance(c, dict)),
        " ".join(stats_text),
        str(callout.get("label") or ""),
        str(callout.get("text") or ""),
    ]
    text = " ".join(text_parts).lower()
    if role == "recommendation" or any(token in text for token in ("recommend", "approve", "decision", "authorize")):
        return "decision"
    if any(token in text for token in ("risk", "security", "compliance", "privacy", "legal", "control", "governance")):
        return "risk"
    if any(token in text for token in ("cost", "revenue", "roi", "budget", "margin", "savings", "tco", "$")):
        return "financial"
    if any(token in text for token in ("architecture", "api", "platform", "system", "data", "model", "integration", "cloud")):
        return "technical"
    if any(token in text for token in ("timeline", "phase", "roadmap", "migration", "launch", "delivery")):
        return "execution"
    return "operational"


def _proof_object_for_spec(spec: dict, role: str) -> str:
    visual_object = str(spec.get("visual_object") or "").strip()
    if visual_object and visual_object != "bullets":
        return visual_object
    if role in {"chart", "table", "timeline", "architecture", "stat_cards"}:
        return role
    if spec.get("columns"):
        return "comparison"
    return "insight_cards"


def _js_slide_blueprint_from_spec(spec: dict, role: str) -> dict:
    """Designer-facing intent passed to the JS compositor.

    The role remains the backwards-compatible renderer route. The blueprint is
    the richer semantic layer: what kind of slide this is, how dense it is, what
    proof object should carry the argument, and which emphasis color family the
    compositor should use.
    """
    layout = str(spec.get("layout") or role or "content").strip()
    archetype = str(spec.get("archetype") or layout or role or "content").strip()
    density = str(spec.get("density") or "medium").strip().lower()
    if density not in {"low", "medium", "high"}:
        density = "medium"
    return {
        "archetype": archetype,
        "layout": layout,
        "density": density,
        "visual_object": str(spec.get("visual_object") or "bullets"),
        "proof_object": _proof_object_for_spec(spec, role),
        "emphasis": _infer_slide_emphasis(spec, role),
    }


def _js_slide_from_markdown_spec(spec: dict) -> dict:
    """Convert one slide from `_parse_pptx_slides`/`_split_dense_slides` into
    the role-based shape consumed by pptx_render/render.js."""
    kind = spec.get("kind")
    notes = spec.get("notes")
    if kind == "section":
        return {"role": "section", "title": spec.get("title") or "Untitled", "notes": notes}
    if kind == "table":
        return {"role": "table", "title": spec.get("title") or "Table", "rows": spec.get("rows") or [], "notes": notes}

    bullets = spec.get("bullets") or [(0, "")]
    return {
        "role": "content",
        "title": spec.get("title") or "Untitled",
        "bullets": [{"level": level, "text": text} for level, text in bullets],
        "notes": notes,
    }


def _number_section_slides(js_slides: list[dict]) -> list[dict]:
    """Assign a 1-based `section_number` to each "section" role slide so the
    renderer can show a "01 / Section Title" style divider, mirroring the
    numbered section breaks seen in the Claude reference deck."""
    n = 0
    for slide in js_slides:
        if slide.get("role") == "section":
            n += 1
            slide["section_number"] = n
    return js_slides


def _build_js_deck_payload(title: str, content: str, subtitle: str | None) -> dict:
    """Normalize either a structured DeckPlan or markdown-ish slide-plan
    content into the JSON payload consumed by pptx_render/render.js."""
    deck_plan = parse_deck_plan(content)
    if deck_plan:
        return {
            "version": 2,
            "design_system": {
                "name": "fronei_board_briefing",
                "mode": "freehand_compositor",
            },
            "title": deck_plan.get("title") or title or "Fronei deck",
            "subtitle": deck_plan.get("subtitle") or subtitle,
            "slides": _number_section_slides(
                [_js_slide_from_deck_spec(spec) for spec in deck_plan.get("slides", [])]
            ),
        }

    title_slide_spec, slides = _parse_pptx_slides(content)
    slides = [
        s for s in slides
        if not (s.get("kind") == "content" and not s.get("bullets") and not s.get("notes"))
    ]
    slides = _split_dense_slides(slides)

    deck_title = (title_slide_spec or {}).get("title") or _clean_inline(title) or "Fronei deck"
    deck_subtitle = (title_slide_spec or {}).get("subtitle") or subtitle

    js_slides: list[dict] = []
    if not slides:
        lines = _clean_inline(content).split("\n")[:MAX_BULLETS_PER_SLIDE] or [""]
        js_slides.append({
            "role": "content",
            "title": "Overview",
            "bullets": [{"level": 0, "text": line} for line in lines],
            "notes": None,
        })
    for spec in slides:
        js_slides.append(_js_slide_from_markdown_spec(spec))

    return {
        "version": 2,
        "design_system": {
            "name": "fronei_board_briefing",
            "mode": "markdown_compositor",
        },
        "title": deck_title,
        "subtitle": deck_subtitle,
        "slides": _number_section_slides(js_slides),
    }


def _render_pptx_via_pptxgenjs(payload: dict) -> bytes:
    result = subprocess.run(
        ["node", str(PPTX_RENDER_JS)],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        timeout=PPTX_RENDER_TIMEOUT_SECONDS,
        check=True,
    )
    return result.stdout


def generate_pptx_bytes(
    title: str,
    content: str,
    subtitle: str | None = None,
    template_id: str | None = None,
    template_path: str | Path | None = None,
) -> bytes:
    """Render markdown-ish slide-plan content (see module docstring above the
    PPTX section), or a structured DeckPlan JSON object, into a PPTX deck.

    Decks with no branded template (`template_id` unset or "fronei-default",
    and no `template_path`) are rendered via PptxGenJS (pptx_render/render.js).
    Decks built from a built-in or user-uploaded `.pptx` template are rendered
    via python-pptx, which can read that template's layouts/placeholders
    directly (see `_pptx_layout_for_role`).
    """
    uses_template = bool(template_path) or (template_id and template_id != "fronei-default")
    if not uses_template and _js_pptx_renderer_available():
        try:
            payload = _build_js_deck_payload(title, content, subtitle)
            return _render_pptx_via_pptxgenjs(payload)
        except Exception:
            logger.exception("PptxGenJS rendering failed; falling back to python-pptx")

    return _generate_pptx_bytes_python_pptx(title, content, subtitle, template_id, template_path)


def _generate_pptx_bytes_python_pptx(
    title: str,
    content: str,
    subtitle: str | None = None,
    template_id: str | None = None,
    template_path: str | Path | None = None,
) -> bytes:
    """python-pptx fallback / template-aware renderer (see generate_pptx_bytes)."""
    prs = _presentation_from_template(template_id, template_path)
    prs.slide_width = PptxInches(13.333)
    prs.slide_height = PptxInches(7.5)

    previous_theme = _pptx_set_theme(template_id)
    try:
        return _render_pptx_body(prs, content, title, subtitle)
    finally:
        _pptx_restore_theme(previous_theme)


def _render_pptx_body(prs: Presentation, content: str, title: str, subtitle: str | None) -> bytes:
    deck_plan = parse_deck_plan(content)
    if deck_plan:
        _pptx_render_deck_plan(prs, deck_plan, title, subtitle)
        output = BytesIO()
        prs.save(output)
        return output.getvalue()

    title_slide_spec, slides = _parse_pptx_slides(content)
    # Drop empty content slides (e.g. an H2 whose only content was a table,
    # which becomes its own slide) — they'd otherwise render as a blank slide.
    slides = [
        s for s in slides
        if not (s.get("kind") == "content" and not s.get("bullets") and not s.get("notes"))
    ]
    slides = _split_dense_slides(slides)

    # Title slide
    deck_title = (title_slide_spec or {}).get("title") or _clean_inline(title) or "Fronei deck"
    deck_subtitle = (title_slide_spec or {}).get("subtitle") or subtitle
    title_layout = _pptx_layout_for_role(prs, "title")
    slide = prs.slides.add_slide(title_layout)
    _pptx_set_title(slide, deck_title)
    if deck_subtitle:
        for shape in slide.placeholders:
            if shape.placeholder_format.idx == 1:
                shape.text_frame.text = ""
                _pptx_add_runs(shape.text_frame.paragraphs[0], deck_subtitle)
                break

    if not slides:
        # No structured body — fall back to a single content slide listing
        # the raw content as bullets so nothing is silently dropped.
        body_layout = _pptx_layout_for_role(prs, "content")
        slide = prs.slides.add_slide(body_layout)
        _pptx_set_title(slide, "Overview")
        body = slide.placeholders[1] if len(slide.placeholders) > 1 else None
        if body is not None:
            tf = body.text_frame
            tf.clear()
            for i, line in enumerate(_clean_inline(content).split("\n")[:MAX_BULLETS_PER_SLIDE] or [""]):
                p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                p.level = 0
                _pptx_add_runs(p, line)

    for spec in slides:
        kind = spec.get("kind")
        if kind == "section":
            layout = _pptx_layout_for_role(prs, "section")
            slide = prs.slides.add_slide(layout)
            _pptx_set_title(slide, spec["title"])
        elif kind == "table":
            layout = _pptx_layout_for_role(prs, "title_only")
            slide = prs.slides.add_slide(layout)
            _pptx_set_title(slide, spec["title"])
            _pptx_render_table(slide, spec["rows"])
        else:  # content
            layout = _pptx_layout_for_role(prs, "content")
            slide = prs.slides.add_slide(layout)
            _pptx_set_title(slide, spec["title"] or "Untitled")
            bullets = spec.get("bullets") or []
            body = slide.placeholders[1] if len(slide.placeholders) > 1 else None
            if body is not None:
                tf = body.text_frame
                tf.clear()
                if not bullets:
                    bullets = [(0, "")]
                for i, (level, text) in enumerate(bullets):
                    p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                    p.level = _pptx_bullet_indent(level)
                    _pptx_add_runs(p, text)

        notes = spec.get("notes")
        if notes:
            slide.notes_slide.notes_text_frame.text = notes

    output = BytesIO()
    prs.save(output)
    return output.getvalue()
