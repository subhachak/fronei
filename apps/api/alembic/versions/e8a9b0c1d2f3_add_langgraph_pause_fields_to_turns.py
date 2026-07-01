"""add langgraph pause fields to turns

Revision ID: e8a9b0c1d2f3
Revises: d926d728ff35
Create Date: 2026-07-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import column_exists, index_exists, table_exists

revision: str = "e8a9b0c1d2f3"
down_revision: Union[str, Sequence[str], None] = "d926d728ff35"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not table_exists("turns"):
        return
    with op.batch_alter_table("turns", schema=None) as batch_op:
        if not column_exists("turns", "langgraph_run_id"):
            batch_op.add_column(sa.Column("langgraph_run_id", sa.String(length=64), nullable=True))
        if not column_exists("turns", "pause_reason"):
            batch_op.add_column(sa.Column("pause_reason", sa.Text(), nullable=True))
    if not index_exists("turns", "ix_turns_langgraph_run_id"):
        op.create_index("ix_turns_langgraph_run_id", "turns", ["langgraph_run_id"], unique=False)


def downgrade() -> None:
    if not table_exists("turns"):
        return
    if index_exists("turns", "ix_turns_langgraph_run_id"):
        op.drop_index("ix_turns_langgraph_run_id", table_name="turns")
    with op.batch_alter_table("turns", schema=None) as batch_op:
        if column_exists("turns", "pause_reason"):
            batch_op.drop_column("pause_reason")
        if column_exists("turns", "langgraph_run_id"):
            batch_op.drop_column("langgraph_run_id")
