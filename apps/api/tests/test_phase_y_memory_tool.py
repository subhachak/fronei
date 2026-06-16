from __future__ import annotations

from types import SimpleNamespace

from app.services.agent_runtime import memory_tool
from app.services.agent_runtime.guardrails import GuardrailService
from app.services.agent_runtime.native_backends import register_all
from app.services.agent_runtime.registry import _load_from_files
from app.services.agent_runtime.tool_runner import ToolRunner
from app.services.turn_graph.state import TurnGraphState


class _Query:
    def __init__(self, rows):
        self.rows = rows
        self.filters = []

    def filter(self, *args):
        self.filters.extend(args)
        return self

    def all(self):
        return self.rows


class _Session:
    def __init__(self, rows=None, *, fail=False):
        self.rows = rows or []
        self.fail = fail
        self.added = []
        self.committed = False

    def __enter__(self):
        if self.fail:
            raise RuntimeError("db down")
        return self

    def __exit__(self, *args):
        return False

    def query(self, _model):
        return _Query(self.rows)

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.committed = True


def test_memory_read_returns_ranked_memories_for_user(monkeypatch):
    rows = [
        SimpleNamespace(content="low", status="active", importance=0.1, confidence=0.1, seen_count=1, pinned=False, category="general", decay_rate=0.05, last_seen_at=None, updated_at=None),
        SimpleNamespace(content="high", status="active", importance=1.0, confidence=1.0, seen_count=5, pinned=True, category="work", decay_rate=0.05, last_seen_at=None, updated_at=None),
    ]
    monkeypatch.setattr("app.db.models.SessionLocal", lambda: _Session(rows))

    result = memory_tool.read_scoped_memory("u1", category_hint="work")

    assert result["memories"][0] == "high"
    assert result["count"] == 2


def test_memory_read_empty_user_id_returns_empty():
    assert memory_tool.read_scoped_memory("") == {"memories": [], "count": 0, "truncated": False}


def test_memory_read_db_failure_never_raises(monkeypatch):
    monkeypatch.setattr("app.db.models.SessionLocal", lambda: _Session(fail=True))
    assert memory_tool.read_scoped_memory("u1") == {"memories": [], "count": 0, "truncated": False}


def test_memory_read_truncates_at_max_chars(monkeypatch):
    rows = [
        SimpleNamespace(content="x" * (memory_tool.MAX_CHARS + 1), status="active", importance=1.0, confidence=1.0, seen_count=1, pinned=False, category="general", decay_rate=0.05, last_seen_at=None, updated_at=None),
    ]
    monkeypatch.setattr("app.db.models.SessionLocal", lambda: _Session(rows))

    result = memory_tool.read_scoped_memory("u1")

    assert result["memories"] == []
    assert result["truncated"] is True


def test_memory_write_saves_with_user_id(monkeypatch):
    session = _Session()
    monkeypatch.setattr("app.db.models.SessionLocal", lambda: session)

    result = memory_tool.write_scoped_memory("u1", "Remember this", category="work")

    assert result == {"written": True}
    assert session.added[0].user_id == "u1"
    assert session.added[0].content == "Remember this"
    assert session.committed is True


def test_memory_write_empty_content_skips():
    assert memory_tool.write_scoped_memory("u1", "  ") == {"written": False, "reason": "empty"}


def test_memory_write_db_failure_never_raises(monkeypatch):
    monkeypatch.setattr("app.db.models.SessionLocal", lambda: _Session(fail=True))
    assert memory_tool.write_scoped_memory("u1", "x") == {"written": False, "reason": "db_error"}


def test_tool_runner_overrides_agent_supplied_user_id(monkeypatch):
    register_all()
    captured: dict[str, str] = {}

    def fake_read(user_id, *, category_hint=None):
        captured["user_id"] = user_id
        return {"memories": ["safe"], "count": 1, "truncated": False}

    monkeypatch.setattr(memory_tool, "read_scoped_memory", fake_read)
    registry = _load_from_files()
    result = ToolRunner(
        registry,
        "direct_answer_agent",
        GuardrailService(registry),
    ).run(
        "memory_read",
        {"user_id": "attacker-id", "category_hint": None},
        state=TurnGraphState(user_message="hi", user_id="victim-id"),
    )

    assert captured["user_id"] == "victim-id"
    assert result.output["memories"] == ["safe"]
