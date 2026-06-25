"""add durable maintenance jobs

Revision ID: b1c2d3e4f5a6
Revises: a0b1c2d3e4f5
Create Date: 2026-06-25
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import index_exists, table_exists


revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, Sequence[str], None] = "a0b1c2d3e4f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not table_exists("maintenance_jobs"):
        op.create_table(
            "maintenance_jobs",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("job_type", sa.String(length=64), nullable=False),
            sa.Column("dedupe_key", sa.String(length=160), nullable=True),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="queued"),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("result_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("lease_owner", sa.String(length=128), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
            sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
    for name, columns in [
        ("ix_maintenance_jobs_job_type", ["job_type"]),
        ("ix_maintenance_jobs_dedupe_key", ["dedupe_key"]),
        ("ix_maintenance_jobs_status", ["status"]),
        ("ix_maintenance_jobs_lease_owner", ["lease_owner"]),
        ("ix_maintenance_jobs_lease_expires_at", ["lease_expires_at"]),
    ]:
        if not index_exists("maintenance_jobs", name):
            op.create_index(
                name,
                "maintenance_jobs",
                columns,
                unique=name == "ix_maintenance_jobs_dedupe_key",
            )


def downgrade() -> None:
    if table_exists("maintenance_jobs"):
        op.drop_table("maintenance_jobs")
