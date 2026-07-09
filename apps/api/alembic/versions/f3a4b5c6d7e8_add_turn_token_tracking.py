"""add token-budget tracking columns to turns

Revision ID: f3a4b5c6d7e8
Revises: f2d3e4f5a6b7
Create Date: 2026-07-09
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import column_exists


revision: str = "f3a4b5c6d7e8"
down_revision: Union[str, Sequence[str], None] = "f2d3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns = [
        ("input_tokens", sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0")),
        ("output_tokens", sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0")),
        ("context_tokens_json", sa.Column("context_tokens_json", sa.Text(), nullable=False, server_default="{}")),
    ]
    for name, column in columns:
        if not column_exists("turns", name):
            op.add_column("turns", column)


def downgrade() -> None:
    for name in ["context_tokens_json", "output_tokens", "input_tokens"]:
        if column_exists("turns", name):
            op.drop_column("turns", name)
