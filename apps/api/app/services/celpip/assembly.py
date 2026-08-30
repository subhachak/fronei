"""Assembling a test from the bank.

Selection is least-recently-served first, then least-served, then newest. That
ordering matters more than it looks: a learner sitting four mocks in the final
week must not meet the same Reading passage twice, and an item they have
already seen inflates their score and hides the weakness the mock was meant to
find.

A full mock also plants unscored content, exactly as the real test does. It is
delivered indistinguishably and silently dropped at scoring time.
"""
from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timedelta, timezone

from app.db.models import (
    CelpipAttempt,
    CelpipQuestion,
    CelpipTest,
    CelpipTestItem,
    SessionLocal,
)
from app.services.agent.models import new_id
from app.services.celpip.spec import (
    ALL_TASKS,
    TASKS_BY_KEY,
    TASKS_BY_SKILL,
    components_for_test_type,
)

logger = logging.getLogger(__name__)

# How recently an item counts as "already seen" for a learner. Inside this
# window it is skipped entirely if any unseen item exists.
RECENTLY_SEEN_DAYS = 21

# Diagnostic coverage: one item per task type would be a full test. A
# diagnostic samples the parts that discriminate most while staying inside
# roughly a third of the time.
DIAGNOSTIC_TASKS: dict[str, tuple[str, ...]] = {
    "listening": ("listening_problem_solving", "listening_viewpoints"),
    "reading": ("reading_correspondence", "reading_viewpoints"),
    "writing": ("writing_email",),
    "speaking": ("speaking_advice", "speaking_opinions"),
}


class NotEnoughItems(RuntimeError):
    """The bank cannot cover the requested test."""

    def __init__(self, shortfalls: dict[str, int]):
        self.shortfalls = shortfalls
        detail = ", ".join(f"{task} (need {n} more)" for task, n in shortfalls.items())
        super().__init__(f"The question bank is short for: {detail}")


def _seen_question_ids(db, user_id: str, since_days: int = RECENTLY_SEEN_DAYS) -> set[str]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    rows = (
        db.query(CelpipTestItem.question_id)
        .join(CelpipAttempt, CelpipAttempt.test_id == CelpipTestItem.test_id)
        .filter(CelpipAttempt.user_id == user_id)
        .filter(CelpipAttempt.created_at >= cutoff)
        .all()
    )
    return {row[0] for row in rows}


def _pick(db, task_key: str, count: int, *, exclude: set[str], target_level: int) -> list[CelpipQuestion]:
    """Choose `count` servable items for a task, avoiding recently seen ones."""
    pool = (
        db.query(CelpipQuestion)
        .filter(CelpipQuestion.task_key == task_key)
        .filter(CelpipQuestion.status == "ready")
        .all()
    )
    fresh = [q for q in pool if q.id not in exclude]
    # Fall back to seen items only when there is nothing else -- a repeated
    # item is worth more than a missing section, but only just.
    candidates = fresh or [q for q in pool if q.id in exclude]

    def sort_key(q: CelpipQuestion):
        return (
            q.last_served_at or datetime.min,
            q.times_served,
            # Prefer the difficulty the learner is aiming at, then anything.
            abs(q.difficulty - target_level),
            -(q.approved_at.timestamp() if q.approved_at else 0),
        )

    candidates.sort(key=sort_key)
    return candidates[:count]


def available_counts(db) -> dict[str, int]:
    """Servable item count per task type, for the Question Bank dashboard."""
    counts = {task.key: 0 for task in ALL_TASKS}
    rows = (
        db.query(CelpipQuestion.task_key, CelpipQuestion.status)
        .filter(CelpipQuestion.status == "ready")
        .all()
    )
    for task_key, _ in rows:
        if task_key in counts:
            counts[task_key] += 1
    return counts


def _task_plan(mode: str, test_type: str, *, components: list[str] | None, task_keys: list[str] | None) -> list[str]:
    """Which task types this test delivers, in official order."""
    if mode == "diagnostic":
        skills = components or list(components_for_test_type(test_type))
        return [key for skill in skills for key in DIAGNOSTIC_TASKS.get(skill, ())]
    if mode == "custom" or mode == "single_task":
        return [k for k in (task_keys or []) if k in TASKS_BY_KEY]
    if mode == "component":
        skills = components or []
        return [t.key for skill in skills for t in TASKS_BY_SKILL.get(skill, ())]
    # full / full_ls
    skills = components or list(components_for_test_type("general_ls" if mode == "full_ls" else "general"))
    return [t.key for skill in skills for t in TASKS_BY_SKILL.get(skill, ())]


def assemble_test(
    *,
    user_id: str,
    mode: str,
    practice_mode: str = "timed",
    test_type: str = "general",
    components: list[str] | None = None,
    task_keys: list[str] | None = None,
    repeats: int = 1,
    target_level: int = 9,
    label: str = "",
    include_unscored: bool | None = None,
) -> dict:
    """Build a test and return its id plus what it contains."""
    plan = _task_plan(mode, test_type, components=components, task_keys=task_keys)
    if not plan:
        raise ValueError("no task types selected for this test")

    # Only a full simulation plants unscored content; a targeted drill has no
    # reason to waste the learner's time on questions that will not count.
    if include_unscored is None:
        include_unscored = mode in {"full", "full_ls"} and practice_mode == "simulation"

    db = SessionLocal()
    try:
        exclude = _seen_question_ids(db, user_id)
        chosen: list[tuple[str, CelpipQuestion]] = []
        shortfalls: dict[str, int] = {}

        for task_key in plan:
            wanted = max(1, repeats)
            picked = _pick(db, task_key, wanted, exclude=exclude, target_level=target_level)
            if len(picked) < wanted:
                shortfalls[task_key] = wanted - len(picked)
            for question in picked:
                exclude.add(question.id)
                chosen.append((task_key, question))

        if shortfalls:
            raise NotEnoughItems(shortfalls)

        skills: list[str] = []
        for task_key, _ in chosen:
            skill = TASKS_BY_KEY[task_key].skill
            if skill not in skills:
                skills.append(skill)

        test = CelpipTest(
            id=new_id("ctest"),
            user_id=user_id,
            label=label or _default_label(mode, skills),
            mode=mode,
            components_json=json.dumps(skills),
            practice_mode=practice_mode,
            target_level=target_level,
        )
        db.add(test)

        unscored_index = -1
        if include_unscored:
            receptive = [i for i, (key, _) in enumerate(chosen)
                         if TASKS_BY_KEY[key].skill in {"listening", "reading"}]
            if receptive:
                unscored_index = random.choice(receptive)

        for position, (task_key, question) in enumerate(chosen):
            task = TASKS_BY_KEY[task_key]
            db.add(CelpipTestItem(
                id=new_id("citem"),
                test_id=test.id,
                question_id=question.id,
                skill=task.skill,
                task_key=task_key,
                position=position,
                is_unscored=(position == unscored_index),
            ))
            question.times_served += 1
            question.last_served_at = datetime.now(timezone.utc)

        db.commit()
        return {
            "test_id": test.id,
            "label": test.label,
            "mode": mode,
            "practice_mode": practice_mode,
            "components": skills,
            "item_count": len(chosen),
            "task_keys": [k for k, _ in chosen],
        }
    finally:
        db.close()


def _default_label(mode: str, skills: list[str]) -> str:
    if mode == "full":
        return "Full mock test"
    if mode == "full_ls":
        return "Full mock test (Listening & Speaking)"
    if mode == "diagnostic":
        return "Diagnostic assessment"
    if mode == "component" and len(skills) == 1:
        return f"{skills[0].title()} component test"
    return "Practice set"
