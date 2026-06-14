from __future__ import annotations

import re
import secrets
import shutil
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from pptx import Presentation

from app.config import get_settings
from app.db.models import DocumentTemplate


TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "assets" / "pptx_templates"
MAX_TEMPLATE_UPLOAD_BYTES = 25 * 1024 * 1024

BUILTIN_PPTX_TEMPLATES: dict[str, dict[str, str]] = {
    "fronei-default": {
        "id": "fronei-default",
        "name": "Fronei default",
        "description": "Clean, neutral deck styling.",
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
    if not template_id:
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
