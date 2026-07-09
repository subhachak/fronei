"""add as_of_date column to known_facts

Revision ID: f2d3e4f5a6b7
Revises: f1c2d3e4f5a6
Create Date: 2026-07-09
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "f2d3e4f5a6b7"
down_revision: Union[str, Sequence[str], None] = "f1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE known_facts ADD COLUMN IF NOT EXISTS as_of_date TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE known_facts DROP COLUMN IF EXISTS as_of_date")
