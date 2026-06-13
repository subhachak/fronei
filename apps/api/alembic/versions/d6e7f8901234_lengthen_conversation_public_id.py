"""lengthen conversation public ids

Revision ID: d6e7f8901234
Revises: c5d6e7f89012
Create Date: 2026-06-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import column_exists


revision: str = "d6e7f8901234"
down_revision: Union[str, Sequence[str], None] = "c5d6e7f89012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if column_exists("conversations", "public_id"):
        with op.batch_alter_table("conversations", schema=None) as batch_op:
            batch_op.alter_column(
                "public_id",
                existing_type=sa.String(16),
                type_=sa.String(24),
                existing_nullable=False,
            )


def downgrade() -> None:
    if column_exists("conversations", "public_id"):
        with op.batch_alter_table("conversations", schema=None) as batch_op:
            batch_op.alter_column(
                "public_id",
                existing_type=sa.String(24),
                type_=sa.String(16),
                existing_nullable=False,
            )
