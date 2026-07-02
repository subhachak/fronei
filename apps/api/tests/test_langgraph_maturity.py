from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.auth import AdminPrincipal
from app.db.models import LangGraphRunContext, SessionLocal, Turn
from app.routers import admin as admin_router
from app.services.agent import model_client
from app.services.agent import persistence
from app.services.agent.langgraph_runtime.nodes import NODE_ORDER
from app.services.agent.langgraph_runtime.graph import get_compiled_research_graph
from app.services.agent.langgraph_runtime.runtime import (
    LangGraphResumeConflict,
    claim_langgraph_run_for_resume,
    resume_langgraph_research,
    run_langgraph_research,
    stream_langgraph_research,
)
from app.services.agent.langgraph_runtime.state import BudgetDecision
from app.services.agent.models import Goal, TurnRequest, TurnResult, new_id
from app.services.agent.orchestrator import OrchestratorDecision
from app.services.agent.research_models import EvidenceItem, EvidencePack, ResearchJudgeResult, ResearchPlan
from app.services.agent.research_synthesis import synthesize_answer_stream
from app.services.agent.runtime import Runtime
from app.services.maintenance_jobs import (
    cleanup_langgraph_checkpoints,
    reconcile_orphaned_langgraph_runs,
)

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
        assert row.completed_at is not None


def test_langgraph_run_context_does_not_persist_plaintext_tool_keys(monkeypatch):
    _patch_completion(monkeypatch)

    run_id = _pause_a_run(monkeypatch)

    with SessionLocal() as db:
        row = db.get(LangGraphRunContext, run_id)
        assert row is not None
        assert row.status == "paused"
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
    assert all(set(delta) == {"text", "source_node"} for delta in deltas)
    assert "synthesize" in {delta["source_node"] for delta in deltas}
    for source_node in {delta["source_node"] for delta in deltas}:
        source_text = "".join(delta["text"] for delta in deltas if delta["source_node"] == source_node)
        assert source_text == "Alpha streamed answer. Beta streamed answer."


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


def test_runtime_research_turn_surfaces_langgraph_pause(monkeypatch):
    _patch_completion(monkeypatch)
    from app.config import get_settings
    import app.services.agent.langgraph_runtime.nodes as nodes_module

    settings = get_settings()
    monkeypatch.setattr(settings, "fronei_orchestrator", "langgraph")
    monkeypatch.setattr(settings, "fronei_orchestrator_qa_override_enabled", False)

    original_bind = nodes_module.bind

    def force_budget_pause(state, *, run_id, request, tools=None, progress=None):
        result = original_bind(state, run_id=run_id, request=request, tools=tools, progress=progress)
        result["cost_usd_spent"] = 10.0
        return result

    monkeypatch.setattr(nodes_module, "bind", force_budget_pause)

    envelopes = list(
        Runtime(tools=FakeTools()).run_stream(
            TurnRequest(message="Research pause lifecycle.", research_level="regular"),
            user_id="u1",
        )
    )

    result = next(envelope.data for envelope in envelopes if envelope.type == "result")
    assert result["turn_status"] == "paused"
    assert result["langgraph_run_id"]
    assert result["pause_reason"]
    assert isinstance(result["required_additional_budget_usd"], float)
    assert result["answer"] == ""


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
    progress_events = [
        envelope.data
        for envelope in envelopes
        if envelope.type == "progress"
    ]
    answer_deltas = [event for event in progress_events if event["stage"] == "answer_delta"]
    reset_index = next(
        index
        for index, event in enumerate(progress_events)
        if event["stage"] == "repair" and event["data"].get("reset") is True
    )
    synth_delta_indexes = [
        index
        for index, event in enumerate(progress_events)
        if event["stage"] == "answer_delta" and event["data"].get("source_node") == "synthesize"
    ]
    repair_delta_indexes = [
        index
        for index, event in enumerate(progress_events)
        if event["stage"] == "answer_delta" and event["data"].get("source_node") == "repair"
    ]
    post_reset_deltas = [
        event["data"]["delta"]
        for index, event in enumerate(progress_events)
        if index > reset_index and event["stage"] == "answer_delta"
    ]
    result = next(envelope.data for envelope in envelopes if envelope.type == "result")

    assert answer_deltas
    assert synth_delta_indexes
    assert repair_delta_indexes
    assert max(synth_delta_indexes) < reset_index < min(repair_delta_indexes)
    assert progress_events[reset_index]["data"]["reset"] is True
    assert "".join(post_reset_deltas) == streamed_text
    assert result["answer"] == streamed_text
    assert result["events"][-1]["stage"] == "answer_complete"


def test_streaming_resume_persists_original_turn_through_envelopes(monkeypatch):
    _patch_completion(monkeypatch)
    import app.services.agent.langgraph_runtime.nodes as nodes_module

    original_judge = nodes_module.judge

    def approve_after_resume(state, *, run_id, request, tools=None, progress=None):
        result = original_judge(state, run_id=run_id, request=request, tools=tools, progress=progress)
        result["judge_result"] = ResearchJudgeResult(status="pass", score=0.9, issues=[], can_publish=True)
        result["next_action"] = "publish"
        return result

    monkeypatch.setattr(nodes_module, "judge", approve_after_resume)

    run_id = _pause_a_run(monkeypatch)
    goal = Goal(user_id="u1", conversation_id=None, objective="Paused turn", route="research")
    turn_id = new_id("turn")
    persistence.complete_turn(
        TurnResult(
            turn_id=turn_id,
            goal=goal,
            answer="",
            route="research",
            turn_status="paused",
            langgraph_run_id=run_id,
            pause_reason="Budget approval is required.",
        )
    )

    with SessionLocal() as db:
        row = db.get(Turn, turn_id)
        assert row is not None
        assert row.status == "paused"
        assert row.completed_at is None
        assert row.langgraph_run_id == run_id

    claim_langgraph_run_for_resume(run_id, resumed_by="admin-1")
    persistence.mark_turn_running_for_resume(turn_id)
    envelopes = list(
        Runtime(tools=FakeTools()).resume_langgraph_turn_stream(
            turn_id,
            run_id,
            approved_by="admin-1",
            updated_budget_ceiling_usd=None,
            user_id="u1",
        )
    )
    for envelope in envelopes:
        assert persistence.persist_turn_envelope(envelope, turn_id) is True

    with SessionLocal() as db:
        row = db.get(Turn, turn_id)
        assert row is not None
        assert row.status == "completed"
        assert row.completed_at is not None
        assert row.langgraph_run_id is None
        assert row.pause_reason is None
        assert row.answer

    stages = [envelope.data["stage"] for envelope in envelopes if envelope.type == "progress"]
    assert "answer_delta" in stages
    assert envelopes[-1].type == "result"


def _pause_a_run(monkeypatch) -> str:
    """Shared setup: force a budget-gate pause and return the paused run_id."""
    from app.services.agent.langgraph_runtime import runtime as runtime_module
    import app.services.agent.langgraph_runtime.nodes as nodes_module

    original_bind = nodes_module.bind

    def force_budget_pause(state, *, run_id, request, tools=None, progress=None):
        result = original_bind(state, run_id=run_id, request=request, tools=tools, progress=progress)
        result["cost_usd_spent"] = 10.0
        return result

    monkeypatch.setattr(nodes_module, "bind", force_budget_pause)

    request = TurnRequest(message="Pause for resume-conflict test.", research_level="regular")
    paused = run_langgraph_research(request, FakeTools())
    run_id = paused["langgraph_run_id"]
    assert paused["langgraph_state"].get("budget_decision") == BudgetDecision.REQUIRE_HUMAN_APPROVAL
    runtime_module._RUN_CONTEXTS.clear()
    return run_id


def test_resume_langgraph_research_rejects_concurrent_duplicate_resume(monkeypatch):
    """Gap 1: a second resume_langgraph_research call for the same run_id
    must raise LangGraphResumeConflict instead of re-invoking the graph
    (which would double-run synthesize/repair LLM calls and double spend).
    """
    _patch_completion(monkeypatch)

    import app.services.agent.langgraph_runtime.nodes as nodes_module

    original_judge = nodes_module.judge

    def approve_after_resume(state, *, run_id, request, tools=None, progress=None):
        result = original_judge(state, run_id=run_id, request=request, tools=tools, progress=progress)
        result["judge_result"] = ResearchJudgeResult(status="pass", score=0.9, issues=[], can_publish=True)
        result["next_action"] = "publish"
        return result

    monkeypatch.setattr(nodes_module, "judge", approve_after_resume)

    run_id = _pause_a_run(monkeypatch)

    invoke_calls = {"count": 0}
    original_invoke = get_compiled_research_graph().invoke

    def counting_invoke(*args, **kwargs):
        invoke_calls["count"] += 1
        return original_invoke(*args, **kwargs)

    monkeypatch.setattr(get_compiled_research_graph(), "invoke", counting_invoke)

    first = resume_langgraph_research(run_id, approved_by="admin-1")
    assert first["feedback"].judge.can_publish is True
    assert invoke_calls["count"] == 1

    with SessionLocal() as db:
        row = db.get(LangGraphRunContext, run_id)
        assert row is not None
        assert row.status == "completed"
        assert row.resumed_at is not None
        assert row.resumed_by == "admin-1"

    with pytest.raises(LangGraphResumeConflict):
        resume_langgraph_research(run_id, approved_by="admin-2")

    # The graph must not have been invoked a second time for the same run_id.
    assert invoke_calls["count"] == 1


def test_cleanup_langgraph_checkpoints_keeps_recent_completed_runs(monkeypatch):
    """Gap 2: successful runs are retained for the configured window, not
    deleted immediately on completion.
    """
    run_id = new_id("lgrun")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        row = LangGraphRunContext(
            run_id=run_id,
            request_json="{}",
            tool_config_json="{}",
            status="completed",
            created_at=now,
            updated_at=now,
            completed_at=now,
        )
        db.add(row)
        db.commit()

    class FakeCheckpointer:
        def delete_thread(self, thread_id: str) -> None:
            raise AssertionError("fresh completed runs must stay until the retention window expires")

    monkeypatch.setattr(
        "app.services.agent.langgraph_runtime.checkpointer.get_checkpointer",
        lambda: FakeCheckpointer(),
    )

    result = cleanup_langgraph_checkpoints(retention_days=7)

    assert run_id not in result["deleted_run_ids"]
    with SessionLocal() as db:
        row = db.get(LangGraphRunContext, run_id)
        assert row is not None
        assert row.status == "completed"


def test_cleanup_langgraph_checkpoints_deletes_old_completed_runs(monkeypatch):
    """Gap 2: rows completed more than the retention window ago get their
    checkpoint deleted and their langgraph_run_contexts row removed.
    """
    run_id = new_id("lgrun")
    old_completed_at = datetime.now(timezone.utc) - timedelta(days=30)

    with SessionLocal() as db:
        row = LangGraphRunContext(
            run_id=run_id,
            request_json="{}",
            tool_config_json="{}",
            status="completed",
            created_at=old_completed_at,
            updated_at=old_completed_at,
            completed_at=old_completed_at,
        )
        db.add(row)
        db.commit()

    deleted_thread_ids: list[str] = []

    class FakeCheckpointer:
        def delete_thread(self, thread_id: str) -> None:
            deleted_thread_ids.append(thread_id)

    monkeypatch.setattr(
        "app.services.agent.langgraph_runtime.checkpointer.get_checkpointer",
        lambda: FakeCheckpointer(),
    )

    result = cleanup_langgraph_checkpoints(retention_days=7)

    assert run_id in result["deleted_run_ids"]
    assert run_id in deleted_thread_ids
    with SessionLocal() as db:
        assert db.get(LangGraphRunContext, run_id) is None


def test_cleanup_langgraph_checkpoints_never_touches_active_statuses(monkeypatch):
    """Gap 2: paused/running/resuming rows are never deleted regardless of age."""
    old = datetime.now(timezone.utc) - timedelta(days=30)
    run_ids = {}
    with SessionLocal() as db:
        for status in ("paused", "running", "resuming"):
            run_id = new_id("lgrun")
            run_ids[status] = run_id
            db.add(
                LangGraphRunContext(
                    run_id=run_id,
                    request_json="{}",
                    tool_config_json="{}",
                    status=status,
                    created_at=old,
                    updated_at=old,
                    completed_at=old if status != "paused" else None,
                )
            )
        db.commit()

    class FakeCheckpointer:
        def delete_thread(self, thread_id: str) -> None:
            raise AssertionError("delete_thread must not be called for active-status runs")

    # The paused-row reconciliation path calls pending_langgraph_pause, which
    # hits the real checkpointer/graph; stub it out so this test only
    # exercises the deletion-eligibility filter, not Gap 4's reconciliation.
    monkeypatch.setattr(
        "app.services.agent.langgraph_runtime.checkpointer.get_checkpointer",
        lambda: FakeCheckpointer(),
    )
    monkeypatch.setattr(
        "app.services.agent.langgraph_runtime.pending_langgraph_pause",
        lambda run_id: {"run_id": run_id, "status": "paused", "pause_contract": {}},
    )

    result = cleanup_langgraph_checkpoints(retention_days=7)

    assert result["deleted_run_ids"] == []
    with SessionLocal() as db:
        for status, run_id in run_ids.items():
            row = db.get(LangGraphRunContext, run_id)
            assert row is not None
            assert row.status == status


def test_reconcile_orphaned_langgraph_runs_flips_stale_running_rows():
    """Gap 3: a 'running' row left over from a crashed process gets flipped
    to 'orphaned' by the startup reconciliation function.
    """
    run_id = new_id("lgrun")
    with SessionLocal() as db:
        db.add(
            LangGraphRunContext(
                run_id=run_id,
                request_json="{}",
                tool_config_json="{}",
                status="running",
            )
        )
        db.commit()

    result = reconcile_orphaned_langgraph_runs()

    assert run_id in result["orphaned_run_ids"]
    with SessionLocal() as db:
        row = db.get(LangGraphRunContext, run_id)
        assert row is not None
        assert row.status == "orphaned"


def test_cleanup_langgraph_checkpoints_corrects_drifted_paused_status(monkeypatch):
    """Gap 4: a row claiming status='paused' whose checkpoint no longer shows
    a pending interrupt (resumed via a path that skipped this row, or the
    checkpoint is gone) gets corrected to 'orphaned' by the maintenance pass.
    """
    run_id = new_id("lgrun")
    with SessionLocal() as db:
        db.add(
            LangGraphRunContext(
                run_id=run_id,
                request_json="{}",
                tool_config_json="{}",
                status="paused",
            )
        )
        db.commit()

    class FakeCheckpointer:
        def delete_thread(self, thread_id: str) -> None:
            raise AssertionError("paused-status reconciliation must not delete checkpoints")

    monkeypatch.setattr(
        "app.services.agent.langgraph_runtime.checkpointer.get_checkpointer",
        lambda: FakeCheckpointer(),
    )
    # Simulate "checkpoint has already resumed/completed" -> no pending interrupt.
    monkeypatch.setattr(
        "app.services.agent.langgraph_runtime.pending_langgraph_pause",
        lambda run_id: None,
    )

    result = cleanup_langgraph_checkpoints(retention_days=7)

    assert run_id in result["reconciled_run_ids"]
    with SessionLocal() as db:
        row = db.get(LangGraphRunContext, run_id)
        assert row is not None
        assert row.status == "orphaned"


def test_admin_langgraph_runs_lists_joined_turn_context():
    run_id = new_id("lgrun")
    turn_id = new_id("turn")
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        db.add(
            LangGraphRunContext(
                run_id=run_id,
                request_json="{}",
                tool_config_json="{}",
                status="paused",
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            Turn(
                id=turn_id,
                user_id="u-admin-list",
                conversation_id=None,
                objective="Research paused admin list context",
                route="research",
                quality_mode="standard",
                status="paused",
                langgraph_run_id=run_id,
                pause_reason="Budget approval is required.",
                created_at=now,
                updated_at=now,
            )
        )
        db.commit()

    response = admin_router.list_langgraph_runs(status="paused", admin=AdminPrincipal(user_id="admin"))
    item = next(item for item in response["items"] if item["run_id"] == run_id)

    assert item["turn_id"] == turn_id
    assert item["objective"] == "Research paused admin list context"
    assert item["user_id"] == "u-admin-list"
    assert item["pause_reason"] == "Budget approval is required."
