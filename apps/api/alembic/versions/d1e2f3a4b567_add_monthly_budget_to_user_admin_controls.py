"""add monthly_budget_usd to user_admin_controls

Revision ID: d1e2f3a4b567
Revises: c3d4e5f6a789
Create Date: 2026-06-11
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import column_exists


revision: str = "d1e2f3a4b567"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a789"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if column_exists("user_admin_controls", "monthly_budget_usd"):
        return
    with op.batch_alter_table("user_admin_controls", schema=None) as batch_op:
        batch_op.add_column(sa.Column("monthly_budget_usd", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("user_admin_controls", schema=None) as batch_op:
        batch_op.drop_column("monthly_budget_usd")
