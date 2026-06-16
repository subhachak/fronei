"""phase b guardrail tables

Revision ID: f8a9b0c1d2e3
Revises: d7e8f9a0b1c2
Create Date: 2026-06-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import index_exists, table_exists


revision: str = "f8a9b0c1d2e3"
down_revision: Union[str, Sequence[str], None] = "d7e8f9a0b1c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not table_exists("guardrail_events"):
        op.create_table(
            "guardrail_events",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("policy_id", sa.Text(), nullable=False),
            sa.Column("boundary", sa.Text(), nullable=False),
            sa.Column("action", sa.Text(), nullable=False),
            sa.Column("triggered_checks_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("user_id", sa.String(length=128), nullable=True),
            sa.Column("tenant_id", sa.String(length=128), nullable=True),
            sa.Column("tool_name", sa.String(length=128), nullable=True),
            sa.Column("turn_id", sa.String(length=64), nullable=True),
            sa.Column("conversation_id", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.PrimaryKeyConstraint("id"),
        )
    for name, columns in [
        ("ix_guardrail_events_user_id", ["user_id"]),
        ("ix_guardrail_events_turn_id", ["turn_id"]),
        ("ix_guardrail_events_conversation_id", ["conversation_id"]),
    ]:
        if not index_exists("guardrail_events", name):
            op.create_index(name, "guardrail_events", columns, unique=False)

    if not table_exists("goals"):
        op.create_table(
            "goals",
            sa.Column("id", sa.String(length=128), nullable=False),
            sa.Column("user_id", sa.String(length=128), nullable=False),
            sa.Column("tenant_id", sa.String(length=128), nullable=True),
            sa.Column("conversation_id", sa.String(length=128), nullable=False),
            sa.Column("turn_id", sa.String(length=128), nullable=False),
            sa.Column("parent_goal_id", sa.String(length=128), nullable=True),
            sa.Column("supersedes_goal_id", sa.String(length=128), nullable=True),
            sa.Column("superseded_by_goal_id", sa.String(length=128), nullable=True),
            sa.Column("objective", sa.Text(), nullable=False),
            sa.Column("quality_mode", sa.String(length=32), nullable=False, server_default="standard"),
            sa.Column("budget_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="created"),
            sa.Column("active_policy", sa.String(length=64), nullable=True),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("lock_owner", sa.String(length=128), nullable=True),
            sa.Column("lock_expires_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.PrimaryKeyConstraint("id"),
        )
    for name, columns in [
        ("ix_goals_user_id", ["user_id"]),
        ("ix_goals_conversation_id", ["conversation_id"]),
        ("ix_goals_turn_id", ["turn_id"]),
    ]:
        if not index_exists("goals", name):
            op.create_index(name, "goals", columns, unique=False)

    if not table_exists("agent_runs"):
        op.create_table(
            "agent_runs",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("goal_id", sa.String(length=128), nullable=False),
            sa.Column("agent_id", sa.Text(), nullable=False),
            sa.Column("parent_run_id", sa.String(length=36), nullable=True),
            sa.Column("status", sa.Text(), nullable=False, server_default="created"),
            sa.Column("failure_code", sa.Text(), nullable=True),
            sa.Column("failure_message", sa.Text(), nullable=True),
            sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_cost_usd", sa.Numeric(10, 6), nullable=False, server_default="0"),
            sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["goal_id"], ["goals.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    if not index_exists("agent_runs", "ix_agent_runs_goal_id"):
        op.create_index("ix_agent_runs_goal_id", "agent_runs", ["goal_id"], unique=False)


def downgrade() -> None:
    for table, indexes in [
        ("agent_runs", ["ix_agent_runs_goal_id"]),
        ("goals", ["ix_goals_turn_id", "ix_goals_conversation_id", "ix_goals_user_id"]),
        (
            "guardrail_events",
            ["ix_guardrail_events_conversation_id", "ix_guardrail_events_turn_id", "ix_guardrail_events_user_id"],
        ),
    ]:
        if table_exists(table):
            for index in indexes:
                if index_exists(table, index):
                    op.drop_index(index, table_name=table)

    for table in ["agent_runs", "goals", "guardrail_events"]:
        if table_exists(table):
            op.drop_table(table)
