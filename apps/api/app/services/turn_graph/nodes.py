from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from app.config import Settings
from app.schemas import ConvChatRequest
from app.services import plan_gate
from app.services.chat_pipeline import (
    _build_doc_context,
    _can_use_trivial_fast_path,
    _has_tool_or_artifact_signal,
    _run_fast_turn_triage,
    _simple_direct_plan_from_triage,
)
from app.services.planner import Plan, passthrough, plan_from_dict, plan_to_dict, run_planner
from app.services.prompts import ARTIFACT_PROMPTS
from app.services.turn_graph.graph import _shadow_guardrail_hook
from app.services.turn_graph.state import TurnGraphState
from app.services.turn_graph.tools import select_tools_from_state


PlannerFn = Callable[..., Plan]
TriageFn = Callable[[ConvChatRequest], dict[str, Any] | None]


def _timed_node(state: TurnGraphState, node: str, fn: Callable[[], None]) -> TurnGraphState:
    started = time.perf_counter()
    state.add_event(node, "started")
    try:
        fn()
        state.add_timing(node, "completed", int((time.perf_counter() - started) * 1000))
        state.add_event(node, "completed")
    except Exception as exc:
        state.add_timing(node, "failed", int((time.perf_counter() - started) * 1000), error=str(exc))
        state.add_event(node, "failed", str(exc))
        raise
    return state


def load_context_node(
    state: TurnGraphState,
    *,
    request: ConvChatRequest,
    history: list[dict[str, Any]] | None = None,
    user_memory: str = "",
    running_summary: str = "",
    active_task: dict[str, Any] | None = None,
) -> TurnGraphState:
    """Populate graph state with context that the current pipeline already builds."""

    def run() -> None:
        state.history = history or state.history
        state.user_memory = user_memory or state.user_memory
        state.running_summary = running_summary or state.running_summary
        state.active_task = active_task if active_task is not None else state.active_task
        state.doc_context = _build_doc_context(request.attached_documents)
        state.artifact_context = ARTIFACT_PROMPTS.get(request.artifact_type or "", "")

    return _timed_node(state, "load_context", run)


def triage_node(
    state: TurnGraphState,
    *,
    request: ConvChatRequest,
    triage_fn: TriageFn = _run_fast_turn_triage,
) -> TurnGraphState:
    """Run the cheap pre-planner triage node.

    This mirrors the live pipeline: deterministic tiny-continuation bypass
    first, then the LLM triage only when no explicit tool/artifact signal is
    present. A full planner remains required for anything non-obvious.
    """

    def run() -> None:
        if _can_use_trivial_fast_path(request, state.history):
            plan = passthrough(request.message)
            plan.turn_type = "follow_up"
            plan.action = "answer_directly"
            plan.task_type = "summarization"
            plan.complexity = "low"
            plan.plan_confidence = "high"
            plan.context_summary = "Fast-path trivial continuation; planner LLM skipped."
            state.triage_decision = {
                "decision": "simple_direct",
                "reason": "trivial_continuation",
                "mode": "deterministic",
            }
            state.plan = plan_to_dict(plan)
            return

        if _has_tool_or_artifact_signal(request):
            state.triage_decision = {
                "decision": "planner_required",
                "reason": "explicit_tool_or_artifact_signal",
                "mode": "deterministic",
            }
            return

        triage = triage_fn(request)
        simple_plan = _simple_direct_plan_from_triage(request, triage)
        if simple_plan is not None:
            state.triage_decision = {
                **(triage or {}),
                "decision": "simple_direct",
                "mode": "llm",
            }
            state.plan = plan_to_dict(simple_plan)
            return

        state.triage_decision = {
            **(triage or {}),
            "decision": "planner_required",
            "mode": "llm" if triage else "fallback",
        }

    return _timed_node(state, "triage", run)


def planner_node(
    state: TurnGraphState,
    *,
    request: ConvChatRequest,
    settings: Settings,
    planner_fn: PlannerFn = run_planner,
) -> TurnGraphState:
    """Run the full planner only if triage did not already produce a plan."""

    def run() -> None:
        if state.plan is not None:
            return
        plan = planner_fn(
            request.message,
            state.history,
            settings.planner_model,
            running_summary=state.running_summary,
            active_task=state.active_task,
            user_memory=state.user_memory,
            doc_context=state.doc_context,
            user_hints={"deep_research": request.deep_research, "document": request.document_requested},
        )
        state.plan = plan_to_dict(plan)

    return _timed_node(state, "planner", run)


def gate_node(
    state: TurnGraphState,
    *,
    request: ConvChatRequest,
) -> TurnGraphState:
    """Evaluate the current plan into auto/confirm capability decisions."""

    def run() -> None:
        if state.plan is None:
            raise ValueError("gate_node requires state.plan")
        plan = plan_from_dict(state.plan, state.user_message)
        gate = plan_gate.evaluate(
            plan,
            explicit_document_request=bool(request.document_requested),
        )
        state.gate = gate.to_dict()
        state.selected_tools = select_tools_from_state(state)
        state.accepted_tools = [
            tool for tool in state.selected_tools
            if not tool.get("requires_confirmation") and state.gate.get("mode") == "auto"
        ]

    return _timed_node(state, "gate", run)


def run_planning_shadow_graph(
    state: TurnGraphState,
    *,
    request: ConvChatRequest,
    settings: Settings,
    history: list[dict[str, Any]] | None = None,
    user_memory: str = "",
    running_summary: str = "",
    active_task: dict[str, Any] | None = None,
    triage_fn: TriageFn = _run_fast_turn_triage,
    planner_fn: PlannerFn = run_planner,
) -> TurnGraphState:
    """Run the first real graph-node sequence in shadow/comparison mode."""

    state.status = "running"
    try:
        load_context_node(
            state,
            request=request,
            history=history,
            user_memory=user_memory,
            running_summary=running_summary,
            active_task=active_task,
        )
        triage_node(state, request=request, triage_fn=triage_fn)
        planner_node(state, request=request, settings=settings, planner_fn=planner_fn)
        gate_node(state, request=request)
        state.status = "completed"
    except Exception as exc:
        state.status = "failed"
        state.error = str(exc)
    _shadow_guardrail_hook(state, settings)
    state.add_event("end", state.status, "Planning shadow graph finished")
    return state
