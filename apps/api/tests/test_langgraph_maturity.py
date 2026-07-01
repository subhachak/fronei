from __future__ import annotations

from app.db.models import LangGraphRunContext, SessionLocal
from app.services.agent.langgraph_runtime.graph import get_compiled_research_graph
from app.services.agent.langgraph_runtime.runtime import resume_langgraph_research, run_langgraph_research
from app.services.agent.langgraph_runtime.state import BudgetDecision
from app.services.agent.models import TurnRequest
from app.services.agent.research_models import ResearchJudgeResult

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
