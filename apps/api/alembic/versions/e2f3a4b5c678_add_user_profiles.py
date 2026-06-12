"""add user profiles

Revision ID: e2f3a4b5c678
Revises: e1f2a3b4c567
Create Date: 2026-06-11
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import index_exists, table_exists


revision: str = "e2f3a4b5c678"
down_revision: Union[str, Sequence[str], None] = "e1f2a3b4c567"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not table_exists("user_profiles"):
        op.create_table(
            "user_profiles",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.String(128), nullable=False),
            sa.Column("profile_json", sa.Text(), nullable=True, server_default="{}"),
            sa.Column("last_consolidated_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            if_not_exists=True,
        )
    if not index_exists("user_profiles", "ix_user_profiles_user_id"):
        op.create_index("ix_user_profiles_user_id", "user_profiles", ["user_id"], unique=True)


def downgrade() -> None:
    if index_exists("user_profiles", "ix_user_profiles_user_id"):
        op.drop_index("ix_user_profiles_user_id", table_name="user_profiles")
    if table_exists("user_profiles"):
        op.drop_table("user_profiles")
