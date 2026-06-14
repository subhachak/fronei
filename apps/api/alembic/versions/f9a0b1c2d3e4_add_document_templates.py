"""add document templates

Revision ID: f9a0b1c2d3e4
Revises: e7f890123456
Create Date: 2026-06-14
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import index_exists, table_exists


revision: str = "f9a0b1c2d3e4"
down_revision: Union[str, Sequence[str], None] = "e7f890123456"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not table_exists("document_templates"):
        op.create_table(
            "document_templates",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("public_id", sa.String(length=32), nullable=False),
            sa.Column("user_id", sa.String(length=128), nullable=False),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("doc_type", sa.String(length=64), nullable=False, server_default="presentation"),
            sa.Column("storage_key", sa.String(length=512), nullable=False),
            sa.Column("original_filename", sa.String(length=255), nullable=True),
            sa.Column("content_type", sa.String(length=120), nullable=True),
            sa.Column("file_size", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            if_not_exists=True,
        )
    if not index_exists("document_templates", "ix_document_templates_public_id"):
        op.create_index("ix_document_templates_public_id", "document_templates", ["public_id"], unique=True)
    if not index_exists("document_templates", "ix_document_templates_user_id"):
        op.create_index("ix_document_templates_user_id", "document_templates", ["user_id"], unique=False)


def downgrade() -> None:
    if table_exists("document_templates"):
        if index_exists("document_templates", "ix_document_templates_user_id"):
            op.drop_index("ix_document_templates_user_id", table_name="document_templates")
        if index_exists("document_templates", "ix_document_templates_public_id"):
            op.drop_index("ix_document_templates_public_id", table_name="document_templates")
        op.drop_table("document_templates")
