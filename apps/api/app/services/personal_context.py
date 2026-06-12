from __future__ import annotations

import json
from datetime import datetime, timezone

from app.db.models import UserMemory, UserProfile
from app.services.memory_ranker import rank_memories


def _load_profile(profile: UserProfile | None) -> dict:
    if not profile or not profile.profile_json:
        return {}
    try:
        data = json.loads(profile.profile_json)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _as_list(value) -> list:
    if isinstance(value, list):
        return [v for v in value if v]
    if value:
        return [value]
    return []


def profile_brief(profile_json: dict, max_chars: int = 900) -> str:
    if not profile_json:
        return ""
    lines: list[str] = []
    role = profile_json.get("role")
    company = profile_json.get("company")
    location = profile_json.get("location")
    if role or company or location:
        parts = [str(v) for v in [role, company, location] if v]
        lines.append(f"- [profile] {'; '.join(parts)}")
    for key, label in [
        ("active_projects", "active projects"),
        ("key_preferences", "preferences"),
        ("constraints", "constraints"),
        ("work_context", "work"),
        ("personal_context", "personal"),
    ]:
        values = _as_list(profile_json.get(key))[:4]
        if values:
            lines.append(f"- [{label}] {'; '.join(str(v) for v in values)}")
    style = profile_json.get("communication_style")
    if isinstance(style, dict) and style:
        style_bits = [f"{k}: {v}" for k, v in list(style.items())[:5] if v]
        if style_bits:
            lines.append(f"- [communication style] {'; '.join(style_bits)}")
    overrides = profile_json.get("overrides")
    if isinstance(overrides, dict) and overrides:
        override_bits = [f"{k}: {v}" for k, v in list(overrides.items())[:5] if v]
        if override_bits:
            lines.append(f"- [user overrides] {'; '.join(override_bits)}")

    output: list[str] = []
    total = 0
    for line in lines:
        if total + len(line) + 1 > max_chars:
            break
        output.append(line)
        total += len(line) + 1
    return "\n".join(output)


def build_context(
    db,
    user_id: str,
    turn_category_hint: str | None = None,
    limit: int = 12,
    max_chars: int = 2500,
) -> str:
    """Return a compact, ranked block of active user memories for prompt injection."""
    profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
    brief = profile_brief(_load_profile(profile))
    memories = (
        db.query(UserMemory)
        .filter(UserMemory.user_id == user_id, UserMemory.status == "active")
        .all()
    )
    ranked = rank_memories(memories, datetime.now(timezone.utc), turn_category_hint, limit)
    sections: list[str] = []
    if brief:
        sections.append("USER PROFILE:\n" + brief)
    if not ranked:
        return "\n\n".join(sections)

    lines: list[str] = []
    total = 0
    for memory in ranked:
        tag = f"[{memory.category}/{memory.scope}]"
        conf = "" if (memory.confidence or 0.0) >= 0.8 else " (uncertain)"
        line = f"- {tag} {memory.content}{conf}"
        if total + len(line) + 1 > max_chars:
            break
        lines.append(line)
        total += len(line) + 1
    if lines:
        sections.append("RANKED USER MEMORIES:\n" + "\n".join(lines))
    return "\n\n".join(sections)
