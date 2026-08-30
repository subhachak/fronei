"""The progress report: four components, twenty sub-categories, and what to do.

The dashboard's job is to answer four questions per task type, in order:

1. How much have I practised this?
2. How am I doing, and is it moving?
3. Where should I spend my time next?
4. What specifically do I do about it?

(1) and (2) come straight from stored attempts -- receptive tasks from the
per-task accuracy already recorded on each attempt's results, productive tasks
from their evaluations. (3) is a deterministic focus score, so the ranking is
explainable rather than an opinion. (4) is the authored Learn library, reached
through the weakness tags the scorer already assigns, so every tip is a real
technique rather than encouragement.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app.db.models import (
    CelpipAttempt,
    CelpipEvaluation,
    CelpipResponse,
)
from app.services.celpip import lessons as lessons_service
from app.services.celpip.spec import (
    TASKS_BY_SKILL,
    WEAKNESS_TAGS,
    components_for_test_type,
)

# Weights for the focus score. Distance from target dominates -- it is the
# thing the learner is actually trying to close -- but a task never attempted
# is ranked high too, because an unmeasured task is a risk, not a strength.
GAP_WEIGHT = 0.5
UNTESTED_WEIGHT = 0.3
STALENESS_WEIGHT = 0.2
STALE_AFTER_DAYS = 14


def _loads(raw: str, default):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return value if value is not None else default


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    aware = _aware(value)
    return aware.isoformat() if aware else None


def _receptive_history(attempts: list[CelpipAttempt]) -> dict[str, list[dict]]:
    """Per-task accuracy over time, from each attempt's stored results."""
    history: dict[str, list[dict]] = {}
    for attempt in attempts:
        stamp = _aware(attempt.completed_at) or _aware(attempt.created_at)
        for component in _loads(attempt.results_json, {}).values():
            for task_key, counts in (component.get("accuracy_by_task") or {}).items():
                total = counts.get("total") or 0
                if not total:
                    continue
                history.setdefault(task_key, []).append({
                    "at": stamp.isoformat() if stamp else None,
                    "attempt_id": attempt.id,
                    "correct": counts.get("correct", 0),
                    "total": total,
                    "accuracy": round(counts.get("correct", 0) / total, 3),
                })
    return history


def _productive_history(evaluations: list[CelpipEvaluation]) -> dict[str, list[dict]]:
    history: dict[str, list[dict]] = {}
    for evaluation in evaluations:
        if evaluation.level_low is None or not evaluation.task_key:
            continue
        stamp = _aware(evaluation.completed_at) or _aware(evaluation.created_at)
        high = evaluation.level_high if evaluation.level_high is not None else evaluation.level_low
        history.setdefault(evaluation.task_key, []).append({
            "at": stamp.isoformat() if stamp else None,
            "attempt_id": evaluation.attempt_id,
            "low": evaluation.level_low,
            "high": high,
            "level": round((evaluation.level_low + high) / 2, 1),
            "confidence": evaluation.confidence,
        })
    return history


def _trend(values: list[float]) -> str:
    """Improving, steady, or slipping -- from the last few data points only.

    Deliberately coarse. Three sittings of a task type is not enough for a
    regression line, and presenting one would imply precision that is not there.
    """
    if len(values) < 2:
        return "unknown"
    recent = values[-3:]
    change = recent[-1] - recent[0]
    if change >= 0.75:
        return "improving"
    if change <= -0.75:
        return "slipping"
    return "steady"


def _focus_score(
    *, level: float | None, target: int, attempts: int, days_since: int | None
) -> float:
    """0-1. Higher means this task type needs the next hour of study."""
    if attempts == 0:
        return 1.0
    gap = max(0.0, min(1.0, (target - (level or 0)) / max(1, target)))
    untested = 1.0 if attempts < 2 else 0.0
    staleness = (
        1.0 if days_since is None
        else max(0.0, min(1.0, days_since / STALE_AFTER_DAYS))
    )
    return round(
        GAP_WEIGHT * gap + UNTESTED_WEIGHT * untested + STALENESS_WEIGHT * staleness, 3
    )


def build_report(db, *, user_id: str, test_type: str, target_level: int) -> dict:
    """The full four-category, twenty-sub-category report."""
    attempts = (
        db.query(CelpipAttempt)
        .filter(CelpipAttempt.user_id == user_id)
        .filter(CelpipAttempt.status == "completed")
        .order_by(CelpipAttempt.created_at.asc())
        .all()
    )
    attempt_ids = [a.id for a in attempts]

    evaluations = (
        db.query(CelpipEvaluation)
        .filter(CelpipEvaluation.attempt_id.in_(attempt_ids))
        .filter(CelpipEvaluation.status == "complete")
        .order_by(CelpipEvaluation.created_at.asc())
        .all()
        if attempt_ids else []
    )

    # "How many did I take" is per task type, counted in distinct sittings
    # rather than responses -- eight speaking answers in one mock is one sitting
    # of each speaking task, not eight of anything.
    taken: dict[str, set[str]] = {}
    last_seen: dict[str, datetime] = {}
    if attempt_ids:
        rows = (
            db.query(CelpipResponse.task_key, CelpipResponse.attempt_id, CelpipResponse.created_at)
            .filter(CelpipResponse.attempt_id.in_(attempt_ids))
            .all()
        )
        for task_key, attempt_id, created_at in rows:
            taken.setdefault(task_key, set()).add(attempt_id)
            stamp = _aware(created_at)
            if stamp and (task_key not in last_seen or stamp > last_seen[task_key]):
                last_seen[task_key] = stamp

    receptive = _receptive_history(attempts)
    productive = _productive_history(evaluations)

    tags_by_task: dict[str, dict[str, int]] = {}
    for evaluation in evaluations:
        bucket = tags_by_task.setdefault(evaluation.task_key, {})
        for tag in _loads(evaluation.weakness_tags_json, []):
            if tag in WEAKNESS_TAGS:
                bucket[tag] = bucket.get(tag, 0) + 1
    for attempt in attempts:
        for skill, component in _loads(attempt.results_json, {}).items():
            for task_key in (component.get("accuracy_by_task") or {}):
                bucket = tags_by_task.setdefault(task_key, {})
                for tag in component.get("weakness_tags") or []:
                    if tag in WEAKNESS_TAGS:
                        bucket[tag] = bucket.get(tag, 0) + 1

    now = datetime.now(timezone.utc)
    categories = []
    for skill in components_for_test_type(test_type):
        tasks = []
        for spec_task in TASKS_BY_SKILL[skill]:
            key = spec_task.key
            sittings = len(taken.get(key, set()))
            seen_at = last_seen.get(key)
            days_since = (now - seen_at).days if seen_at else None

            if skill in {"listening", "reading"}:
                points = receptive.get(key, [])
                correct = sum(p["correct"] for p in points)
                total = sum(p["total"] for p in points)
                accuracy = round(correct / total, 3) if total else None
                # Accuracy maps onto the same approximate level bands the
                # receptive scorer uses, so one number means one thing here.
                level = round(accuracy * 12, 1) if accuracy is not None else None
                series = [p["accuracy"] * 12 for p in points]
                history = points
            else:
                points = productive.get(key, [])
                correct = total = 0
                accuracy = None
                level = round(sum(p["level"] for p in points) / len(points), 1) if points else None
                series = [p["level"] for p in points]
                history = points

            tag_counts = sorted(
                tags_by_task.get(key, {}).items(), key=lambda pair: pair[1], reverse=True
            )[:4]
            tips = lessons_service.lessons_for_tags(db, [tag for tag, _ in tag_counts])
            if not tips:
                # No measured weakness yet: fall back to the strategy lesson for
                # the task itself, so every sub-category always has a next step.
                task_lesson = lessons_service.list_lessons(db, skill=skill)
                tips = [entry for entry in task_lesson if entry.get("task_key") == key]

            tasks.append({
                "task_key": key,
                "label": spec_task.label,
                "part": spec_task.part,
                "description": spec_task.description,
                "sittings": sittings,
                "correct": correct,
                "total": total,
                "accuracy": accuracy,
                "level": level,
                "trend": _trend(series),
                "history": history[-12:],
                "last_attempted": _iso(seen_at),
                "days_since": days_since,
                "focus_score": _focus_score(
                    level=level, target=target_level, attempts=sittings, days_since=days_since,
                ),
                "weakness_tags": [
                    {"tag": tag, "label": WEAKNESS_TAGS[tag], "count": count}
                    for tag, count in tag_counts
                ],
                "tips": tips[:3],
            })

        measured = [t["level"] for t in tasks if t["level"] is not None]
        sittings_total = sum(t["sittings"] for t in tasks)
        categories.append({
            "skill": skill,
            "label": skill.title(),
            "level": round(sum(measured) / len(measured), 1) if measured else None,
            "target": target_level,
            "sittings": sittings_total,
            "tasks_attempted": sum(1 for t in tasks if t["sittings"] > 0),
            "tasks_total": len(tasks),
            "trend": _trend([t["level"] for t in tasks if t["level"] is not None]),
            "tasks": tasks,
        })

    focus = sorted(
        (
            {**task, "skill": category["skill"]}
            for category in categories for task in category["tasks"]
        ),
        key=lambda task: task["focus_score"],
        reverse=True,
    )[:5]

    return {
        "target_level": target_level,
        "categories": categories,
        "focus": [
            {
                "task_key": task["task_key"],
                "label": task["label"],
                "skill": task["skill"],
                "level": task["level"],
                "sittings": task["sittings"],
                "focus_score": task["focus_score"],
                "reason": _focus_reason(task, target_level),
                "tips": task["tips"],
                "weakness_tags": task["weakness_tags"],
            }
            for task in focus
        ],
        "total_sittings": sum(c["sittings"] for c in categories),
        "computed_at": now.isoformat(),
    }


def _focus_reason(task: dict, target: int) -> str:
    """Why this task is near the top. Stated, not implied."""
    if task["sittings"] == 0:
        return "Never attempted — an unmeasured task type is a risk, not a strength."
    if task["level"] is not None and task["level"] < target:
        gap = round(target - task["level"], 1)
        return f"{gap} levels below your target of {target}."
    if task["days_since"] is not None and task["days_since"] >= STALE_AFTER_DAYS:
        return f"Not practised for {task['days_since']} days."
    if task["sittings"] < 2:
        return "Only one sitting so far — one score is not a level."
    return "Close to target; keep it warm."
