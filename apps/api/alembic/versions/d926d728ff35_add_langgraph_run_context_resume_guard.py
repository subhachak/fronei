"""add resumed_at/resumed_by columns to langgraph_run_contexts

Revision ID: d926d728ff35
Revises: f7b8c9d0e1f2
Create Date: 2026-07-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import column_exists, table_exists

revision: str = "d926d728ff35"
down_revision: Union[str, Sequence[str], None] = "f7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not table_exists("langgraph_run_contexts"):
        return
    with op.batch_alter_table("langgraph_run_contexts", schema=None) as batch_op:
        if not column_exists("langgraph_run_contexts", "resumed_at"):
            batch_op.add_column(sa.Column("resumed_at", sa.DateTime(), nullable=True))
        if not column_exists("langgraph_run_contexts", "resumed_by"):
            batch_op.add_column(sa.Column("resumed_by", sa.String(length=128), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("langgraph_run_contexts", schema=None) as batch_op:
        if column_exists("langgraph_run_contexts", "resumed_by"):
            batch_op.drop_column("resumed_by")
        if column_exists("langgraph_run_contexts", "resumed_at"):
            batch_op.drop_column("resumed_at")
