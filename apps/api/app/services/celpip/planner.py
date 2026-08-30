"""The one-month study plan.

Deterministic on purpose. Everything a plan needs -- days until the test,
hours available, which task types have been attempted, which weakness tags the
scorer measured -- is already in the database, so scheduling is arithmetic over
a template rather than a model call. That makes it reproducible, instant, free
to rebalance after every attempt, and explainable line by line ("this is here
because your last two Speaking evaluations both tagged idea_development").

Two behaviours matter more than the template:

* **Rebalancing preserves history.** A rebalance replaces only its own
  untouched future items. Anything completed, in progress, or already skipped
  survives, so the plan is a record as well as a schedule.
* **Missed days move forward selectively.** Doubling tomorrow's load after a
  missed day is how plans get abandoned in week two. Missed items are ranked
  and only the highest-value ones are carried, inside the day's real budget.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from app.db.models import (
    CelpipAttempt,
    CelpipEvaluation,
    CelpipLesson,
    CelpipProfile,
    CelpipStudyPlanItem,
)
from app.services.agent.models import new_id
from app.services.celpip.spec import (
    TASKS_BY_KEY,
    TASKS_BY_SKILL,
    WEAKNESS_TAGS,
    components_for_test_type,
    task_keys_for_test_type,
)

# Minutes a typical activity takes, used to fit a day inside its budget.
ACTIVITY_MINUTES = {
    "diagnostic": 60,
    "lesson": 12,
    "drill": 20,
    "vocabulary": 15,
    "timed_component": 55,
    "full_mock": 180,
    "simulation": 180,
    "review": 25,
}

# A day never gets scheduled past this, whatever the profile claims, because a
# four-hour evening after work is aspiration rather than plan.
MAX_DAILY_MINUTES = 240
# The last two days before the test are deliberately light.
TAPER_DAYS = 2
TAPER_MINUTES = 40


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _loads(raw: str, default):
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _daily_budget(profile: CelpipProfile, day: date, days_left: int) -> int:
    if days_left <= TAPER_DAYS:
        return TAPER_MINUTES
    hours = profile.weekend_hours if day.weekday() >= 5 else profile.weekday_hours
    return int(min(MAX_DAILY_MINUTES, max(0, hours * 60)))


def measured_weaknesses(db, user_id: str, *, limit: int = 8) -> list[tuple[str, int]]:
    """Weakness tags across scored attempts, most frequent first.

    Counts every evaluation, not just the latest: a tag that appeared once is
    noise, one that appears in four evaluations is the thing to drill.
    """
    attempt_ids = [
        row[0] for row in
        db.query(CelpipAttempt.id).filter(CelpipAttempt.user_id == user_id).all()
    ]
    if not attempt_ids:
        return []
    counts: dict[str, int] = {}
    evaluations = (
        db.query(CelpipEvaluation)
        .filter(CelpipEvaluation.attempt_id.in_(attempt_ids))
        .filter(CelpipEvaluation.status == "complete")
        .all()
    )
    for evaluation in evaluations:
        for tag in _loads(evaluation.weakness_tags_json, []):
            if tag in WEAKNESS_TAGS:
                counts[tag] = counts.get(tag, 0) + 1
    for attempt in db.query(CelpipAttempt).filter(CelpipAttempt.id.in_(attempt_ids)).all():
        for component in _loads(attempt.results_json, {}).values():
            for tag in (component or {}).get("weakness_tags") or []:
                if tag in WEAKNESS_TAGS:
                    counts[tag] = counts.get(tag, 0) + 1
    return sorted(counts.items(), key=lambda pair: pair[1], reverse=True)[:limit]


def weakest_tasks(db, user_id: str, *, limit: int = 4) -> list[str]:
    """Task types with the lowest measured level, worst first."""
    attempt_ids = [
        row[0] for row in
        db.query(CelpipAttempt.id).filter(CelpipAttempt.user_id == user_id).all()
    ]
    if not attempt_ids:
        return []
    scores: dict[str, list[float]] = {}
    for evaluation in (
        db.query(CelpipEvaluation)
        .filter(CelpipEvaluation.attempt_id.in_(attempt_ids))
        .filter(CelpipEvaluation.status == "complete")
        .all()
    ):
        if evaluation.level_low is None or not evaluation.task_key:
            continue
        scores.setdefault(evaluation.task_key, []).append(
            (evaluation.level_low + (evaluation.level_high or evaluation.level_low)) / 2
        )
    ranked = sorted(
        ((key, sum(values) / len(values)) for key, values in scores.items()),
        key=lambda pair: pair[1],
    )
    return [key for key, _ in ranked[:limit]]


def _lessons_for(db, tags: list[str], task_keys: list[str]) -> dict[str, CelpipLesson]:
    """Map each tag/task to a lesson that addresses it, when one exists."""
    lessons = db.query(CelpipLesson).all()
    by_tag: dict[str, CelpipLesson] = {}
    for lesson in lessons:
        for tag in _loads(lesson.weakness_tags_json, []):
            by_tag.setdefault(tag, lesson)
        if lesson.task_key:
            by_tag.setdefault(f"task:{lesson.task_key}", lesson)
    out: dict[str, CelpipLesson] = {}
    for tag in tags:
        if tag in by_tag:
            out[tag] = by_tag[tag]
    for key in task_keys:
        marker = f"task:{key}"
        if marker in by_tag:
            out[marker] = by_tag[marker]
    return out


def _item(
    *, user_id: str, day: date, week: int, activity: str, title: str, rationale: str,
    generation: int, skill: str | None = None, task_keys: list[str] | None = None,
    tags: list[str] | None = None, lesson_id: str | None = None, minutes: int | None = None,
) -> CelpipStudyPlanItem:
    return CelpipStudyPlanItem(
        id=new_id("cplan"),
        user_id=user_id,
        scheduled_for=day,
        week_index=week,
        activity_type=activity,
        title=title[:255],
        rationale=rationale,
        skill=skill,
        task_keys_json=json.dumps(task_keys or []),
        weakness_tags_json=json.dumps(tags or []),
        lesson_id=lesson_id,
        estimated_minutes=minutes or ACTIVITY_MINUTES.get(activity, 20),
        status="pending",
        plan_generation=generation,
    )


def build_plan(db, *, user_id: str, generation: int = 1) -> list[CelpipStudyPlanItem]:
    """Generate the schedule from today to the test date."""
    profile = db.query(CelpipProfile).filter(CelpipProfile.user_id == user_id).first()
    if profile is None:
        raise ValueError("no CELPIP profile; complete onboarding first")

    start = _today()
    end = profile.test_date or (start + timedelta(days=28))
    total_days = max(1, (end - start).days)
    skills = list(components_for_test_type(profile.test_type))
    all_tasks = task_keys_for_test_type(profile.test_type)

    tags = [tag for tag, _ in measured_weaknesses(db, user_id)]
    if not tags:
        tags = [t for t in _loads(profile.self_reported_weaknesses_json, []) if t in WEAKNESS_TAGS]
    weak_tasks = weakest_tasks(db, user_id) or all_tasks[:4]
    lessons = _lessons_for(db, tags, all_tasks)

    attempted = {
        row[0] for row in
        db.query(CelpipAttempt.id).filter(CelpipAttempt.user_id == user_id).all()
    }
    needs_diagnostic = profile.diagnostic_attempt_id is None and not attempted

    # Week boundaries scale to whatever time is actually left. A learner with
    # 11 days does not get four weeks of plan compressed into a fortnight; they
    # get the same four phases, proportionally shorter.
    phase_ends = [round(total_days * f) for f in (0.25, 0.5, 0.75, 1.0)]

    def phase_for(offset: int) -> int:
        for index, boundary in enumerate(phase_ends, start=1):
            if offset < boundary:
                return index
        return 4

    items: list[CelpipStudyPlanItem] = []
    task_cycle = list(all_tasks)
    lesson_cycle = [t for t in all_tasks]

    for offset in range(total_days):
        day = start + timedelta(days=offset)
        days_left = total_days - offset
        budget = _daily_budget(profile, day, days_left)
        if budget <= 0:
            continue
        week = phase_for(offset)
        spent = 0

        def add(item: CelpipStudyPlanItem) -> bool:
            nonlocal spent
            if spent + item.estimated_minutes > budget:
                return False
            items.append(item)
            spent += item.estimated_minutes
            return True

        if offset == 0 and needs_diagnostic:
            add(_item(
                user_id=user_id, day=day, week=1, activity="diagnostic",
                title="Diagnostic assessment",
                rationale=(
                    "Nothing else in this plan is targeted until there is a measured "
                    "starting point for each component."
                ),
                generation=generation, task_keys=[], minutes=ACTIVITY_MINUTES["diagnostic"],
            ))

        if week == 1:
            # Meet every task type once, with its strategy lesson first.
            if lesson_cycle:
                task_key = lesson_cycle.pop(0)
                task = TASKS_BY_KEY[task_key]
                lesson = lessons.get(f"task:{task_key}")
                add(_item(
                    user_id=user_id, day=day, week=week, activity="lesson",
                    title=f"Strategy: {task.label}",
                    rationale="Week one covers the format and approach for every task type once.",
                    generation=generation, skill=task.skill, task_keys=[task_key],
                    lesson_id=lesson.id if lesson else None,
                ))
                add(_item(
                    user_id=user_id, day=day, week=week, activity="drill",
                    title=f"Learn-mode practice: {task.label}",
                    rationale="First attempt at this task type, untimed, with feedback after each answer.",
                    generation=generation, skill=task.skill, task_keys=[task_key],
                ))
        elif week == 2:
            # Drill the measured weaknesses, plus vocabulary and one timed run.
            focus = weak_tasks[offset % len(weak_tasks)] if weak_tasks else task_cycle[0]
            task = TASKS_BY_KEY[focus]
            focus_tags = tags[:2]
            add(_item(
                user_id=user_id, day=day, week=week, activity="drill",
                title=f"Timed drill: {task.label}",
                rationale=(
                    "Lowest measured task type so far."
                    + (f" Recurring: {', '.join(focus_tags)}." if focus_tags else "")
                ),
                generation=generation, skill=task.skill, task_keys=[focus], tags=focus_tags,
            ))
            if tags:
                tag = tags[offset % len(tags)]
                lesson = lessons.get(tag)
                add(_item(
                    user_id=user_id, day=day, week=week, activity="vocabulary" if tag in {
                        "vocabulary_range", "word_choice"} else "review",
                    title=f"Fix: {WEAKNESS_TAGS[tag].rstrip('.')}",
                    rationale="Tagged in your scored responses; drilled directly rather than hoped away.",
                    generation=generation, tags=[tag], lesson_id=lesson.id if lesson else None,
                ))
            if day.weekday() in {2, 5}:
                skill = skills[offset % len(skills)]
                add(_item(
                    user_id=user_id, day=day, week=week, activity="timed_component",
                    title=f"{skill.title()} component, under time",
                    rationale="Timing is scored separately from accuracy; both need practice.",
                    generation=generation, skill=skill,
                    task_keys=[t.key for t in TASKS_BY_SKILL[skill]],
                ))
        elif week == 3:
            if day.weekday() in {5, 6}:
                add(_item(
                    user_id=user_id, day=day, week=week, activity="simulation",
                    title="Full exam simulation",
                    rationale="Two full simulations this week build the stamina the real sitting needs.",
                    generation=generation,
                    task_keys=all_tasks, minutes=ACTIVITY_MINUTES["simulation"],
                ))
            else:
                skill = skills[offset % len(skills)]
                add(_item(
                    user_id=user_id, day=day, week=week, activity="timed_component",
                    title=f"{skill.title()} under exam timing",
                    rationale="Component-level timing before the full mocks in the final week.",
                    generation=generation, skill=skill,
                    task_keys=[t.key for t in TASKS_BY_SKILL[skill]],
                ))
                if weak_tasks:
                    focus = weak_tasks[offset % len(weak_tasks)]
                    add(_item(
                        user_id=user_id, day=day, week=week, activity="review",
                        title=f"Correction pass: {TASKS_BY_KEY[focus].label}",
                        rationale="Re-attempt the responses that scored lowest, against the feedback.",
                        generation=generation, skill=TASKS_BY_KEY[focus].skill, task_keys=[focus],
                    ))
        else:
            if days_left <= TAPER_DAYS:
                add(_item(
                    user_id=user_id, day=day, week=week, activity="review",
                    title="Light review only",
                    rationale=(
                        "Deliberately light. Cramming in the last two days lowers scores "
                        "more often than it raises them."
                    ),
                    generation=generation, minutes=TAPER_MINUTES,
                ))
            elif day.weekday() in {5, 6}:
                add(_item(
                    user_id=user_id, day=day, week=week, activity="full_mock",
                    title="Full mock test",
                    rationale="Final-week mocks measure consistency, not new learning.",
                    generation=generation, task_keys=all_tasks,
                    minutes=ACTIVITY_MINUTES["full_mock"],
                ))
            else:
                focus = weak_tasks[offset % len(weak_tasks)] if weak_tasks else all_tasks[0]
                add(_item(
                    user_id=user_id, day=day, week=week, activity="drill",
                    title=f"Final polish: {TASKS_BY_KEY[focus].label}",
                    rationale="Last targeted work on the weakest task before the test.",
                    generation=generation, skill=TASKS_BY_KEY[focus].skill, task_keys=[focus],
                ))

    return items


def regenerate_plan(db, *, user_id: str) -> dict:
    """Rebuild the future plan, preserving everything already acted on."""
    existing = (
        db.query(CelpipStudyPlanItem)
        .filter(CelpipStudyPlanItem.user_id == user_id)
        .all()
    )
    generation = max([i.plan_generation for i in existing], default=0) + 1

    today = _today()
    removed = 0
    for item in existing:
        # Only future, untouched, planner-authored items are replaced. A day
        # the learner already worked stays in the record.
        if item.scheduled_for >= today and item.status == "pending":
            db.delete(item)
            removed += 1

    created = build_plan(db, user_id=user_id, generation=generation)
    for item in created:
        db.add(item)
    db.commit()
    return {"generation": generation, "created": len(created), "replaced": removed}


def roll_forward_missed(db, *, user_id: str) -> dict:
    """Carry yesterday's unfinished work forward without doubling today.

    Ranked by value: a missed full mock or diagnostic matters; a missed
    vocabulary drill does not survive the triage. Anything that does not fit
    today's remaining budget is marked skipped rather than silently piling up.
    """
    today = _today()
    overdue = (
        db.query(CelpipStudyPlanItem)
        .filter(CelpipStudyPlanItem.user_id == user_id)
        .filter(CelpipStudyPlanItem.status == "pending")
        .filter(CelpipStudyPlanItem.scheduled_for < today)
        .order_by(CelpipStudyPlanItem.scheduled_for.asc())
        .all()
    )
    if not overdue:
        return {"moved": 0, "skipped": 0}

    profile = db.query(CelpipProfile).filter(CelpipProfile.user_id == user_id).first()
    days_left = ((profile.test_date - today).days if profile and profile.test_date else 28)
    budget = _daily_budget(profile, today, days_left) if profile else 60

    already = sum(
        i.estimated_minutes for i in
        db.query(CelpipStudyPlanItem)
        .filter(CelpipStudyPlanItem.user_id == user_id)
        .filter(CelpipStudyPlanItem.scheduled_for == today)
        .filter(CelpipStudyPlanItem.status.in_(("pending", "in_progress")))
        .all()
    )
    remaining = max(0, budget - already)

    priority = {
        "diagnostic": 0, "full_mock": 1, "simulation": 1, "timed_component": 2,
        "drill": 3, "review": 4, "lesson": 5, "vocabulary": 6,
    }
    overdue.sort(key=lambda i: (priority.get(i.activity_type, 9), i.scheduled_for))

    moved = skipped = 0
    for item in overdue:
        history = _loads(item.reschedule_history_json, [])
        if item.estimated_minutes <= remaining:
            history.append({
                "from": item.scheduled_for.isoformat(),
                "to": today.isoformat(),
                "reason": "missed; carried forward",
            })
            item.scheduled_for = today
            item.reschedule_history_json = json.dumps(history)
            remaining -= item.estimated_minutes
            moved += 1
        else:
            history.append({
                "from": item.scheduled_for.isoformat(),
                "to": None,
                "reason": "missed; dropped to protect today's workload",
            })
            item.status = "skipped"
            item.reschedule_history_json = json.dumps(history)
            skipped += 1
    db.commit()
    return {"moved": moved, "skipped": skipped}
