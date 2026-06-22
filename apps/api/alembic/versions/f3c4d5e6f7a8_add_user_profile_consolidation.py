"""add consolidated preferences/priorities profile to users

Revision ID: f3c4d5e6f7a8
Revises: f2b3c4d5e6f7
Create Date: 2026-06-22
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import column_exists


revision: str = "f3c4d5e6f7a8"
down_revision: Union[str, Sequence[str], None] = "f2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not column_exists("users", "profile_json"):
        op.add_column(
            "users",
            sa.Column("profile_json", sa.Text(), nullable=False, server_default="{}"),
        )
    if not column_exists("users", "profile_consolidated_at"):
        op.add_column(
            "users",
            sa.Column("profile_consolidated_at", sa.DateTime(), nullable=True),
        )


def downgrade() -> None:
    if column_exists("users", "profile_consolidated_at"):
        op.drop_column("users", "profile_consolidated_at")
    if column_exists("users", "profile_json"):
        op.drop_column("users", "profile_json")
