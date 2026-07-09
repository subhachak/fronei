"""Part 2 -- admin-facing token usage tracking and stats.

Covers: /admin/overview and /admin/users(/{user_id}) token aggregates, and
the two new endpoints /admin/context-usage and /admin/context-pressure
(including RequireAdmin gating for both).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import AdminPrincipal, require_admin_principal
from app.db.models import Base, Turn, User
from app.main import app
from app.routers import admin as admin_router


def _sqlite_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _as_admin():
    app.dependency_overrides[require_admin_principal] = lambda: AdminPrincipal(
        user_id="admin_1", email="admin@example.com",
    )


def _make_turn(db, turn_id, user_id, *, route="direct", input_tokens=0, output_tokens=0, context_tokens=None, status="completed"):
    db.add(Turn(
        id=turn_id,
        user_id=user_id,
        objective="ask",
        route=route,
        status=status,
        answer="ok",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        context_tokens_json=json.dumps(context_tokens or {}),
        created_at=datetime.now(timezone.utc),
    ))


def test_overview_returns_token_aggregates_and_route_breakdown(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(admin_router, "SessionLocal", Session)
    with Session() as db:
        _make_turn(db, "t1", "u1", route="research", input_tokens=100, output_tokens=50)
        _make_turn(db, "t2", "u1", route="direct", input_tokens=20, output_tokens=10)
        db.commit()

    _as_admin()
    try:
        with TestClient(app) as client:
            resp = client.get("/admin/overview")
        assert resp.status_code == 200
        body = resp.json()
        assert body["input_tokens_today"] == 120
        assert body["output_tokens_today"] == 60
        assert body["tokens_by_route_today"]["research"] == {"requests": 1, "input_tokens": 100, "output_tokens": 50}
        assert body["tokens_by_route_today"]["direct"] == {"requests": 1, "input_tokens": 20, "output_tokens": 10}
    finally:
        app.dependency_overrides.clear()


def test_usage_summary_includes_input_output_tokens(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(admin_router, "SessionLocal", Session)
    with Session() as db:
        _make_turn(db, "t1", "u1", input_tokens=100, output_tokens=50)
        db.commit()

    _as_admin()
    try:
        with TestClient(app) as client:
            resp = client.get("/admin/usage?range=7d")
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert summary["input_tokens"] == 100
        assert summary["output_tokens"] == 50
        assert summary["tokens"] == 150
    finally:
        app.dependency_overrides.clear()


def test_users_list_includes_per_user_token_totals(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(admin_router, "SessionLocal", Session)
    with Session() as db:
        db.add(User(clerk_id="u1", email="u1@example.com"))
        _make_turn(db, "t1", "u1", input_tokens=100, output_tokens=50)
        _make_turn(db, "t2", "u1", input_tokens=20, output_tokens=10)
        db.commit()

    _as_admin()
    try:
        with TestClient(app) as client:
            resp = client.get("/admin/users?limit=50")
        assert resp.status_code == 200
        row = next(item for item in resp.json()["items"] if item["user_id"] == "u1")
        assert row["total_input_tokens"] == 120
        assert row["total_output_tokens"] == 60
    finally:
        app.dependency_overrides.clear()


def test_user_detail_includes_token_totals_and_per_turn_breakdown(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(admin_router, "SessionLocal", Session)
    with Session() as db:
        db.add(User(clerk_id="u1", email="u1@example.com"))
        _make_turn(db, "t1", "u1", route="research", input_tokens=100, output_tokens=50, context_tokens={"evidence": 90})
        db.commit()

    _as_admin()
    try:
        with TestClient(app) as client:
            resp = client.get("/admin/users/u1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["counts"]["total_input_tokens"] == 100
        assert body["counts"]["total_output_tokens"] == 50
        turn = next(t for t in body["recent_turns"] if t["id"] == "t1")
        assert turn["input_tokens"] == 100
        assert turn["output_tokens"] == 50
        assert turn["context_tokens"] == {"evidence": 90}
    finally:
        app.dependency_overrides.clear()


def test_context_usage_reports_per_layer_averages_and_max(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(admin_router, "SessionLocal", Session)
    with Session() as db:
        _make_turn(db, "t1", "u1", route="research", context_tokens={"conversation": 200, "facts": 400, "evidence": 3000})
        _make_turn(db, "t2", "u1", route="research", context_tokens={"conversation": 600, "evidence": 1000})
        _make_turn(db, "t3", "u1", route="direct")  # no context data -- excluded
        db.commit()

    _as_admin()
    try:
        with TestClient(app) as client:
            resp = client.get("/admin/context-usage?range=7d")
        assert resp.status_code == 200
        body = resp.json()
        assert body["turns_with_context_data"] == 2
        assert body["layers"]["conversation"]["sample_count"] == 2
        assert body["layers"]["conversation"]["avg_tokens"] == 400.0
        assert body["layers"]["conversation"]["max_tokens"] == 600
        assert body["layers"]["facts"]["sample_count"] == 1
        assert body["layers"]["evidence"]["max_tokens"] == 3000
    finally:
        app.dependency_overrides.clear()


def test_context_usage_requires_admin():
    with TestClient(app) as client:
        resp = client.get("/admin/context-usage")
    assert resp.status_code == 401


def test_context_pressure_reports_eviction_counts_and_turn_ids(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(admin_router, "SessionLocal", Session)
    with Session() as db:
        _make_turn(db, "t1", "u1", route="direct", context_tokens={"conversation": 5000, "evicted": {"conversation": 3}})
        _make_turn(db, "t2", "u1", route="research", context_tokens={"facts": 100, "evicted": {"facts": 2}})
        _make_turn(db, "t3", "u1", route="direct", context_tokens={"conversation": 200})  # no eviction
        db.commit()

    _as_admin()
    try:
        with TestClient(app) as client:
            resp = client.get("/admin/context-pressure?range=7d")
        assert resp.status_code == 200
        body = resp.json()
        assert body["turns_with_eviction"] == 2
        assert body["turns_scanned"] == 3
        assert body["evicted_item_counts_by_layer"] == {"conversation": 3, "facts": 2}
        assert body["most_evicted_layer"] == "conversation"
        turn_ids = {t["turn_id"] for t in body["turns"]}
        assert turn_ids == {"t1", "t2"}
    finally:
        app.dependency_overrides.clear()


def test_context_pressure_requires_admin():
    with TestClient(app) as client:
        resp = client.get("/admin/context-pressure")
    assert resp.status_code == 401
