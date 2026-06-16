"""phase d agent steps

Revision ID: b0c1d2e3f4a5
Revises: a9b8c7d6e5f4
Create Date: 2026-06-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import index_exists, table_exists


revision: str = "b0c1d2e3f4a5"
down_revision: Union[str, Sequence[str], None] = "a9b8c7d6e5f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not table_exists("agent_steps"):
        op.create_table(
            "agent_steps",
            sa.Column("id", sa.Text(), nullable=False),
            sa.Column("run_id", sa.Text(), nullable=False),
            sa.Column("step_type", sa.Text(), nullable=False),
            sa.Column("input_summary", sa.Text(), nullable=False, server_default=""),
            sa.Column("output_summary", sa.Text(), nullable=False, server_default=""),
            sa.Column("model_used", sa.Text(), nullable=True),
            sa.Column("tool_name", sa.Text(), nullable=True),
            sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.PrimaryKeyConstraint("id"),
        )
    if not index_exists("agent_steps", "ix_agent_steps_run_id"):
        op.create_index("ix_agent_steps_run_id", "agent_steps", ["run_id"], unique=False)


def downgrade() -> None:
    if table_exists("agent_steps"):
        if index_exists("agent_steps", "ix_agent_steps_run_id"):
            op.drop_index("ix_agent_steps_run_id", table_name="agent_steps")
        op.drop_table("agent_steps")
