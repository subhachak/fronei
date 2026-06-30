"""add is_active to eval_cases

Revision ID: c3d4e5f6a7b8
Revises: bd26b4ea06b3
Create Date: 2026-06-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import column_exists

revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "bd26b4ea06b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not column_exists("eval_cases", "is_active"):
        op.add_column(
            "eval_cases",
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
        )


def downgrade() -> None:
    op.drop_column("eval_cases", "is_active")
