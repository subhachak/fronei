import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import get_current_user_id, get_current_user_is_admin, get_current_user_payload
from app.db.models import Base
from app.main import app
from app.routers import twin_profile


@pytest.fixture
def client(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)

    monkeypatch.setattr(twin_profile, "SessionLocal", Session)
    app.dependency_overrides[get_current_user_id] = lambda: "u1"
    app.dependency_overrides[get_current_user_payload] = lambda: {"sub": "u1"}
    app.dependency_overrides[get_current_user_is_admin] = lambda: False
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_get_twin_profile_empty(client):
    response = client.get("/twin-profile")
    assert response.status_code == 200
    assert response.json() == {
        "user_id": "",
        "fingerprint": None,
        "rewrite_prompt": None,
        "prefs": {},
        "extracted_at": None,
        "sample_count": 0,
    }


def test_writing_sample_lifecycle(client):
    content = "This is a client-ready architecture note with enough content to pass validation."
    create_response = client.post(
        "/twin-profile/samples",
        json={"content": content, "label": "architecture note"},
    )
    assert create_response.status_code == 201
    created = create_response.json()
    assert created["content"] == content
    assert created["label"] == "architecture note"
    assert created["char_count"] == len(content)

    list_response = client.get("/twin-profile/samples")
    assert list_response.status_code == 200
    assert len(list_response.json()) == 1

    delete_response = client.delete(f"/twin-profile/samples/{created['id']}")
    assert delete_response.status_code == 204

    list_response = client.get("/twin-profile/samples")
    assert list_response.status_code == 200
    assert list_response.json() == []


def test_trigger_extraction(client):
    response = client.post("/twin-profile/extract")
    assert response.status_code == 202
    assert response.json() == {"status": "extraction queued"}
