"""add v2_spec_json to eval_cases (scoring_spec.md v2 schema)

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-06-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import column_exists

revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not column_exists("eval_cases", "v2_spec_json"):
        op.add_column("eval_cases", sa.Column("v2_spec_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("eval_cases", "v2_spec_json")
