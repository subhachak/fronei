"""add offered_deep_research column to turns

Revision ID: 14686aa3846c
Revises: d35cb001c0ef
Create Date: 2026-07-10
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import column_exists


revision: str = "14686aa3846c"
down_revision: Union[str, Sequence[str], None] = "d35cb001c0ef"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not column_exists("turns", "offered_deep_research"):
        op.add_column(
            "turns",
            sa.Column("offered_deep_research", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    if column_exists("turns", "offered_deep_research"):
        op.drop_column("turns", "offered_deep_research")
