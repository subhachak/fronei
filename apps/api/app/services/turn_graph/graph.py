from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from app.config import get_settings
from app.db.models import GuardrailEvent, SessionLocal
from app.services.agent_runtime import GuardrailContext, GuardrailDecision, GuardrailService, load_default_registry
from app.services.turn_graph.state import TurnGraphState


GraphShellHandler = Callable[[TurnGraphState], TurnGraphState | dict[str, Any] | None]
logger = logging.getLogger(__name__)


def run_turn_graph_shell(
    state: TurnGraphState,
    *,
    existing_pipeline: GraphShellHandler | None = None,
    settings: Any | None = None,
) -> TurnGraphState:
    """Run the first feature-flag-safe turn graph shell.

    This is not the final LangGraph runtime. It is the compatibility layer that
    lets us introduce a canonical state, graph events, and node timings while
    reusing the existing pipeline as a single node. The next phase can replace
    `execute_existing_pipeline` with real LangGraph nodes one at a time.
    """

    state.status = "running"
    state.add_event("start", "started", "Turn graph shell started")

    started = time.perf_counter()
    state.add_event("execute_existing_pipeline", "started", "Delegating to existing pipeline")
    try:
        result = existing_pipeline(state) if existing_pipeline else None
        if isinstance(result, TurnGraphState):
            state = result
        elif isinstance(result, dict):
            for key, value in result.items():
                if hasattr(state, key):
                    setattr(state, key, value)
        state.add_timing(
            "execute_existing_pipeline",
            "completed",
            int((time.perf_counter() - started) * 1000),
        )
        state.add_event("execute_existing_pipeline", "completed", "Existing pipeline completed")
        if state.status == "running":
            state.status = "completed"
    except Exception as exc:
        state.error = str(exc)
        state.status = "failed"
        state.add_timing(
            "execute_existing_pipeline",
            "failed",
            int((time.perf_counter() - started) * 1000),
            error=state.error,
        )
        state.add_event("execute_existing_pipeline", "failed", state.error)

    _shadow_guardrail_hook(state, settings or get_settings())

    state.add_event("end", state.status, "Turn graph shell finished")
    return state


def _shadow_guardrail_hook(state: TurnGraphState, settings) -> None:
    """Fire-and-forget shadow guardrail evaluation. Never raises."""

    if not getattr(settings, "turn_graph_enabled", False):
        return
    try:
        registry = load_default_registry()
        service = GuardrailService(registry)
        event_rows: list[tuple[str, str | None, GuardrailDecision]] = []

        for tool in state.selected_tools:
            tool_name = str(tool.get("name") or "")
            if tool_name in {"web_context", "web_search", "generate_document"}:
                tool_input = _shadow_tool_input(tool_name, state)
                tool_pre_context = GuardrailContext(
                    boundary="tool_pre",
                    user_id=state.user_id or "",
                    tenant_id=None,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    tool_output=None,
                    request_text=None,
                    plan=state.plan,
                    response_text=None,
                )
                event_rows.extend(
                    ("tool_pre", tool_name, decision)
                    for decision in service.evaluate_boundary("tool_pre", tool_pre_context)
                )

                tool_output = _shadow_tool_output(tool_name, state)
                if tool_output is not None:
                    tool_post_context = GuardrailContext(
                        boundary="tool_post",
                        user_id=state.user_id or "",
                        tenant_id=None,
                        tool_name=tool_name,
                        tool_input=None,
                        tool_output=tool_output,
                        request_text=None,
                        plan=state.plan,
                        response_text=None,
                    )
                    event_rows.extend(
                        ("tool_post", tool_name, decision)
                        for decision in service.evaluate_boundary("tool_post", tool_post_context)
                    )

        output_context = GuardrailContext(
            boundary="output",
            user_id=state.user_id or "",
            tenant_id=None,
            tool_name=None,
            tool_input=None,
            tool_output=None,
            request_text=state.user_message,
            plan=state.plan,
            response_text=state.final_answer,
        )
        event_rows.extend(
            ("output", None, decision)
            for decision in service.evaluate_boundary("output", output_context)
        )
        _write_guardrail_events(event_rows, state)
    except Exception:
        logger.exception("shadow guardrail hook failed; ignoring")


def _shadow_tool_input(tool_name: str, state: TurnGraphState) -> dict[str, Any]:
    if tool_name in {"web_context", "web_search"}:
        return {"query": state.user_message, "max_results": 5}
    if tool_name == "generate_document":
        plan = state.plan or {}
        document_brief = plan.get("document_brief") if isinstance(plan, dict) else None
        tool_input: dict[str, Any] = {"document_brief": document_brief or {}}
        if isinstance(document_brief, dict) and isinstance(document_brief.get("template_id"), str):
            tool_input["template_id"] = document_brief["template_id"]
        return tool_input
    return {}


def _shadow_tool_output(tool_name: str, state: TurnGraphState) -> dict[str, Any] | None:
    if tool_name in {"web_context", "web_search"} and isinstance(state.web_context, dict):
        return state.web_context
    if tool_name == "generate_document" and isinstance(state.document_result, dict):
        return state.document_result
    return None


def _write_guardrail_events(rows: list[tuple[str, str | None, GuardrailDecision]], state: TurnGraphState) -> None:
    if not rows:
        return
    db = SessionLocal()
    try:
        for boundary, tool_name, decision in rows:
            db.add(GuardrailEvent(
                id=str(uuid.uuid4()),
                policy_id=decision.policy_id,
                boundary=boundary,
                action=decision.action,
                triggered_checks_json=json.dumps(decision.triggered_checks),
                reason=decision.reason,
                user_id=state.user_id,
                tenant_id=None,
                tool_name=tool_name,
                turn_id=state.turn_id,
                conversation_id=state.conversation_id,
            ))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("failed to write guardrail events; ignoring")
    finally:
        db.close()
