"""add blog posts table

Revision ID: 8a6faada6ef2
Revises: 14686aa3846c
Create Date: 2026-07-27
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import index_exists, table_exists

revision: str = "8a6faada6ef2"
down_revision: Union[str, Sequence[str], None] = "14686aa3846c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not table_exists("blog_posts"):
        op.create_table(
            "blog_posts",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("slug", sa.String(160), nullable=False),
            sa.Column("title", sa.String(255), nullable=False),
            sa.Column("excerpt", sa.Text(), nullable=False, server_default=""),
            sa.Column("body_markdown", sa.Text(), nullable=False, server_default=""),
            sa.Column("tags_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("author", sa.String(120), nullable=False, server_default="Subh Chakraborty"),
            sa.Column("voice", sa.String(16), nullable=False, server_default="personal"),
            sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
            sa.Column("cover_image_url", sa.String(1024), nullable=True),
            sa.Column("published_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
    if not index_exists("blog_posts", "ix_blog_posts_slug"):
        op.create_index("ix_blog_posts_slug", "blog_posts", ["slug"], unique=True)
    if not index_exists("blog_posts", "ix_blog_posts_status"):
        op.create_index("ix_blog_posts_status", "blog_posts", ["status"])
    if not index_exists("blog_posts", "ix_blog_posts_published_at"):
        op.create_index("ix_blog_posts_published_at", "blog_posts", ["published_at"])


def downgrade() -> None:
    if index_exists("blog_posts", "ix_blog_posts_published_at"):
        op.drop_index("ix_blog_posts_published_at", table_name="blog_posts")
    if index_exists("blog_posts", "ix_blog_posts_status"):
        op.drop_index("ix_blog_posts_status", table_name="blog_posts")
    if index_exists("blog_posts", "ix_blog_posts_slug"):
        op.drop_index("ix_blog_posts_slug", table_name="blog_posts")
    if table_exists("blog_posts"):
        op.drop_table("blog_posts")
