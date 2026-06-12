from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import get_current_user_id
from app.db.models import Base
from app.main import app
from app.routers import personal_context


def test_profile_overrides_lifecycle(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(personal_context, "SessionLocal", Session)
    app.dependency_overrides[get_current_user_id] = lambda: "u1"
    try:
        with TestClient(app) as client:
            response = client.get("/personal-context/profile")
            assert response.status_code == 200
            assert response.json() == {"profile": {}, "last_consolidated_at": None}

            response = client.patch(
                "/personal-context/profile",
                json={"overrides": {"role": "Enterprise Architect", "company": "Acme"}},
            )
            assert response.status_code == 200
            assert response.json()["profile"]["overrides"] == {
                "role": "Enterprise Architect",
                "company": "Acme",
            }

            response = client.patch("/personal-context/profile", json={"overrides": {"company": ""}})
            assert response.status_code == 200
            assert response.json()["profile"]["overrides"] == {"role": "Enterprise Architect"}
    finally:
        app.dependency_overrides.clear()
