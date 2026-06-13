from __future__ import annotations

import re
from datetime import date
from io import BytesIO

from docx import Document
from docx.enum.text import WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt
from docx.text.paragraph import Paragraph


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
}
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
