import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app import main as main_module
from app.db.models import Base
from app.routers import admin as admin_router
from app.services.agent_runtime import registry as registry_module
from app.services.agent_runtime.adapters import RuntimeContext, empty_runtime_trace, runtime_trace_payload
from app.services.agent_runtime.db_models import DBPromptTemplate
from app.services.agent_runtime.fixtures import FIXTURES_DIR
from app.services.agent_runtime.registry import (
    _load_from_files,
    invalidate_registry_cache,
    load_default_registry,
    load_registry_from_db,
)
from app.services.agent_runtime.seeder import seed_registry_from_defaults
from app.services.llm_gateway import LLMResult


@pytest.fixture
def db_session(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(registry_module, "SessionLocal", Session)
    monkeypatch.setattr(admin_router, "SessionLocal", Session)
    monkeypatch.setattr(main_module, "SessionLocal", Session)
    invalidate_registry_cache()
    with Session() as db:
        yield db
    invalidate_registry_cache()


@pytest.fixture
def admin_client(db_session, monkeypatch):
    app.dependency_overrides[admin_router.require_admin] = lambda: admin_router.AdminPrincipal(
        user_id="admin_1",
        email="admin@example.com",
    )
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def test_seed_registry_from_defaults_populates_all_tables(db_session):
    counts = seed_registry_from_defaults(db_session)
    assert counts["agents"] >= 4
    assert counts["prompts"] >= 4
    assert counts["tools"] >= 4
    assert counts["guardrails"] >= 3
    assert counts["model_policies"] >= 2


def test_load_registry_from_db_matches_file_defaults(db_session):
    seed_registry_from_defaults(db_session)
    db_registry = load_registry_from_db(db_session)
    file_registry = _load_from_files()
    assert set(db_registry.agents.keys()) == set(file_registry.agents.keys())
    assert set(db_registry.prompts.keys()) == set(file_registry.prompts.keys())


def test_load_default_registry_falls_back_to_files_when_db_empty(db_session):
    registry = load_default_registry()
    assert registry.agent("orchestrator") is not None


def test_admin_registry_agents_endpoint(admin_client):
    response = admin_client.get("/admin/registry/agents")
    assert response.status_code == 200
    ids = {item["id"] for item in response.json()}
    assert "orchestrator" in ids


def _fixture_llm_result(answer: str = '{"route":"direct_answer","selected_tools":["answer_directly"]}') -> LLMResult:
    return LLMResult(
        answer=answer,
        model_used="test-model",
        latency_ms=1,
        prompt_tokens=1,
        completion_tokens=1,
        estimated_cost_usd=0.0,
    )


def _fixture_llm_for_orchestrator(messages, *_args, **_kwargs) -> LLMResult:
    payload = messages[-1]["content"] if messages else ""
    # Strings match user_message values in defaults/fixtures/prompt.orchestrator.default.json.
    # Keep this mock in sync when fixture scenarios change.
    if "latest mortgage rates" in payload:
        return _fixture_llm_result('{"route":"research","selected_tools":["web_search"]}')
    if "10-slide board deck" in payload:
        return _fixture_llm_result('{"route":"document","selected_tools":["generate_document"]}')
    return _fixture_llm_result('{"route":"direct_answer","selected_tools":["answer_directly"]}')


def test_prompt_activation_requires_fixture_pass(db_session, admin_client, monkeypatch):
    seed_registry_from_defaults(db_session)
    db_session.execute(text("UPDATE prompt_templates SET status='draft' WHERE id='prompt.orchestrator.default'"))
    db_session.commit()
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm_json",
        _fixture_llm_for_orchestrator,
    )

    response = admin_client.post("/admin/registry/prompts/prompt.orchestrator.default/activate")

    assert response.status_code == 200
    assert response.json()["activated"] == "prompt.orchestrator.default"
    assert response.json()["fixture_summary"]["failed"] == 0


def test_prompt_activation_blocked_if_missing_required_variable(db_session, admin_client, tmp_path, monkeypatch):
    seed_registry_from_defaults(db_session)
    bad_prompt = DBPromptTemplate(
        id="prompt.bad.default",
        agent_id="orchestrator",
        version="2.0.0",
        system_prompt="Use {required_value}",
        variables=json.dumps(["required_value"]),
        status="draft",
    )
    db_session.add(bad_prompt)
    db_session.commit()
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "prompt.bad.default.json").write_text(json.dumps([
        {
            "scenario": "missing required variable",
            "input": {"other": "value"},
            "expect": {"response_contains": ["value"]},
        }
    ]))
    monkeypatch.setattr("app.services.agent_runtime.fixtures.FIXTURES_DIR", fixture_dir)

    response = admin_client.post("/admin/registry/prompts/prompt.bad.default/activate")

    assert response.status_code == 422
    assert response.json()["detail"]["fixture_failures"]["failed"] == 1


def test_prompt_rollback_restores_previous_version(db_session, admin_client, tmp_path, monkeypatch):
    seed_registry_from_defaults(db_session)
    v1 = db_session.get(DBPromptTemplate, "prompt.orchestrator.default")
    assert v1 is not None
    v1.status = "active"
    v2 = DBPromptTemplate(
        id="prompt.orchestrator.v2",
        agent_id="orchestrator",
        version="2.0.0",
        system_prompt=v1.system_prompt,
        developer_prompt=v1.developer_prompt,
        output_schema=v1.output_schema,
        variables=v1.variables,
        status="draft",
    )
    db_session.add(v2)
    db_session.commit()

    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "prompt.orchestrator.v2.json").write_text(
        (FIXTURES_DIR / "prompt.orchestrator.default.json").read_text()
    )
    monkeypatch.setattr("app.services.agent_runtime.fixtures.FIXTURES_DIR", fixture_dir)
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm_json",
        _fixture_llm_for_orchestrator,
    )

    activate = admin_client.post("/admin/registry/prompts/prompt.orchestrator.v2/activate")
    assert activate.status_code == 200

    rollback = admin_client.post("/admin/registry/prompts/prompt.orchestrator.v2/rollback")
    assert rollback.status_code == 200
    assert rollback.json()["rolled_back_to"] == "prompt.orchestrator.default"


def test_runtime_trace_includes_prompt_versions_field():
    context = RuntimeContext(user_id="u1", conversation_id="c1", turn_id="t1", user_message="hi")
    trace = empty_runtime_trace(context)
    payload = runtime_trace_payload(trace)
    assert "prompt_versions" in payload
    assert payload["prompt_versions"] == {}
