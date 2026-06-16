import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app import main as main_module
from app.db.models import AgentGoal, AgentRunLog, AgentStep, Base, GuardrailEvent
from app.routers import admin as admin_router
from app.schemas import RouteDecision
from app.services.agent_runtime.direct_answer import DirectAnswerAgent
from app.services.agent_runtime.orchestrator import OrchestratorAgent, _parse_orchestrator_response
from app.services.agent_runtime.registry import load_default_registry
from app.services.llm_gateway import LLMResult
from app.services.turn_graph import graph as turn_graph
from app.services.turn_graph.state import TurnGraphState


@pytest.fixture
def db_session(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(turn_graph, "SessionLocal", Session)
    monkeypatch.setattr(admin_router, "SessionLocal", Session)
    monkeypatch.setattr(main_module, "SessionLocal", Session)
    with Session() as db:
        yield db


@pytest.fixture
def admin_client(db_session):
    app.dependency_overrides[admin_router.require_admin] = lambda: admin_router.AdminPrincipal(
        user_id="admin_1",
        email="admin@example.com",
    )
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def _llm_result(answer: str = '{"route":"direct_answer","reasoning":"clear","selected_tools":["answer_directly"]}'):
    return LLMResult(
        answer=answer,
        model_used="test-model",
        latency_ms=12,
        prompt_tokens=10,
        completion_tokens=5,
        estimated_cost_usd=0.001,
    )


def test_orchestrator_agent_routes_direct_answer(monkeypatch):
    monkeypatch.setattr("app.services.llm_gateway.invoke_llm_json", lambda *_args, **_kwargs: _llm_result())
    decision = OrchestratorAgent(load_default_registry()).route(TurnGraphState(user_message="Explain rate limiting."))

    assert decision.route == "direct_answer"
    assert decision.model_used == "test-model"
    assert decision.prompt_id == "prompt.orchestrator.default"


def test_orchestrator_agent_routes_clarify(monkeypatch):
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm_json",
        lambda *_args, **_kwargs: _llm_result(
            '{"route":"clarify","reasoning":"ambiguous","clarification_question":"Which platform?"}'
        ),
    )
    decision = OrchestratorAgent(load_default_registry()).route(TurnGraphState(user_message="Build it."))

    assert decision.route == "clarify"
    assert decision.clarification_question == "Which platform?"


def test_orchestrator_messages_remap_developer_prompt_for_non_claude():
    registry = load_default_registry()
    agent = OrchestratorAgent(registry)
    agent.prompt.developer_prompt = "Return JSON only."
    agent.model_policy.primary_model = "gpt-4.1-mini"

    messages = agent._build_messages(TurnGraphState(user_message="Hi"))

    assert {"role": "system", "content": "Return JSON only."} in messages
    assert all(message["role"] != "developer" for message in messages)


def test_orchestrator_agent_parse_failure_defaults_to_direct_answer():
    decision = _parse_orchestrator_response("not json")

    assert decision.route == "direct_answer"
    assert decision.reasoning == "parse_failed"


def test_direct_answer_agent_returns_llm_result(monkeypatch):
    monkeypatch.setattr(
        "app.services.agent_runtime.direct_answer.invoke_llm",
        lambda **_kwargs: _llm_result("Plain answer."),
    )
    result = DirectAnswerAgent(load_default_registry()).answer(TurnGraphState(user_message="Hi"))

    assert result.answer == "Plain answer."
    assert result.prompt_id == "prompt.direct_answer.default"


def test_orchestrator_node_skips_existing_pipeline_on_direct_answer(monkeypatch):
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm_json",
        lambda *_args, **_kwargs: _llm_result(),
    )
    monkeypatch.setattr(
        "app.services.agent_runtime.direct_answer.invoke_llm",
        lambda **_kwargs: _llm_result("Direct answer."),
    )

    state, handled = turn_graph._orchestrator_node(
        TurnGraphState(user_message="Explain rate limiting.", turn_id="t1", conversation_id="c1", user_id="u1"),
        SimpleNamespace(),
    )

    assert handled is True
    assert state.final_answer == "Direct answer."


def test_orchestrator_node_falls_through_for_research_route(monkeypatch):
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm_json",
        lambda *_args, **_kwargs: _llm_result('{"route":"research","reasoning":"current","selected_tools":["web_search"]}'),
    )

    state, handled = turn_graph._orchestrator_node(
        TurnGraphState(user_message="Find the latest.", turn_id="t1", conversation_id="c1", user_id="u1"),
        SimpleNamespace(),
    )

    assert handled is False
    assert state.triage_decision["route"] == "research"


def test_orchestrator_node_falls_through_on_llm_failure(monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("model down")

    monkeypatch.setattr("app.services.llm_gateway.invoke_llm_json", fail)
    state, handled = turn_graph._orchestrator_node(TurnGraphState(user_message="Hi"), SimpleNamespace())

    assert handled is False
    assert state.status == "pending"


def test_run_turn_graph_shell_skips_orchestrator_when_flag_off(monkeypatch):
    called = {"existing": False}

    def existing(state):
        called["existing"] = True
        state.final_answer = "legacy"
        return state

    state = turn_graph.run_turn_graph_shell(
        TurnGraphState(user_message="Hi"),
        existing_pipeline=existing,
        settings=SimpleNamespace(orchestrator_enabled=False, turn_graph_enabled=False),
    )

    assert called["existing"] is True
    assert state.final_answer == "legacy"


def test_run_turn_graph_shell_uses_orchestrator_when_flag_on(monkeypatch):
    called = {"existing": False}
    monkeypatch.setattr(
        turn_graph,
        "_orchestrator_node",
        lambda state, settings: (state.model_copy(update={"final_answer": "handled", "status": "completed"}), True),
    )

    def existing(state):
        called["existing"] = True
        return state

    state = turn_graph.run_turn_graph_shell(
        TurnGraphState(user_message="Hi"),
        existing_pipeline=existing,
        settings=SimpleNamespace(orchestrator_enabled=True, turn_graph_enabled=False),
    )

    assert called["existing"] is False
    assert state.final_answer == "handled"


def test_orchestrator_writes_goal_and_agent_runs(db_session):
    decision = SimpleNamespace(
        run_id="orch-run-1",
        cost_usd=0.001,
        latency_ms=10,
        model_used="model-a",
        prompt_id="prompt.orchestrator.default",
        route="direct_answer",
    )
    da_result = SimpleNamespace(
        run_id="direct-run-1",
        cost_usd=0.002,
        latency_ms=20,
        model_used="model-b",
        prompt_id="prompt.direct_answer.default",
    )
    state = TurnGraphState(
        user_message="Hi",
        turn_id="turn_1",
        conversation_id="conv_1",
        user_id="u1",
        final_answer="Hello",
        status="completed",
    )

    turn_graph._write_orchestrator_trace(state, decision, load_default_registry(), db_session=None, da_result=da_result)

    assert db_session.get(AgentGoal, "goal_turn_1") is not None
    assert db_session.query(AgentRunLog).count() == 2
    assert db_session.query(AgentStep).count() == 2


def test_admin_turn_trace_endpoint(admin_client, db_session):
    db_session.add(AgentGoal(
        id="goal_turn_2",
        user_id="u1",
        conversation_id="conv_1",
        turn_id="turn_2",
        objective="Answer",
    ))
    db_session.add(AgentRunLog(id="run_1", goal_id="goal_turn_2", agent_id="orchestrator", status="completed"))
    db_session.add(AgentStep(
        id="step_1",
        run_id="run_1",
        step_type="llm_call",
        model_used="model-a",
        metadata_json=json.dumps({"prompt_id": "prompt.orchestrator.default"}),
    ))
    db_session.add(GuardrailEvent(
        id="event_1",
        policy_id="policy",
        boundary="output",
        action="allow",
        triggered_checks_json="[]",
        reason="ok",
        turn_id="turn_2",
    ))
    db_session.commit()

    response = admin_client.get("/admin/turns/turn_2/trace")

    assert response.status_code == 200
    payload = response.json()
    assert payload["goal"]["id"] == "goal_turn_2"
    assert payload["prompt_versions"]["orchestrator"] == "prompt.orchestrator.default"
    assert payload["guardrail_events"][0]["policy_id"] == "policy"


def test_invoke_llm_json_falls_back_to_plain_text_on_unsupported_provider(monkeypatch):
    from app.services import llm_gateway

    calls = []

    class Message:
        content = '{"route":"direct_answer"}'

    class Choice:
        message = Message()

    class Response:
        choices = [Choice()]
        usage = None

    def fake_completion(**kwargs):
        calls.append(kwargs)
        if "response_format" in kwargs:
            raise RuntimeError("unsupported response_format")
        return Response()

    monkeypatch.setattr(llm_gateway, "completion", fake_completion)
    route = RouteDecision(
        task_type="planning",
        complexity="low",
        profile="balanced",
        primary_model="gpt-4.1-mini",
        fallbacks=[],
        reason="test",
    )

    result = llm_gateway.invoke_llm_json([{"role": "user", "content": "{}"}], route)

    assert result.answer == '{"route":"direct_answer"}'
    assert len(calls) == 2
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]
