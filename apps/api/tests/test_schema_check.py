from __future__ import annotations

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from app.db import schema_check


def _expected_head() -> str:
    config = Config(str(schema_check._ALEMBIC_INI))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1
    return heads[0]


def _stamp(engine, revision: str) -> None:
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": revision},
        )


def test_schema_check_rejects_blank_database():
    engine = create_engine("sqlite:///:memory:")

    with pytest.raises(schema_check.SchemaVersionMismatch, match="unstamped"):
        schema_check.check_schema_version(engine)
    assert inspect(engine).get_table_names() == []


def test_schema_check_rejects_stale_sqlite_database():
    engine = create_engine("sqlite:///:memory:")
    _stamp(engine, "f5e6f7a8b9c0")

    with pytest.raises(schema_check.SchemaVersionMismatch, match="Run `alembic upgrade head`"):
        schema_check.check_schema_version(engine)


def test_schema_check_accepts_database_at_head():
    engine = create_engine("sqlite:///:memory:")
    _stamp(engine, _expected_head())

    schema_check.check_schema_version(engine)
