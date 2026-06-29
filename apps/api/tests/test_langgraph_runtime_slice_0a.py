from __future__ import annotations

import pytest

from app.services.agent.langgraph_runtime.nodes import NODE_ORDER
from app.services.agent.langgraph_runtime.runtime import configured_orchestrator, run_langgraph_research
from app.services.agent.models import TurnRequest
from app.services.agent.runtime import Runtime

from test_agent_runtime import FakeTools, _patch_completion


def test_langgraph_slice_0a_returns_legacy_public_shape(monkeypatch):
    events = []

    def progress(stage, message, **data):
        events.append((stage, message, data))

    result = run_langgraph_research(TurnRequest(message="Research something simple."), FakeTools(), progress)

    assert {
        "sources",
        "tool_calls",
        "evidence",
        "response",
        "plan",
        "worker_reports",
        "feedback",
        "answer_streamed",
        "replay_final_answer",
    }.issubset(result)
    assert result["response"].text == ""
    assert result["response"].model_used == "langgraph-slice-0a-stub"
    assert result["sources"] == []
    assert result["tool_calls"] == []
    assert result["langgraph_state"]["visited_nodes"] == list(NODE_ORDER)
    assert [stage for stage, _message, _data in events] == list(NODE_ORDER)


def test_server_side_langgraph_flag_runs_stub_path(monkeypatch):
    _patch_completion(monkeypatch)
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "fronei_orchestrator", "langgraph")
    monkeypatch.setattr(settings, "fronei_orchestrator_qa_override_enabled", False)
    runtime = Runtime(tools=FakeTools())

    envelopes = list(
        runtime.run_stream(
            TurnRequest(message="Please research test topic.", research_level="regular"),
            user_id="u1",
        )
    )

    result = next(envelope.data for envelope in envelopes if envelope.type == "result")
    assert result["route"] == "research"
    assert result["answer"] == ""
    assert result["model_used"] == "langgraph-slice-0a-stub"
    assert any(event["stage"] == "brief" for event in result["events"])


def test_ordinary_request_cannot_select_langgraph_path(monkeypatch):
    _patch_completion(monkeypatch)
    from app.config import get_settings
    from app.services.agent.langgraph_runtime import runtime as lg_runtime

    settings = get_settings()
    monkeypatch.setattr(settings, "fronei_orchestrator", "legacy")
    monkeypatch.setattr(settings, "fronei_orchestrator_qa_override_enabled", False)

    called = False

    def fail_if_called(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("ordinary request selected langgraph")

    monkeypatch.setattr(lg_runtime, "run_langgraph_research", fail_if_called)
    runtime = Runtime(tools=FakeTools())
    request = TurnRequest.model_validate(
        {
            "message": "Please research test topic.",
            "research_level": "regular",
            "fronei_orchestrator": "langgraph",
            "orchestrator": "langgraph",
            "headers": {"x-fronei-orchestrator": "langgraph"},
        }
    )

    envelopes = list(runtime.run_stream(request, user_id="u1"))

    assert called is False
    result = next(envelope.data for envelope in envelopes if envelope.type == "result")
    assert result["route"] == "research"
    assert result["model_used"] != "langgraph-slice-0a-stub"


def test_production_unsafe_qa_override_fails_closed(monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "fronei_orchestrator", "legacy")
    monkeypatch.setattr(settings, "fronei_orchestrator_qa_override_enabled", True)

    with pytest.raises(RuntimeError, match="Unsafe research orchestrator QA override"):
        configured_orchestrator()
