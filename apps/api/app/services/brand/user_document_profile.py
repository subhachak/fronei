"""User document-generation profile contracts for AgentDeck v2 (#156)."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class UserDocumentProfile(BaseModel):
    user_id: str
    preferred_tone: Optional[str] = None
    preferred_depth: Optional[str] = None
    preferred_slide_density: Optional[str] = None
    brand_profile_ids: list[str] = Field(default_factory=list)
    common_audiences: list[str] = Field(default_factory=list)
    industry_context: Optional[str] = None
    writing_style: Optional[str] = None
    past_accepted_decks: list[str] = Field(default_factory=list)
    past_rejected_patterns: list[str] = Field(default_factory=list)


def user_document_profile_from_memory(user_id: str, profile_json: dict[str, Any] | None) -> UserDocumentProfile:
    profile_json = profile_json or {}
    communication_style = profile_json.get("communication_style")
    key_preferences = profile_json.get("key_preferences") or []
    common_audiences = profile_json.get("common_audiences") or []
    return UserDocumentProfile(
        user_id=user_id,
        preferred_tone=str(profile_json.get("preferred_tone") or "") or None,
        preferred_depth=str(profile_json.get("preferred_depth") or "") or None,
        preferred_slide_density=str(profile_json.get("preferred_slide_density") or "") or None,
        common_audiences=[str(item) for item in common_audiences if str(item).strip()],
        industry_context=str(profile_json.get("industry_context") or "") or None,
        writing_style=communication_style if isinstance(communication_style, str) else None,
        past_rejected_patterns=[str(item) for item in key_preferences if "avoid" in str(item).lower()],
    )
