from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import get_current_user_id
from app.db.models import Base, DocumentTemplate
from app.main import app
from app.routers import documents
from app.services import document_templates


def test_user_template_upload_list_and_delete(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(documents, "SessionLocal", Session)
    monkeypatch.setattr(
        document_templates,
        "get_settings",
        lambda: SimpleNamespace(document_template_storage_dir=str(tmp_path)),
    )
    app.dependency_overrides[get_current_user_id] = lambda: "u1"

    template_bytes = Path("app/assets/pptx_templates/strategy_canvas.pptx").read_bytes()
    try:
        with TestClient(app) as client:
            upload = client.post(
                "/documents/templates",
                data={"name": "Acme Board Template"},
                files={
                    "file": (
                        "acme-template.pptx",
                        template_bytes,
                        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    )
                },
            )
            assert upload.status_code == 200
            uploaded = upload.json()
            assert uploaded["name"] == "Acme Board Template"
            assert uploaded["user_template"] is True

            listing = client.get("/documents/templates?doc_type=presentation")
            assert listing.status_code == 200
            templates = listing.json()["templates"]
            assert templates[0]["id"] == uploaded["id"]
            assert templates[0]["recommended"] is True
            assert any(t["id"] == "strategy-canvas" for t in templates)

            with Session() as db:
                row = db.query(DocumentTemplate).filter(DocumentTemplate.public_id == uploaded["id"]).one()
                assert (tmp_path / row.storage_key).exists()

            delete = client.delete(f"/documents/templates/{uploaded['id']}")
            assert delete.status_code == 200
            listing = client.get("/documents/templates?doc_type=presentation")
            assert all(t["id"] != uploaded["id"] for t in listing.json()["templates"])
    finally:
        app.dependency_overrides.clear()


def test_user_template_upload_rejects_non_pptx(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(documents, "SessionLocal", Session)
    monkeypatch.setattr(
        document_templates,
        "get_settings",
        lambda: SimpleNamespace(document_template_storage_dir=str(tmp_path)),
    )
    app.dependency_overrides[get_current_user_id] = lambda: "u1"
    try:
        with TestClient(app) as client:
            response = client.post(
                "/documents/templates",
                files={"file": ("template.txt", b"not a pptx", "text/plain")},
            )
            assert response.status_code == 422
            assert "Only .pptx" in response.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_template_grammar_for_builtin_template():
    grammar = document_templates.template_grammar_for_selection(
        None,
        "u1",
        "strategy-canvas",
        {"doc_type": "presentation", "audience": "Executive committee"},
    )

    assert grammar["mode"] == "template_following"
    assert grammar["template_id"] == "strategy-canvas"
    assert grammar["available_slide_types"]
    assert "TEMPLATE-FIRST PRESENTATION DESIGN BRIEF" in document_templates.template_design_context(grammar)


def test_template_grammar_for_default_freehand_theme():
    grammar = document_templates.template_grammar_for_selection(
        None,
        "u1",
        "fronei-default",
        {"doc_type": "presentation", "audience": "Client steering committee"},
    )

    assert grammar["mode"] == "fronei_premium_freehand"
    assert "Fronei premium freehand" in document_templates.template_design_context(grammar)
