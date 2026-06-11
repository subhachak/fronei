"""Shared helpers for writing idempotent Alembic migrations.

Production (Neon Postgres) was originally bootstrapped via SQLAlchemy's
`Base.metadata.create_all()` rather than Alembic, so the DB may not have an
`alembic_version` row reflecting its true schema state. To make `alembic
upgrade head` safe to run from *any* starting point — including a completely
unstamped DB where every table/column already exists — every migration's
`upgrade()` should guard its DDL with these helpers instead of assuming a
clean slate.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


def table_exists(table: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table in inspector.get_table_names()


def column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def index_exists(table: str, index: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    return index in {ix["name"] for ix in inspector.get_indexes(table)}
