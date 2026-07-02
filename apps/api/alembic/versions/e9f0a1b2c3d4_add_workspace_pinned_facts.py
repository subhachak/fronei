"""add user-curated pinned facts to workspaces

Revision ID: e9f0a1b2c3d4
Revises: e8a9b0c1d2f3
Create Date: 2026-07-02
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import column_exists


revision: str = "e9f0a1b2c3d4"
down_revision: Union[str, Sequence[str], None] = "e8a9b0c1d2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not column_exists("workspaces", "pinned_facts_json"):
        op.add_column(
            "workspaces",
            sa.Column("pinned_facts_json", sa.Text(), nullable=False, server_default="[]"),
        )


def downgrade() -> None:
    if column_exists("workspaces", "pinned_facts_json"):
        op.drop_column("workspaces", "pinned_facts_json")
