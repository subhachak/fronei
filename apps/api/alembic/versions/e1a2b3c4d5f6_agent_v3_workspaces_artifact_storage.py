"""agent v3 workspaces and artifact storage

Revision ID: e1a2b3c4d5f6
Revises: e0f1a2b3c4d5
Create Date: 2026-06-17
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import column_exists, index_exists, table_exists


revision: str = "e1a2b3c4d5f6"
down_revision: Union[str, Sequence[str], None] = "e0f1a2b3c4d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not table_exists("agent_v3_workspaces"):
        op.create_table(
            "agent_v3_workspaces",
            sa.Column("id", sa.String(64), nullable=False),
            sa.Column("user_id", sa.String(128), nullable=False),
            sa.Column("name", sa.String(160), nullable=False, server_default="Personal workspace"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.PrimaryKeyConstraint("id"),
        )
    for name, columns in [
        ("ix_agent_v3_workspaces_user_id", ["user_id"]),
        ("ix_agent_v3_workspaces_updated_at", ["updated_at"]),
    ]:
        if not index_exists("agent_v3_workspaces", name):
            op.create_index(name, "agent_v3_workspaces", columns)

    if not table_exists("agent_v3_conversations"):
        op.create_table(
            "agent_v3_conversations",
            sa.Column("id", sa.String(64), nullable=False),
            sa.Column("user_id", sa.String(128), nullable=False),
            sa.Column("workspace_id", sa.String(64), nullable=False),
            sa.Column("title", sa.String(180), nullable=False, server_default="New conversation"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["workspace_id"], ["agent_v3_workspaces.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
    for name, columns in [
        ("ix_agent_v3_conversations_user_id", ["user_id"]),
        ("ix_agent_v3_conversations_workspace_id", ["workspace_id"]),
        ("ix_agent_v3_conversations_updated_at", ["updated_at"]),
    ]:
        if not index_exists("agent_v3_conversations", name):
            op.create_index(name, "agent_v3_conversations", columns)

    if table_exists("agent_v3_artifacts"):
        for column_name, column in [
            ("storage_path", sa.Column("storage_path", sa.Text(), nullable=True)),
            ("size_bytes", sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0")),
            ("sha256", sa.Column("sha256", sa.String(64), nullable=True)),
        ]:
            if not column_exists("agent_v3_artifacts", column_name):
                op.add_column("agent_v3_artifacts", column)
        if not index_exists("agent_v3_artifacts", "ix_agent_v3_artifacts_sha256"):
            op.create_index("ix_agent_v3_artifacts_sha256", "agent_v3_artifacts", ["sha256"])


def downgrade() -> None:
    if table_exists("agent_v3_artifacts"):
        if index_exists("agent_v3_artifacts", "ix_agent_v3_artifacts_sha256"):
            op.drop_index("ix_agent_v3_artifacts_sha256", table_name="agent_v3_artifacts")
        for column_name in ["sha256", "size_bytes", "storage_path"]:
            if column_exists("agent_v3_artifacts", column_name):
                op.drop_column("agent_v3_artifacts", column_name)

    if table_exists("agent_v3_conversations"):
        for index in [
            "ix_agent_v3_conversations_updated_at",
            "ix_agent_v3_conversations_workspace_id",
            "ix_agent_v3_conversations_user_id",
        ]:
            if index_exists("agent_v3_conversations", index):
                op.drop_index(index, table_name="agent_v3_conversations")
        op.drop_table("agent_v3_conversations")

    if table_exists("agent_v3_workspaces"):
        for index in ["ix_agent_v3_workspaces_updated_at", "ix_agent_v3_workspaces_user_id"]:
            if index_exists("agent_v3_workspaces", index):
                op.drop_index(index, table_name="agent_v3_workspaces")
        op.drop_table("agent_v3_workspaces")
