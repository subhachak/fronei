"""Brand profile contracts for AgentDeck v2 (#155)."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class BrandProfile(BaseModel):
    id: str
    user_id: str
    source_template_id: Optional[str] = None
    logo_assets: list[dict[str, Any]] = Field(default_factory=list)
    color_tokens: list[str] = Field(default_factory=list)
    font_tokens: list[str] = Field(default_factory=list)
    layout_preferences: list[str] = Field(default_factory=list)
    forbidden_patterns: list[str] = Field(default_factory=list)
    example_slide_images: list[str] = Field(default_factory=list)
    extracted_components: list[str] = Field(default_factory=list)


def brand_profile_from_template_grammar(
    grammar: dict[str, Any],
    *,
    user_id: str,
    profile_id: str | None = None,
) -> BrandProfile:
    template_id = str(grammar.get("template_id") or "fronei-default")
    return BrandProfile(
        id=profile_id or template_id,
        user_id=user_id,
        source_template_id=template_id,
        color_tokens=[str(value) for value in (grammar.get("colors") or [])],
        font_tokens=[str(value) for value in (grammar.get("fonts") or [])],
        layout_preferences=[str(value) for value in (grammar.get("available_slide_types") or [])],
        forbidden_patterns=[
            "Do not force article-style markdown into slides.",
            "Do not exceed template placeholder density.",
        ],
        extracted_components=[str(value) for value in (grammar.get("observed_slide_roles") or [])],
    )
