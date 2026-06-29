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
    """LangGraph research entry point — Slice 1.

    The first four nodes (brief, subject_derivation, contract, plan) are real.
    Remaining nodes (search → repair) are still stubs until Slice 2+.
    The returned dictionary matches the public keys of lead_research_loop.
    """
    _ = tools
    run_id = new_id("lgrun")
    final_state = run_stub_graph(
        {"request_message": getattr(request, "message", ""), "visited_nodes": [], "artifacts": {}},
        run_id=run_id,
        request=request,
        progress=progress,
    )
    stub_model = "langgraph-slice-1-stub"
    response = model_client.ModelResponse(
        text=final_state.get("answer", ""),
        model_used=final_state.get("model_used", stub_model),
        latency_ms=final_state.get("latency_ms", 0),
        cost_usd=final_state.get("cost_usd_spent", 0.0),
        model_role="research_synthesis",
    )
    feedback = ResearchFeedbackLoop(
        judge=ResearchJudgeResult(status="pass", score=1.0, issues=[], can_publish=True),
        repaired=False,
        repair_attempts=0,
        final_score=1.0,
    )
    return {
        "sources": [],
        "tool_calls": [],
        "evidence": EvidencePack(),
        "response": response,
        "plan": final_state.get("plan") or ResearchPlan(source="stub", fallback_reason="LangGraph Slice 1 stub — search not yet wired."),
        "worker_reports": [],
        "feedback": feedback,
        "answer_streamed": False,
        "replay_final_answer": False,
        "langgraph_run_id": run_id,
        "langgraph_state": final_state,
    }
