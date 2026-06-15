"""add design_system_id to document_templates

Revision ID: d7e8f9a0b1c2
Revises: b3c4d5e6f789
Create Date: 2026-06-15 00:00:00.000000

#182: per-user brand design systems (#181) generated from an uploaded
template's BrandProfile are registered here so the template picker can
select a `design_system` directly instead of only content-grammar guidance.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d7e8f9a0b1c2"
down_revision: Union[str, Sequence[str], None] = "b3c4d5e6f789"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    with op.batch_alter_table("document_templates", schema=None) as batch_op:
        if not _column_exists("document_templates", "design_system_id"):
            batch_op.add_column(sa.Column("design_system_id", sa.String(length=128), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("document_templates", schema=None) as batch_op:
        if _column_exists("document_templates", "design_system_id"):
            batch_op.drop_column("design_system_id")
