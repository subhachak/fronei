"""rename agent_v3_* tables (drop the version qualifier)

Agent v3 is the only architecture left, so the "agent_v3" qualifier on its
tables no longer disambiguates anything. Renamed to plain domain names —
the old conversations/request_logs/etc. names this frees up were dropped by
the previous migration. Indexes are dropped and recreated under matching
names (SQLite has no ALTER INDEX ... RENAME, so this is the portable path).

Revision ID: f2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-06-22
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from app.db.migration_helpers import index_exists, table_exists


revision: str = "f2b3c4d5e6f7"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_RENAMES = [
    ("agent_v3_workspaces", "workspaces"),
    ("agent_v3_conversations", "conversations"),
    ("agent_v3_turns", "turns"),
    ("agent_v3_events", "events"),
    ("agent_v3_tool_calls", "tool_calls"),
    ("agent_v3_artifacts", "artifacts"),
    ("agent_v3_prompt_templates", "prompt_templates"),
    ("agent_v3_routing_signal_candidates", "routing_signal_candidates"),
    ("agent_v3_routing_decision_feedback", "routing_decision_feedback"),
]

# (old_index_name, new_index_name, table_after_rename, columns)
_INDEXES = [
    ("ix_agent_v3_workspaces_user_id", "ix_workspaces_user_id", "workspaces", ["user_id"]),
    ("ix_agent_v3_workspaces_updated_at", "ix_workspaces_updated_at", "workspaces", ["updated_at"]),
    ("ix_agent_v3_conversations_user_id", "ix_conversations_user_id", "conversations", ["user_id"]),
    ("ix_agent_v3_conversations_workspace_id", "ix_conversations_workspace_id", "conversations", ["workspace_id"]),
    ("ix_agent_v3_conversations_updated_at", "ix_conversations_updated_at", "conversations", ["updated_at"]),
    ("ix_agent_v3_turns_user_id", "ix_turns_user_id", "turns", ["user_id"]),
    ("ix_agent_v3_turns_conversation_id", "ix_turns_conversation_id", "turns", ["conversation_id"]),
    ("ix_agent_v3_turns_status", "ix_turns_status", "turns", ["status"]),
    ("ix_agent_v3_turns_route", "ix_turns_route", "turns", ["route"]),
    ("ix_agent_v3_events_turn_id", "ix_events_turn_id", "events", ["turn_id"]),
    ("ix_agent_v3_tool_calls_turn_id", "ix_tool_calls_turn_id", "tool_calls", ["turn_id"]),
    ("ix_agent_v3_tool_calls_name", "ix_tool_calls_name", "tool_calls", ["name"]),
    ("ix_agent_v3_artifacts_turn_id", "ix_artifacts_turn_id", "artifacts", ["turn_id"]),
    ("ix_agent_v3_artifacts_sha256", "ix_artifacts_sha256", "artifacts", ["sha256"]),
    ("ix_agent_v3_prompt_templates_agent_id", "ix_prompt_templates_agent_id", "prompt_templates", ["agent_id"]),
    ("ix_agent_v3_prompt_templates_profile", "ix_prompt_templates_profile", "prompt_templates", ["profile"]),
    ("ix_agent_v3_prompt_templates_status", "ix_prompt_templates_status", "prompt_templates", ["status"]),
    (
        "ix_agent_v3_prompt_templates_agent_profile_status",
        "ix_prompt_templates_agent_profile_status",
        "prompt_templates",
        ["agent_id", "profile", "status"],
    ),
    (
        "ix_agent_v3_routing_signal_candidates_normalized_phrase",
        "ix_routing_signal_candidates_normalized_phrase",
        "routing_signal_candidates",
        ["normalized_phrase"],
    ),
    (
        "ix_agent_v3_routing_signal_candidates_signal_group",
        "ix_routing_signal_candidates_signal_group",
        "routing_signal_candidates",
        ["signal_group"],
    ),
    (
        "ix_agent_v3_routing_signal_candidates_suggested_route",
        "ix_routing_signal_candidates_suggested_route",
        "routing_signal_candidates",
        ["suggested_route"],
    ),
    (
        "ix_agent_v3_routing_signal_candidates_status",
        "ix_routing_signal_candidates_status",
        "routing_signal_candidates",
        ["status"],
    ),
    (
        "ix_agent_v3_routing_decision_feedback_user_id",
        "ix_routing_decision_feedback_user_id",
        "routing_decision_feedback",
        ["user_id"],
    ),
    (
        "ix_agent_v3_routing_decision_feedback_conversation_id",
        "ix_routing_decision_feedback_conversation_id",
        "routing_decision_feedback",
        ["conversation_id"],
    ),
    (
        "ix_agent_v3_routing_decision_feedback_selected_route",
        "ix_routing_decision_feedback_selected_route",
        "routing_decision_feedback",
        ["selected_route"],
    ),
    (
        "ix_agent_v3_routing_decision_feedback_final_route",
        "ix_routing_decision_feedback_final_route",
        "routing_decision_feedback",
        ["final_route"],
    ),
    (
        "ix_agent_v3_routing_decision_feedback_outcome",
        "ix_routing_decision_feedback_outcome",
        "routing_decision_feedback",
        ["outcome"],
    ),
]


def upgrade() -> None:
    for old_name, new_name in _RENAMES:
        if table_exists(old_name) and not table_exists(new_name):
            op.rename_table(old_name, new_name)
    for old_ix, new_ix, table, columns in _INDEXES:
        if index_exists(table, old_ix):
            op.drop_index(old_ix, table_name=table)
        if not index_exists(table, new_ix):
            op.create_index(new_ix, table, columns)


def downgrade() -> None:
    for old_ix, new_ix, table, columns in _INDEXES:
        if index_exists(table, new_ix):
            op.drop_index(new_ix, table_name=table)
        if not index_exists(table, old_ix):
            op.create_index(old_ix, table, columns)
    for old_name, new_name in reversed(_RENAMES):
        if table_exists(new_name) and not table_exists(old_name):
            op.rename_table(new_name, old_name)
