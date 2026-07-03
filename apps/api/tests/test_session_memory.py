from __future__ import annotations

import logging

from app.services.agent import model_client
from app.services.agent import session_memory


class _Dialect:
    def __init__(self, name: str):
        self.name = name


class _Bind:
    def __init__(self, name: str):
        self.dialect = _Dialect(name)


class _Db:
    def __init__(self, dialect: str = "sqlite"):
        self.bind = _Bind(dialect)
        self.committed = False
        self.executed = False
        self.statements: list[str] = []
        self.rolled_back = False

    def execute(self, *_args, **_kwargs):
        self.executed = True
        self.statements.append(str(_args[0]) if _args else "")
        return _Result()

    def get_bind(self):
        return self.bind

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class _Result:
    def fetchall(self):
        return [("Prior session summary",)]


def test_recall_similar_sessions_returns_empty_on_sqlite(monkeypatch):
    def fail_embed(*_args, **_kwargs):
        raise AssertionError("SQLite recall should not embed")

    monkeypatch.setattr(model_client, "embed", fail_embed)

    assert session_memory.recall_similar_sessions("user_1", "remember this", db=_Db("sqlite")) == []


def test_save_session_summary_noops_on_embedding_failure(monkeypatch):
    def fail_embed(*_args, **_kwargs):
        raise RuntimeError("embedding unavailable")

    db = _Db("postgresql")
    monkeypatch.setattr(model_client, "embed", fail_embed)

    session_memory.save_session_summary("user_1", "conv_1", "summary", db)

    assert db.executed is False
    assert db.committed is False
    assert db.rolled_back is True


def test_recall_similar_sessions_returns_empty_on_slow_query(monkeypatch, caplog):
    ticks = iter([0.0, 2.0])
    db = _Db("postgresql")
    monkeypatch.setattr(model_client, "embed", lambda *_args, **_kwargs: [0.1] * 1536)
    monkeypatch.setattr(session_memory.time, "perf_counter", lambda: next(ticks))

    with caplog.at_level(logging.WARNING):
        summaries = session_memory.recall_similar_sessions("user_1", "query", db=db)

    assert summaries == []
    assert db.statements[0] == "SET LOCAL statement_timeout = '1500ms'"
    assert "session_memory_recall_slow" in caplog.text
