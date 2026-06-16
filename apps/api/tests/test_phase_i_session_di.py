from types import SimpleNamespace
from unittest.mock import MagicMock

from app.db.models import DocumentTemplate
from app.services.agent_runtime import document_agent
from app.services.agent_runtime.document_agent import DocumentAgent, _fetch_template_grammar
from app.services.agent_runtime.guardrails import _query_template_ownership, _template_belongs_to_user_db
from app.services.agent_runtime.native_backends import register_all
from app.services.agent_runtime.registry import _load_from_files
from app.services.llm_gateway import LLMResult
from app.services.turn_graph import graph as turn_graph
from app.services.turn_graph.state import TurnGraphState


def _state(message: str = "Create a document") -> TurnGraphState:
    return TurnGraphState(user_message=message, user_id="u1", turn_id="t1", conversation_id="c1")


def _llm(answer: str, *, latency_ms: int = 10) -> LLMResult:
    return LLMResult(
        answer=answer,
        model_used="test-model",
        latency_ms=latency_ms,
        prompt_tokens=1,
        completion_tokens=1,
        estimated_cost_usd=0.001,
    )


class _FakeNodeSession:
    closed = False

    def close(self):
        self.closed = True


def test_orchestrator_node_does_not_open_session_for_trace(monkeypatch):
    session_calls = []
    captured = {}

    class FakeOrchestratorAgent:
        def __init__(self, registry):
            self.registry = registry

        def route(self, state):
            return SimpleNamespace(
                route="direct_answer",
                reasoning="clear",
                model_used="test-model",
                prompt_id="prompt.orchestrator.default",
                run_id="orch-run",
                selected_tools=[],
                plan={},
                cost_usd=0.0,
                latency_ms=1,
            )

    class FakeDirectAnswerAgent:
        def __init__(self, registry):
            self.registry = registry

        def answer(self, state):
            return SimpleNamespace(
                answer="Direct answer.",
                latency_ms=2,
                model_used="test-model",
                prompt_id="prompt.direct_answer.default",
                cost_usd=0.0,
                run_id="direct-run",
            )

    def fake_session_local():
        session = _FakeNodeSession()
        session_calls.append(session)
        return session

    def capture_trace(*_args, **kwargs):
        captured["db_session"] = kwargs.get("db_session")

    monkeypatch.setattr(turn_graph, "SessionLocal", fake_session_local)
    monkeypatch.setattr(turn_graph, "load_default_registry", _load_from_files)
    monkeypatch.setattr("app.services.agent_runtime.orchestrator.OrchestratorAgent", FakeOrchestratorAgent)
    monkeypatch.setattr("app.services.agent_runtime.direct_answer.DirectAnswerAgent", FakeDirectAnswerAgent)
    monkeypatch.setattr(turn_graph, "_write_orchestrator_trace", capture_trace)

    state, handled = turn_graph._orchestrator_node(_state("Explain rate limiting."), SimpleNamespace())

    assert handled is True
    assert state.final_answer == "Direct answer."
    assert captured["db_session"] is session_calls[0]
    assert session_calls[0].closed is True
    assert len(session_calls) == 1


def test_document_agent_run_uses_provided_db_for_guardrail(monkeypatch):
    register_all()
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = object()
    captured = {}

    class FakeGuardrailService:
        def __init__(self, registry, *, template_owner_lookup=None):
            captured["template_owner_lookup"] = template_owner_lookup

        def evaluate_boundary(self, boundary, context):
            return []

    monkeypatch.setattr(document_agent, "GuardrailService", FakeGuardrailService)
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm_json",
        lambda *_args, **_kwargs: _llm('{"document_brief":{"title":"Report","doc_type":"memo"}}'),
    )
    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", lambda **_kwargs: _llm("# Report"))
    monkeypatch.setattr("app.services.document_generator.generate_docx_bytes", lambda *args, **kwargs: b"DOCX")

    DocumentAgent(_load_from_files()).run(_state(), SimpleNamespace(plan={}), db=mock_db)

    assert captured["template_owner_lookup"] is not _template_belongs_to_user_db
    assert captured["template_owner_lookup"]("some-id", "u1") is True
    mock_db.query.assert_called_with(DocumentTemplate)


def test_document_agent_run_uses_provided_db_for_grammar(monkeypatch):
    register_all()
    mock_db = MagicMock()
    captured = {}

    def capture_grammar(*args, **kwargs):
        captured["db"] = kwargs.get("db")
        return {}

    monkeypatch.setattr(document_agent, "_fetch_template_grammar", capture_grammar)
    monkeypatch.setattr(
        "app.services.llm_gateway.invoke_llm_json",
        lambda *_args, **_kwargs: _llm('{"document_brief":{"title":"Deck","doc_type":"presentation"}}'),
    )
    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", lambda **_kwargs: _llm("# Deck"))
    monkeypatch.setattr("app.services.document_generator.generate_pptx_bytes", lambda **_kwargs: b"PPTX")

    DocumentAgent(_load_from_files()).run(
        _state(),
        SimpleNamespace(plan={"brand_profile": {"doc_type": "presentation"}}),
        db=mock_db,
    )

    assert captured["db"] is mock_db


def test_fetch_template_grammar_uses_provided_db(monkeypatch):
    mock_db = MagicMock()
    captured = {}

    def fake_template_grammar_for_selection(db, user_id, template_id, brief):
        captured["db"] = db
        return {"mode": "template_following"}

    monkeypatch.setattr(
        "app.services.document_templates.template_grammar_for_selection",
        fake_template_grammar_for_selection,
    )
    monkeypatch.setattr("app.db.models.SessionLocal", lambda: (_ for _ in ()).throw(RuntimeError("should not open")))

    result = _fetch_template_grammar("u1", "fronei-default", {}, db=mock_db)

    assert result == {"mode": "template_following"}
    assert captured["db"] is mock_db


def test_fetch_template_grammar_opens_own_session_when_no_db(monkeypatch):
    session = MagicMock()
    session_context = MagicMock()
    session_context.__enter__.return_value = session
    session_context.__exit__.return_value = None
    session_local = MagicMock(return_value=session_context)
    captured = {}

    def fake_template_grammar_for_selection(db, user_id, template_id, brief):
        captured["db"] = db
        return {}

    monkeypatch.setattr("app.db.models.SessionLocal", session_local)
    monkeypatch.setattr(
        "app.services.document_templates.template_grammar_for_selection",
        fake_template_grammar_for_selection,
    )

    result = _fetch_template_grammar("u1", "fronei-default", {}, db=None)

    assert result == {}
    assert captured["db"] is session
    session_local.assert_called_once_with()


def test_query_template_ownership_uses_provided_db(monkeypatch):
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = object()

    assert _query_template_ownership(mock_db, "tmpl-abc", "u1") is True
    mock_db.query.assert_called_with(DocumentTemplate)
