from __future__ import annotations

from typing import Any


def strip_json_fence(raw: str) -> str:
    """Remove a simple Markdown JSON fence from model output."""

    raw = raw.lstrip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    return raw.strip()


def effective_max_repair_iters(quality_mode: str | None, policy: Any | None) -> int:
    """Return the repair budget allowed by quality mode and judge policy.

    Supports both the existing product vocabulary and the Phase-N wording:
    draft/economy -> 0, standard -> 1, executive/premium -> policy max.
    Unknown or missing values are treated as standard.
    """

    base = int(getattr(policy, "max_repair_iterations", 1) or 1)
    mode = (quality_mode or "standard").strip().lower()
    if mode in {"draft", "economy"}:
        return 0
    if mode in {"executive", "premium"}:
        return base
    return min(1, base)
