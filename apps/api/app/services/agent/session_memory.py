from __future__ import annotations

import json
import logging
import time

from sqlalchemy import text

from app.services.agent import model_client
from app.services.agent.models import new_id

logger = logging.getLogger(__name__)

RECALL_TIMEOUT_MS = 1_500


def save_session_summary(user_id: str, conversation_id: str, summary: str, db) -> None:
    """Embed summary and insert into session_summaries.

    Best-effort by design: failures are logged and never propagated to the
    caller, because memory writes must not affect the user turn.
    """
    if not summary.strip():
        return
    try:
        if _dialect_name(db) != "postgresql":
            return
        embedding = model_client.embed(summary, role="embedding")
        embedding_payload = _embedding_payload(embedding, db)
        db.execute(
            text(
                """
                INSERT INTO session_summaries
                    (id, user_id, conversation_id, summary, embedding)
                VALUES
                    (:id, :user_id, :conversation_id, :summary, :embedding)
                """
            ),
            {
                "id": new_id("ssum"),
                "user_id": user_id,
                "conversation_id": conversation_id,
                "summary": summary,
                "embedding": embedding_payload,
            },
        )
        db.commit()
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("session_memory_error", extra={"error": str(exc)[:500], "operation": "save"})


def recall_similar_sessions(user_id: str, query: str, *, db=None, limit: int = 3) -> list[str]:
    """Return up to `limit` summary strings most similar to query.

    Returns [] on any failure. SQLite/local dev returns [] immediately because
    pgvector similarity is unavailable there.
    """
    try:
        if db is None or _dialect_name(db) != "postgresql":
            return []
        embedding = model_client.embed(query, role="embedding")
        started = time.perf_counter()
        db.execute(text("SET LOCAL statement_timeout = '1500ms'"))
        rows = db.execute(
            text(
                """
                SELECT summary
                FROM session_summaries
                WHERE user_id = :user_id AND embedding IS NOT NULL
                ORDER BY embedding <=> CAST(:embedding AS vector)
                LIMIT :limit
                """
            ),
            {
                "user_id": user_id,
                "embedding": _vector_literal(embedding),
                "limit": max(1, min(20, int(limit))),
            },
        ).fetchall()
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if elapsed_ms > RECALL_TIMEOUT_MS:
            logger.warning(
                "session_memory_recall_slow",
                extra={"elapsed_ms": elapsed_ms, "limit": limit},
            )
            return []
        return [str(row[0]) for row in rows[:limit] if row and row[0]]
    except Exception as exc:
        logger.warning("session_memory_error", extra={"error": str(exc)[:500], "operation": "recall"})
        return []


def summarize_conversation(messages: list[dict]) -> str:
    """Produce a short factual summary of a completed conversation."""
    if not messages:
        return ""
    payload = json.dumps(messages[-12:], ensure_ascii=False, default=str)
    response = model_client.simple_completion(
        "Summarize the conversation facts and decisions in 3-5 concise bullets. Do not invent details.",
        payload,
        role="summary",
        max_tokens=320,
        timeout_s=12,
    )
    return response.text.strip()


def _embedding_payload(embedding: list[float], db) -> str:
    if _dialect_name(db) == "postgresql":
        return _vector_literal(embedding)
    return json.dumps(embedding)


def _dialect_name(db) -> str:
    try:
        return str(db.get_bind().dialect.name)
    except AttributeError:
        bind = getattr(db, "bind", None)
        dialect = getattr(bind, "dialect", None)
        return str(getattr(dialect, "name", "") or "")


def _vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(f"{float(value):.8f}" for value in embedding) + "]"
