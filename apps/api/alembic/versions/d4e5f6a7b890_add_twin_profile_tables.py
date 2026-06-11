"""add twin profile tables

Revision ID: d4e5f6a7b890
Revises: c2d3e4f56789
Create Date: 2026-06-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import index_exists


revision: str = 'd4e5f6a7b890'
down_revision: Union[str, Sequence[str], None] = 'c2d3e4f56789'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'writing_samples',
        sa.Column('id',         sa.Integer(),     primary_key=True, autoincrement=True),
        sa.Column('user_id',    sa.String(128),   nullable=False),
        sa.Column('content',    sa.Text(),        nullable=False),
        sa.Column('label',      sa.String(120),   nullable=True),
        sa.Column('char_count', sa.Integer(),     nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(),    nullable=False),
        if_not_exists=True,
    )
    if not index_exists('writing_samples', 'ix_writing_samples_user_id'):
        op.create_index('ix_writing_samples_user_id', 'writing_samples', ['user_id'])

    op.create_table(
        'twin_profiles',
        sa.Column('id',               sa.Integer(),  primary_key=True, autoincrement=True),
        sa.Column('user_id',          sa.String(128), unique=True, nullable=False),
        sa.Column('fingerprint_json', sa.Text(),     nullable=True),
        sa.Column('rewrite_prompt',   sa.Text(),     nullable=True),
        sa.Column('extracted_at',     sa.DateTime(), nullable=True),
        sa.Column('prefs_json',       sa.Text(),     nullable=True),
        sa.Column('created_at',       sa.DateTime(), nullable=False),
        sa.Column('updated_at',       sa.DateTime(), nullable=False),
        if_not_exists=True,
    )
    if not index_exists('twin_profiles', 'ix_twin_profiles_user_id'):
        op.create_index('ix_twin_profiles_user_id', 'twin_profiles', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_twin_profiles_user_id', table_name='twin_profiles')
    op.drop_table('twin_profiles')
    op.drop_index('ix_writing_samples_user_id', table_name='writing_samples')
    op.drop_table('writing_samples')
