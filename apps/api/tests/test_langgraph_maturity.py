from __future__ import annotations

from app.db.models import LangGraphRunContext, SessionLocal
from app.services.agent import model_client
from app.services.agent.langgraph_runtime.nodes import NODE_ORDER
from app.services.agent.langgraph_runtime.graph import get_compiled_research_graph
from app.services.agent.langgraph_runtime.runtime import (
    resume_langgraph_research,
    run_langgraph_research,
    stream_langgraph_research,
)
from app.services.agent.langgraph_runtime.state import BudgetDecision
from app.services.agent.models import TurnRequest
from app.services.agent.orchestrator import OrchestratorDecision
from app.services.agent.research_models import EvidenceItem, EvidencePack, ResearchJudgeResult, ResearchPlan
from app.services.agent.research_synthesis import synthesize_answer_stream
from app.services.agent.runtime import Runtime

from test_agent_runtime import FakeTools, _patch_completion


def test_research_graph_compiles_once_with_checkpointer():
    first = get_compiled_research_graph()
    second = get_compiled_research_graph()

    assert first is second
    assert getattr(first, "checkpointer", None) is not None


def test_langgraph_pause_resume_survives_empty_in_memory_context(monkeypatch):
    _patch_completion(monkeypatch)

    from app.services.agent.langgraph_runtime import runtime as runtime_module
    import app.services.agent.langgraph_runtime.nodes as nodes_module

    original_bind = nodes_module.bind
    original_judge = nodes_module.judge

    def force_budget_pause(state, *, run_id, request, tools=None, progress=None):
        result = original_bind(state, run_id=run_id, request=request, tools=tools, progress=progress)
        result["cost_usd_spent"] = 10.0
        return result

    def approve_after_resume(state, *, run_id, request, tools=None, progress=None):
        result = original_judge(state, run_id=run_id, request=request, tools=tools, progress=progress)
        result["judge_result"] = ResearchJudgeResult(status="pass", score=0.9, issues=[], can_publish=True)
        result["next_action"] = "publish"
        return result

    monkeypatch.setattr(nodes_module, "bind", force_budget_pause)
    monkeypatch.setattr(nodes_module, "judge", approve_after_resume)

    request = TurnRequest(message="Pause and resume test.", research_level="regular")
    paused = run_langgraph_research(request, FakeTools())
    run_id = paused["langgraph_run_id"]

    assert paused["langgraph_state"].get("budget_decision") == BudgetDecision.REQUIRE_HUMAN_APPROVAL
    assert paused["langgraph_state"].get("pause_contract")

    runtime_module._RUN_CONTEXTS.clear()
    with SessionLocal() as db:
        row = db.get(LangGraphRunContext, run_id)
        assert row is not None
        assert row.status == "paused"

    resumed = resume_langgraph_research(run_id, approved_by="test-admin")

    assert resumed["response"].text
    assert resumed["feedback"].judge.can_publish is True
    assert resumed["langgraph_state"].get("approval_contract", {}).get("approved_by") == "test-admin"
    with SessionLocal() as db:
        row = db.get(LangGraphRunContext, run_id)
        assert row is not None
        assert row.status == "completed"


def test_langgraph_run_context_does_not_persist_plaintext_tool_keys(monkeypatch):
    _patch_completion(monkeypatch)

    result = run_langgraph_research(
        TurnRequest(message="Please research stored tool context.", research_level="regular"),
        FakeTools(),
    )

    with SessionLocal() as db:
        row = db.get(LangGraphRunContext, result["langgraph_run_id"])
        assert row is not None
        assert "fake" not in row.tool_config_json.lower()
        assert "you_api_key" not in row.tool_config_json
        assert "tavily_api_key" not in row.tool_config_json
        assert "nimble_api_key" not in row.tool_config_json


def test_synthesize_answer_stream_emits_incremental_deltas(monkeypatch):
    deltas: list[str] = []

    def fake_stream_complete(messages, **kwargs):
        yield model_client.ModelDelta("First chunk. ")
        yield model_client.ModelDelta("Second chunk.")
        yield model_client.ModelResponse(
            text="First chunk. Second chunk.",
            model_used="fake-model",
            latency_ms=4,
            cost_usd=0.0,
        )

    monkeypatch.setattr(model_client, "stream_complete", fake_stream_complete)

    response = synthesize_answer_stream(
        TurnRequest(message="Research a topic."),
        ResearchPlan(questions=["What matters?"], search_queries=["topic evidence"]),
        EvidencePack(
            items=[
                EvidenceItem(
                    source_id="S1",
                    title="Source",
                    url="https://example.com",
                    evidence="Evidence text",
                )
            ],
            coverage=1.0,
        ),
        on_delta=deltas.append,
    )

    assert deltas == ["First chunk. ", "Second chunk."]
    assert response.text == "First chunk. Second chunk."


def test_stream_langgraph_research_yields_node_and_answer_delta_events(monkeypatch):
    _patch_completion(monkeypatch, text="Alpha streamed answer. Beta streamed answer.")

    events = list(
        stream_langgraph_research(
            TurnRequest(message="Please research streaming progress.", research_level="regular"),
            FakeTools(),
        )
    )

    node_names = [payload["node_name"] for kind, payload in events if kind == "node"]
    deltas = [payload for kind, payload in events if kind == "delta"]

    assert "brief" in node_names
    assert "synthesize" in node_names
    assert any(node in node_names for node in NODE_ORDER)
    assert len(deltas) >= 2
    assert "".join(deltas) == "Alpha streamed answer. Beta streamed answer."


def test_langgraph_research_sse_streams_progress_and_answer_before_result(monkeypatch):
    _patch_completion(monkeypatch, text="Alpha streamed answer. Beta streamed answer.")
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "fronei_orchestrator", "langgraph")
    monkeypatch.setattr(settings, "fronei_orchestrator_qa_override_enabled", False)

    envelopes = list(
        Runtime(tools=FakeTools()).run_stream(
            TurnRequest(message="Please research streaming progress.", research_level="regular"),
            user_id="u1",
        )
    )

    event_types = [envelope.type for envelope in envelopes]
    result_index = event_types.index("result")
    progress_before_result = [
        envelope.data
        for envelope in envelopes[:result_index]
        if envelope.type == "progress"
    ]
    stages = [event["stage"] for event in progress_before_result]

    assert "brief" in stages
    assert "synthesize" in stages
    assert "answer_delta" in stages
    assert stages.index("answer_delta") < stages.index("judge")

    result = envelopes[result_index].data
    assert result["answer"] == "Alpha streamed answer. Beta streamed answer."
    assert result["events"][-1]["stage"] == "answer_complete"


def test_langgraph_deep_repair_does_not_buffer_replay_after_stream(monkeypatch):
    streamed_text = "Alpha streamed answer. Beta streamed answer."
    _patch_completion(monkeypatch, text=streamed_text)
    from app.config import get_settings
    from app.services.agent import runtime as runtime_module
    import app.services.agent.langgraph_runtime.nodes as nodes_module

    settings = get_settings()
    monkeypatch.setattr(settings, "fronei_orchestrator", "langgraph")
    monkeypatch.setattr(settings, "fronei_orchestrator_qa_override_enabled", False)
    monkeypatch.setattr(runtime_module, "decide_fast_path", lambda request: type("FastPath", (), {"path": "none"})())
    monkeypatch.setattr(
        runtime_module,
        "decide_with_options",
        lambda request, **kwargs: OrchestratorDecision(
            route="research",
            research_level="deep",
            requires_confirmation=False,
            reason="test",
            source="test",
            available_routes=kwargs.get("available_routes", []),
            available_tools=kwargs.get("available_tools", []),
        ),
    )

    original_judge = nodes_module.judge

    def request_repair(state, *, run_id, request, tools=None, progress=None):
        result = original_judge(state, run_id=run_id, request=request, tools=tools, progress=progress)
        result["judge_result"] = ResearchJudgeResult(
            status="repair",
            score=0.55,
            issues=["Needs polish."],
            repair_instruction="Polish the answer.",
            can_publish=False,
        )
        result["next_action"] = "research_more"
        return result

    monkeypatch.setattr(nodes_module, "judge", request_repair)

    envelopes = list(
        Runtime(tools=FakeTools()).run_stream(
            TurnRequest(message="Please research with repair.", research_level="deep"),
            user_id="u1",
        )
    )
    answer_deltas = [
        envelope.data["data"]["delta"]
        for envelope in envelopes
        if envelope.type == "progress" and envelope.data["stage"] == "answer_delta"
    ]
    result = next(envelope.data for envelope in envelopes if envelope.type == "result")

    assert "".join(answer_deltas) == streamed_text
    assert result["answer"] == streamed_text
    assert result["events"][-1]["stage"] == "answer_complete"
