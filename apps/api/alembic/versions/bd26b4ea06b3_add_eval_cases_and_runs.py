"""add eval_cases and eval_runs tables

Revision ID: a1b2c3d4e5f6
Revises: cc1dd2ee3ff4
Create Date: 2026-06-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import index_exists, table_exists

revision: str = "bd26b4ea06b3"
down_revision: Union[str, Sequence[str], None] = "cc1dd2ee3ff4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not table_exists("eval_cases"):
        op.create_table(
            "eval_cases",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("title", sa.String(256), nullable=False),
            sa.Column("query", sa.Text(), nullable=False),
            sa.Column("category", sa.String(128), nullable=True),
            sa.Column("expected_criteria_json", sa.Text(), nullable=True),
            sa.Column("expected_primary_role", sa.String(64), nullable=True),
            sa.Column("min_independent_sources", sa.Integer(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_by", sa.String(128), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )

    if not table_exists("eval_runs"):
        op.create_table(
            "eval_runs",
            sa.Column("id", sa.String(64), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="running"),
            sa.Column("started_by", sa.String(128), nullable=True),
            sa.Column("case_ids_json", sa.Text(), nullable=True),
            sa.Column("results_json", sa.Text(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        if not index_exists("eval_runs", "ix_eval_runs_status"):
            op.create_index("ix_eval_runs_status", "eval_runs", ["status"])
        if not index_exists("eval_runs", "ix_eval_runs_started_at"):
            op.create_index("ix_eval_runs_started_at", "eval_runs", ["started_at"])


def downgrade() -> None:
    op.drop_table("eval_runs")
    op.drop_table("eval_cases")
