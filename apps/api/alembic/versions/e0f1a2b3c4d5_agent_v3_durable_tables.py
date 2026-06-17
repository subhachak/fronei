"""agent v3 durable tables

Revision ID: e0f1a2b3c4d5
Revises: d0e1f2a3b4c5
Create Date: 2026-06-16
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import index_exists, table_exists


revision: str = "e0f1a2b3c4d5"
down_revision: Union[str, Sequence[str], None] = "d0e1f2a3b4c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not table_exists("agent_v3_turns"):
        op.create_table(
            "agent_v3_turns",
            sa.Column("id", sa.String(64), nullable=False),
            sa.Column("user_id", sa.String(128), nullable=False),
            sa.Column("conversation_id", sa.String(128), nullable=True),
            sa.Column("objective", sa.Text(), nullable=False),
            sa.Column("route", sa.String(32), nullable=False),
            sa.Column("quality_mode", sa.String(32), nullable=False, server_default="standard"),
            sa.Column("status", sa.String(24), nullable=False, server_default="running"),
            sa.Column("answer", sa.Text(), nullable=False, server_default=""),
            sa.Column("model_used", sa.String(128), nullable=False, server_default=""),
            sa.Column("sources_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
    for name, columns in [
        ("ix_agent_v3_turns_user_id", ["user_id"]),
        ("ix_agent_v3_turns_conversation_id", ["conversation_id"]),
        ("ix_agent_v3_turns_route", ["route"]),
        ("ix_agent_v3_turns_status", ["status"]),
    ]:
        if not index_exists("agent_v3_turns", name):
            op.create_index(name, "agent_v3_turns", columns)

    if not table_exists("agent_v3_events"):
        op.create_table(
            "agent_v3_events",
            sa.Column("id", sa.String(64), nullable=False),
            sa.Column("turn_id", sa.String(64), nullable=False),
            sa.Column("stage", sa.String(64), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("data_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["turn_id"], ["agent_v3_turns.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
    if not index_exists("agent_v3_events", "ix_agent_v3_events_turn_id"):
        op.create_index("ix_agent_v3_events_turn_id", "agent_v3_events", ["turn_id"])

    if not table_exists("agent_v3_tool_calls"):
        op.create_table(
            "agent_v3_tool_calls",
            sa.Column("id", sa.String(64), nullable=False),
            sa.Column("turn_id", sa.String(64), nullable=False),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("input_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("output_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["turn_id"], ["agent_v3_turns.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
    for name, columns in [
        ("ix_agent_v3_tool_calls_turn_id", ["turn_id"]),
        ("ix_agent_v3_tool_calls_name", ["name"]),
    ]:
        if not index_exists("agent_v3_tool_calls", name):
            op.create_index(name, "agent_v3_tool_calls", columns)

    if not table_exists("agent_v3_artifacts"):
        op.create_table(
            "agent_v3_artifacts",
            sa.Column("id", sa.String(64), nullable=False),
            sa.Column("turn_id", sa.String(64), nullable=False),
            sa.Column("kind", sa.String(32), nullable=False),
            sa.Column("filename", sa.Text(), nullable=False),
            sa.Column("mime_type", sa.Text(), nullable=False),
            sa.Column("base64_data", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["turn_id"], ["agent_v3_turns.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
    if not index_exists("agent_v3_artifacts", "ix_agent_v3_artifacts_turn_id"):
        op.create_index("ix_agent_v3_artifacts_turn_id", "agent_v3_artifacts", ["turn_id"])


def downgrade() -> None:
    for table, indexes in [
        ("agent_v3_artifacts", ["ix_agent_v3_artifacts_turn_id"]),
        ("agent_v3_tool_calls", ["ix_agent_v3_tool_calls_name", "ix_agent_v3_tool_calls_turn_id"]),
        ("agent_v3_events", ["ix_agent_v3_events_turn_id"]),
        (
            "agent_v3_turns",
            [
                "ix_agent_v3_turns_status",
                "ix_agent_v3_turns_route",
                "ix_agent_v3_turns_conversation_id",
                "ix_agent_v3_turns_user_id",
            ],
        ),
    ]:
        if table_exists(table):
            for index in indexes:
                if index_exists(table, index):
                    op.drop_index(index, table_name=table)
            op.drop_table(table)
