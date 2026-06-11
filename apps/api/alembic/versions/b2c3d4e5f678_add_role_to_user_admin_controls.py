"""add role to user_admin_controls

Revision ID: b2c3d4e5f678
Revises: a7b8c9d0e123
Create Date: 2026-06-10
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2c3d4e5f678"
down_revision: Union[str, Sequence[str], None] = "a7b8c9d0e123"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("user_admin_controls", schema=None) as batch_op:
        batch_op.add_column(sa.Column("role", sa.String(32), nullable=True, server_default="user"))


def downgrade() -> None:
    with op.batch_alter_table("user_admin_controls", schema=None) as batch_op:
        batch_op.drop_column("role")
