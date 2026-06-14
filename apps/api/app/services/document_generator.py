from __future__ import annotations

import json
import re
from datetime import date
from io import BytesIO
from pathlib import Path

from docx import Document
from docx.enum.text import WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt
from docx.text.paragraph import Paragraph
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from pptx import Presentation
from pptx.util import Inches as PptxInches, Pt as PptxPt

from app.services.document_templates import resolve_pptx_template_path


TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
INLINE_RE = re.compile(
    r"!\[([^\]]*)\]\(([^)]+)\)"
    r"|\[([^\]]+)\]\(([^)]+)\)"
    r"|`([^`]+)`"
    r"|\*\*([^*]+)\*\*"
    r"|__([^_]+)__"
    r"|\*([^*]+)\*"
    r"|_([^_]+)_"
)
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
MAX_SLIDE_TITLE_CHARS = 92
MAX_BULLET_CHARS = 120
COVER_DOC_TYPES = {"executive_report", "proposal", "technical_spec"}
COMPACT_HEADER_DOC_TYPES = {"memo", "one_pager", "resume"}
TOC_DOC_TYPES = {"executive_report", "proposal"}


def _clean_inline(text: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"(\*\*|__)(.*?)\1", r"\2", text)
    text = re.sub(r"(\*|_)(.*?)\1", r"\2", text)
    return text.strip()


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
    """Add Markdown-ish inline text as formatted DOCX runs.

    This intentionally handles the common inline shapes Fronei emits rather
    than trying to be a complete Markdown parser.
    """
    pos = 0
    for match in INLINE_RE.finditer(text.strip()):
        _add_run(paragraph, text[pos:match.start()], base_bold=base_bold, base_italic=base_italic)

        if match.group(1) is not None:
            _add_run(paragraph, match.group(1), base_bold=base_bold, base_italic=base_italic)
        elif match.group(3) is not None:
            _add_hyperlink(paragraph, match.group(3), match.group(4), base_bold, base_italic)
        elif match.group(5) is not None:
            _add_run(paragraph, match.group(5), code=True, base_bold=base_bold, base_italic=base_italic)
        elif match.group(6) is not None:
            _add_run(paragraph, match.group(6), bold=True, base_bold=base_bold, base_italic=base_italic)
        elif match.group(7) is not None:
            _add_run(paragraph, match.group(7), bold=True, base_bold=base_bold, base_italic=base_italic)
        elif match.group(8) is not None:
            _add_run(paragraph, match.group(8), italic=True, base_bold=base_bold, base_italic=base_italic)
        elif match.group(9) is not None:
            _add_run(paragraph, match.group(9), italic=True, base_bold=base_bold, base_italic=base_italic)

        pos = match.end()

    _add_run(paragraph, text[pos:].strip() if pos == 0 else text[pos:], base_bold=base_bold, base_italic=base_italic)


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

def _pptx_layout(prs: Presentation, idx: int, fallback: int = 6):
    layouts = prs.slide_layouts
    try:
        return layouts[idx]
    except IndexError:
        return layouts[fallback if fallback < len(layouts) else 0]


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
    """Add Markdown-ish inline text (bold/italic/code/links) as runs to a
    pptx paragraph. `text_frame_or_paragraph` must already be a paragraph."""
    paragraph = text_frame_or_paragraph
    pos = 0
    cleaned = text.strip()
    matched = False
    for match in INLINE_RE.finditer(cleaned):
        matched = True
        before = cleaned[pos:match.start()]
        if before:
            paragraph.add_run().text = before
        if match.group(1) is not None:
            paragraph.add_run().text = match.group(1)
        elif match.group(3) is not None:
            run = paragraph.add_run()
            run.text = match.group(3)
            run.font.underline = True
        elif match.group(5) is not None:
            run = paragraph.add_run()
            run.text = match.group(5)
            run.font.name = "Courier New"
        elif match.group(6) is not None or match.group(7) is not None:
            run = paragraph.add_run()
            run.text = match.group(6) or match.group(7)
            run.font.bold = True
        elif match.group(8) is not None or match.group(9) is not None:
            run = paragraph.add_run()
            run.text = match.group(8) or match.group(9)
            run.font.italic = True
        pos = match.end()
    remainder = cleaned[pos:]
    if remainder or not matched:
        paragraph.add_run().text = remainder if matched else cleaned


def _pptx_bullet_indent(level: int) -> int:
    return max(0, min(level, 4))


def _shorten(text: str, limit: int) -> str:
    cleaned = re.sub(r"\s+", " ", _clean_inline(str(text or ""))).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 3)].rstrip() + "..."


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
        title = _shorten(raw.get("title") or raw.get("headline") or raw.get("key_message") or "Untitled", MAX_SLIDE_TITLE_CHARS)
        bullets = raw.get("bullets") or raw.get("points") or []
        if isinstance(bullets, str):
            bullets = [bullets]
        bullets = [_shorten(b, MAX_BULLET_CHARS) for b in bullets if str(b or "").strip()]
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
        normalized["slides"].append({
            "layout": layout,
            "title": title,
            "bullets": bullets,
            "table": table_rows,
            "columns": normalized_columns,
            "speaker_notes": _clean_inline(str(notes or "")).strip(),
        })
    return normalized if normalized["slides"] else None


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
        lines.extend(["", f"## {slide['title']}"])
        for bullet in slide.get("bullets") or []:
            lines.append(f"- {bullet}")
        for col in slide.get("columns") or []:
            if col.get("heading"):
                lines.append(f"### {col['heading']}")
            for bullet in col.get("bullets") or []:
                lines.append(f"- {bullet}")
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


def _pptx_set_title(slide, text: str) -> None:
    if slide.shapes.title is not None:
        slide.shapes.title.text = ""
        p = slide.shapes.title.text_frame.paragraphs[0]
        _pptx_add_runs(p, text or "Untitled")


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


def _pptx_add_text_box(slide, left, top, width, height, heading: str, bullets: list[str]) -> None:
    box = slide.shapes.add_textbox(left, top, width, height)
    tf = box.text_frame
    tf.clear()
    if heading:
        p = tf.paragraphs[0]
        _pptx_add_runs(p, heading)
        for run in p.runs:
            run.font.bold = True
            run.font.size = PptxPt(15)
    for idx, bullet in enumerate(bullets):
        p = tf.paragraphs[0] if idx == 0 and not heading else tf.add_paragraph()
        p.level = 0
        _pptx_add_runs(p, bullet)
        for run in p.runs:
            run.font.size = PptxPt(13)


def _pptx_render_deck_plan(prs: Presentation, plan: dict, fallback_title: str, subtitle: str | None) -> None:
    title_layout = _pptx_layout(prs, 0)
    title_slide = prs.slides.add_slide(title_layout)
    _pptx_set_title(title_slide, plan.get("title") or fallback_title or "Fronei deck")
    deck_subtitle = plan.get("subtitle") or subtitle
    if deck_subtitle:
        for shape in title_slide.placeholders:
            if shape.placeholder_format.idx == 1:
                shape.text_frame.text = ""
                _pptx_add_runs(shape.text_frame.paragraphs[0], deck_subtitle)
                break

    for spec in plan.get("slides", []):
        layout = spec.get("layout") or "bullets"
        title = spec.get("title") or "Untitled"
        notes = spec.get("speaker_notes")
        if layout in {"section", "section_divider"}:
            slide = prs.slides.add_slide(_pptx_layout(prs, 2, fallback=5))
            _pptx_set_title(slide, title)
        elif spec.get("table"):
            slide = prs.slides.add_slide(_pptx_layout(prs, 5, fallback=1))
            _pptx_set_title(slide, title)
            _pptx_render_table(slide, spec["table"])
        elif layout in {"two_column", "comparison"} and spec.get("columns"):
            slide = prs.slides.add_slide(_pptx_layout(prs, 5, fallback=1))
            _pptx_set_title(slide, title)
            cols = spec["columns"][:2]
            col_w = PptxInches(5.8)
            top = PptxInches(1.55)
            height = PptxInches(4.9)
            for idx, col in enumerate(cols):
                left = PptxInches(0.65 + idx * 6.05)
                _pptx_add_text_box(slide, left, top, col_w, height, col.get("heading") or "", col.get("bullets") or [])
        else:
            slide = prs.slides.add_slide(_pptx_layout(prs, 1))
            _pptx_set_title(slide, title)
            bullets = spec.get("bullets") or []
            if layout in {"recommendation", "decision"} and bullets:
                bullets = [f"Recommendation: {bullets[0]}", *bullets[1:]]
            body = slide.placeholders[1] if len(slide.placeholders) > 1 else None
            if body is not None:
                tf = body.text_frame
                tf.clear()
                for i, bullet in enumerate(bullets[:MAX_BULLETS_PER_SLIDE] or [""]):
                    p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                    p.level = 0
                    _pptx_add_runs(p, bullet)
        if notes:
            slide.notes_slide.notes_text_frame.text = notes


def generate_pptx_bytes(
    title: str,
    content: str,
    subtitle: str | None = None,
    template_id: str | None = None,
    template_path: str | Path | None = None,
) -> bytes:
    """Render markdown-ish slide-plan content (see module docstring above the
    PPTX section), or a structured DeckPlan JSON object, into a PPTX deck."""
    prs = _presentation_from_template(template_id, template_path)
    prs.slide_width = PptxInches(13.333)
    prs.slide_height = PptxInches(7.5)

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
    title_layout = _pptx_layout(prs, 0)
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
        body_layout = _pptx_layout(prs, 1)
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
            layout = _pptx_layout(prs, 2, fallback=5)
            slide = prs.slides.add_slide(layout)
            _pptx_set_title(slide, spec["title"])
        elif kind == "table":
            layout = _pptx_layout(prs, 5, fallback=1)
            slide = prs.slides.add_slide(layout)
            _pptx_set_title(slide, spec["title"])
            _pptx_render_table(slide, spec["rows"])
        else:  # content
            layout = _pptx_layout(prs, 1)
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
