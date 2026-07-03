"""add session summaries for cross-session memory

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4
Create Date: 2026-07-03
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "f0a1b2c3d4e5"
down_revision: Union[str, Sequence[str], None] = "e9f0a1b2c3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _pgvector_available() -> bool:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return False
    try:
        available = bind.execute(
            text("SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector')")
        ).scalar()
        if not available:
            return False
        bind.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        return True
    except Exception:
        return False


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    use_vector = _pgvector_available()
    embedding_type = "vector(1536)" if dialect == "postgresql" and use_vector else "TEXT"
    created_at_default = "now()" if dialect == "postgresql" else "CURRENT_TIMESTAMP"

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS session_summaries (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            summary TEXT NOT NULL,
            embedding {embedding_type},
            created_at TIMESTAMP NOT NULL DEFAULT {created_at_default}
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_session_summaries_user_id ON session_summaries (user_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_session_summaries_conversation_id "
        "ON session_summaries (conversation_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_session_summaries_conversation_id")
    op.execute("DROP INDEX IF EXISTS ix_session_summaries_user_id")
    op.execute("DROP TABLE IF EXISTS session_summaries")
