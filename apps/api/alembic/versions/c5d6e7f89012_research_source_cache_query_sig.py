"""key research source cache by (url, query_signature)

Revision ID: c5d6e7f89012
Revises: a8b9c0d1e234
Create Date: 2026-06-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import column_exists, index_exists


revision: str = "c5d6e7f89012"
down_revision: Union[str, Sequence[str], None] = "a8b9c0d1e234"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not column_exists("research_source_cache", "query_signature"):
        op.add_column(
            "research_source_cache",
            sa.Column("query_signature", sa.String(32), nullable=False, server_default=""),
        )

    # Claim extraction is query-specific, so the old url-only *unique* index
    # is wrong — replace it with a non-unique index on `url` (matching the
    # model's `index=True` naming convention, ix_<table>_<column>) plus a new
    # unique index on (url, query_signature).
    if index_exists("research_source_cache", "ix_research_source_cache_url"):
        op.drop_index("ix_research_source_cache_url", table_name="research_source_cache")

    if not index_exists("research_source_cache", "ix_research_source_cache_url"):
        op.create_index("ix_research_source_cache_url", "research_source_cache", ["url"], unique=False)

    if not index_exists("research_source_cache", "uq_research_source_cache_url_query"):
        op.create_index(
            "uq_research_source_cache_url_query",
            "research_source_cache",
            ["url", "query_signature"],
            unique=True,
        )


def downgrade() -> None:
    if index_exists("research_source_cache", "uq_research_source_cache_url_query"):
        op.drop_index("uq_research_source_cache_url_query", table_name="research_source_cache")
    if index_exists("research_source_cache", "ix_research_source_cache_url"):
        op.drop_index("ix_research_source_cache_url", table_name="research_source_cache")
    op.create_index("ix_research_source_cache_url", "research_source_cache", ["url"], unique=True)
    if column_exists("research_source_cache", "query_signature"):
        op.drop_column("research_source_cache", "query_signature")
