"""add research source cache table

Revision ID: a8b9c0d1e234
Revises: f7a8b9c01234
Create Date: 2026-06-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import index_exists, table_exists


revision: str = "a8b9c0d1e234"
down_revision: Union[str, Sequence[str], None] = "f7a8b9c01234"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not table_exists("research_source_cache"):
        op.create_table(
            "research_source_cache",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("url", sa.String(2048), nullable=False),
            sa.Column("title", sa.Text(), nullable=False, server_default=""),
            sa.Column("source_type", sa.String(64), nullable=True),
            sa.Column("source_tier", sa.String(32), nullable=False, server_default="tier_2_expert"),
            sa.Column("source_family", sa.String(255), nullable=True),
            sa.Column("source_role_prior", sa.String(48), nullable=False, server_default="background_context"),
            sa.Column("published_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.Column("source_date_confidence", sa.String(16), nullable=False, server_default="unknown"),
            sa.Column("admission_status", sa.String(24), nullable=False, server_default="admitted"),
            sa.Column("admission_reason", sa.Text(), nullable=True),
            sa.Column("credibility_score", sa.Float(), nullable=False, server_default="0"),
            sa.Column("freshness_score", sa.Float(), nullable=False, server_default="0"),
            sa.Column("cache_category", sa.String(16), nullable=False, server_default="current"),
            sa.Column("claims_json", sa.Text(), nullable=True),
            sa.Column("cached_at", sa.DateTime(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
        )
    if not index_exists("research_source_cache", "ix_research_source_cache_url"):
        op.create_index("ix_research_source_cache_url", "research_source_cache", ["url"], unique=True)


def downgrade() -> None:
    if index_exists("research_source_cache", "ix_research_source_cache_url"):
        op.drop_index("ix_research_source_cache_url", table_name="research_source_cache")
    if table_exists("research_source_cache"):
        op.drop_table("research_source_cache")
