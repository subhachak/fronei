"""add research run id to messages

Revision ID: f6a7b8901234
Revises: e5f6a7b89012
Create Date: 2026-06-10
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import column_exists, index_exists


revision: str = "f6a7b8901234"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b89012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not column_exists("conversation_messages", "research_run_id"):
        op.add_column("conversation_messages", sa.Column("research_run_id", sa.Integer(), nullable=True))
    if not index_exists("conversation_messages", "ix_conversation_messages_research_run_id"):
        op.create_index("ix_conversation_messages_research_run_id", "conversation_messages", ["research_run_id"])


def downgrade() -> None:
    op.drop_index("ix_conversation_messages_research_run_id", table_name="conversation_messages")
    op.drop_column("conversation_messages", "research_run_id")
