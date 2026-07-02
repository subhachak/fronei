from __future__ import annotations

from app.services.agent.langgraph_runtime.nodes import NODE_ORDER
from app.services.agent.langgraph_runtime.runtime import run_langgraph_research
from app.services.agent.models import TurnRequest
from app.services.agent.runtime import Runtime

from test_agent_runtime import FakeTools, _patch_completion


def test_langgraph_slice_0a_returns_legacy_public_shape(monkeypatch):
    # Slice 3: synthesis/repair make real LLM calls — patch model_client.
    _patch_completion(monkeypatch)

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
    # Slice 3: answer is real (non-empty from fake_simple_completion).
    assert isinstance(result["response"].text, str)
    # model_used is from the real synthesize/repair call (fake-model in tests).
    assert result["response"].model_used not in ("", "langgraph-slice-2-stub")
    # Slice 2: search is real; sources/tool_calls are populated by FakeTools.
    assert isinstance(result["sources"], list)
    assert isinstance(result["tool_calls"], list)
    # All pipeline nodes must be visited; budget gate nodes are additional.
    visited = result["langgraph_state"]["visited_nodes"]
    for node in NODE_ORDER:
        assert node in visited, f"Expected node '{node}' in visited_nodes"
    # Progress events are emitted for every pipeline node (plus budget gate nodes).
    stages = [stage for stage, _message, _data in events]
    for node in NODE_ORDER:
        assert node in stages, f"Expected progress event for '{node}'"


def test_runtime_research_runs_langgraph_path(monkeypatch):
    _patch_completion(monkeypatch)
    runtime = Runtime(tools=FakeTools())

    envelopes = list(
        runtime.run_stream(
            TurnRequest(message="Please research test topic.", research_level="regular"),
            user_id="u1",
        )
    )

    result = next(envelope.data for envelope in envelopes if envelope.type == "result")
    assert result["route"] == "research"
    # Slice 3: answer is real (from fake_simple_completion patch).
    assert isinstance(result["answer"], str)
    # model_used is from the synthesize/repair call — no longer "-stub" suffix.
    assert result["model_used"] not in ("", "langgraph-slice-2-stub")
    assert any(event["stage"] == "brief" for event in result["events"])
