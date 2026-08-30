"""add celpip preparation app tables

Creates the eleven tables backing the admin-only CELPIP preparation app
(docs/celpip-app-plan.md). The CELPIP workspace deliberately owns its own
schema rather than reusing the conversational agent's tables: a timed exam has
a server-authoritative clock, a fixed per-task item schema, and a two-evaluator
scoring pipeline with nothing in common with a chat turn.

Idempotent (table_exists / index_exists guards) in line with the rest of this
migration tree, so a partially-applied run is safe to repeat.

Revision ID: c7d1e2f3a4b5
Revises: 1bc49879334b
Create Date: 2026-08-30
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import table_exists

revision: str = "c7d1e2f3a4b5"
down_revision: Union[str, Sequence[str], None] = "1bc49879334b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CELPIP_TABLES: tuple[str, ...] = (
    "celpip_generation_runs",
    "celpip_study_plan_items",
    "celpip_evaluations",
    "celpip_responses",
    "celpip_attempts",
    "celpip_test_items",
    "celpip_tests",
    "celpip_question_assets",
    "celpip_questions",
    "celpip_lessons",
    "celpip_profiles",
)


def upgrade() -> None:
    if not table_exists("celpip_profiles"):
        op.create_table(
            "celpip_profiles",
            sa.Column("id", sa.String(length=64), nullable=False, primary_key=True),
            sa.Column("user_id", sa.String(length=128), nullable=False),
            sa.Column("test_type", sa.String(length=16), nullable=False, server_default='general'),
            sa.Column("test_date", sa.Date(), nullable=True),
            sa.Column("target_level", sa.Integer(), nullable=False, server_default='9'),
            sa.Column("weekday_hours", sa.Float(), nullable=False, server_default='1.0'),
            sa.Column("weekend_hours", sa.Float(), nullable=False, server_default='2.0'),
            sa.Column("self_reported_weaknesses_json", sa.Text(), nullable=False, server_default='[]'),
            sa.Column("onboarding_state", sa.String(length=16), nullable=False, server_default='pending'),
            sa.Column("diagnostic_attempt_id", sa.String(length=64), nullable=True),
            sa.Column("readiness_json", sa.Text(), nullable=False, server_default='{}'),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_celpip_profiles_user_id", "celpip_profiles", ["user_id"], unique=True)

    if not table_exists("celpip_lessons"):
        op.create_table(
            "celpip_lessons",
            sa.Column("id", sa.String(length=64), nullable=False, primary_key=True),
            sa.Column("slug", sa.String(length=160), nullable=False),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("category", sa.String(length=32), nullable=False, server_default='overview'),
            sa.Column("skill", sa.String(length=16), nullable=True),
            sa.Column("task_key", sa.String(length=64), nullable=True),
            sa.Column("summary", sa.Text(), nullable=False, server_default=''),
            sa.Column("body_markdown", sa.Text(), nullable=False, server_default=''),
            sa.Column("weakness_tags_json", sa.Text(), nullable=False, server_default='[]'),
            sa.Column("estimated_minutes", sa.Integer(), nullable=False, server_default='5'),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default='0'),
            sa.Column("source_hash", sa.String(length=64), nullable=False, server_default=''),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_celpip_lessons_slug", "celpip_lessons", ["slug"], unique=True)
        op.create_index("ix_celpip_lessons_category", "celpip_lessons", ["category"], unique=False)
        op.create_index("ix_celpip_lessons_skill", "celpip_lessons", ["skill"], unique=False)
        op.create_index("ix_celpip_lessons_task_key", "celpip_lessons", ["task_key"], unique=False)

    if not table_exists("celpip_questions"):
        op.create_table(
            "celpip_questions",
            sa.Column("id", sa.String(length=64), nullable=False, primary_key=True),
            sa.Column("skill", sa.String(length=16), nullable=False),
            sa.Column("task_key", sa.String(length=64), nullable=False),
            sa.Column("part", sa.Integer(), nullable=False, server_default='1'),
            sa.Column("title", sa.String(length=255), nullable=False, server_default=''),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default='{}'),
            sa.Column("difficulty", sa.Integer(), nullable=False, server_default='9'),
            sa.Column("topic", sa.String(length=120), nullable=False, server_default=''),
            sa.Column("source", sa.String(length=16), nullable=False, server_default='generated'),
            sa.Column("status", sa.String(length=16), nullable=False, server_default='draft'),
            sa.Column("validation_json", sa.Text(), nullable=False, server_default='{}'),
            sa.Column("validated_at", sa.DateTime(), nullable=True),
            sa.Column("approved_at", sa.DateTime(), nullable=True),
            sa.Column("generation_run_id", sa.String(length=64), nullable=True),
            sa.Column("generator_model", sa.String(length=120), nullable=False, server_default=''),
            sa.Column("validator_model", sa.String(length=120), nullable=False, server_default=''),
            sa.Column("spec_version", sa.String(length=32), nullable=False, server_default=''),
            sa.Column("content_fingerprint", sa.String(length=64), nullable=False, server_default=''),
            sa.Column("times_served", sa.Integer(), nullable=False, server_default='0'),
            sa.Column("last_served_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_celpip_questions_skill", "celpip_questions", ["skill"], unique=False)
        op.create_index("ix_celpip_questions_task_key", "celpip_questions", ["task_key"], unique=False)
        op.create_index("ix_celpip_questions_status", "celpip_questions", ["status"], unique=False)
        op.create_index("ix_celpip_questions_generation_run_id", "celpip_questions", ["generation_run_id"], unique=False)
        op.create_index("ix_celpip_questions_content_fingerprint", "celpip_questions", ["content_fingerprint"], unique=False)

    if not table_exists("celpip_question_assets"):
        op.create_table(
            "celpip_question_assets",
            sa.Column("id", sa.String(length=64), nullable=False, primary_key=True),
            sa.Column("question_id", sa.String(length=64), nullable=False),
            sa.Column("kind", sa.String(length=16), nullable=False, server_default='audio'),
            sa.Column("segment_index", sa.Integer(), nullable=False, server_default='0'),
            sa.Column("blob_location", sa.String(length=1024), nullable=True),
            sa.Column("content_type", sa.String(length=120), nullable=False, server_default=''),
            sa.Column("size_bytes", sa.Integer(), nullable=False, server_default='0'),
            sa.Column("sha256", sa.String(length=64), nullable=False, server_default=''),
            sa.Column("duration_seconds", sa.Float(), nullable=False, server_default='0.0'),
            sa.Column("text_content", sa.Text(), nullable=False, server_default=''),
            sa.Column("voice", sa.String(length=64), nullable=False, server_default=''),
            sa.Column("status", sa.String(length=16), nullable=False, server_default='pending'),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_celpip_question_assets_question_id", "celpip_question_assets", ["question_id"], unique=False)
        op.create_index("ix_celpip_question_assets_status", "celpip_question_assets", ["status"], unique=False)

    if not table_exists("celpip_tests"):
        op.create_table(
            "celpip_tests",
            sa.Column("id", sa.String(length=64), nullable=False, primary_key=True),
            sa.Column("user_id", sa.String(length=128), nullable=False),
            sa.Column("label", sa.String(length=255), nullable=False, server_default=''),
            sa.Column("mode", sa.String(length=24), nullable=False, server_default='custom'),
            sa.Column("components_json", sa.Text(), nullable=False, server_default='[]'),
            sa.Column("practice_mode", sa.String(length=16), nullable=False, server_default='timed'),
            sa.Column("target_level", sa.Integer(), nullable=False, server_default='9'),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_celpip_tests_user_id", "celpip_tests", ["user_id"], unique=False)
        op.create_index("ix_celpip_tests_mode", "celpip_tests", ["mode"], unique=False)

    if not table_exists("celpip_test_items"):
        op.create_table(
            "celpip_test_items",
            sa.Column("id", sa.String(length=64), nullable=False, primary_key=True),
            sa.Column("test_id", sa.String(length=64), nullable=False),
            sa.Column("question_id", sa.String(length=64), nullable=False),
            sa.Column("skill", sa.String(length=16), nullable=False),
            sa.Column("task_key", sa.String(length=64), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False, server_default='0'),
            sa.Column("is_unscored", sa.Boolean(), nullable=False, server_default=sa.text('0')),
            sa.Column("is_practice_task", sa.Boolean(), nullable=False, server_default=sa.text('0')),
        )
        op.create_index("ix_celpip_test_items_test_id", "celpip_test_items", ["test_id"], unique=False)
        op.create_index("ix_celpip_test_items_question_id", "celpip_test_items", ["question_id"], unique=False)

    if not table_exists("celpip_attempts"):
        op.create_table(
            "celpip_attempts",
            sa.Column("id", sa.String(length=64), nullable=False, primary_key=True),
            sa.Column("user_id", sa.String(length=128), nullable=False),
            sa.Column("test_id", sa.String(length=64), nullable=False),
            sa.Column("practice_mode", sa.String(length=16), nullable=False, server_default='timed'),
            sa.Column("status", sa.String(length=16), nullable=False, server_default='not_started'),
            sa.Column("section_state_json", sa.Text(), nullable=False, server_default='{}'),
            sa.Column("current_skill", sa.String(length=16), nullable=True),
            sa.Column("current_position", sa.Integer(), nullable=False, server_default='0'),
            sa.Column("flagged_json", sa.Text(), nullable=False, server_default='[]'),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("submitted_at", sa.DateTime(), nullable=True),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.Column("results_json", sa.Text(), nullable=False, server_default='{}'),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_celpip_attempts_user_id", "celpip_attempts", ["user_id"], unique=False)
        op.create_index("ix_celpip_attempts_test_id", "celpip_attempts", ["test_id"], unique=False)
        op.create_index("ix_celpip_attempts_status", "celpip_attempts", ["status"], unique=False)

    if not table_exists("celpip_responses"):
        op.create_table(
            "celpip_responses",
            sa.Column("id", sa.String(length=64), nullable=False, primary_key=True),
            sa.Column("attempt_id", sa.String(length=64), nullable=False),
            sa.Column("question_id", sa.String(length=64), nullable=False),
            sa.Column("skill", sa.String(length=16), nullable=False),
            sa.Column("task_key", sa.String(length=64), nullable=False),
            sa.Column("question_index", sa.Integer(), nullable=False, server_default='0'),
            sa.Column("selected_option", sa.String(length=16), nullable=True),
            sa.Column("response_text", sa.Text(), nullable=False, server_default=''),
            sa.Column("audio_blob_location", sa.String(length=1024), nullable=True),
            sa.Column("audio_duration_seconds", sa.Float(), nullable=False, server_default='0.0'),
            sa.Column("transcript", sa.Text(), nullable=False, server_default=''),
            sa.Column("transcript_words_json", sa.Text(), nullable=False, server_default='[]'),
            sa.Column("transcription_status", sa.String(length=16), nullable=False, server_default='none'),
            sa.Column("time_spent_ms", sa.Integer(), nullable=False, server_default='0'),
            sa.Column("flagged", sa.Boolean(), nullable=False, server_default=sa.text('0')),
            sa.Column("late", sa.Boolean(), nullable=False, server_default=sa.text('0')),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_celpip_responses_attempt_id", "celpip_responses", ["attempt_id"], unique=False)
        op.create_index("ix_celpip_responses_question_id", "celpip_responses", ["question_id"], unique=False)

    if not table_exists("celpip_evaluations"):
        op.create_table(
            "celpip_evaluations",
            sa.Column("id", sa.String(length=64), nullable=False, primary_key=True),
            sa.Column("attempt_id", sa.String(length=64), nullable=False),
            sa.Column("response_id", sa.String(length=64), nullable=True),
            sa.Column("question_id", sa.String(length=64), nullable=True),
            sa.Column("skill", sa.String(length=16), nullable=False),
            sa.Column("task_key", sa.String(length=64), nullable=False, server_default=''),
            sa.Column("method", sa.String(length=16), nullable=False, server_default='rubric'),
            sa.Column("status", sa.String(length=16), nullable=False, server_default='pending'),
            sa.Column("level_low", sa.Integer(), nullable=True),
            sa.Column("level_high", sa.Integer(), nullable=True),
            sa.Column("dimensions_json", sa.Text(), nullable=False, server_default='{}'),
            sa.Column("confidence", sa.Float(), nullable=False, server_default='0.0'),
            sa.Column("evaluator_a_json", sa.Text(), nullable=False, server_default='{}'),
            sa.Column("evaluator_b_json", sa.Text(), nullable=False, server_default='{}'),
            sa.Column("reconciliation_json", sa.Text(), nullable=False, server_default='{}'),
            sa.Column("feedback_json", sa.Text(), nullable=False, server_default='{}'),
            sa.Column("exemplar_json", sa.Text(), nullable=False, server_default='{}'),
            sa.Column("delivery_metrics_json", sa.Text(), nullable=False, server_default='{}'),
            sa.Column("weakness_tags_json", sa.Text(), nullable=False, server_default='[]'),
            sa.Column("evaluator_a_model", sa.String(length=120), nullable=False, server_default=''),
            sa.Column("evaluator_b_model", sa.String(length=120), nullable=False, server_default=''),
            sa.Column("reconciler_model", sa.String(length=120), nullable=False, server_default=''),
            sa.Column("rubric_version", sa.String(length=32), nullable=False, server_default=''),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_celpip_evaluations_attempt_id", "celpip_evaluations", ["attempt_id"], unique=False)
        op.create_index("ix_celpip_evaluations_response_id", "celpip_evaluations", ["response_id"], unique=False)
        op.create_index("ix_celpip_evaluations_question_id", "celpip_evaluations", ["question_id"], unique=False)
        op.create_index("ix_celpip_evaluations_skill", "celpip_evaluations", ["skill"], unique=False)
        op.create_index("ix_celpip_evaluations_status", "celpip_evaluations", ["status"], unique=False)

    if not table_exists("celpip_study_plan_items"):
        op.create_table(
            "celpip_study_plan_items",
            sa.Column("id", sa.String(length=64), nullable=False, primary_key=True),
            sa.Column("user_id", sa.String(length=128), nullable=False),
            sa.Column("scheduled_for", sa.Date(), nullable=False),
            sa.Column("week_index", sa.Integer(), nullable=False, server_default='1'),
            sa.Column("activity_type", sa.String(length=24), nullable=False, server_default='drill'),
            sa.Column("title", sa.String(length=255), nullable=False, server_default=''),
            sa.Column("rationale", sa.Text(), nullable=False, server_default=''),
            sa.Column("skill", sa.String(length=16), nullable=True),
            sa.Column("task_keys_json", sa.Text(), nullable=False, server_default='[]'),
            sa.Column("weakness_tags_json", sa.Text(), nullable=False, server_default='[]'),
            sa.Column("lesson_id", sa.String(length=64), nullable=True),
            sa.Column("estimated_minutes", sa.Integer(), nullable=False, server_default='30'),
            sa.Column("status", sa.String(length=16), nullable=False, server_default='pending'),
            sa.Column("attempt_id", sa.String(length=64), nullable=True),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.Column("reschedule_history_json", sa.Text(), nullable=False, server_default='[]'),
            sa.Column("plan_generation", sa.Integer(), nullable=False, server_default='1'),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_celpip_study_plan_items_user_id", "celpip_study_plan_items", ["user_id"], unique=False)
        op.create_index("ix_celpip_study_plan_items_scheduled_for", "celpip_study_plan_items", ["scheduled_for"], unique=False)
        op.create_index("ix_celpip_study_plan_items_status", "celpip_study_plan_items", ["status"], unique=False)

    if not table_exists("celpip_generation_runs"):
        op.create_table(
            "celpip_generation_runs",
            sa.Column("id", sa.String(length=64), nullable=False, primary_key=True),
            sa.Column("user_id", sa.String(length=128), nullable=False),
            sa.Column("task_key", sa.String(length=64), nullable=False),
            sa.Column("requested_count", sa.Integer(), nullable=False, server_default='1'),
            sa.Column("difficulty", sa.Integer(), nullable=False, server_default='9'),
            sa.Column("topic_hint", sa.String(length=255), nullable=False, server_default=''),
            sa.Column("status", sa.String(length=16), nullable=False, server_default='queued'),
            sa.Column("accepted_count", sa.Integer(), nullable=False, server_default='0'),
            sa.Column("rejected_count", sa.Integer(), nullable=False, server_default='0'),
            sa.Column("rejections_json", sa.Text(), nullable=False, server_default='[]'),
            sa.Column("question_ids_json", sa.Text(), nullable=False, server_default='[]'),
            sa.Column("generator_model", sa.String(length=120), nullable=False, server_default=''),
            sa.Column("validator_model", sa.String(length=120), nullable=False, server_default=''),
            sa.Column("spec_version", sa.String(length=32), nullable=False, server_default=''),
            sa.Column("job_id", sa.String(length=64), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_celpip_generation_runs_user_id", "celpip_generation_runs", ["user_id"], unique=False)
        op.create_index("ix_celpip_generation_runs_task_key", "celpip_generation_runs", ["task_key"], unique=False)
        op.create_index("ix_celpip_generation_runs_status", "celpip_generation_runs", ["status"], unique=False)
        op.create_index("ix_celpip_generation_runs_job_id", "celpip_generation_runs", ["job_id"], unique=False)


def downgrade() -> None:
    # Reverse creation order so nothing depends on a table already dropped.
    for table in CELPIP_TABLES:
        if table_exists(table):
            op.drop_table(table)
