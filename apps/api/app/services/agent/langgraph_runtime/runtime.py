from __future__ import annotations

import json
import logging
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any

from langgraph.types import Command
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.db.models import LangGraphRunContext, SessionLocal
from app.services.agent import model_client
from app.services.agent.grounding import log_context_entry_state
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


class LangGraphResumeConflict(RuntimeError):
    """Raised when resume_langgraph_research is called for a run_id that is
    not currently paused (already resumed/resuming, or never paused).

    This guards against double-spend: two concurrent approve calls for the
    same run_id (double-click, or two admins racing) must not both invoke
    the compiled graph against the same checkpoint, which would re-run
    synthesize/repair LLM calls (and any remaining tool calls) twice for one
    approval. See resume_langgraph_research's atomic conditional UPDATE.
    """

# In-process convenience cache for pause/resume. Checkpoints are persisted in
# SQLite; this only preserves richer Python objects such as injected test tools.
_RUN_CONTEXTS: dict[str, dict[str, Any]] = {}


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
        return {"source": "default_settings"}
    default_tools = Tools.from_settings()
    current = asdict(tools) if is_dataclass(tools) else {
        "you_api_key": getattr(tools, "you_api_key", None),
        "tavily_api_key": getattr(tools, "tavily_api_key", None),
        "nimble_api_key": getattr(tools, "nimble_api_key", None),
        "nimble_api_endpoint": getattr(tools, "nimble_api_endpoint", Tools().nimble_api_endpoint),
    }
    defaults = asdict(default_tools)
    if current == defaults:
        return {"source": "default_settings"}
    providers = [
        provider
        for provider, present in (
            ("you", bool(current.get("you_api_key"))),
            ("tavily", bool(current.get("tavily_api_key"))),
            ("nimble", bool(current.get("nimble_api_key"))),
        )
        if present
    ]
    return {
        "source": "custom_tools",
        "providers": providers,
        "nimble_api_endpoint": current.get("nimble_api_endpoint") or Tools().nimble_api_endpoint,
    }


def _tools_from_config(payload: dict[str, Any] | None) -> Tools:
    payload = payload or {}
    if payload.get("source") in {None, "default_settings", "custom_tools"}:
        return Tools.from_settings()
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


def _run_context_status(run_id: str) -> str | None:
    db = SessionLocal()
    try:
        row = db.get(LangGraphRunContext, run_id)
        return row.status if row is not None else None
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


def _complete_run(run_id: str) -> None:
    _mark_run_context(run_id, "completed")


def _fail_run(run_id: str) -> None:
    _mark_run_context(run_id, "failed")


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
                _mark_run_context(run_id, "paused")
                return {"run_id": run_id, "status": "paused", "pause_contract": payload}

    values = getattr(snapshot, "values", None) or {}
    pause_contract = values.get("pause_contract")
    if pause_contract:
        _mark_run_context(run_id, "paused")
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
        "orchestrator": "langgraph",
        "langgraph_run_id": run_id,
        "langgraph_state": final_state,
    }


def _summarize_node_delta(node_name: str, delta: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {"node_name": node_name}
    for key in (
        "message",
        "cost_usd_spent",
        "tool_calls_made",
        "model_calls_made",
        "model_used",
        "latency_ms",
        "budget_decision",
        "next_action",
    ):
        if key in delta:
            value = delta[key]
            payload[key] = value.value if hasattr(value, "value") else value
    for key in ("sources", "tool_calls", "worker_reports"):
        value = delta.get(key)
        if isinstance(value, list):
            payload[f"{key}_count"] = len(value)
    evidence = delta.get("evidence")
    if evidence is not None:
        payload["evidence_item_count"] = len(getattr(evidence, "items", []) or [])
    visited = delta.get("visited_nodes")
    if isinstance(visited, list):
        payload["visited_count"] = len(visited)
    return payload


def stream_langgraph_research(request: Any, tools: Any, progress: Any = None):
    """Streaming LangGraph research runner.

    Yields ("node", payload) for completed graph nodes and
    ("delta", {"text": text, "source_node": source_node}) for answer text
    emitted via LangGraph custom stream writer. Returns the same final result
    dict as run_langgraph_research via StopIteration.value.
    """
    run_id = new_id("lgrun")
    log_context_entry_state(
        logger,
        request=request if isinstance(request, TurnRequest) else None,
        entry_point="stream_langgraph_research",
        run_id=run_id,
    )
    _persist_run_context(run_id, request, tools, status="running")
    _RUN_CONTEXTS[run_id] = {"request": request, "tools": tools, "progress": progress}
    # Progress callbacks are bridged from graph updates below; passing None into
    # nodes avoids double-recording the same progress event.
    config = _langgraph_config(run_id, request, tools, None)
    graph = get_compiled_research_graph()

    pause_contract: dict[str, Any] | None = None
    try:
        for mode, payload in graph.stream(
            _initial_state(run_id, request),
            config=config,
            stream_mode=["updates", "custom"],
        ):
            if mode == "custom":
                if isinstance(payload, dict) and payload.get("answer_delta"):
                    source_node = str(payload.get("source_node") or "")
                    yield ("delta", {"text": str(payload["answer_delta"]), "source_node": source_node})
                continue
            pause_contract = _interrupt_payload(payload)
            if pause_contract is not None:
                break
            if not isinstance(payload, dict) or not payload:
                continue
            node_name, delta = next(iter(payload.items()))
            if isinstance(delta, dict):
                yield ("node", _summarize_node_delta(str(node_name), delta))
    except BaseException:
        _RUN_CONTEXTS.pop(run_id, None)
        _fail_run(run_id)
        raise

    final_state = _snapshot_values(run_id, request, tools)
    if pause_contract is not None:
        final_state["pause_contract"] = pause_contract
        final_state["budget_decision"] = BudgetDecision.REQUIRE_HUMAN_APPROVAL
        final_state["interrupted"] = True
        _mark_run_context(run_id, "paused")
        _RUN_CONTEXTS.pop(run_id, None)
        return _result_from_state(run_id, final_state)

    _RUN_CONTEXTS.pop(run_id, None)
    _complete_run(run_id)
    return _result_from_state(run_id, final_state)


def run_langgraph_research(request: Any, tools: Any, progress: Any = None) -> dict[str, Any]:
    """Blocking wrapper over stream_langgraph_research."""
    log_context_entry_state(
        logger,
        request=request if isinstance(request, TurnRequest) else None,
        entry_point="run_langgraph_research",
    )
    gen = stream_langgraph_research(request, tools, progress)
    try:
        while True:
            kind, payload = next(gen)
            if kind == "node" and progress:
                node_name = str(payload.get("node_name") or "")
                data = {k: v for k, v in payload.items() if k != "node_name"}
                progress(node_name, data.pop("message", node_name), **data)
    except StopIteration as stop:
        return stop.value


def _claim_run_for_resume(run_id: str, *, resumed_by: str) -> None:
    """Atomically transition a run_id from status='paused' to 'resuming'.

    This is a check-and-set, not a read-then-write: the UPDATE's WHERE
    clause encodes the precondition (status='paused') and we inspect the
    actual rowcount the database reports, so a concurrent duplicate call
    (double-click, two admins racing) can affect at most one of the two
    calls. The loser raises LangGraphResumeConflict instead of proceeding
    to invoke the graph, which would otherwise double-run the remaining
    synthesize/repair LLM calls (and tool calls) against the same paused
    checkpoint and double real spend.
    """
    def is_sqlite_lock_error(exc: OperationalError) -> bool:
        text = str(exc).lower()
        return "database is locked" in text or "database table is locked" in text or "database is busy" in text

    def try_claim() -> bool:
        now = datetime.now(timezone.utc)
        db = SessionLocal()
        try:
            updated = (
                db.query(LangGraphRunContext)
                .filter(
                    LangGraphRunContext.run_id == run_id,
                    LangGraphRunContext.status == "paused",
                )
                .update(
                    {
                        LangGraphRunContext.status: "resuming",
                        LangGraphRunContext.resumed_at: now,
                        LangGraphRunContext.resumed_by: resumed_by,
                        LangGraphRunContext.updated_at: now,
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            return updated == 1
        except OperationalError as exc:
            db.rollback()
            if is_sqlite_lock_error(exc):
                raise LangGraphResumeConflict(
                    f"LangGraph run_id={run_id!r} is already being resumed; refusing to resume it again."
                ) from exc
            raise
        finally:
            db.close()

    if try_claim():
        return

    # The checkpoint is the source of truth for pause state. If the DB status
    # drifted while the run is still non-terminal, pending_langgraph_pause()
    # will observe the interrupt and mark the context paused, then this one
    # retry can claim it atomically. Terminal rows must never be resurrected by
    # a stale pause_contract value left in the checkpoint state.
    status = _run_context_status(run_id)
    if status not in {None, "completed", "failed", "orphaned"} and pending_langgraph_pause(run_id) is not None and try_claim():
        return

    raise LangGraphResumeConflict(
        f"LangGraph run_id={run_id!r} is not awaiting approval (already resumed, "
        "completed, or never paused) — refusing to resume it again."
    )


def resume_langgraph_research(
    run_id: str,
    *,
    approved_by: str,
    updated_budget_ceiling_usd: float | None = None,
    progress: Any = None,
) -> dict[str, Any]:
    """Resume a paused LangGraph research run after human budget approval.

    Raises LangGraphResumeConflict if run_id is not currently paused —
    callers (e.g. the /approve endpoint) should translate that into a 409,
    not silently no-op or proceed.
    """
    _claim_run_for_resume(run_id, resumed_by=approved_by)

    ctx = _RUN_CONTEXTS.get(run_id) or _load_run_context(run_id) or {}
    if ctx.get("request") is None:
        _mark_run_context(run_id, "paused")
        raise RuntimeError(f"LangGraph run context is missing for run_id={run_id!r}")
    log_context_entry_state(
        logger,
        request=ctx.get("request") if isinstance(ctx.get("request"), TurnRequest) else None,
        entry_point="resume_langgraph_research",
        run_id=run_id,
        approved_by=approved_by,
    )

    approval: dict[str, Any] = {
        "approved_by": approved_by,
        "approved_at": datetime.utcnow().isoformat() + "Z",
        "approval_audit_event_id": new_id("lgapprove"),
    }
    if updated_budget_ceiling_usd is not None:
        approval["updated_budget_ceiling_usd"] = updated_budget_ceiling_usd

    config = _langgraph_config(run_id, ctx.get("request"), ctx.get("tools"), progress or ctx.get("progress"))
    try:
        result = get_compiled_research_graph().invoke(Command(resume=approval), config=config)
    except BaseException:
        _mark_run_context(run_id, "paused")
        raise
    pause_contract = _interrupt_payload(result)
    if pause_contract is not None:
        final_state = _snapshot_values(run_id, ctx.get("request"), ctx.get("tools"))
        final_state["pause_contract"] = pause_contract
        final_state["budget_decision"] = BudgetDecision.REQUIRE_HUMAN_APPROVAL
        final_state["interrupted"] = True
        _mark_run_context(run_id, "paused")
        return _result_from_state(run_id, final_state)

    _RUN_CONTEXTS.pop(run_id, None)
    _complete_run(run_id)
    return _result_from_state(run_id, result)


def stream_resume_langgraph_research(
    run_id: str,
    *,
    approved_by: str,
    updated_budget_ceiling_usd: float | None = None,
    progress: Any = None,
    already_claimed: bool = False,
):
    """Streaming twin of resume_langgraph_research."""
    if not already_claimed:
        _claim_run_for_resume(run_id, resumed_by=approved_by)

    ctx = _RUN_CONTEXTS.get(run_id) or _load_run_context(run_id) or {}
    if ctx.get("request") is None:
        _mark_run_context(run_id, "paused")
        raise RuntimeError(f"LangGraph run context is missing for run_id={run_id!r}")
    log_context_entry_state(
        logger,
        request=ctx.get("request") if isinstance(ctx.get("request"), TurnRequest) else None,
        entry_point="stream_resume_langgraph_research",
        run_id=run_id,
        approved_by=approved_by,
    )

    approval: dict[str, Any] = {
        "approved_by": approved_by,
        "approved_at": datetime.utcnow().isoformat() + "Z",
        "approval_audit_event_id": new_id("lgapprove"),
    }
    if updated_budget_ceiling_usd is not None:
        approval["updated_budget_ceiling_usd"] = updated_budget_ceiling_usd

    config = _langgraph_config(run_id, ctx.get("request"), ctx.get("tools"), None)
    graph = get_compiled_research_graph()

    pause_contract: dict[str, Any] | None = None
    try:
        for mode, payload in graph.stream(
            Command(resume=approval),
            config=config,
            stream_mode=["updates", "custom"],
        ):
            if mode == "custom":
                if isinstance(payload, dict) and payload.get("answer_delta"):
                    source_node = str(payload.get("source_node") or "")
                    yield ("delta", {"text": str(payload["answer_delta"]), "source_node": source_node})
                continue
            pause_contract = _interrupt_payload(payload)
            if pause_contract is not None:
                break
            if not isinstance(payload, dict) or not payload:
                continue
            node_name, delta = next(iter(payload.items()))
            if isinstance(delta, dict):
                yield ("node", _summarize_node_delta(str(node_name), delta))
    except BaseException:
        _mark_run_context(run_id, "paused")
        raise

    final_state = _snapshot_values(run_id, ctx.get("request"), ctx.get("tools"))
    if pause_contract is not None:
        final_state["pause_contract"] = pause_contract
        final_state["budget_decision"] = BudgetDecision.REQUIRE_HUMAN_APPROVAL
        final_state["interrupted"] = True
        _mark_run_context(run_id, "paused")
        return _result_from_state(run_id, final_state)

    _RUN_CONTEXTS.pop(run_id, None)
    _complete_run(run_id)
    return _result_from_state(run_id, final_state)


def claim_langgraph_run_for_resume(run_id: str, *, resumed_by: str) -> None:
    _claim_run_for_resume(run_id, resumed_by=resumed_by)
