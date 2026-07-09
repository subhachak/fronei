"""add had_unresolved_gaps column to turns

Revision ID: d35cb001c0ef
Revises: f3a4b5c6d7e8
Create Date: 2026-07-09
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import column_exists


revision: str = "d35cb001c0ef"
down_revision: Union[str, Sequence[str], None] = "f3a4b5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not column_exists("turns", "had_unresolved_gaps"):
        op.add_column(
            "turns",
            sa.Column("had_unresolved_gaps", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    if column_exists("turns", "had_unresolved_gaps"):
        op.drop_column("turns", "had_unresolved_gaps")
