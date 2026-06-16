"""phase cc job checkpoint updated_at onupdate note

Revision ID: d0e1f2a3b4c5
Revises: c0d1e2f3a4b5
Create Date: 2026-06-16
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "d0e1f2a3b4c5"
down_revision: Union[str, Sequence[str], None] = "c0d1e2f3a4b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite/Postgres do not need a schema change for SQLAlchemy's Python-side
    # onupdate hook. The ORM config owns updated_at mutation for checkpoints.
    op.execute("SELECT 1")


def downgrade() -> None:
    op.execute("SELECT 1")
