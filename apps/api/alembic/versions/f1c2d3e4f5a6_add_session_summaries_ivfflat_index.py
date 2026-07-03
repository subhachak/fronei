"""add IVFFlat index on session_summaries.embedding

Revision ID: f1c2d3e4f5a6
Revises: f0b1c2d3e4f6
Create Date: 2026-07-03
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "f1c2d3e4f5a6"
down_revision: Union[str, Sequence[str], None] = "f0b1c2d3e4f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX_NAME = "ix_session_summaries_embedding_ivfflat"
_MIN_ROWS_FOR_INDEX = 100


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    col_type = bind.execute(
        text(
            """
            SELECT data_type
            FROM information_schema.columns
            WHERE table_name = 'session_summaries'
              AND column_name = 'embedding'
            """
        )
    ).scalar()
    if not col_type or "USER-DEFINED" not in col_type.upper():
        return

    row_count = bind.execute(
        text("SELECT COUNT(*) FROM session_summaries WHERE embedding IS NOT NULL")
    ).scalar() or 0
    if row_count < _MIN_ROWS_FOR_INDEX:
        return

    lists = max(10, min(1000, int(row_count ** 0.5)))

    bind.execute(
        text(
            f"""
            CREATE INDEX IF NOT EXISTS {_INDEX_NAME}
            ON session_summaries
            USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = {lists})
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    bind.execute(text(f"DROP INDEX IF EXISTS {_INDEX_NAME}"))
