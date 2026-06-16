"""phase c registry tables

Revision ID: a9b8c7d6e5f4
Revises: f8a9b0c1d2e3
Create Date: 2026-06-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.migration_helpers import index_exists, table_exists


revision: str = "a9b8c7d6e5f4"
down_revision: Union[str, Sequence[str], None] = "f8a9b0c1d2e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    ]


def upgrade() -> None:
    if not table_exists("agent_definitions"):
        op.create_table(
            "agent_definitions",
            sa.Column("id", sa.Text(), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("role", sa.Text(), nullable=False),
            sa.Column("prompt_template_id", sa.Text(), nullable=False),
            sa.Column("allowed_tools", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("model_policy_id", sa.Text(), nullable=False),
            sa.Column("guardrail_policy_ids", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("judge_policy_id", sa.Text(), nullable=True),
            sa.Column("max_iterations", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("max_tool_calls", sa.Integer(), nullable=False, server_default="4"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("version", sa.Text(), nullable=False, server_default="1.0.0"),
            *_timestamps(),
            sa.PrimaryKeyConstraint("id"),
        )

    if not table_exists("prompt_templates"):
        op.create_table(
            "prompt_templates",
            sa.Column("id", sa.Text(), nullable=False),
            sa.Column("agent_id", sa.Text(), nullable=False),
            sa.Column("version", sa.Text(), nullable=False, server_default="1.0.0"),
            sa.Column("system_prompt", sa.Text(), nullable=False),
            sa.Column("developer_prompt", sa.Text(), nullable=True),
            sa.Column("output_schema", sa.Text(), nullable=True),
            sa.Column("variables", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
            *_timestamps(),
            sa.PrimaryKeyConstraint("id"),
        )
    if not index_exists("prompt_templates", "ix_prompt_templates_agent_id_status"):
        op.create_index(
            "ix_prompt_templates_agent_id_status",
            "prompt_templates",
            ["agent_id", "status"],
            unique=False,
        )

    if not table_exists("model_policies"):
        op.create_table(
            "model_policies",
            sa.Column("id", sa.Text(), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("allowed_models", sa.Text(), nullable=False),
            sa.Column("primary_model", sa.Text(), nullable=False),
            sa.Column("fallback_models", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("max_input_tokens", sa.Integer(), nullable=False, server_default="16000"),
            sa.Column("max_output_tokens", sa.Integer(), nullable=False, server_default="2000"),
            sa.Column("max_cost_usd_per_call", sa.Float(), nullable=False, server_default="0.10"),
            sa.Column("timeout_ms", sa.Integer(), nullable=False, server_default="30000"),
            sa.Column("parallel_fallback_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("quality_modes", sa.Text(), nullable=False, server_default='["draft","standard"]'),
            sa.Column("sensitive_domain_allowed", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("version", sa.Text(), nullable=False, server_default="1.0.0"),
            *_timestamps(),
            sa.PrimaryKeyConstraint("id"),
        )

    if not table_exists("tool_definitions"):
        op.create_table(
            "tool_definitions",
            sa.Column("id", sa.Text(), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("input_schema", sa.Text(), nullable=False),
            sa.Column("output_schema", sa.Text(), nullable=False),
            sa.Column("allowed_agent_ids", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("required_user_roles", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("guardrail_policy_ids", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("timeout_ms", sa.Integer(), nullable=False, server_default="15000"),
            sa.Column("retry_policy", sa.Text(), nullable=False, server_default='{"max_attempts":1}'),
            sa.Column("idempotent", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("backend", sa.Text(), nullable=False, server_default="native"),
            sa.Column("backend_ref", sa.Text(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("version", sa.Text(), nullable=False, server_default="1.0.0"),
            *_timestamps(),
            sa.PrimaryKeyConstraint("id"),
        )

    if not table_exists("guardrail_policies"):
        op.create_table(
            "guardrail_policies",
            sa.Column("id", sa.Text(), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("applies_to", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("checks", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("action_map", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("severity", sa.Text(), nullable=False, server_default="medium"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("version", sa.Text(), nullable=False, server_default="1.0.0"),
            *_timestamps(),
            sa.PrimaryKeyConstraint("id"),
        )


def downgrade() -> None:
    if table_exists("prompt_templates") and index_exists("prompt_templates", "ix_prompt_templates_agent_id_status"):
        op.drop_index("ix_prompt_templates_agent_id_status", table_name="prompt_templates")

    for table in [
        "guardrail_policies",
        "tool_definitions",
        "model_policies",
        "prompt_templates",
        "agent_definitions",
    ]:
        if table_exists(table):
            op.drop_table(table)
