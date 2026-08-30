"""The CELPIP migration must produce DDL Postgres accepts, not just SQLite.

This exists because it didn't. The migration's boolean columns were written with
`server_default=sa.text('0')`, which SQLite takes happily and Postgres rejects
outright:

    column "is_unscored" is of type boolean but default expression is of type
    integer

Local development and the whole test suite run on SQLite, so nothing caught it
until a production deploy failed at the migration step. Generating the DDL for
the Postgres dialect here closes that gap without needing a Postgres to talk to.
"""
from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path

import pytest
from alembic.config import Config

import app.config
import app.db.migration_helpers as migration_helpers
from alembic import command

API_ROOT = Path(__file__).resolve().parents[1]
CELPIP_REVISION = "c7d1e2f3a4b5"
PREVIOUS_REVISION = "1bc49879334b"


@pytest.fixture
def postgres_ddl(monkeypatch) -> str:
    """Render the CELPIP migration as Postgres DDL, without a database.

    Alembic's offline mode cannot run this migration as-is: its idempotency
    guards call `inspect()` on the connection, and offline mode has none. The
    guards are stubbed to "nothing exists yet", which is exactly the state a
    fresh production database is in.
    """
    monkeypatch.setattr(migration_helpers, "table_exists", lambda table: False)
    monkeypatch.setattr(migration_helpers, "index_exists", lambda table, index: False)

    # alembic/env.py reads the URL from app settings. Patching `get_settings`
    # rather than the DATABASE_URL environment variable keeps this out of the
    # process-wide settings cache: clearing that cache mid-run leaks a Postgres
    # URL into whatever test happens to read settings next, which is a very
    # confusing failure to chase down two files away.
    pg_settings = app.config.get_settings().model_copy(
        update={"database_url": "postgresql+psycopg2://u:p@localhost:5432/verify"}
    )
    monkeypatch.setattr(app.config, "get_settings", lambda: pg_settings)

    # Built WITHOUT alembic.ini on purpose. env.py calls
    # logging.config.fileConfig() whenever a config file is present, and that
    # runs with disable_existing_loggers=True -- it silences every logger
    # created before it, so later tests asserting on log output fail in a way
    # that points nowhere near this file. Skipping the ini file skips that.
    config = Config()
    config.set_main_option("script_location", str(API_ROOT / "alembic"))

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        command.upgrade(config, f"{PREVIOUS_REVISION}:{CELPIP_REVISION}", sql=True)
    return buffer.getvalue()


def _column_lines(ddl: str) -> list[str]:
    return [line.strip() for line in ddl.splitlines() if line.strip()]


def test_every_celpip_table_is_created(postgres_ddl: str) -> None:
    created = set(re.findall(r"CREATE TABLE (celpip_\w+)", postgres_ddl))
    assert created == {
        "celpip_profiles",
        "celpip_lessons",
        "celpip_questions",
        "celpip_question_assets",
        "celpip_tests",
        "celpip_test_items",
        "celpip_attempts",
        "celpip_responses",
        "celpip_evaluations",
        "celpip_study_plan_items",
        "celpip_generation_runs",
    }


def test_boolean_columns_default_to_a_boolean_literal(postgres_ddl: str) -> None:
    """`BOOLEAN DEFAULT 0` is the exact statement Postgres refused."""
    booleans = [line for line in _column_lines(postgres_ddl) if "BOOLEAN" in line]
    assert booleans, "expected the migration to create boolean columns"

    integer_defaults = [
        line for line in booleans if re.search(r"BOOLEAN\s+DEFAULT\s+'?[01]'?\b", line)
    ]
    assert integer_defaults == [], (
        "boolean columns must default to true/false, not 0/1 -- Postgres rejects "
        f"the integer form: {integer_defaults}"
    )
    for line in booleans:
        if "DEFAULT" in line:
            assert re.search(r"DEFAULT\s+(true|false)\b", line, re.IGNORECASE), line


def test_the_ddl_names_the_postgres_dialect_types(postgres_ddl: str) -> None:
    """A sanity check that this really rendered for Postgres and not SQLite --
    otherwise the assertions above would be testing the wrong dialect."""
    assert "SERIAL" not in postgres_ddl  # no autoincrement columns in this migration
    assert "VARCHAR(64)" in postgres_ddl
    assert "DATETIME" not in postgres_ddl, "DATETIME is SQLite's spelling; Postgres uses TIMESTAMP"
    assert "TIMESTAMP" in postgres_ddl


def test_the_migration_stamps_its_revision(postgres_ddl: str) -> None:
    assert CELPIP_REVISION in postgres_ddl
