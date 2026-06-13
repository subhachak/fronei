"""add research evidence metadata

Revision ID: f7a8b9c01234
Revises: f6a7b8901234, a1b2c3d4e5f6
Create Date: 2026-06-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import column_exists, index_exists, table_exists


revision: str = "f7a8b9c01234"
down_revision: Union[str, Sequence[str], None] = ("f6a7b8901234", "a1b2c3d4e5f6")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if table_exists("research_questions"):
        with op.batch_alter_table("research_questions", schema=None) as batch_op:
            if not column_exists("research_questions", "claim_type"):
                batch_op.add_column(sa.Column("claim_type", sa.String(32), nullable=True, server_default="unknown"))
            if not column_exists("research_questions", "evidence_role"):
                batch_op.add_column(sa.Column("evidence_role", sa.String(48), nullable=True, server_default="background_context"))
            if not column_exists("research_questions", "freshness_requirement"):
                batch_op.add_column(sa.Column("freshness_requirement", sa.String(16), nullable=True, server_default="default"))
            if not column_exists("research_questions", "required_source_tiers_json"):
                batch_op.add_column(sa.Column("required_source_tiers_json", sa.Text(), nullable=True))
            if not column_exists("research_questions", "budget_json"):
                batch_op.add_column(sa.Column("budget_json", sa.Text(), nullable=True))
            if not column_exists("research_questions", "stop_reason"):
                batch_op.add_column(sa.Column("stop_reason", sa.Text(), nullable=True))
            if not column_exists("research_questions", "confidence"):
                batch_op.add_column(sa.Column("confidence", sa.String(32), nullable=True))

    if not table_exists("research_sources"):
        pass
    else:
        with op.batch_alter_table("research_sources", schema=None) as batch_op:
            if not column_exists("research_sources", "source_tier"):
                batch_op.add_column(sa.Column("source_tier", sa.String(32), nullable=True, server_default="tier_2_expert"))
            if not column_exists("research_sources", "source_family"):
                batch_op.add_column(sa.Column("source_family", sa.String(255), nullable=True))
            if not column_exists("research_sources", "source_role_prior"):
                batch_op.add_column(sa.Column("source_role_prior", sa.String(48), nullable=True, server_default="background_context"))
            if not column_exists("research_sources", "published_at"):
                batch_op.add_column(sa.Column("published_at", sa.DateTime(), nullable=True))
            if not column_exists("research_sources", "updated_at"):
                batch_op.add_column(sa.Column("updated_at", sa.DateTime(), nullable=True))
            if not column_exists("research_sources", "source_date_confidence"):
                batch_op.add_column(sa.Column("source_date_confidence", sa.String(16), nullable=True, server_default="unknown"))
            if not column_exists("research_sources", "admission_status"):
                batch_op.add_column(sa.Column("admission_status", sa.String(24), nullable=True, server_default="admitted"))
            if not column_exists("research_sources", "admission_reason"):
                batch_op.add_column(sa.Column("admission_reason", sa.Text(), nullable=True))

        if not index_exists("research_sources", "ix_research_sources_run_tier"):
            op.create_index("ix_research_sources_run_tier", "research_sources", ["run_id", "source_tier"])
        if not index_exists("research_sources", "ix_research_sources_run_family"):
            op.create_index("ix_research_sources_run_family", "research_sources", ["run_id", "source_family"])

    if table_exists("research_claims"):
        with op.batch_alter_table("research_claims", schema=None) as batch_op:
            if not column_exists("research_claims", "claim_type"):
                batch_op.add_column(sa.Column("claim_type", sa.String(32), nullable=True, server_default="unknown"))
            if not column_exists("research_claims", "claim_role"):
                batch_op.add_column(sa.Column("claim_role", sa.String(48), nullable=True, server_default="background_context"))
            if not column_exists("research_claims", "freshness_risk"):
                batch_op.add_column(sa.Column("freshness_risk", sa.String(16), nullable=True, server_default="unknown"))


def downgrade() -> None:
    if table_exists("research_questions"):
        with op.batch_alter_table("research_questions", schema=None) as batch_op:
            for column in [
                "confidence",
                "stop_reason",
                "budget_json",
                "required_source_tiers_json",
                "freshness_requirement",
                "evidence_role",
                "claim_type",
            ]:
                if column_exists("research_questions", column):
                    batch_op.drop_column(column)

    if not table_exists("research_sources"):
        pass
    else:
        if index_exists("research_sources", "ix_research_sources_run_family"):
            op.drop_index("ix_research_sources_run_family", table_name="research_sources")
        if index_exists("research_sources", "ix_research_sources_run_tier"):
            op.drop_index("ix_research_sources_run_tier", table_name="research_sources")
        with op.batch_alter_table("research_sources", schema=None) as batch_op:
            for column in [
                "admission_reason",
                "admission_status",
                "source_date_confidence",
                "updated_at",
                "published_at",
                "source_role_prior",
                "source_family",
                "source_tier",
            ]:
                if column_exists("research_sources", column):
                    batch_op.drop_column(column)
    if table_exists("research_claims"):
        with op.batch_alter_table("research_claims", schema=None) as batch_op:
            for column in ["freshness_risk", "claim_role", "claim_type"]:
                if column_exists("research_claims", column):
                    batch_op.drop_column(column)
