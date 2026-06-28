"""add turn feedback column

Revision ID: cc1dd2ee3ff4
Revises: b1c2d3e4f5a6, f4a5b6c7d890
Create Date: 2026-06-28

Merge migration: collapses the two pre-existing branch heads
(b1c2d3e4f5a6 add_maintenance_jobs, f4a5b6c7d890 add_plan_json)
into a single head, and adds the turns.feedback column.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cc1dd2ee3ff4"
down_revision: Union[tuple[str, str], None] = ("b1c2d3e4f5a6", "f4a5b6c7d890")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "turns",
        sa.Column("feedback", sa.String(16), nullable=True, server_default=None),
    )


def downgrade() -> None:
    op.drop_column("turns", "feedback")
