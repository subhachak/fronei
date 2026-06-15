"""add component usage stats

Revision ID: c2d3e4f5a6b7
Revises: f9a0b1c2d3e4
Create Date: 2026-06-14
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import index_exists, table_exists


revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, Sequence[str], None] = "f9a0b1c2d3e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not table_exists("component_usage_stats"):
        op.create_table(
            "component_usage_stats",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("component_id", sa.String(length=64), nullable=False),
            sa.Column("slide_layout", sa.String(length=64), nullable=False),
            sa.Column("design_system", sa.String(length=64), nullable=False, server_default="agentdeck_v1"),
            sa.Column("theme", sa.String(length=16), nullable=False, server_default="dark"),
            sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            if_not_exists=True,
        )
    if not index_exists("component_usage_stats", "ix_component_usage_stats_component_id"):
        op.create_index(
            "ix_component_usage_stats_component_id", "component_usage_stats", ["component_id"], unique=False
        )
    if not index_exists("component_usage_stats", "ix_component_usage_stats_slide_layout"):
        op.create_index(
            "ix_component_usage_stats_slide_layout", "component_usage_stats", ["slide_layout"], unique=False
        )
    if not index_exists("component_usage_stats", "ix_component_usage_stats_key"):
        op.create_index(
            "ix_component_usage_stats_key",
            "component_usage_stats",
            ["component_id", "slide_layout", "design_system", "theme"],
            unique=True,
        )


def downgrade() -> None:
    if table_exists("component_usage_stats"):
        if index_exists("component_usage_stats", "ix_component_usage_stats_key"):
            op.drop_index("ix_component_usage_stats_key", table_name="component_usage_stats")
        if index_exists("component_usage_stats", "ix_component_usage_stats_slide_layout"):
            op.drop_index("ix_component_usage_stats_slide_layout", table_name="component_usage_stats")
        if index_exists("component_usage_stats", "ix_component_usage_stats_component_id"):
            op.drop_index("ix_component_usage_stats_component_id", table_name="component_usage_stats")
        op.drop_table("component_usage_stats")
