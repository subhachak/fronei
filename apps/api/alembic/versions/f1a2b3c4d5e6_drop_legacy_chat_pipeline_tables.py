"""drop legacy chat-pipeline tables

Agent v3 is now the only chat/document/research architecture; the classic
chat pipeline (conversations, request logs, personal memory, twin profile,
deep research, legacy agent-runtime tracing) has been fully removed from the
codebase. This migration drops the now-orphaned tables. This is destructive
and intentionally has no data-preserving downgrade — restore from a backup
taken before this migration if the drop needs to be undone.

Revision ID: f1a2b3c4d5e6
Revises: e6a7b8c9d0e1
Create Date: 2026-06-22
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from app.db.migration_helpers import table_exists


revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "e6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Child-before-parent order so foreign-key-dependent tables drop cleanly even
# on backends without ON DELETE CASCADE enforcement during DDL.
_TABLES_TO_DROP = [
    "research_findings",
    "research_claims",
    "research_sources",
    "research_questions",
    "research_source_cache",
    "research_runs",
    "twin_profiles",
    "writing_samples",
    "user_profiles",
    "user_memories",
    "request_logs",
    "agent_traces",
    "job_checkpoint",
    "agent_steps",
    "agent_runs",
    "goals",
    "guardrail_events",
    "conversation_turns",
    "conversation_messages",
    "conversations",
    "component_usage_stats",
    # Legacy agent-runtime registry (services/agent_runtime/), fully removed —
    # superseded by Agent v3's own prompt library / model policy / tool registry.
    "agent_definitions",
    "guardrail_policies",
    "model_policies",
    "tool_definitions",
    "prompt_templates",
]


def upgrade() -> None:
    for table in _TABLES_TO_DROP:
        if table_exists(table):
            op.drop_table(table)


def downgrade() -> None:
    raise RuntimeError(
        "This migration permanently dropped legacy chat-pipeline tables and "
        "their data. There is no downgrade path — restore from a pre-migration "
        "backup if you need this data back."
    )
