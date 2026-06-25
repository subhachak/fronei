"""Startup guard: verify the live database schema matches the code's
expected Alembic migration head.

This exists to catch the class of bug where a migration appears to succeed
in deploy logs — clean "Running upgrade ..." lines, no errors — but its DDL
is silently rolled back (e.g. an unflushed SQLAlchemy 2.0 autobegin
transaction left open in alembic/env.py before Alembic's own transaction
starts). Without this check, the app boots successfully against a stale
schema and only fails later, at request time, on the first query that
touches a missing column/table (psycopg2.errors.UndefinedColumn).
"""
from __future__ import annotations

import logging
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# apps/api/app/db/schema_check.py -> apps/api
_API_ROOT = Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _API_ROOT / "alembic.ini"


class SchemaVersionMismatch(RuntimeError):
    pass


def check_schema_version(engine: Engine) -> None:
    """Compare the DB's alembic_version table to the code's migration head.

    Raises SchemaVersionMismatch in every environment if they differ.
    Application startup never creates or repairs schema; run Alembic first.
    """
    cfg = Config(str(_ALEMBIC_INI))
    script = ScriptDirectory.from_config(cfg)
    expected_heads = set(script.get_heads())

    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        current_heads = set(context.get_current_heads())

    if current_heads != expected_heads:
        message = (
            f"Schema version mismatch: database is at {current_heads or '(unstamped)'}, "
            f"but code expects {expected_heads}. The database has not received "
            "migrations that the running code depends on (or a prior migration "
            "run did not commit). Run `alembic upgrade head` against this "
            "database, then redeploy."
        )
        raise SchemaVersionMismatch(message)
    else:
        logger.info("Schema version check OK: alembic head %s matches database.", current_heads)
