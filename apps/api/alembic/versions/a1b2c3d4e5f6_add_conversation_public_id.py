"""add public_id (hex token) to conversations

Revision ID: a1b2c3d4e5f6
Revises: f4a5b6c7d890
Create Date: 2026-06-12 00:00:00.000000

"""
import secrets
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "f4a5b6c7d890"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    if not _column_exists("conversations", "public_id"):
        with op.batch_alter_table("conversations", schema=None) as batch_op:
            batch_op.add_column(sa.Column("public_id", sa.String(16), nullable=True))

        # Backfill existing rows with a random hex token.
        bind = op.get_bind()
        conv_table = sa.table(
            "conversations",
            sa.column("id", sa.Integer),
            sa.column("public_id", sa.String),
        )
        rows = bind.execute(sa.select(conv_table.c.id)).fetchall()
        seen: set[str] = set()
        for (conv_id,) in rows:
            token = secrets.token_hex(6)
            while token in seen:
                token = secrets.token_hex(6)
            seen.add(token)
            bind.execute(
                conv_table.update().where(conv_table.c.id == conv_id).values(public_id=token)
            )

        with op.batch_alter_table("conversations", schema=None) as batch_op:
            batch_op.alter_column("public_id", existing_type=sa.String(16), nullable=False)
            batch_op.create_unique_constraint("uq_conversations_public_id", ["public_id"])
            batch_op.create_index("ix_conversations_public_id", ["public_id"])


def downgrade() -> None:
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.drop_index("ix_conversations_public_id")
        batch_op.drop_constraint("uq_conversations_public_id", type_="unique")
        batch_op.drop_column("public_id")
