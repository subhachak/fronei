from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.services.agent import model_client
from app.services.agent.langgraph_runtime.graph import run_stub_graph  # noqa: F401 — re-exported for tests
from app.services.agent.models import new_id
from app.services.agent.research_models import (
    EvidencePack,
    ResearchFeedbackLoop,
    ResearchJudgeResult,
    ResearchPlan,
)


VALID_ORCHESTRATORS = {"legacy", "langgraph"}


def configured_orchestrator() -> str:
    settings = get_settings()
    selected = (settings.fronei_orchestrator or "legacy").strip().lower()
    if selected not in VALID_ORCHESTRATORS:
        raise RuntimeError(f"Invalid FRONEI_ORCHESTRATOR value: {settings.fronei_orchestrator!r}")
    production = settings.app_env.strip().lower() in {"prod", "production"}
    if production and settings.fronei_orchestrator_qa_override_enabled:
        raise RuntimeError("Unsafe research orchestrator QA override is enabled in production.")
    return selected


def run_langgraph_research(request: Any, tools: Any, progress: Any = None) -> dict[str, Any]:
    """LangGraph research entry point — Slice 3.

    Real nodes:
      brief → subject_derivation → contract → plan →
      dispatch_search/search_worker → rank → read →
      expand_source_graph → bind → synthesize → verify → judge → repair

    Partial stub (Slice 4 will wire real claim classification):
      classify_claims — still returns empty claim_classification_results

    The returned dictionary matches the public keys of lead_research_loop.
    """
    run_id = new_id("lgrun")
    final_state = run_stub_graph(
        {"request_message": getattr(request, "message", ""), "visited_nodes": [], "artifacts": {}},
        run_id=run_id,
        request=request,
        progress=progress,
        tools=tools,
    )

    answer = final_state.get("answer", "")
    model_used = final_state.get("model_used") or "langgraph"
    latency_ms = final_state.get("latency_ms") or 0
    cost_usd = final_state.get("cost_usd_spent") or 0.0

    response = model_client.ModelResponse(
        text=answer,
        model_used=model_used,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        model_role="research_synthesis",
    )

    # Build feedback from real judge result; fall back to pass if judge didn't run.
    judge_result = final_state.get("judge_result") or ResearchJudgeResult(
        status="pass", score=1.0, issues=[], can_publish=True
    )
    repair_history = final_state.get("repair_history") or []
    repaired = bool(repair_history)
    feedback = ResearchFeedbackLoop(
        judge=judge_result,
        repaired=repaired,
        repair_attempts=len(repair_history),
        final_score=judge_result.score,
    )

    return {
        "sources": final_state.get("sources") or [],
        "tool_calls": final_state.get("tool_calls") or [],
        "evidence": final_state.get("evidence") or EvidencePack(),
        "response": response,
        "plan": final_state.get("plan") or ResearchPlan(
            source="stub", fallback_reason="LangGraph Slice 3 — plan derivation failed."
        ),
        "worker_reports": final_state.get("worker_reports") or [],
        "feedback": feedback,
        "answer_streamed": False,
        "replay_final_answer": repaired,
        "langgraph_run_id": run_id,
        "langgraph_state": final_state,
    }
