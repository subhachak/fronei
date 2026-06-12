"""add plan_json to conversation_messages

Revision ID: f4a5b6c7d890
Revises: e3f4a5b6c789
Create Date: 2026-06-12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import column_exists


revision: str = "f4a5b6c7d890"
down_revision: Union[str, Sequence[str], None] = "e3f4a5b6c789"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not column_exists("conversation_messages", "plan_json"):
        op.add_column(
            "conversation_messages",
            sa.Column("plan_json", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    if column_exists("conversation_messages", "plan_json"):
        op.drop_column("conversation_messages", "plan_json")
