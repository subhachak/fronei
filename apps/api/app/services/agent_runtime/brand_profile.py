from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BrandProfile:
    """Runtime brand/template profile used by document and deck agents."""

    template_id: str | None = None
    template_name: str | None = None
    design_system_id: str | None = None
    mode: str = "freehand"
    colors: list[str] = field(default_factory=list)
    fonts: list[str] = field(default_factory=list)
    slide_roles: list[str] = field(default_factory=list)
    source: str = "default"

    def is_default(self) -> bool:
        return self.source == "default" and self.template_id is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "template_name": self.template_name,
            "design_system_id": self.design_system_id,
            "mode": self.mode,
            "colors": self.colors,
            "fonts": self.fonts,
            "slide_roles": self.slide_roles,
            "source": self.source,
        }


def brand_profile_from_template(
    *,
    template_id: str | None,
    template_name: str | None = None,
    design_system_id: str | None = None,
    grammar: dict[str, Any] | None = None,
    source: str = "default",
) -> BrandProfile:
    grammar = grammar or {}
    roles: list[str] = []
    for item in grammar.get("slide_patterns") or grammar.get("slides") or []:
        if isinstance(item, dict):
            role = item.get("role") or item.get("slide_type")
            if role:
                roles.append(str(role))
    return BrandProfile(
        template_id=template_id,
        template_name=template_name or str(grammar.get("template_name") or "") or None,
        design_system_id=design_system_id or grammar.get("design_system") or grammar.get("design_system_id"),
        mode=str(grammar.get("mode") or ("template_following" if template_id else "freehand")),
        colors=[str(c) for c in (grammar.get("colors") or grammar.get("theme_colors") or [])],
        fonts=[str(f) for f in (grammar.get("fonts") or [])],
        slide_roles=roles,
        source=source,
    )
