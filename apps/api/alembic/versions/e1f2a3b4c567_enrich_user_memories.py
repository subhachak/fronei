"""enrich user memories

Revision ID: e1f2a3b4c567
Revises: d1e2f3a4b567
Create Date: 2026-06-11
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import column_exists, index_exists


revision: str = "e1f2a3b4c567"
down_revision: Union[str, Sequence[str], None] = "d1e2f3a4b567"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("user_memories", schema=None) as batch_op:
        if not column_exists("user_memories", "scope"):
            batch_op.add_column(sa.Column("scope", sa.String(32), nullable=True, server_default="global"))
        if not column_exists("user_memories", "confidence"):
            batch_op.add_column(sa.Column("confidence", sa.Float(), nullable=True, server_default="0.6"))
        if not column_exists("user_memories", "source"):
            batch_op.add_column(sa.Column("source", sa.String(16), nullable=True, server_default="stated"))
        if not column_exists("user_memories", "seen_count"):
            batch_op.add_column(sa.Column("seen_count", sa.Integer(), nullable=True, server_default="1"))
        if not column_exists("user_memories", "last_seen_at"):
            batch_op.add_column(sa.Column("last_seen_at", sa.DateTime(), nullable=True))
        if not column_exists("user_memories", "importance"):
            batch_op.add_column(sa.Column("importance", sa.Float(), nullable=True, server_default="0.5"))
        if not column_exists("user_memories", "decay_rate"):
            batch_op.add_column(sa.Column("decay_rate", sa.Float(), nullable=True, server_default="0.05"))
        if not column_exists("user_memories", "pinned"):
            batch_op.add_column(sa.Column("pinned", sa.Boolean(), nullable=True, server_default=sa.false()))
        if not column_exists("user_memories", "status"):
            batch_op.add_column(sa.Column("status", sa.String(16), nullable=True, server_default="active"))
        if not column_exists("user_memories", "superseded_by_id"):
            batch_op.add_column(sa.Column("superseded_by_id", sa.Integer(), nullable=True))

    op.execute("UPDATE user_memories SET last_seen_at = updated_at WHERE last_seen_at IS NULL")

    if not index_exists("user_memories", "ix_user_memories_user_id_status"):
        op.create_index(
            "ix_user_memories_user_id_status",
            "user_memories",
            ["user_id", "status"],
            unique=False,
        )


def downgrade() -> None:
    if index_exists("user_memories", "ix_user_memories_user_id_status"):
        op.drop_index("ix_user_memories_user_id_status", table_name="user_memories")
    with op.batch_alter_table("user_memories", schema=None) as batch_op:
        for column in [
            "superseded_by_id",
            "status",
            "pinned",
            "decay_rate",
            "importance",
            "last_seen_at",
            "seen_count",
            "source",
            "confidence",
            "scope",
        ]:
            if column_exists("user_memories", column):
                batch_op.drop_column(column)
