"""agent v3 adaptive routing signals

Revision ID: e6a7b8c9d0e1
Revises: e4a5b6c7d8e9
Create Date: 2026-06-18
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import index_exists, table_exists


revision: str = "e6a7b8c9d0e1"
down_revision: Union[str, Sequence[str], None] = "e4a5b6c7d8e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not table_exists("agent_v3_routing_signal_candidates"):
        op.create_table(
            "agent_v3_routing_signal_candidates",
            sa.Column("id", sa.String(64), nullable=False),
            sa.Column("phrase", sa.Text(), nullable=False),
            sa.Column("normalized_phrase", sa.String(240), nullable=False),
            sa.Column("signal_group", sa.String(80), nullable=False),
            sa.Column("suggested_route", sa.String(32), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
            sa.Column("support_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("false_positive_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("example_turn_ids_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("status", sa.String(32), nullable=False, server_default="candidate"),
            sa.Column("source", sa.String(32), nullable=False, server_default="learned"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.PrimaryKeyConstraint("id"),
        )

    if not table_exists("agent_v3_routing_decision_feedback"):
        op.create_table(
            "agent_v3_routing_decision_feedback",
            sa.Column("turn_id", sa.String(64), nullable=False),
            sa.Column("user_id", sa.String(128), nullable=False),
            sa.Column("conversation_id", sa.String(128), nullable=True),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("selected_route", sa.String(32), nullable=False),
            sa.Column("final_route", sa.String(32), nullable=False),
            sa.Column("matched_signals_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("outcome", sa.String(32), nullable=False, server_default="completed"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["turn_id"], ["agent_v3_turns.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("turn_id"),
        )

    for name, table, columns in [
        ("ix_agent_v3_routing_signal_candidates_normalized_phrase", "agent_v3_routing_signal_candidates", ["normalized_phrase"]),
        ("ix_agent_v3_routing_signal_candidates_signal_group", "agent_v3_routing_signal_candidates", ["signal_group"]),
        ("ix_agent_v3_routing_signal_candidates_suggested_route", "agent_v3_routing_signal_candidates", ["suggested_route"]),
        ("ix_agent_v3_routing_signal_candidates_status", "agent_v3_routing_signal_candidates", ["status"]),
        ("ix_agent_v3_routing_decision_feedback_user_id", "agent_v3_routing_decision_feedback", ["user_id"]),
        ("ix_agent_v3_routing_decision_feedback_conversation_id", "agent_v3_routing_decision_feedback", ["conversation_id"]),
        ("ix_agent_v3_routing_decision_feedback_selected_route", "agent_v3_routing_decision_feedback", ["selected_route"]),
        ("ix_agent_v3_routing_decision_feedback_final_route", "agent_v3_routing_decision_feedback", ["final_route"]),
        ("ix_agent_v3_routing_decision_feedback_outcome", "agent_v3_routing_decision_feedback", ["outcome"]),
    ]:
        if not index_exists(table, name):
            op.create_index(name, table, columns)


def downgrade() -> None:
    for name, table in [
        ("ix_agent_v3_routing_decision_feedback_outcome", "agent_v3_routing_decision_feedback"),
        ("ix_agent_v3_routing_decision_feedback_final_route", "agent_v3_routing_decision_feedback"),
        ("ix_agent_v3_routing_decision_feedback_selected_route", "agent_v3_routing_decision_feedback"),
        ("ix_agent_v3_routing_decision_feedback_conversation_id", "agent_v3_routing_decision_feedback"),
        ("ix_agent_v3_routing_decision_feedback_user_id", "agent_v3_routing_decision_feedback"),
        ("ix_agent_v3_routing_signal_candidates_status", "agent_v3_routing_signal_candidates"),
        ("ix_agent_v3_routing_signal_candidates_suggested_route", "agent_v3_routing_signal_candidates"),
        ("ix_agent_v3_routing_signal_candidates_signal_group", "agent_v3_routing_signal_candidates"),
        ("ix_agent_v3_routing_signal_candidates_normalized_phrase", "agent_v3_routing_signal_candidates"),
    ]:
        if table_exists(table) and index_exists(table, name):
            op.drop_index(name, table_name=table)
    if table_exists("agent_v3_routing_decision_feedback"):
        op.drop_table("agent_v3_routing_decision_feedback")
    if table_exists("agent_v3_routing_signal_candidates"):
        op.drop_table("agent_v3_routing_signal_candidates")
