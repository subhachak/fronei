"""Native backend implementations for the agent ToolRunner."""

from __future__ import annotations

import base64
import logging
import re

from app.services.agent_runtime.tool_runner import register_native_backend


logger = logging.getLogger(__name__)

_SAFE_FILENAME_RE = re.compile(r"[^\w\s-]")
_WHITESPACE_RE = re.compile(r"[\s_-]+")
_BUILTIN_TEMPLATE_IDS = {
    "fronei-default",
    "warm-editorial",
    "modern-tech",
    "executive-navy",
    "data-product-os",
    "clean-light",
}


def _safe_filename(title: str) -> str:
    name = _SAFE_FILENAME_RE.sub("", title.strip().lower())
    return _WHITESPACE_RE.sub("-", name)[:80] or "document"


def _generate_document_output(inputs: dict) -> dict:
    """Native backend for the generate_document tool."""

    from app.services.document_generator import KNOWN_DOC_TYPES, generate_docx_bytes

    title = str(inputs.get("title") or "Document")
    content = str(inputs.get("content") or "")
    doc_type = str(inputs.get("doc_type") or "executive_report")
    subtitle = inputs.get("subtitle") or None
    template_id = inputs.get("template_id") or None

    if doc_type not in KNOWN_DOC_TYPES:
        logger.warning("Unknown doc_type %r; defaulting to executive_report", doc_type)
        doc_type = "executive_report"

    content_bytes = generate_docx_bytes(title, content, subtitle, doc_type=doc_type)
    return {
        "title": title,
        "doc_type": doc_type,
        "filename": f"{_safe_filename(title)}.docx",
        "markdown": content,
        "docx_base64": base64.b64encode(content_bytes).decode("ascii"),
        "template_id": template_id,
    }


def _render_pptx_output(inputs: dict) -> dict:
    """Native backend for the render_pptx tool."""

    from app.db.models import SessionLocal
    from app.services.document_generator import generate_pptx_bytes
    from app.services.document_templates import resolve_pptx_template_path, resolve_template_path

    title = str(inputs.get("title") or "Presentation")
    content = str(inputs.get("content") or "")
    subtitle = inputs.get("subtitle") or None
    template_id = inputs.get("template_id") or None
    user_id = str(inputs.get("user_id") or "")

    template_path = None
    builtin_path = resolve_pptx_template_path(template_id)
    if builtin_path:
        template_path = builtin_path
    elif template_id and user_id and template_id not in _BUILTIN_TEMPLATE_IDS:
        try:
            with SessionLocal() as db:
                template_path = resolve_template_path(db, user_id, template_id)
        except Exception:
            logger.warning(
                "Could not resolve template path for %r; using freehand renderer",
                template_id,
            )

    pptx_bytes = generate_pptx_bytes(
        title=title,
        content=content,
        subtitle=subtitle,
        template_id=template_id,
        template_path=template_path,
    )
    return {
        "title": title,
        "filename": f"{_safe_filename(title)}.pptx",
        "markdown": content,
        "pptx_base64": base64.b64encode(pptx_bytes).decode("ascii"),
        "template_id": template_id,
    }


def register_all() -> None:
    """Register all native tool backends."""

    register_native_backend("documents.generate_document_output", _generate_document_output)
    register_native_backend("documents.render_pptx_output", _render_pptx_output)
    logger.debug("Native tool backends registered")
