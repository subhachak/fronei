"""add user_memories table

Revision ID: b1c2d3e4f567
Revises: ad98597a4589
Create Date: 2026-06-09 00:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = 'b1c2d3e4f567'
down_revision: Union[str, Sequence[str], None] = 'ad98597a4589'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_memories',
        sa.Column('id',                     sa.Integer(),     primary_key=True, autoincrement=True),
        sa.Column('user_id',                sa.String(128),   nullable=False),
        sa.Column('content',                sa.Text(),        nullable=False),
        sa.Column('category',               sa.String(64),    nullable=False, server_default='general'),
        sa.Column('source_conversation_id', sa.Integer(),     nullable=True),
        sa.Column('created_at',             sa.DateTime(),    nullable=False),
        sa.Column('updated_at',             sa.DateTime(),    nullable=False),
    )
    op.create_index('ix_user_memories_user_id', 'user_memories', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_user_memories_user_id', table_name='user_memories')
    op.drop_table('user_memories')
