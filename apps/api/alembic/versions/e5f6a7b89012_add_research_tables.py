"""add research tables

Revision ID: e5f6a7b89012
Revises: d4e5f6a7b890
Create Date: 2026-06-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import index_exists


revision: str = 'e5f6a7b89012'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b890'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'research_runs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.String(128), nullable=False),
        sa.Column('conversation_id', sa.Integer(), nullable=True),
        sa.Column('query', sa.Text(), nullable=False),
        sa.Column('mode', sa.String(32), nullable=True, server_default='deep'),
        sa.Column('status', sa.String(32), nullable=True, server_default='running'),
        sa.Column('iterations', sa.Integer(), nullable=True, server_default='0'),
        sa.Column('max_sources', sa.Integer(), nullable=True, server_default='12'),
        sa.Column('source_count', sa.Integer(), nullable=True, server_default='0'),
        sa.Column('claim_count', sa.Integer(), nullable=True, server_default='0'),
        sa.Column('confidence', sa.String(32), nullable=True),
        sa.Column('gaps_json', sa.Text(), nullable=True),
        sa.Column('contradictions_json', sa.Text(), nullable=True),
        sa.Column('verifier_notes', sa.Text(), nullable=True),
        sa.Column('final_answer', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        if_not_exists=True,
    )
    if not index_exists('research_runs', 'ix_research_runs_user_id'):
        op.create_index('ix_research_runs_user_id', 'research_runs', ['user_id'])
    if not index_exists('research_runs', 'ix_research_runs_conversation_id'):
        op.create_index('ix_research_runs_conversation_id', 'research_runs', ['conversation_id'])

    op.create_table(
        'research_questions',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('run_id', sa.Integer(), sa.ForeignKey('research_runs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('question', sa.Text(), nullable=False),
        sa.Column('search_query', sa.Text(), nullable=True),
        sa.Column('status', sa.String(32), nullable=True, server_default='pending'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        if_not_exists=True,
    )
    if not index_exists('research_questions', 'ix_research_questions_run_id'):
        op.create_index('ix_research_questions_run_id', 'research_questions', ['run_id'])

    op.create_table(
        'research_sources',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('run_id', sa.Integer(), sa.ForeignKey('research_runs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('question_id', sa.Integer(), nullable=True),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('url', sa.Text(), nullable=False),
        sa.Column('provider', sa.String(64), nullable=True, server_default=''),
        sa.Column('excerpt', sa.Text(), nullable=True),
        sa.Column('credibility_score', sa.Float(), nullable=True, server_default='0'),
        sa.Column('relevance_score', sa.Float(), nullable=True, server_default='0'),
        sa.Column('freshness_score', sa.Float(), nullable=True, server_default='0'),
        sa.Column('source_type', sa.String(64), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        if_not_exists=True,
    )
    if not index_exists('research_sources', 'ix_research_sources_run_id'):
        op.create_index('ix_research_sources_run_id', 'research_sources', ['run_id'])
    if not index_exists('research_sources', 'ix_research_sources_question_id'):
        op.create_index('ix_research_sources_question_id', 'research_sources', ['question_id'])

    op.create_table(
        'research_claims',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('run_id', sa.Integer(), sa.ForeignKey('research_runs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('source_id', sa.Integer(), sa.ForeignKey('research_sources.id', ondelete='CASCADE'), nullable=False),
        sa.Column('claim', sa.Text(), nullable=False),
        sa.Column('quote', sa.Text(), nullable=True),
        sa.Column('confidence', sa.String(32), nullable=True, server_default='medium'),
        sa.Column('relevance_score', sa.Float(), nullable=True, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        if_not_exists=True,
    )
    if not index_exists('research_claims', 'ix_research_claims_run_id'):
        op.create_index('ix_research_claims_run_id', 'research_claims', ['run_id'])
    if not index_exists('research_claims', 'ix_research_claims_source_id'):
        op.create_index('ix_research_claims_source_id', 'research_claims', ['source_id'])

    op.create_table(
        'research_findings',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('run_id', sa.Integer(), sa.ForeignKey('research_runs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('finding', sa.Text(), nullable=False),
        sa.Column('evidence_json', sa.Text(), nullable=True),
        sa.Column('confidence', sa.String(32), nullable=True, server_default='medium'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        if_not_exists=True,
    )
    if not index_exists('research_findings', 'ix_research_findings_run_id'):
        op.create_index('ix_research_findings_run_id', 'research_findings', ['run_id'])


def downgrade() -> None:
    op.drop_index('ix_research_findings_run_id', table_name='research_findings')
    op.drop_table('research_findings')
    op.drop_index('ix_research_claims_source_id', table_name='research_claims')
    op.drop_index('ix_research_claims_run_id', table_name='research_claims')
    op.drop_table('research_claims')
    op.drop_index('ix_research_sources_question_id', table_name='research_sources')
    op.drop_index('ix_research_sources_run_id', table_name='research_sources')
    op.drop_table('research_sources')
    op.drop_index('ix_research_questions_run_id', table_name='research_questions')
    op.drop_table('research_questions')
    op.drop_index('ix_research_runs_conversation_id', table_name='research_runs')
    op.drop_index('ix_research_runs_user_id', table_name='research_runs')
    op.drop_table('research_runs')
