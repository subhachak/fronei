"""add execution log to conversation messages

Revision ID: c2d3e4f56789
Revises: b1c2d3e4f567
Create Date: 2026-06-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c2d3e4f56789'
down_revision: Union[str, Sequence[str], None] = 'b1c2d3e4f567'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    with op.batch_alter_table('conversation_messages', schema=None) as batch_op:
        if not _column_exists('conversation_messages', 'execution_log_json'):
            batch_op.add_column(sa.Column('execution_log_json', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('conversation_messages', schema=None) as batch_op:
        if _column_exists('conversation_messages', 'execution_log_json'):
            batch_op.drop_column('execution_log_json')
