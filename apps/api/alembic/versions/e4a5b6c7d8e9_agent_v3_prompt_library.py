"""agent v3 prompt library

Revision ID: e4a5b6c7d8e9
Revises: e2a3b4c5d6f7
Create Date: 2026-06-17
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import index_exists, table_exists


revision: str = "e4a5b6c7d8e9"
down_revision: Union[str, Sequence[str], None] = "e2a3b4c5d6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not table_exists("agent_v3_prompt_templates"):
        op.create_table(
            "agent_v3_prompt_templates",
            sa.Column("id", sa.String(128), nullable=False),
            sa.Column("agent_id", sa.String(128), nullable=False),
            sa.Column("profile", sa.String(64), nullable=True),
            sa.Column("version", sa.String(32), nullable=False, server_default="1.0.0"),
            sa.Column("status", sa.String(24), nullable=False, server_default="draft"),
            sa.Column("system_prompt", sa.Text(), nullable=False),
            sa.Column("developer_prompt", sa.Text(), nullable=True),
            sa.Column("variables_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.PrimaryKeyConstraint("id"),
        )
    for name, columns in [
        ("ix_agent_v3_prompt_templates_agent_id", ["agent_id"]),
        ("ix_agent_v3_prompt_templates_profile", ["profile"]),
        ("ix_agent_v3_prompt_templates_status", ["status"]),
        ("ix_agent_v3_prompt_templates_agent_profile_status", ["agent_id", "profile", "status"]),
    ]:
        if not index_exists("agent_v3_prompt_templates", name):
            op.create_index(name, "agent_v3_prompt_templates", columns)


def downgrade() -> None:
    if table_exists("agent_v3_prompt_templates"):
        for index in [
            "ix_agent_v3_prompt_templates_agent_profile_status",
            "ix_agent_v3_prompt_templates_status",
            "ix_agent_v3_prompt_templates_profile",
            "ix_agent_v3_prompt_templates_agent_id",
        ]:
            if index_exists("agent_v3_prompt_templates", index):
                op.drop_index(index, table_name="agent_v3_prompt_templates")
        op.drop_table("agent_v3_prompt_templates")
