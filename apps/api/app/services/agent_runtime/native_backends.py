"""Native backend implementations for the agent ToolRunner."""

from __future__ import annotations

import base64
import logging
import re

from app.services.agent_runtime.tool_runner import register_native_backend


logger = logging.getLogger(__name__)

_SAFE_FILENAME_RE = re.compile(r"[^\w\s-]")
_WHITESPACE_RE = re.compile(r"[\s_-]+")


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
    }


def register_all() -> None:
    """Register all native tool backends."""

    register_native_backend("documents.generate_document_output", _generate_document_output)
    logger.debug("Native tool backends registered")
