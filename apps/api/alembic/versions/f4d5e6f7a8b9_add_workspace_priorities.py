"""scope consolidated "current priorities" to the workspace, not the user

Originally `current_priorities` was stored alongside `preferences` on
`User.profile_json` and derived from a user's turns across ALL of their
workspaces. That meant an active project in one workspace could bleed into
an unrelated workspace's context. Priorities are now consolidated and stored
per-workspace; `User.profile_json` keeps only the durable, genuinely
global `preferences`.

Revision ID: f4d5e6f7a8b9
Revises: f3c4d5e6f7a8
Create Date: 2026-06-22
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import column_exists


revision: str = "f4d5e6f7a8b9"
down_revision: Union[str, Sequence[str], None] = "f3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not column_exists("workspaces", "priorities_json"):
        op.add_column(
            "workspaces",
            sa.Column("priorities_json", sa.Text(), nullable=False, server_default="[]"),
        )
    if not column_exists("workspaces", "priorities_consolidated_at"):
        op.add_column(
            "workspaces",
            sa.Column("priorities_consolidated_at", sa.DateTime(), nullable=True),
        )


def downgrade() -> None:
    if column_exists("workspaces", "priorities_consolidated_at"):
        op.drop_column("workspaces", "priorities_consolidated_at")
    if column_exists("workspaces", "priorities_json"):
        op.drop_column("workspaces", "priorities_json")
