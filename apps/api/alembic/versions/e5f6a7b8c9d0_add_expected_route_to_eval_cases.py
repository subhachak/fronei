"""add expected_route to eval_cases

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import column_exists

revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not column_exists("eval_cases", "expected_route"):
        op.add_column("eval_cases", sa.Column("expected_route", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("eval_cases", "expected_route")
