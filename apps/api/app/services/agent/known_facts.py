from __future__ import annotations

import logging

from sqlalchemy import text

from app.services.agent.models import new_id

logger = logging.getLogger(__name__)


def upsert_fact(
    user_id: str,
    entity_id: str,
    entity_type: str,
    fact_key: str,
    fact_value: str,
    *,
    db,
    source_conversation_id: str | None = None,
    confidence: float = 1.0,
) -> None:
    """Insert or update a structured fact.

    Best-effort: failures are logged and swallowed so fact storage never breaks
    the turn path that eventually calls it.
    """
    try:
        if not all(str(value).strip() for value in [user_id, entity_id, entity_type, fact_key, fact_value]):
            return
        db.execute(
            text(
                """
                INSERT INTO known_facts (
                    id,
                    user_id,
                    entity_id,
                    entity_type,
                    fact_key,
                    fact_value,
                    source_conversation_id,
                    confidence,
                    last_verified_at
                )
                VALUES (
                    :id,
                    :user_id,
                    :entity_id,
                    :entity_type,
                    :fact_key,
                    :fact_value,
                    :source_conversation_id,
                    :confidence,
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT (user_id, entity_id, fact_key)
                DO UPDATE SET
                    entity_type = excluded.entity_type,
                    fact_value = excluded.fact_value,
                    source_conversation_id = excluded.source_conversation_id,
                    confidence = excluded.confidence,
                    last_verified_at = CURRENT_TIMESTAMP
                """
            ),
            {
                "id": new_id("fact"),
                "user_id": user_id,
                "entity_id": entity_id,
                "entity_type": entity_type,
                "fact_key": fact_key,
                "fact_value": fact_value,
                "source_conversation_id": source_conversation_id,
                "confidence": max(0.0, min(1.0, float(confidence))),
            },
        )
        db.commit()
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("known_facts_error", extra={"error": str(exc)[:500], "operation": "upsert"})


def get_facts(user_id: str, entity_id: str, *, db) -> list[dict]:
    """Return all facts for one entity."""
    try:
        rows = db.execute(
            text(
                """
                SELECT fact_key, fact_value, confidence
                FROM known_facts
                WHERE user_id = :user_id AND entity_id = :entity_id
                ORDER BY fact_key
                """
            ),
            {"user_id": user_id, "entity_id": entity_id},
        ).mappings()
        return [_fact_dict(row) for row in rows]
    except Exception as exc:
        logger.warning("known_facts_error", extra={"error": str(exc)[:500], "operation": "get"})
        return []


def get_facts_for_type(user_id: str, entity_type: str, *, db) -> list[dict]:
    """Return facts across all entities of a type."""
    try:
        rows = db.execute(
            text(
                """
                SELECT entity_id, fact_key, fact_value, confidence
                FROM known_facts
                WHERE user_id = :user_id AND entity_type = :entity_type
                ORDER BY entity_id, fact_key
                """
            ),
            {"user_id": user_id, "entity_type": entity_type},
        ).mappings()
        return [
            {
                "entity_id": str(row["entity_id"]),
                **_fact_dict(row),
            }
            for row in rows
        ]
    except Exception as exc:
        logger.warning("known_facts_error", extra={"error": str(exc)[:500], "operation": "get_for_type"})
        return []


def _fact_dict(row) -> dict:
    return {
        "fact_key": str(row["fact_key"]),
        "fact_value": str(row["fact_value"]),
        "confidence": float(row["confidence"]),
    }
