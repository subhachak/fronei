"""add admin settings

Revision ID: e3f4a5b6c789
Revises: e2f3a4b5c678
Create Date: 2026-06-12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import index_exists, table_exists


revision: str = "e3f4a5b6c789"
down_revision: Union[str, Sequence[str], None] = "e2f3a4b5c678"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not table_exists("admin_settings"):
        op.create_table(
            "admin_settings",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("key", sa.String(128), nullable=False),
            sa.Column("value_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            if_not_exists=True,
        )
    if not index_exists("admin_settings", "ix_admin_settings_key"):
        op.create_index("ix_admin_settings_key", "admin_settings", ["key"], unique=True)


def downgrade() -> None:
    if index_exists("admin_settings", "ix_admin_settings_key"):
        op.drop_index("ix_admin_settings_key", table_name="admin_settings")
    if table_exists("admin_settings"):
        op.drop_table("admin_settings")
