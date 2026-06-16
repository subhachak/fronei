"""phase v job checkpoint and agent trace rows

Revision ID: c0d1e2f3a4b5
Revises: b0c1d2e3f4a5
Create Date: 2026-06-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import index_exists, table_exists


revision: str = "c0d1e2f3a4b5"
down_revision: Union[str, Sequence[str], None] = "b0c1d2e3f4a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not table_exists("job_checkpoint"):
        op.create_table(
            "job_checkpoint",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("turn_id", sa.String(64), nullable=False),
            sa.Column("stage", sa.String(64), nullable=False),
            sa.Column("payload", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.UniqueConstraint("turn_id", "stage", name="uq_job_checkpoint_turn_stage"),
        )
    if not index_exists("job_checkpoint", "ix_job_checkpoint_turn_id"):
        op.create_index("ix_job_checkpoint_turn_id", "job_checkpoint", ["turn_id"], unique=False)

    if not table_exists("agent_traces"):
        op.create_table(
            "agent_traces",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("data_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )


def downgrade() -> None:
    if table_exists("agent_traces"):
        op.drop_table("agent_traces")
    if table_exists("job_checkpoint"):
        if index_exists("job_checkpoint", "ix_job_checkpoint_turn_id"):
            op.drop_index("ix_job_checkpoint_turn_id", table_name="job_checkpoint")
        op.drop_table("job_checkpoint")
