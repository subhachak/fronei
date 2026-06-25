from fastapi.testclient import TestClient
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db.models import Base
from app.db.schema_check import _ALEMBIC_INI
from app.main import app
from app.routers import internal


def test_internal_smoke_requires_secret(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "internal_task_secret", "test-secret")
    with TestClient(app) as client:
        response = client.post("/internal/smoke")
    assert response.status_code == 403


def test_internal_smoke_checks_database(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    config = Config(str(_ALEMBIC_INI))
    head = ScriptDirectory.from_config(config).get_current_head()
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES (:head)"), {"head": head})
    Session = sessionmaker(bind=engine)
    settings = get_settings()
    monkeypatch.setattr(settings, "internal_task_secret", "test-secret")
    monkeypatch.setattr(internal, "engine", engine)
    monkeypatch.setattr(internal, "SessionLocal", Session)

    with TestClient(app) as client:
        response = client.post("/internal/smoke", headers={"X-Internal-Secret": "test-secret"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["schema"] == "ok"
    assert "workspaces" in body["tables_checked"]
