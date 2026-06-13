"""add durable conversation turns

Revision ID: e7f890123456
Revises: d6e7f8901234
Create Date: 2026-06-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import column_exists, index_exists, table_exists


revision: str = "e7f890123456"
down_revision: Union[str, Sequence[str], None] = "d6e7f8901234"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not table_exists("conversation_turns"):
        op.create_table(
            "conversation_turns",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("public_id", sa.String(24), nullable=False),
            sa.Column("user_id", sa.String(128), nullable=False, server_default=""),
            sa.Column("conversation_id", sa.Integer(), nullable=False),
            sa.Column("user_message_id", sa.Integer(), nullable=True),
            sa.Column("assistant_message_id", sa.Integer(), nullable=True),
            sa.Column("client_request_id", sa.String(128), nullable=True),
            sa.Column("turn_kind", sa.String(24), nullable=False, server_default="quick"),
            sa.Column("status", sa.String(24), nullable=False, server_default="running"),
            sa.Column("progress_json", sa.Text(), nullable=True),
            sa.Column("result_json", sa.Text(), nullable=True),
            sa.Column("lifecycle_json", sa.Text(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("public_id", name="uq_conversation_turns_public_id"),
            sa.UniqueConstraint("user_id", "client_request_id", name="uq_conversation_turns_user_client_request"),
        )

    if table_exists("conversation_turns") and not column_exists("conversation_turns", "lifecycle_json"):
        op.add_column("conversation_turns", sa.Column("lifecycle_json", sa.Text(), nullable=True))
    if table_exists("conversation_turns") and not column_exists("conversation_turns", "turn_kind"):
        op.add_column("conversation_turns", sa.Column("turn_kind", sa.String(24), nullable=False, server_default="quick"))

    for name, columns, unique in [
        ("ix_conversation_turns_public_id", ["public_id"], True),
        ("ix_conversation_turns_user_id", ["user_id"], False),
        ("ix_conversation_turns_conversation_id", ["conversation_id"], False),
        ("ix_conversation_turns_user_message_id", ["user_message_id"], False),
        ("ix_conversation_turns_assistant_message_id", ["assistant_message_id"], False),
        ("ix_conversation_turns_turn_kind", ["turn_kind"], False),
        ("ix_conversation_turns_status", ["status"], False),
    ]:
        if not index_exists("conversation_turns", name):
            op.create_index(name, "conversation_turns", columns, unique=unique)


def downgrade() -> None:
    if table_exists("conversation_turns"):
        for name in [
            "ix_conversation_turns_status",
            "ix_conversation_turns_assistant_message_id",
            "ix_conversation_turns_turn_kind",
            "ix_conversation_turns_user_message_id",
            "ix_conversation_turns_conversation_id",
            "ix_conversation_turns_user_id",
            "ix_conversation_turns_public_id",
        ]:
            if index_exists("conversation_turns", name):
                op.drop_index(name, table_name="conversation_turns")
        op.drop_table("conversation_turns")
