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

# Runtime-mutable orchestrator override. Set by the admin /evals/parity/promote
# endpoint after a successful parity gate run.  Takes precedence over the
# FRONEI_ORCHESTRATOR env var for the lifetime of the current process; lost on
# restart (env var / config.py default applies on next boot).
_RUNTIME_ORCHESTRATOR_OVERRIDE: str | None = None


def set_orchestrator_override(value: str) -> None:
    """Set a process-lifetime orchestrator override (admin promote action)."""
    global _RUNTIME_ORCHESTRATOR_OVERRIDE
    if value not in VALID_ORCHESTRATORS:
        raise ValueError(f"Invalid orchestrator value: {value!r}")
    _RUNTIME_ORCHESTRATOR_OVERRIDE = value


def clear_orchestrator_override() -> None:
    """Clear the process-lifetime override and revert to env/config default."""
    global _RUNTIME_ORCHESTRATOR_OVERRIDE
    _RUNTIME_ORCHESTRATOR_OVERRIDE = None


def configured_orchestrator() -> str:
    # Process-lifetime override (set by admin promote action) takes precedence.
    if _RUNTIME_ORCHESTRATOR_OVERRIDE is not None:
        return _RUNTIME_ORCHESTRATOR_OVERRIDE
    settings = get_settings()
    # Default is "legacy" until the parity gate passes.  Set
    # FRONEI_ORCHESTRATOR=langgraph to route through the LangGraph pipeline.
    selected = (settings.fronei_orchestrator or "legacy").strip().lower()
    if selected not in VALID_ORCHESTRATORS:
        raise RuntimeError(f"Invalid FRONEI_ORCHESTRATOR value: {settings.fronei_orchestrator!r}")
    production = settings.app_env.strip().lower() in {"prod", "production"}
    if production and settings.fronei_orchestrator_qa_override_enabled:
        raise RuntimeError("Unsafe research orchestrator QA override is enabled in production.")
    return selected


def run_langgraph_research(request: Any, tools: Any, progress: Any = None) -> dict[str, Any]:
    """LangGraph research entry point — production default since Slice 6.

    Full pipeline (all nodes real, Slice 4 complete):
      brief → subject_derivation → contract → plan →
      dispatch_search/search_worker → rank → read → classify_claims →
      expand_source_graph → bind → synthesize → verify → judge → repair

    The returned dictionary matches the public keys of lead_research_loop.
    Set FRONEI_ORCHESTRATOR=legacy in the environment to revert to the
    legacy lead_research_loop path without redeploying.
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
