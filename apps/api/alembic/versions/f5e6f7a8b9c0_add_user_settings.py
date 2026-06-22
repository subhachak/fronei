"""add explicit user-set default settings (quality_mode/output_format/research_level)

Revision ID: f5e6f7a8b9c0
Revises: f4d5e6f7a8b9
Create Date: 2026-06-22
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import column_exists


revision: str = "f5e6f7a8b9c0"
down_revision: Union[str, Sequence[str], None] = "f4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not column_exists("users", "settings_json"):
        op.add_column(
            "users",
            sa.Column("settings_json", sa.Text(), nullable=False, server_default="{}"),
        )


def downgrade() -> None:
    if column_exists("users", "settings_json"):
        op.drop_column("users", "settings_json")
