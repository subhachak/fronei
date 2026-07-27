"""Blog authoring (admin) and public read endpoints.

Covers the draft -> preview -> publish lifecycle and the security boundary
that matters most here: an unauthenticated visitor (or a signed-in
non-admin) must never be able to see a draft through either the public
endpoints or the admin endpoints.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import AdminPrincipal, require_admin_principal
from app.db.models import Base
from app.main import app
from app.routers import blog as blog_router
from app.services.agent import model_client


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


def test_admin_can_draft_preview_and_publish_a_post(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(blog_router, "SessionLocal", Session)

    _as_admin()
    try:
        with TestClient(app) as client:
            created = client.post(
                "/admin/blog/posts",
                json={"title": "Hot Take: Agents Are Just State Machines", "body_markdown": "Draft body."},
            )
            assert created.status_code == 200
            post = created.json()
            assert post["status"] == "draft"
            assert post["slug"] == "hot-take-agents-are-just-state-machines"
            assert post["published_at"] is None

            # Not visible publicly while a draft.
            public_list = client.get("/blog/posts")
            assert public_list.json()["items"] == []
            public_get = client.get(f"/blog/posts/{post['slug']}")
            assert public_get.status_code == 404

            # Admin can fetch it by slug for preview before publishing.
            preview = client.get(f"/admin/blog/posts/by-slug/{post['slug']}")
            assert preview.status_code == 200
            assert preview.json()["body_markdown"] == "Draft body."

            # Edit while still a draft.
            edited = client.patch(f"/admin/blog/posts/{post['id']}", json={"body_markdown": "Revised body."})
            assert edited.status_code == 200
            assert edited.json()["body_markdown"] == "Revised body."

            published = client.post(f"/admin/blog/posts/{post['id']}/publish")
            assert published.status_code == 200
            assert published.json()["status"] == "published"
            assert published.json()["published_at"] is not None

            public_get_after = client.get(f"/blog/posts/{post['slug']}")
            assert public_get_after.status_code == 200
            assert public_get_after.json()["body_markdown"] == "Revised body."

            unpublished = client.post(f"/admin/blog/posts/{post['id']}/unpublish")
            assert unpublished.status_code == 200
            assert unpublished.json()["status"] == "draft"
            assert client.get(f"/blog/posts/{post['slug']}").status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_duplicate_titles_get_a_unique_slug(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(blog_router, "SessionLocal", Session)

    _as_admin()
    try:
        with TestClient(app) as client:
            first = client.post("/admin/blog/posts", json={"title": "Predictions for 2027"})
            second = client.post("/admin/blog/posts", json={"title": "Predictions for 2027"})
            assert first.json()["slug"] == "predictions-for-2027"
            assert second.json()["slug"] == "predictions-for-2027-2"
    finally:
        app.dependency_overrides.clear()


def test_public_list_filters_by_tag_and_excludes_drafts(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(blog_router, "SessionLocal", Session)

    _as_admin()
    try:
        with TestClient(app) as client:
            ai_post = client.post(
                "/admin/blog/posts",
                json={"title": "AI Post", "tags": ["ai", "predictions"]},
            ).json()
            project_post = client.post(
                "/admin/blog/posts",
                json={"title": "Project Update", "tags": ["projects"]},
            ).json()
            draft_post = client.post(
                "/admin/blog/posts",
                json={"title": "Still Cooking", "tags": ["ai"]},
            ).json()
            client.post(f"/admin/blog/posts/{ai_post['id']}/publish")
            client.post(f"/admin/blog/posts/{project_post['id']}/publish")
            # draft_post stays a draft.

            all_published = client.get("/blog/posts").json()["items"]
            assert {p["slug"] for p in all_published} == {ai_post["slug"], project_post["slug"]}

            ai_only = client.get("/blog/posts?tag=ai").json()["items"]
            assert [p["slug"] for p in ai_only] == [ai_post["slug"]]
            assert draft_post["slug"] not in {p["slug"] for p in ai_only}
    finally:
        app.dependency_overrides.clear()


def test_non_admin_cannot_reach_admin_blog_endpoints(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(blog_router, "SessionLocal", Session)

    with TestClient(app) as client:
        # No admin override installed -- real require_admin_principal runs
        # and should reject an unauthenticated caller before touching the DB.
        list_resp = client.get("/admin/blog/posts")
        assert list_resp.status_code in (401, 403)

        create_resp = client.post("/admin/blog/posts", json={"title": "Should Not Work"})
        assert create_resp.status_code in (401, 403)


def test_draft_from_notes_fills_structured_fields(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(blog_router, "SessionLocal", Session)

    def fake_complete(messages, *, role=None, **kwargs):
        assert role == "blog_draft"
        assert "half-baked thought" in messages[-1]["content"]
        return SimpleNamespace(
            text=json.dumps({
                "title": "Agents Are Just State Machines",
                "excerpt": "A hot take on agent architecture.",
                "tags": ["AI", " Agents ", ""],
                "body_markdown": "## My take\n\nAgents are state machines with good PR.",
            }),
            model_used="test",
            latency_ms=1,
            cost_usd=0.0,
        )

    monkeypatch.setattr(model_client, "complete", fake_complete)

    _as_admin()
    try:
        with TestClient(app) as client:
            response = client.post(
                "/admin/blog/posts/draft-from-notes",
                json={"notes": "half-baked thought: agents are just state machines with good PR"},
            )
            assert response.status_code == 200
            body = response.json()
            assert body["title"] == "Agents Are Just State Machines"
            # Tags are lowercased, trimmed, and empties dropped.
            assert body["tags"] == ["ai", "agents"]
            assert "state machines" in body["body_markdown"]
    finally:
        app.dependency_overrides.clear()


def test_draft_from_notes_returns_clean_error_on_malformed_output(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(blog_router, "SessionLocal", Session)

    def fake_complete(*args, **kwargs):
        return SimpleNamespace(text="not json at all", model_used="test", latency_ms=1, cost_usd=0.0)

    monkeypatch.setattr(model_client, "complete", fake_complete)

    _as_admin()
    try:
        with TestClient(app) as client:
            response = client.post("/admin/blog/posts/draft-from-notes", json={"notes": "some notes"})
            assert response.status_code == 502
    finally:
        app.dependency_overrides.clear()


def test_non_admin_cannot_reach_draft_from_notes(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(blog_router, "SessionLocal", Session)

    with TestClient(app) as client:
        response = client.post("/admin/blog/posts/draft-from-notes", json={"notes": "some notes"})
        assert response.status_code in (401, 403)
