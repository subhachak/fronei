"""Shared quality-mode policy for AgentDeck v2.

Quality mode is deliberately centralized here so planning, judging, and repair
loops do not grow separate ad hoc interpretations of "draft", "standard", and
"executive".
"""

from __future__ import annotations

from typing import Literal

QualityMode = Literal["draft", "standard", "executive"]

DEFAULT_QUALITY_MODE: QualityMode = "standard"

QUALITY_MODES: tuple[QualityMode, ...] = ("draft", "standard", "executive")

REPAIR_ITERATION_CAPS: dict[QualityMode, int] = {
    "draft": 0,
    "standard": 1,
    "executive": 5,
}

# Tuple is (fail_below, warn_below). Higher modes hold the deck to a stricter
# bar while still letting deterministic slide-level failures force a fail.
DECK_JUDGE_THRESHOLDS: dict[QualityMode, tuple[float, float]] = {
    "draft": (0.45, 0.65),
    "standard": (0.65, 0.82),
    "executive": (0.78, 0.90),
}

QUALITY_MODE_DENSITY: dict[QualityMode, str] = {
    "draft": "dense",
    "standard": "balanced",
    "executive": "sparse",
}

QUALITY_MODE_BRAND_STRICTNESS: dict[QualityMode, str] = {
    "draft": "loose",
    "standard": "balanced",
    "executive": "strict",
}


def normalize_quality_mode(value: object) -> QualityMode:
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in QUALITY_MODES:
            return candidate  # type: ignore[return-value]
    return DEFAULT_QUALITY_MODE


def repair_iteration_cap(value: object) -> int:
    return REPAIR_ITERATION_CAPS[normalize_quality_mode(value)]


def deck_judge_thresholds(value: object) -> tuple[float, float]:
    return DECK_JUDGE_THRESHOLDS[normalize_quality_mode(value)]


def density_target_for_quality(value: object) -> str:
    return QUALITY_MODE_DENSITY[normalize_quality_mode(value)]


def brand_strictness_for_quality(value: object) -> str:
    return QUALITY_MODE_BRAND_STRICTNESS[normalize_quality_mode(value)]
