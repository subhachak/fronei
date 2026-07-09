"""add as_of_date column to known_facts

Revision ID: f2d3e4f5a6b7
Revises: f1c2d3e4f5a6
Create Date: 2026-07-09
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "f2d3e4f5a6b7"
down_revision: Union[str, Sequence[str], None] = "f1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(bind, table: str, column: str) -> bool:
    if bind.dialect.name == "postgresql":
        return bool(
            bind.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = :table AND column_name = :column"
                ),
                {"table": table, "column": column},
            ).scalar()
        )
    # SQLite has no information_schema; PRAGMA table_info is the equivalent.
    rows = bind.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def upgrade() -> None:
    bind = op.get_bind()
    # SQLite doesn't support "ADD COLUMN IF NOT EXISTS" -- only Postgres does.
    # Check column existence explicitly so this works on both dialects.
    if _has_column(bind, "known_facts", "as_of_date"):
        return
    op.execute("ALTER TABLE known_facts ADD COLUMN as_of_date TEXT")


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "known_facts", "as_of_date"):
        return
    # SQLite (3.35+) and Postgres both support plain DROP COLUMN.
    op.execute("ALTER TABLE known_facts DROP COLUMN as_of_date")
