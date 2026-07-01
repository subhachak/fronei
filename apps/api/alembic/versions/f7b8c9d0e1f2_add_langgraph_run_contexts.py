"""add durable LangGraph run contexts

Revision ID: f7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-07-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import table_exists

revision: str = "f7b8c9d0e1f2"
down_revision: Union[str, Sequence[str], None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if table_exists("langgraph_run_contexts"):
        return
    op.create_table(
        "langgraph_run_contexts",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("request_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("tool_config_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="running"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index(
        "ix_langgraph_run_contexts_status",
        "langgraph_run_contexts",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_langgraph_run_contexts_status", table_name="langgraph_run_contexts")
    op.drop_table("langgraph_run_contexts")
