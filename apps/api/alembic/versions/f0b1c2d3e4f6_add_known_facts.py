"""add known facts structured store

Revision ID: f0b1c2d3e4f6
Revises: f0a1b2c3d4e5
Create Date: 2026-07-03
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "f0b1c2d3e4f6"
down_revision: Union[str, Sequence[str], None] = "f0a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS known_facts (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            fact_key TEXT NOT NULL,
            fact_value TEXT NOT NULL,
            source_conversation_id TEXT,
            confidence REAL NOT NULL DEFAULT 1.0,
            last_verified_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_known_facts_user_entity_key
        ON known_facts (user_id, entity_id, fact_key)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_known_facts_user_entity_type
        ON known_facts (user_id, entity_type)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_known_facts_user_entity_type")
    op.execute("DROP INDEX IF EXISTS ux_known_facts_user_entity_key")
    op.execute("DROP TABLE IF EXISTS known_facts")
