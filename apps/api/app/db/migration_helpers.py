"""Shared helpers for writing idempotent Alembic migrations.

Some legacy databases were originally bootstrapped via SQLAlchemy
`create_all()` before Alembic became authoritative. These helpers keep older
migrations idempotent for those historical databases. Application startup no
longer creates or repairs schema; all new schema evolution belongs in Alembic.
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
