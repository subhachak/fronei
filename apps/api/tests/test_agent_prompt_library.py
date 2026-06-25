from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import PromptTemplate, Base
from app.main import app
from app.auth import AdminPrincipal, require_admin_principal
from app.routers import admin as admin_router
from app.services.agent import prompt_library


def _sqlite_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def test_agent_prompt_resolves_code_fallback_when_db_empty(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(prompt_library, "SessionLocal", Session)

    resolved = prompt_library.resolve_prompt(
        "agent.test.default",
        agent_id="test_agent",
        fallback_system_prompt="Fallback prompt",
        variables=["message"],
        profile="technical_architecture",
    )

    assert resolved.source == "code"
    assert resolved.system_prompt == "Fallback prompt"
    assert resolved.profile == "technical_architecture"
    assert resolved.telemetry()["prompt_source"] == "code"


def test_agent_prompt_seed_and_resolve_from_db(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(prompt_library, "SessionLocal", Session)

    counts = prompt_library.seed_defaults()
    resolved = prompt_library.resolve_prompt(
        "agent.research.synthesis.default",
        agent_id="synthesis",
        fallback_system_prompt="Fallback prompt",
    )

    assert counts["inserted"] >= 10
    assert resolved.source == "db"
    assert resolved.id == "agent.research.synthesis.default"
    assert "source-grounded" in resolved.system_prompt.lower() or "synthes" in resolved.system_prompt.lower()


def test_agent_prompt_activate_archives_same_agent_profile(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(prompt_library, "SessionLocal", Session)
    prompt_library.seed_defaults()
    draft = prompt_library.PromptSpec(
        id="agent.research.synthesis.v2",
        agent_id="synthesis",
        profile=None,
        version="2.0.0",
        status="draft",
        system_prompt="New synthesis prompt",
    )
    prompt_library.upsert_prompt(draft)

    activated = prompt_library.activate_prompt("agent.research.synthesis.v2")

    assert activated is not None
    assert activated.status == "active"
    with Session() as db:
        old = db.get(PromptTemplate, "agent.research.synthesis.default")
        assert old is not None
        assert old.status == "archived"


def test_agent_prompt_rollback_restores_archived_version(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(prompt_library, "SessionLocal", Session)
    prompt_library.seed_defaults()
    prompt_library.upsert_prompt(
        prompt_library.PromptSpec(
            id="agent.document.write.v2",
            agent_id="document_writer",
            version="2.0.0",
            status="draft",
            system_prompt="New document writer prompt",
        )
    )
    prompt_library.activate_prompt("agent.document.write.v2")

    rolled_back = prompt_library.rollback_prompt("agent.document.write.v2")

    assert rolled_back is not None
    assert rolled_back.id == "agent.document.write.default"
    assert rolled_back.status == "active"


def test_admin_agent_prompt_endpoints(monkeypatch):
    Session = _sqlite_session()
    monkeypatch.setattr(prompt_library, "SessionLocal", Session)
    monkeypatch.setattr(admin_router, "SessionLocal", Session)
    app.dependency_overrides[require_admin_principal] = lambda: AdminPrincipal(
        user_id="admin_1",
        email="admin@example.com",
    )
    try:
        with TestClient(app) as client:
            seed = client.post("/admin/prompts/seed")
            assert seed.status_code == 200
            assert seed.json()["seeded"]["inserted"] >= 10

            listed = client.get("/admin/prompts")
            assert listed.status_code == 200
            ids = {item["id"] for item in listed.json()["prompts"]}
            assert "agent.research.brief.default" in ids

            upsert = client.post(
                "/admin/prompts",
                json={
                    "id": "agent.research.brief.v2",
                    "agent_id": "research_brief",
                    "version": "2.0.0",
                    "status": "draft",
                    "system_prompt": "Updated research brief prompt",
                    "variables": ["message"],
                },
            )
            assert upsert.status_code == 200

            activated = client.post("/admin/prompts/agent.research.brief.v2/activate")
            assert activated.status_code == 200
            assert activated.json()["activated"] == "agent.research.brief.v2"

            rollback = client.post("/admin/prompts/agent.research.brief.v2/rollback")
            assert rollback.status_code == 200
            assert rollback.json()["rolled_back_to"] == "agent.research.brief.default"
    finally:
        app.dependency_overrides.clear()
