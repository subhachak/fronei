from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.db.models import DocumentTemplate
from app.services.agent_runtime.brand_profile import BrandProfile, brand_profile_from_template
from app.services.document_templates import BUILTIN_PPTX_TEMPLATES


class TemplateOwnershipError(Exception):
    """Raised when a user-selected template does not belong to the user."""


@dataclass
class TemplateSelection:
    template_id: str | None
    owned: bool
    row: DocumentTemplate | None = None
    builtin: dict[str, str] | None = None


def resolve_template_selection(db, user_id: str, template_id: str | None) -> TemplateSelection:
    if not template_id or template_id == "fronei-default":
        return TemplateSelection(template_id=template_id or "fronei-default", owned=True, builtin=BUILTIN_PPTX_TEMPLATES.get("fronei-default"))
    builtin = BUILTIN_PPTX_TEMPLATES.get(template_id)
    if builtin:
        return TemplateSelection(template_id=template_id, owned=True, builtin=builtin)
    row = (
        db.query(DocumentTemplate)
        .filter(
            DocumentTemplate.user_id == user_id,
            DocumentTemplate.public_id == template_id,
            DocumentTemplate.is_active == True,  # noqa: E712
        )
        .first()
        if db is not None
        else None
    )
    if not row:
        return TemplateSelection(template_id=template_id, owned=False)
    return TemplateSelection(template_id=template_id, owned=True, row=row)


def brand_profile_for_selection(
    db,
    user_id: str,
    template_id: str | None,
    *,
    grammar: dict[str, Any] | None = None,
) -> BrandProfile:
    selection = resolve_template_selection(db, user_id, template_id)
    if not selection.owned:
        raise TemplateOwnershipError(f"Template {template_id!r} does not belong to user {user_id!r}")
    if selection.row is not None:
        return brand_profile_from_template(
            template_id=selection.row.public_id,
            template_name=selection.row.name,
            design_system_id=selection.row.design_system_id,
            grammar=grammar,
            source="user_template",
        )
    builtin = selection.builtin or {}
    return brand_profile_from_template(
        template_id=selection.template_id,
        template_name=builtin.get("name"),
        design_system_id=builtin.get("design_system"),
        grammar=grammar,
        source="builtin",
    )
