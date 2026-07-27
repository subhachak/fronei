"""add blog post revisions

Revision ID: 1bc49879334b
Revises: 8a6faada6ef2
Create Date: 2026-07-27
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import column_exists

revision: str = "1bc49879334b"
down_revision: Union[str, Sequence[str], None] = "8a6faada6ef2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not column_exists("blog_posts", "revisions_json"):
        op.add_column(
            "blog_posts",
            sa.Column("revisions_json", sa.Text(), nullable=False, server_default="[]"),
        )


def downgrade() -> None:
    if column_exists("blog_posts", "revisions_json"):
        op.drop_column("blog_posts", "revisions_json")
