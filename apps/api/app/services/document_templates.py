from __future__ import annotations

import logging
import re
import secrets
import shutil
from collections import Counter
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from app.config import get_settings
from app.db.models import DocumentTemplate

logger = logging.getLogger(__name__)


TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "assets" / "pptx_templates"
MAX_TEMPLATE_UPLOAD_BYTES = 25 * 1024 * 1024

BUILTIN_PPTX_TEMPLATES: dict[str, dict[str, str]] = {
    "fronei-default": {
        "id": "fronei-default",
        "name": "Fronei Editorial",
        "description": "Premium warm editorial strategy styling for template-free client decks.",
    },
    "strategy-canvas": {
        "id": "strategy-canvas",
        "name": "Strategy canvas",
        "description": "Executive strategy deck with crisp sectioning and decision framing.",
        "filename": "strategy_canvas.pptx",
    },
    "boardroom-navy": {
        "id": "boardroom-navy",
        "name": "Boardroom navy",
        "description": "Formal boardroom-style deck for senior stakeholder presentations.",
        "filename": "boardroom_navy.pptx",
    },
    "architecture-slate": {
        "id": "architecture-slate",
        "name": "Architecture slate",
        "description": "Technical architecture deck for design, platform, and engineering reviews.",
        "filename": "architecture_slate.pptx",
    },
}

PREMIUM_FREEHAND_TEMPLATE_ID = "fronei-default"

FREEHAND_SLIDE_TYPES = [
    "hero_cover",
    "executive_summary",
    "decision_recommendation",
    "architecture_map",
    "three_card_system",
    "process_steps",
    "governance_grid",
    "principles_grid",
    "financial_exhibit",
    "risk_matrix",
    "takeaways",
]

TEMPLATE_FOLLOWING_SLIDE_TYPES = [
    "cover",
    "section_divider",
    "executive_summary",
    "two_column_comparison",
    "process_steps",
    "architecture_map",
    "data_exhibit",
    "recommendation",
    "appendix",
]


def resolve_pptx_template_path(template_id: str | None) -> Path | None:
    if not template_id or template_id == "fronei-default":
        return None
    template = BUILTIN_PPTX_TEMPLATES.get(template_id)
    if not template:
        return None
    filename = template.get("filename")
    if not filename:
        return None
    path = TEMPLATE_DIR / filename
    return path if path.exists() else None


def _storage_root() -> Path:
    root = Path(get_settings().document_template_storage_dir).expanduser()
    if not root.is_absolute():
        root = (Path.cwd() / root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def template_path_for_row(template: DocumentTemplate) -> Path:
    return _storage_root() / template.storage_key


def resolve_template_path(db, user_id: str, template_id: str | None) -> Path | None:
    builtin = resolve_pptx_template_path(template_id)
    if builtin:
        return builtin
    if not template_id or db is None:
        return None
    row = (
        db.query(DocumentTemplate)
        .filter(
            DocumentTemplate.user_id == user_id,
            DocumentTemplate.public_id == template_id,
            DocumentTemplate.is_active == True,  # noqa: E712
        )
        .first()
    )
    if not row:
        return None
    path = template_path_for_row(row)
    return path if path.exists() else None


def _shape_text(shape) -> str:
    if not getattr(shape, "has_text_frame", False):
        return ""
    try:
        return (shape.text or "").strip()
    except Exception:
        return ""


def _font_names(prs: Presentation) -> list[str]:
    fonts: Counter[str] = Counter()
    for slide in prs.slides:
        for shape in slide.shapes:
            if not getattr(shape, "has_text_frame", False):
                continue
            for paragraph in shape.text_frame.paragraphs:
                for run in paragraph.runs:
                    name = getattr(run.font, "name", None)
                    if name:
                        fonts[name] += 1
    return [name for name, _count in fonts.most_common(5)]


def _rgb_hex_from_color(color) -> str | None:
    try:
        rgb = color.rgb
    except Exception:
        return None
    return str(rgb) if rgb is not None else None


def _theme_colors(prs: Presentation) -> list[str]:
    colors: Counter[str] = Counter()
    for slide in prs.slides:
        for shape in slide.shapes:
            try:
                fill = shape.fill
                color = _rgb_hex_from_color(fill.fore_color)
                if color:
                    colors[color] += 1
            except Exception:
                pass
            if not getattr(shape, "has_text_frame", False):
                continue
            for paragraph in shape.text_frame.paragraphs:
                for run in paragraph.runs:
                    color = _rgb_hex_from_color(run.font.color)
                    if color:
                        colors[color] += 1
    return [color for color, _count in colors.most_common(8)]


def _infer_slide_role(slide) -> str:
    shapes = list(slide.shapes)
    text_shapes = [shape for shape in shapes if _shape_text(shape)]
    picture_count = sum(1 for shape in shapes if shape.shape_type == MSO_SHAPE_TYPE.PICTURE)
    table_count = sum(1 for shape in shapes if getattr(shape, "has_table", False))
    chart_count = sum(1 for shape in shapes if getattr(shape, "has_chart", False))
    text_count = len(text_shapes)
    bulletish_count = sum(1 for shape in text_shapes if "\n" in _shape_text(shape))

    if table_count or chart_count:
        return "data_exhibit"
    if picture_count >= 1 and text_count <= 4:
        return "hero_cover"
    if text_count >= 7:
        return "governance_grid"
    if bulletish_count >= 3 or text_count >= 5:
        return "three_card_system"
    if text_count >= 3:
        return "process_steps"
    return "content"


# Cache of computed grammars, keyed by (path, mtime, size) so an edited or
# re-uploaded template (same path, new content) is re-inspected, but repeat
# requests for an unchanged template avoid re-parsing the whole .pptx
# (shapes/fonts/colors/role-inference over every slide) on every presentation
# generation request.
_TEMPLATE_GRAMMAR_CACHE: dict[tuple[str, float, int], dict[str, object]] = {}


def template_grammar_for_path(path: Path) -> dict[str, object]:
    """Inspect a real PPTX template and return a compact design grammar.

    This is intentionally deterministic and cheap. It gives the LLM enough
    guidance to write slides that match the selected template's apparent
    structure before the renderer ever sees the DeckPlan. Results are cached
    per (path, mtime, size) to avoid re-parsing the template on every request.
    """
    try:
        stat = path.stat()
        cache_key = (str(path), stat.st_mtime, stat.st_size)
    except OSError:
        cache_key = None

    if cache_key is not None and cache_key in _TEMPLATE_GRAMMAR_CACHE:
        return _TEMPLATE_GRAMMAR_CACHE[cache_key]

    prs = Presentation(str(path))
    layout_names = [layout.name for layout in prs.slide_layouts if layout.name]
    slide_roles = [_infer_slide_role(slide) for slide in prs.slides]
    role_counts = Counter(slide_roles)
    observed_roles = [role for role, _count in role_counts.most_common()]
    logger.info(
        "template_grammar_for_path: inferred slide roles for %s: %s (counts=%s)",
        path.name,
        slide_roles,
        dict(role_counts),
    )
    grammar = {
        "mode": "template_following",
        "source": "pptx_template",
        "slide_count": len(prs.slides),
        "layout_names": layout_names[:12],
        "observed_slide_roles": observed_roles[:8],
        "available_slide_types": list(dict.fromkeys(observed_roles + TEMPLATE_FOLLOWING_SLIDE_TYPES)),
        "fonts": _font_names(prs),
        "colors": _theme_colors(prs),
        "guidance": [
            "Treat the selected PPTX as a design system, not just a file container.",
            "Use the available slide types repeatedly and consistently rather than inventing a new layout for every slide.",
            "Keep content sparse enough for the template placeholders; move nuance into speaker notes.",
        ],
    }
    if cache_key is not None:
        _TEMPLATE_GRAMMAR_CACHE[cache_key] = grammar
    return grammar


def freehand_premium_grammar(brief: dict | None = None) -> dict[str, object]:
    brief = brief or {}
    return {
        "mode": "fronei_premium_freehand",
        "source": "fronei_default_theme",
        "template_id": PREMIUM_FREEHAND_TEMPLATE_ID,
        "visual_direction": "warm editorial enterprise strategy deck",
        "available_slide_types": FREEHAND_SLIDE_TYPES,
        "fonts": ["Georgia", "Segoe UI"],
        "colors": ["F7F1EE", "E04F00", "282421", "FFFFFF"],
        "guidance": [
            "Use an editorial consulting-deck rhythm: one sharp assertion per slide, visual proof object, short support.",
            "Prefer cards, process diagrams, comparison canvases, and exhibit slides over plain bullet lists.",
            "Use speaker notes for detail that would make the slide crowded.",
        ],
        "recommended_for": brief.get("audience") or "senior stakeholders",
    }


def template_grammar_for_selection(db, user_id: str, template_id: str | None, brief: dict | None = None) -> dict[str, object]:
    if not template_id or template_id == PREMIUM_FREEHAND_TEMPLATE_ID:
        return freehand_premium_grammar(brief)
    path = resolve_template_path(db, user_id, template_id)
    if not path:
        grammar = freehand_premium_grammar(brief)
        grammar["template_id"] = template_id or PREMIUM_FREEHAND_TEMPLATE_ID
        grammar["fallback_reason"] = "selected template was unavailable"
        return grammar
    grammar = template_grammar_for_path(path)
    grammar["template_id"] = template_id
    return grammar


def template_design_context(grammar: dict[str, object] | None) -> str:
    if not grammar:
        return ""
    mode = grammar.get("mode") or "fronei_premium_freehand"
    slide_types = ", ".join(str(x) for x in grammar.get("available_slide_types") or []) or "executive_summary, recommendation"
    fonts = ", ".join(str(x) for x in grammar.get("fonts") or []) or "template fonts"
    colors = ", ".join(str(x) for x in grammar.get("colors") or []) or "template palette"
    guidance = "\n".join(f"- {item}" for item in (grammar.get("guidance") or []))
    if mode == "template_following":
        layout_names = ", ".join(str(x) for x in grammar.get("layout_names") or [])
        observed = ", ".join(str(x) for x in grammar.get("observed_slide_roles") or [])
        return f"""TEMPLATE-FIRST PRESENTATION DESIGN BRIEF:
Mode: follow the selected PowerPoint template.
Selected template id: {grammar.get("template_id") or "unknown"}
Observed template slide roles: {observed or "not enough sample slides to infer; use conventional executive deck roles"}.
Available template layout names: {layout_names or "unavailable"}.
Use only these semantic slide types unless the user explicitly asks otherwise: {slide_types}.
Template fonts observed: {fonts}.
Template colors observed: {colors}.
{guidance}

DeckPlan rules for this template:
- Do not produce a Markdown article in slide form. Produce a designed presentation plan.
- Each slide must have one assertion-style title under 12 words and one visual job: compare, explain flow, quantify impact, show timeline, or ask for a decision.
- Prefer `executive_summary`, `recommendation`, `timeline`, `architecture`, `comparison`, `financial_model`, `risk_matrix`, and `table` layouts over generic `bullets`.
- Keep visible slide text sparse: max 3 bullets per slide, max 12 words per bullet, no paragraph bullets.
- Put detail, caveats, and narration in `speaker_notes`, not on the slide."""

    return f"""TEMPLATE-FIRST PRESENTATION DESIGN BRIEF:
Mode: Fronei premium freehand theme.
Visual direction: {grammar.get("visual_direction") or "editorial enterprise strategy deck"}.
Use these semantic slide types: {slide_types}.
Theme fonts: {fonts}.
Theme colors: {colors}.
{guidance}

DeckPlan rules for this theme:
- Think like a human presentation designer: define the story spine, choose a slide archetype for each moment, then write sparse slide copy.
- Do not produce generic bullet-only decks. Every slide needs a visual structure: callout, cards, matrix, roadmap, architecture map, chart, or decision panel.
- Each slide title must be an assertion under 12 words. Avoid centered, multi-line newspaper headlines.
- Visible text limit: max 3 bullets per slide, max 12 words per bullet. Move nuance into `speaker_notes`.
- For business/strategy decks, include an executive summary, evidence/analysis section, decision/recommendation slide, and next steps."""


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", (name or "").strip())
    return cleaned[:160] or "Presentation template"


def _template_option_from_row(row: DocumentTemplate, *, recommended: bool = False) -> dict[str, object]:
    return {
        "id": row.public_id,
        "name": row.name,
        "description": row.description or f"Uploaded from {row.original_filename or 'PowerPoint template'}",
        "recommended": recommended,
        "user_template": True,
        "design_mode": "template_following",
    }


def recommend_template_id(brief: dict | None) -> str:
    brief = brief or {}
    text = " ".join(str(brief.get(k) or "") for k in ("doc_type", "title", "audience", "tone", "length")).lower()
    if "architecture" in text or "technical" in text or "engineering" in text or "platform" in text:
        return "architecture-slate"
    if "board" in text or "steering" in text or "executive" in text or "client" in text:
        return "boardroom-navy"
    if (brief or {}).get("doc_type") == "presentation":
        return "strategy-canvas"
    return "fronei-default"


def list_document_templates(
    doc_type: str | None = None,
    brief: dict | None = None,
    db=None,
    user_id: str | None = None,
) -> list[dict[str, object]]:
    recommendation = recommend_template_id(brief)
    if doc_type != "presentation":
        base = BUILTIN_PPTX_TEMPLATES["fronei-default"].copy()
        base["recommended"] = True
        base["design_mode"] = "fronei_premium_freehand"
        return [base]

    templates: list[dict[str, object]] = []
    user_rows: list[DocumentTemplate] = []
    if db is not None and user_id:
        user_rows = (
            db.query(DocumentTemplate)
            .filter(
                DocumentTemplate.user_id == user_id,
                DocumentTemplate.doc_type == "presentation",
                DocumentTemplate.is_active == True,  # noqa: E712
            )
            .order_by(DocumentTemplate.updated_at.desc())
            .all()
        )
        templates.extend(_template_option_from_row(row) for row in user_rows)

    for template_id in ("fronei-default", "strategy-canvas", "boardroom-navy", "architecture-slate"):
        item = BUILTIN_PPTX_TEMPLATES[template_id].copy()
        if template_id != "fronei-default" and not resolve_pptx_template_path(template_id):
            continue
        item["recommended"] = not user_rows and template_id == recommendation
        item["design_mode"] = "fronei_premium_freehand" if template_id == "fronei-default" else "template_following"
        templates.append(item)
    if user_rows and templates:
        templates[0]["recommended"] = True
    elif not any(t.get("recommended") for t in templates) and templates:
        templates[0]["recommended"] = True
    return templates


def store_user_pptx_template(
    db,
    user_id: str,
    *,
    filename: str,
    content_type: str | None,
    data: bytes,
    name: str | None = None,
    description: str | None = None,
) -> DocumentTemplate:
    if not data:
        raise ValueError("Template file is empty.")
    if len(data) > MAX_TEMPLATE_UPLOAD_BYTES:
        raise ValueError("Template file is too large.")
    if not (filename or "").lower().endswith(".pptx"):
        raise ValueError("Only .pptx templates are supported.")

    # Validate before writing permanently.
    Presentation(BytesIO(data))

    now = datetime.now(timezone.utc)
    public_id = secrets.token_hex(12)
    user_dir = _storage_root() / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    storage_key = f"{user_id}/{public_id}.pptx"
    path = _storage_root() / storage_key
    with path.open("wb") as f:
        f.write(data)

    row = DocumentTemplate(
        public_id=public_id,
        user_id=user_id,
        name=_safe_name(name or Path(filename).stem),
        description=(description or "").strip()[:500] or None,
        doc_type="presentation",
        storage_key=storage_key,
        original_filename=filename[:255],
        content_type=(content_type or "")[:120] or None,
        file_size=len(data),
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def archive_user_template(db, user_id: str, template_id: str) -> bool:
    row = (
        db.query(DocumentTemplate)
        .filter(
            DocumentTemplate.user_id == user_id,
            DocumentTemplate.public_id == template_id,
            DocumentTemplate.is_active == True,  # noqa: E712
        )
        .first()
    )
    if not row:
        return False
    row.is_active = False
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    path = template_path_for_row(row)
    try:
        if path.exists():
            archive_dir = path.parent / ".archived"
            archive_dir.mkdir(exist_ok=True)
            shutil.move(str(path), str(archive_dir / path.name))
    except Exception:
        # DB archive is authoritative; filesystem cleanup can be retried later.
        pass
    return True
