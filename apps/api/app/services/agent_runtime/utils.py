from __future__ import annotations


def strip_json_fence(raw: str) -> str:
    """Remove a simple Markdown JSON fence from model output."""

    raw = raw.lstrip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    return raw.strip()
