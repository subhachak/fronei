"""add eval_case benchmark thresholds, consolidate categories

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import column_exists

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Old (pre-consolidation) category -> new consolidated category. Cases with a
# category not listed here (including NULL) are left untouched.
CATEGORY_CONSOLIDATION_MAP = {
    "time_sensitive_factual_routing": "routing_classification",
    "research_level_classification": "routing_classification",
    "deep_classification": "routing_classification",
    "entity_status_check": "routing_classification",
    "freshness": "freshness_facts",
    "simple_fact": "freshness_facts",
    "subject_extraction": "subject_extraction",
    "multi_subject_coverage": "subject_extraction",
    "multi_subject_generalization": "subject_extraction",
    "conflict": "evidence_quality",
    "medical_conflict": "evidence_quality",
    "independence": "evidence_quality",
    "operational_noisy": "evidence_quality",
    "operational_primary": "evidence_quality",
    "immigration_operational": "domain_specific",
    "immigration_policy": "domain_specific",
    "medical": "domain_specific",
    "financial": "domain_specific",
    "tech_product": "domain_specific",
    "tech_product_operational": "domain_specific",
    "permission_asking_boundary": "answer_behavior",
}


def upgrade() -> None:
    if not column_exists("eval_cases", "min_evidence_items"):
        op.add_column("eval_cases", sa.Column("min_evidence_items", sa.Integer(), nullable=True))
    if not column_exists("eval_cases", "min_criteria_score"):
        op.add_column("eval_cases", sa.Column("min_criteria_score", sa.Float(), nullable=True))

    bind = op.get_bind()
    eval_cases = sa.table("eval_cases", sa.column("category", sa.String))
    for old_category, new_category in CATEGORY_CONSOLIDATION_MAP.items():
        bind.execute(
            eval_cases.update()
            .where(eval_cases.c.category == old_category)
            .values(category=new_category)
        )


def downgrade() -> None:
    # Category consolidation is one-way (the original fine-grained categories
    # aren't recoverable from the consolidated ones); only the schema change
    # is reversed.
    op.drop_column("eval_cases", "min_criteria_score")
    op.drop_column("eval_cases", "min_evidence_items")
