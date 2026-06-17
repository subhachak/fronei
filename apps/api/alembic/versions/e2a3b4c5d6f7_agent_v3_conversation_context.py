"""agent v3 workspace and conversation context

Revision ID: e2a3b4c5d6f7
Revises: e1a2b3c4d5f6
Create Date: 2026-06-17
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import column_exists, table_exists


revision: str = "e2a3b4c5d6f7"
down_revision: Union[str, Sequence[str], None] = "e1a2b3c4d5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if table_exists("agent_v3_workspaces"):
        if not column_exists("agent_v3_workspaces", "context_json"):
            op.add_column(
                "agent_v3_workspaces",
                sa.Column("context_json", sa.Text(), nullable=False, server_default="{}"),
            )
        if not column_exists("agent_v3_workspaces", "context_updated_at"):
            op.add_column(
                "agent_v3_workspaces",
                sa.Column("context_updated_at", sa.DateTime(), nullable=True),
            )
    if table_exists("agent_v3_conversations") and not column_exists("agent_v3_conversations", "context_json"):
        op.add_column(
            "agent_v3_conversations",
            sa.Column("context_json", sa.Text(), nullable=False, server_default="{}"),
        )
    if table_exists("agent_v3_conversations") and not column_exists("agent_v3_conversations", "context_updated_at"):
        op.add_column(
            "agent_v3_conversations",
            sa.Column("context_updated_at", sa.DateTime(), nullable=True),
        )


def downgrade() -> None:
    if table_exists("agent_v3_conversations") and column_exists("agent_v3_conversations", "context_updated_at"):
        op.drop_column("agent_v3_conversations", "context_updated_at")
    if table_exists("agent_v3_conversations") and column_exists("agent_v3_conversations", "context_json"):
        op.drop_column("agent_v3_conversations", "context_json")
    if table_exists("agent_v3_workspaces") and column_exists("agent_v3_workspaces", "context_updated_at"):
        op.drop_column("agent_v3_workspaces", "context_updated_at")
    if table_exists("agent_v3_workspaces") and column_exists("agent_v3_workspaces", "context_json"):
        op.drop_column("agent_v3_workspaces", "context_json")
