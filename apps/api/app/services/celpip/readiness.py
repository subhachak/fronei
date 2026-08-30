"""Readiness, computed from signals the system holds.

A model asked "how ready is this person" will produce a confident percentage
from nothing. This module refuses to do that. Readiness here is a weighted
function of six measured quantities, each stored alongside the composite so
the dashboard can say *why* the number moved and what would move it.

Every sub-score is 0-1. None of them is a model's opinion.
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone

from app.db.models import CelpipAttempt, CelpipProfile, CelpipResponse
from app.services.celpip.spec import (
    components_for_test_type,
    task_keys_for_test_type,
)

# Weights sum to 1.0. Component level dominates because it is the thing the
# test actually measures; the rest describe whether that level will hold up
# under exam conditions.
WEIGHTS = {
    "component_levels": 0.35,
    "full_test": 0.15,
    "consistency": 0.15,
    "timing": 0.15,
    "coverage": 0.10,
    "recency": 0.10,
}

# Practice older than this contributes nothing to the recency signal.
RECENCY_HORIZON_DAYS = 14
# A full mock older than this no longer counts as evidence of exam stamina.
FULL_TEST_HORIZON_DAYS = 21


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _loads(raw: str, default):
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _component_levels(attempts: list[CelpipAttempt], skills: tuple[str, ...]) -> dict[str, dict]:
    """Most recent level estimate per component, plus its history."""
    history: dict[str, list[tuple[datetime, float]]] = {s: [] for s in skills}
    for attempt in sorted(attempts, key=lambda a: _aware(a.completed_at) or _aware(a.created_at)):
        results = _loads(attempt.results_json, {})
        stamp = _aware(attempt.completed_at) or _aware(attempt.created_at)
        for skill in skills:
            entry = results.get(skill)
            if not entry:
                continue
            level = entry.get("level") or {}
            low, high = level.get("low"), level.get("high")
            if low is None or high is None:
                continue
            history[skill].append((stamp, (float(low) + float(high)) / 2))

    out: dict[str, dict] = {}
    for skill, points in history.items():
        if not points:
            out[skill] = {"latest": None, "history": [], "attempts": 0}
            continue
        out[skill] = {
            "latest": round(points[-1][1], 1),
            "history": [{"at": p[0].isoformat(), "level": round(p[1], 1)} for p in points],
            "attempts": len(points),
        }
    return out


def _score_component_levels(levels: dict[str, dict], target: int, skills: tuple[str, ...]) -> tuple[float, dict]:
    """How close each component is to the target, averaged.

    An untested component scores 0 rather than being skipped: not knowing
    where you stand in Speaking is not the same as being ready in Speaking.
    """
    per_skill: dict[str, float] = {}
    for skill in skills:
        latest = levels.get(skill, {}).get("latest")
        per_skill[skill] = 0.0 if latest is None else min(1.0, float(latest) / max(1, target))
    score = sum(per_skill.values()) / len(per_skill) if per_skill else 0.0
    return score, {k: round(v, 2) for k, v in per_skill.items()}


def _score_full_test(attempts: list[CelpipAttempt], test_ids_by_mode: dict[str, str]) -> tuple[float, dict]:
    full = [
        a for a in attempts
        if test_ids_by_mode.get(a.test_id) in {"full", "full_ls"} and a.status == "completed"
    ]
    if not full:
        return 0.0, {"completed": 0, "most_recent": None}
    latest = max(_aware(a.completed_at) or _aware(a.created_at) for a in full)
    age_days = (_now() - latest).days
    freshness = max(0.0, 1.0 - age_days / FULL_TEST_HORIZON_DAYS)
    # Two full mocks is the point of diminishing returns for the "have I sat
    # the whole thing" question; more matters through the other signals.
    volume = min(1.0, len(full) / 2)
    return round(0.5 * volume + 0.5 * freshness, 3), {
        "completed": len(full), "most_recent": latest.isoformat(), "age_days": age_days,
    }


def _score_consistency(levels: dict[str, dict]) -> tuple[float, dict]:
    """Stability of recent estimates. Swinging two levels between attempts
    means the score is not reliable yet, whatever the average says."""
    spreads: dict[str, float] = {}
    for skill, data in levels.items():
        recent = [point["level"] for point in data.get("history", [])][-4:]
        if len(recent) < 2:
            continue
        spreads[skill] = statistics.pstdev(recent)
    if not spreads:
        return 0.0, {"note": "needs at least two scored attempts in a component"}
    mean_spread = sum(spreads.values()) / len(spreads)
    # A standard deviation of 0 scores 1.0; 1.5 levels or more scores 0.
    score = max(0.0, 1.0 - mean_spread / 1.5)
    return round(score, 3), {k: round(v, 2) for k, v in spreads.items()}


def _score_timing(db, attempts: list[CelpipAttempt]) -> tuple[float, dict]:
    """Fraction of answers given inside the section deadline."""
    attempt_ids = [a.id for a in attempts]
    if not attempt_ids:
        return 0.0, {"answered": 0, "late": 0, "unanswered": 0}
    rows = db.query(CelpipResponse).filter(CelpipResponse.attempt_id.in_(attempt_ids)).all()
    if not rows:
        return 0.0, {"answered": 0, "late": 0, "unanswered": 0}
    answered = [
        r for r in rows
        if r.selected_option or (r.response_text or "").strip() or r.audio_blob_location
    ]
    late = [r for r in answered if r.late]
    unanswered = len(rows) - len(answered)
    on_time = len(answered) - len(late)
    score = on_time / len(rows) if rows else 0.0
    return round(score, 3), {
        "answered": len(answered), "late": len(late), "unanswered": unanswered,
        "total": len(rows),
    }


def _score_coverage(db, attempts: list[CelpipAttempt], test_type: str) -> tuple[float, dict]:
    attempt_ids = [a.id for a in attempts]
    expected = task_keys_for_test_type(test_type)
    if not attempt_ids:
        return 0.0, {"attempted": 0, "expected": len(expected), "missing": expected}
    seen = {
        row[0] for row in
        db.query(CelpipResponse.task_key).filter(CelpipResponse.attempt_id.in_(attempt_ids)).distinct().all()
    }
    missing = [k for k in expected if k not in seen]
    score = (len(expected) - len(missing)) / len(expected) if expected else 0.0
    return round(score, 3), {
        "attempted": len(expected) - len(missing), "expected": len(expected), "missing": missing,
    }


def _score_recency(attempts: list[CelpipAttempt]) -> tuple[float, dict]:
    stamps = [_aware(a.created_at) for a in attempts if a.created_at]
    if not stamps:
        return 0.0, {"days_since_practice": None, "sessions_last_7_days": 0}
    last = max(stamps)
    days = (_now() - last).days
    week_ago = _now() - timedelta(days=7)
    recent_sessions = sum(1 for s in stamps if s >= week_ago)
    freshness = max(0.0, 1.0 - days / RECENCY_HORIZON_DAYS)
    # Five sessions a week is a full cadence for a one-month run-up.
    cadence = min(1.0, recent_sessions / 5)
    return round(0.5 * freshness + 0.5 * cadence, 3), {
        "days_since_practice": days, "sessions_last_7_days": recent_sessions,
    }


def compute_readiness(db, *, user_id: str, profile: CelpipProfile | None = None) -> dict:
    """Full readiness breakdown for one learner."""
    from app.db.models import CelpipTest

    if profile is None:
        profile = db.query(CelpipProfile).filter(CelpipProfile.user_id == user_id).first()
    test_type = profile.test_type if profile else "general"
    target = profile.target_level if profile else 9
    skills = components_for_test_type(test_type)

    attempts = (
        db.query(CelpipAttempt)
        .filter(CelpipAttempt.user_id == user_id)
        .filter(CelpipAttempt.status.in_(("completed", "submitted", "evaluating", "in_progress")))
        .all()
    )
    scored = [a for a in attempts if a.status == "completed"]
    modes = {
        row[0]: row[1]
        for row in db.query(CelpipTest.id, CelpipTest.mode).filter(CelpipTest.user_id == user_id).all()
    }

    levels = _component_levels(scored, skills)
    signals: dict[str, dict] = {}

    level_score, level_detail = _score_component_levels(levels, target, skills)
    full_score, full_detail = _score_full_test(scored, modes)
    consistency_score, consistency_detail = _score_consistency(levels)
    timing_score, timing_detail = _score_timing(db, attempts)
    coverage_score, coverage_detail = _score_coverage(db, attempts, test_type)
    recency_score, recency_detail = _score_recency(attempts)

    parts = {
        "component_levels": (level_score, level_detail),
        "full_test": (full_score, full_detail),
        "consistency": (consistency_score, consistency_detail),
        "timing": (timing_score, timing_detail),
        "coverage": (coverage_score, coverage_detail),
        "recency": (recency_score, recency_detail),
    }
    for name, (score, detail) in parts.items():
        signals[name] = {
            "score": round(score, 3),
            "weight": WEIGHTS[name],
            "contribution": round(score * WEIGHTS[name], 3),
            "detail": detail,
        }

    composite = sum(s["contribution"] for s in signals.values())

    # The single most useful output: which signal is costing the most, and so
    # what to do next. Ranked by how much weight is currently unclaimed.
    gaps = sorted(
        ((name, (1 - data["score"]) * data["weight"]) for name, data in signals.items()),
        key=lambda pair: pair[1],
        reverse=True,
    )

    days_left = None
    if profile and profile.test_date:
        days_left = (profile.test_date - _now().date()).days

    return {
        "readiness": round(composite * 100),
        "target_level": target,
        "test_type": test_type,
        "days_until_test": days_left,
        "component_levels": levels,
        "signals": signals,
        "biggest_gaps": [{"signal": name, "unclaimed": round(value, 3)} for name, value in gaps[:3]],
        "computed_at": _now().isoformat(),
    }
