"""initial schema

Revision ID: f8372fd84be2
Revises:
Create Date: 2026-06-07 23:47:36.037394

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f8372fd84be2'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    # if_not_exists=True → safe to run against a DB that was bootstrapped by the
    # old hand-rolled init_db() path; CREATE TABLE is silently skipped when the
    # table already exists.  Fresh DBs get the full schema including the two
    # memory columns so no ADD COLUMN is needed for them.
    op.create_table(
        'conversations',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('title', sa.String(120), nullable=False),
        sa.Column('profile', sa.String(32), nullable=False),
        sa.Column('message_count', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('running_summary', sa.Text(), nullable=True),
        sa.Column('active_task_json', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        if_not_exists=True,
    )
    op.create_table(
        'conversation_messages',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('conversation_id', sa.Integer(), nullable=False),
        sa.Column('role', sa.String(16), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('task_type', sa.String(64), nullable=True),
        sa.Column('complexity', sa.String(32), nullable=True),
        sa.Column('model_used', sa.String(128), nullable=True),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('prompt_tokens', sa.Integer(), nullable=True),
        sa.Column('completion_tokens', sa.Integer(), nullable=True),
        sa.Column('estimated_cost_usd', sa.Float(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        if_not_exists=True,
    )
    op.create_table(
        'request_logs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('task_type', sa.String(64), nullable=False),
        sa.Column('complexity', sa.String(32), nullable=False),
        sa.Column('profile', sa.String(32), nullable=False),
        sa.Column('selected_model', sa.String(128), nullable=False),
        sa.Column('model_used', sa.String(128), nullable=False),
        sa.Column('latency_ms', sa.Integer(), nullable=False),
        sa.Column('prompt_tokens', sa.Integer(), nullable=True),
        sa.Column('completion_tokens', sa.Integer(), nullable=True),
        sa.Column('estimated_cost_usd', sa.Float(), nullable=True),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        if_not_exists=True,
    )

    # For databases that existed before Alembic was introduced, the conversations
    # table may be missing the two memory columns.  Add them only when absent.
    with op.batch_alter_table('conversations', schema=None) as batch_op:
        if not _column_exists('conversations', 'running_summary'):
            batch_op.add_column(sa.Column('running_summary', sa.Text(), nullable=True))
        if not _column_exists('conversations', 'active_task_json'):
            batch_op.add_column(sa.Column('active_task_json', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_table('request_logs')
    op.drop_table('conversation_messages')
    op.drop_table('conversations')
