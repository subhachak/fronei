"""Seeding and querying the Learn library.

Seeding is idempotent and hash-guarded: a lesson row is rewritten only when its
authored source actually changed. That keeps startup cheap and, more usefully,
means editing one lesson does not churn the `updated_at` of the other twenty-six.
"""
from __future__ import annotations

import hashlib
import json
import logging

from app.db.models import CelpipLesson, SessionLocal
from app.services.agent.models import new_id

logger = logging.getLogger(__name__)


def _hash(lesson: dict) -> str:
    payload = json.dumps(lesson, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def seed_lessons(*, force: bool = False) -> dict:
    from app.services.celpip.lessons_content import LESSONS

    db = SessionLocal()
    created = updated = unchanged = 0
    try:
        for order, lesson in enumerate(LESSONS):
            digest = _hash(lesson)
            row = db.query(CelpipLesson).filter(CelpipLesson.slug == lesson["slug"]).first()
            if row is not None and row.source_hash == digest and not force:
                unchanged += 1
                continue
            if row is None:
                row = CelpipLesson(id=new_id("clesson"), slug=lesson["slug"])
                db.add(row)
                created += 1
            else:
                updated += 1
            row.title = lesson["title"]
            row.category = lesson.get("category", "overview")
            row.skill = lesson.get("skill")
            row.task_key = lesson.get("task_key")
            row.summary = lesson.get("summary", "")
            row.body_markdown = lesson["body"].strip()
            row.weakness_tags_json = json.dumps(lesson.get("tags", []))
            row.estimated_minutes = lesson.get("estimated_minutes", 5)
            row.sort_order = order
            row.source_hash = digest
        db.commit()
        return {"created": created, "updated": updated, "unchanged": unchanged}
    finally:
        db.close()


def list_lessons(db, *, skill: str | None = None, category: str | None = None) -> list[dict]:
    query = db.query(CelpipLesson)
    if skill:
        query = query.filter(CelpipLesson.skill == skill)
    if category:
        query = query.filter(CelpipLesson.category == category)
    rows = query.order_by(CelpipLesson.sort_order.asc()).all()
    return [_serialize(row, include_body=False) for row in rows]


def get_lesson(db, slug: str) -> dict | None:
    row = db.query(CelpipLesson).filter(CelpipLesson.slug == slug).first()
    return _serialize(row, include_body=True) if row else None


def lessons_for_tags(db, tags: list[str]) -> list[dict]:
    """Lessons addressing any of the given weakness tags, in library order."""
    if not tags:
        return []
    wanted = set(tags)
    out = []
    for row in db.query(CelpipLesson).order_by(CelpipLesson.sort_order.asc()).all():
        try:
            covered = set(json.loads(row.weakness_tags_json))
        except (TypeError, ValueError):
            continue
        if covered & wanted:
            entry = _serialize(row, include_body=False)
            entry["matched_tags"] = sorted(covered & wanted)
            out.append(entry)
    return out


def _serialize(row: CelpipLesson, *, include_body: bool) -> dict:
    try:
        tags = json.loads(row.weakness_tags_json)
    except (TypeError, ValueError):
        tags = []
    data = {
        "id": row.id,
        "slug": row.slug,
        "title": row.title,
        "category": row.category,
        "skill": row.skill,
        "task_key": row.task_key,
        "summary": row.summary,
        "weakness_tags": tags,
        "estimated_minutes": row.estimated_minutes,
        "sort_order": row.sort_order,
    }
    if include_body:
        data["body_markdown"] = row.body_markdown
    return data
