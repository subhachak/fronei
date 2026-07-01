from __future__ import annotations

import json
import logging
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any

from langgraph.types import Command

from app.config import get_settings
from app.db.models import LangGraphRunContext, SessionLocal
from app.services.agent import model_client
from app.services.agent.langgraph_runtime.graph import (
    get_compiled_research_graph,
    run_stub_graph,  # noqa: F401 - re-exported for tests
)
from app.services.agent.langgraph_runtime.state import BudgetDecision
from app.services.agent.models import TurnRequest, new_id
from app.services.agent.research_models import (
    EvidencePack,
    ResearchFeedbackLoop,
    ResearchJudgeResult,
    ResearchPlan,
)
from app.services.agent.tools import Tools

logger = logging.getLogger(__name__)

VALID_ORCHESTRATORS = {"legacy", "langgraph"}

# Runtime-mutable orchestrator override. Set by the admin /evals/parity/promote
# endpoint after a successful parity gate run.  Takes precedence over the
# FRONEI_ORCHESTRATOR env var for the lifetime of the current process; lost on
# restart (env var / config.py default applies on next boot).
_RUNTIME_ORCHESTRATOR_OVERRIDE: str | None = None

# In-process convenience cache for pause/resume. Checkpoints are persisted in
# SQLite; this only preserves richer Python objects such as injected test tools.
_RUN_CONTEXTS: dict[str, dict[str, Any]] = {}


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
    selected = (settings.fronei_orchestrator or "legacy").strip().lower()
    if selected not in VALID_ORCHESTRATORS:
        raise RuntimeError(f"Invalid FRONEI_ORCHESTRATOR value: {settings.fronei_orchestrator!r}")
    production = settings.app_env.strip().lower() in {"prod", "production"}
    if production and settings.fronei_orchestrator_qa_override_enabled:
        raise RuntimeError("Unsafe research orchestrator QA override is enabled in production.")
    return selected


def _langgraph_config(run_id: str, request: Any | None, tools: Any | None, progress: Any = None) -> dict[str, Any]:
    return {
        "configurable": {
            "thread_id": run_id,
            "run_id": run_id,
            "request": request,
            "tools": tools,
            "progress": progress,
            "preserve_tools_none": False,
        },
        "metadata": {
            "run_id": run_id,
            "research_level": getattr(request, "research_level", None),
            "conversation_id": getattr(request, "conversation_id", None),
            "orchestrator": "langgraph",
        },
        "tags": ["fronei", "research", "langgraph"],
    }


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str)


def _loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _tool_config(tools: Any | None) -> dict[str, Any]:
    if tools is None:
        tools = Tools.from_settings()
    if is_dataclass(tools):
        return asdict(tools)
    return {
        "you_api_key": getattr(tools, "you_api_key", None),
        "tavily_api_key": getattr(tools, "tavily_api_key", None),
        "nimble_api_key": getattr(tools, "nimble_api_key", None),
        "nimble_api_endpoint": getattr(tools, "nimble_api_endpoint", Tools().nimble_api_endpoint),
    }


def _tools_from_config(payload: dict[str, Any] | None) -> Tools:
    payload = payload or {}
    return Tools(
        you_api_key=payload.get("you_api_key"),
        tavily_api_key=payload.get("tavily_api_key"),
        nimble_api_key=payload.get("nimble_api_key"),
        nimble_api_endpoint=payload.get("nimble_api_endpoint") or Tools().nimble_api_endpoint,
    )


def _persist_run_context(run_id: str, request: Any, tools: Any | None, *, status: str = "running") -> None:
    now = datetime.now(timezone.utc)
    request_payload = request.model_dump(mode="json") if hasattr(request, "model_dump") else {}
    db = SessionLocal()
    try:
        row = db.get(LangGraphRunContext, run_id)
        if row is None:
            row = LangGraphRunContext(run_id=run_id, created_at=now)
            db.add(row)
        row.request_json = _dumps(request_payload)
        row.tool_config_json = _dumps(_tool_config(tools))
        row.status = status
        row.updated_at = now
        if status == "completed":
            row.completed_at = now
        db.commit()
    finally:
        db.close()


def _load_run_context(run_id: str) -> dict[str, Any] | None:
    db = SessionLocal()
    try:
        row = db.get(LangGraphRunContext, run_id)
        if row is None:
            return None
        request_payload = _loads(row.request_json, {})
        tool_payload = _loads(row.tool_config_json, {})
        request = TurnRequest.model_validate(request_payload) if isinstance(request_payload, dict) else None
        return {
            "request": request,
            "tools": _tools_from_config(tool_payload if isinstance(tool_payload, dict) else {}),
            "status": row.status,
        }
    finally:
        db.close()


def _mark_run_context(run_id: str, status: str) -> None:
    db = SessionLocal()
    try:
        row = db.get(LangGraphRunContext, run_id)
        if row is None:
            return
        row.status = status
        row.updated_at = datetime.now(timezone.utc)
        if status == "completed":
            row.completed_at = row.updated_at
        db.commit()
    finally:
        db.close()


def _initial_state(run_id: str, request: Any) -> dict[str, Any]:
    state = {
        "run_id": run_id,
        "request_message": getattr(request, "message", ""),
        "research_level": getattr(request, "research_level", None),
        "visited_nodes": [],
        "artifacts": {},
    }
    if hasattr(request, "model_dump"):
        state["request_payload"] = request.model_dump(mode="json")
    return state


def _interrupt_payload(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or "__interrupt__" not in value:
        return None
    interrupts = value.get("__interrupt__") or ()
    if not interrupts:
        return {}
    interrupt = interrupts[-1]
    payload = getattr(interrupt, "value", interrupt)
    return dict(payload) if isinstance(payload, dict) else {"value": payload}


def _snapshot_values(run_id: str, request: Any | None = None, tools: Any | None = None) -> dict[str, Any]:
    snapshot = get_compiled_research_graph().get_state(_langgraph_config(run_id, request, tools))
    return dict(getattr(snapshot, "values", None) or {})


def pending_langgraph_pause(run_id: str) -> dict[str, Any] | None:
    """Return the current interrupt payload for a paused LangGraph run."""
    ctx = _RUN_CONTEXTS.get(run_id) or _load_run_context(run_id) or {}
    config = _langgraph_config(run_id, ctx.get("request"), ctx.get("tools"), ctx.get("progress"))
    snapshot = get_compiled_research_graph().get_state(config)

    for task in reversed(getattr(snapshot, "tasks", ()) or ()):
        for interrupt in reversed(getattr(task, "interrupts", ()) or ()):
            payload = getattr(interrupt, "value", interrupt)
            if isinstance(payload, dict):
                return {"run_id": run_id, "status": "paused", "pause_contract": payload}

    values = getattr(snapshot, "values", None) or {}
    pause_contract = values.get("pause_contract")
    if pause_contract:
        return {"run_id": run_id, "status": "paused", "pause_contract": pause_contract}
    return None


def _result_from_state(run_id: str, final_state: dict[str, Any]) -> dict[str, Any]:
    answer = final_state.get("answer", "")
    model_used = final_state.get("model_used") or "langgraph"
    latency_ms = final_state.get("latency_ms") or 0
    cost_usd = final_state.get("cost_usd_spent") or 0.0

    judge_result = final_state.get("judge_result")
    if judge_result is None:
        budget_decision = final_state.get("budget_decision")
        pause_contract = final_state.get("pause_contract") or {}
        if budget_decision == BudgetDecision.REQUIRE_HUMAN_APPROVAL:
            reason = pause_contract.get("pause_reason") or "Budget approval is required before synthesis can continue."
        else:
            reason = f"Graph ended without running synthesis (budget_decision={budget_decision!r})."
        logger.warning(
            "langgraph research ended without judge_result: %s (visited_nodes=%s)",
            reason,
            final_state.get("visited_nodes"),
        )
        answer = ""
        judge_result = ResearchJudgeResult(status="fail", score=0.0, issues=[reason], can_publish=False)

    response = model_client.ModelResponse(
        text=answer,
        model_used=model_used,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        model_role="research_synthesis",
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
        "plan": final_state.get("plan")
        or ResearchPlan(source="stub", fallback_reason="LangGraph plan derivation failed."),
        "worker_reports": final_state.get("worker_reports") or [],
        "feedback": feedback,
        "answer_streamed": False,
        "replay_final_answer": repaired,
        "langgraph_run_id": run_id,
        "langgraph_state": final_state,
    }


def run_langgraph_research(request: Any, tools: Any, progress: Any = None) -> dict[str, Any]:
    """Run LangGraph research with a compile-once graph and SQLite checkpoints."""
    run_id = new_id("lgrun")
    _persist_run_context(run_id, request, tools, status="running")
    _RUN_CONTEXTS[run_id] = {"request": request, "tools": tools, "progress": progress}
    config = _langgraph_config(run_id, request, tools, progress)
    graph = get_compiled_research_graph()

    pause_contract: dict[str, Any] | None = None
    for update in graph.stream(_initial_state(run_id, request), config=config):
        pause_contract = _interrupt_payload(update)
        if pause_contract is not None:
            break

    final_state = _snapshot_values(run_id, request, tools)
    if pause_contract is not None:
        final_state["pause_contract"] = pause_contract
        final_state["budget_decision"] = BudgetDecision.REQUIRE_HUMAN_APPROVAL
        final_state["interrupted"] = True
        _mark_run_context(run_id, "paused")
        _RUN_CONTEXTS.pop(run_id, None)
        return _result_from_state(run_id, final_state)

    _RUN_CONTEXTS.pop(run_id, None)
    _mark_run_context(run_id, "completed")
    return _result_from_state(run_id, final_state)


def resume_langgraph_research(
    run_id: str,
    *,
    approved_by: str,
    updated_budget_ceiling_usd: float | None = None,
    progress: Any = None,
) -> dict[str, Any]:
    """Resume a paused LangGraph research run after human budget approval."""
    ctx = _RUN_CONTEXTS.get(run_id) or _load_run_context(run_id) or {}
    if ctx.get("request") is None:
        raise RuntimeError(f"LangGraph run context is missing for run_id={run_id!r}")
    approval: dict[str, Any] = {
        "approved_by": approved_by,
        "approved_at": datetime.utcnow().isoformat() + "Z",
        "approval_audit_event_id": new_id("lgapprove"),
    }
    if updated_budget_ceiling_usd is not None:
        approval["updated_budget_ceiling_usd"] = updated_budget_ceiling_usd

    config = _langgraph_config(run_id, ctx.get("request"), ctx.get("tools"), progress or ctx.get("progress"))
    result = get_compiled_research_graph().invoke(Command(resume=approval), config=config)
    pause_contract = _interrupt_payload(result)
    if pause_contract is not None:
        final_state = _snapshot_values(run_id, ctx.get("request"), ctx.get("tools"))
        final_state["pause_contract"] = pause_contract
        final_state["budget_decision"] = BudgetDecision.REQUIRE_HUMAN_APPROVAL
        final_state["interrupted"] = True
        _mark_run_context(run_id, "paused")
        return _result_from_state(run_id, final_state)

    _RUN_CONTEXTS.pop(run_id, None)
    _mark_run_context(run_id, "completed")
    return _result_from_state(run_id, result)
