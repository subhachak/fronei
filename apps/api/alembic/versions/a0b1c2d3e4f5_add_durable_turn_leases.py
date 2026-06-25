"""add durable turn leases

Revision ID: a0b1c2d3e4f5
Revises: f5e6f7a8b9c0
Create Date: 2026-06-24
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import column_exists, index_exists


revision: str = "a0b1c2d3e4f5"
down_revision: Union[str, Sequence[str], None] = "f5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns = [
        ("request_json", sa.Column("request_json", sa.Text(), nullable=False, server_default="{}")),
        ("attempt_count", sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0")),
        ("max_attempts", sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3")),
        ("lease_owner", sa.Column("lease_owner", sa.String(length=128), nullable=True)),
        ("lease_expires_at", sa.Column("lease_expires_at", sa.DateTime(), nullable=True)),
        ("heartbeat_at", sa.Column("heartbeat_at", sa.DateTime(), nullable=True)),
        ("cancel_requested", sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false())),
    ]
    for name, column in columns:
        if not column_exists("turns", name):
            op.add_column("turns", column)
    if not index_exists("turns", "ix_turns_lease_owner"):
        op.create_index("ix_turns_lease_owner", "turns", ["lease_owner"], unique=False)
    if not index_exists("turns", "ix_turns_lease_expires_at"):
        op.create_index("ix_turns_lease_expires_at", "turns", ["lease_expires_at"], unique=False)


def downgrade() -> None:
    if index_exists("turns", "ix_turns_lease_expires_at"):
        op.drop_index("ix_turns_lease_expires_at", table_name="turns")
    if index_exists("turns", "ix_turns_lease_owner"):
        op.drop_index("ix_turns_lease_owner", table_name="turns")
    for name in [
        "cancel_requested",
        "heartbeat_at",
        "lease_expires_at",
        "lease_owner",
        "max_attempts",
        "attempt_count",
        "request_json",
    ]:
        if column_exists("turns", name):
            op.drop_column("turns", name)
