from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.db.models import AgentGoal, AgentRunLog, AgentStep, GuardrailEvent, SessionLocal
from app.services.agent_runtime import GuardrailContext, GuardrailDecision, GuardrailService, load_default_registry
from app.services.agent_runtime.guardrails import max_boundary_action
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

    settings = settings or get_settings()

    if getattr(settings, "orchestrator_enabled", False):
        state, handled = _orchestrator_node(state, settings)
        if handled:
            _shadow_guardrail_hook(state, settings)
            state.add_event("end", state.status, "Turn graph shell finished (orchestrator)")
            return state

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

    _shadow_guardrail_hook(state, settings)

    state.add_event("end", state.status, "Turn graph shell finished")
    return state


def _orchestrator_node(state: TurnGraphState, settings) -> tuple[TurnGraphState, bool]:
    """Run Phase-D orchestrator/direct-answer nodes. Never raises."""

    from app.services.agent_runtime.direct_answer import DirectAnswerAgent
    from app.services.agent_runtime.orchestrator import OrchestratorAgent

    try:
        registry = load_default_registry()
        orchestrator = OrchestratorAgent(registry)

        state.add_event("orchestrator", "started", "Orchestrator routing")
        started = time.perf_counter()
        decision = orchestrator.route(state)
        state.add_timing("orchestrator", "completed", int((time.perf_counter() - started) * 1000))
        state.add_event(
            "orchestrator",
            "completed",
            decision.route,
            route=decision.route,
            reasoning=decision.reasoning,
        )

        state.triage_decision = {
            "route": decision.route,
            "reasoning": decision.reasoning,
            "model_used": decision.model_used,
            "prompt_id": decision.prompt_id,
            "run_id": decision.run_id,
        }
        state.plan = decision.plan or {}
        state.selected_tools = [{"name": tool} for tool in decision.selected_tools]

        service = GuardrailService(registry)
        plan_context = GuardrailContext(
            boundary="planning",
            user_id=state.user_id or "",
            tenant_id=None,
            tool_name=None,
            tool_input=None,
            tool_output=None,
            request_text=state.user_message,
            plan=state.plan,
            response_text=None,
        )
        plan_decisions = service.evaluate_boundary("planning", plan_context)
        plan_action = max_boundary_action(plan_decisions)
        if plan_action == "block":
            state.final_answer = "I can't complete that request."
            state.status = "completed"
            _write_orchestrator_trace(state, decision, registry, db_session=None)
            return state, True
        if plan_action == "ask_user":
            state.final_answer = (
                plan_decisions[0].reason
                if plan_decisions
                else "I need more information before proceeding."
            )
            state.status = "completed"
            _write_orchestrator_trace(state, decision, registry, db_session=None)
            return state, True

        if decision.route == "clarify":
            state.final_answer = decision.clarification_question or "Could you clarify what you're looking for?"
            state.status = "completed"
            _write_orchestrator_trace(state, decision, registry, db_session=None)
            return state, True

        if decision.route == "direct_answer":
            direct_answer = DirectAnswerAgent(registry)
            da_result = direct_answer.answer(state)
            state.final_answer = da_result.answer
            state.status = "completed"
            state.add_event("direct_answer_agent", "completed", f"Answered in {da_result.latency_ms}ms")
            _write_orchestrator_trace(state, decision, registry, db_session=None, da_result=da_result)
            return state, True

        if decision.route == "research":
            from app.services.agent_runtime.research_agent import ResearchAgent

            research_agent = ResearchAgent(registry)
            ra_result = research_agent.run(state, decision)
            state.research_result = {
                "answer": ra_result.answer,
                "sources": ra_result.sources,
                "model_used": ra_result.model_used,
                "latency_ms": ra_result.latency_ms,
                "cost_usd": ra_result.cost_usd,
            }
            state.final_answer = ra_result.answer
            state.status = "completed"
            state.add_event(
                "research_agent",
                "completed",
                f"Research completed: {len(ra_result.sources)} sources, {ra_result.latency_ms}ms",
            )
            _write_orchestrator_trace(state, decision, registry, db_session=None, ra_result=ra_result)
            return state, True

        if decision.route == "document":
            from app.services.agent_runtime.document_agent import DocumentAgent

            document_agent = DocumentAgent(registry)
            doc_result = document_agent.run(state, decision)
            state.final_answer = doc_result.markdown
            state.document_result = {
                "title": doc_result.title,
                "doc_type": doc_result.doc_type,
                "filename": doc_result.filename,
                "markdown": doc_result.markdown,
                "docx_base64": doc_result.docx_base64,
                "pptx_base64": doc_result.pptx_base64,
                "model_used": doc_result.model_used,
                "latency_ms": doc_result.latency_ms,
                "cost_usd": doc_result.cost_usd,
            }
            state.status = "completed"
            state.add_event(
                "document_agent",
                "completed",
                f"Document generated: {doc_result.title!r} ({doc_result.doc_type})",
            )
            _write_orchestrator_trace(state, decision, registry, db_session=None, doc_result=doc_result)
            return state, True

        state.add_event(
            "orchestrator",
            "deferred",
            f"Route {decision.route} deferred to existing pipeline",
        )
        _write_orchestrator_trace(state, decision, registry, db_session=None)
        return state, False
    except Exception:
        logger.exception("Orchestrator node failed; falling back to existing pipeline")
        return state, False


def _write_orchestrator_trace(
    state: TurnGraphState,
    decision,
    registry,
    *,
    db_session,
    da_result=None,
    ra_result=None,
    doc_result=None,
) -> None:
    """Write Phase-D orchestrator trace rows. Never raises."""

    try:
        # TODO Phase E: replace standalone SessionLocal usage with request/job-scoped DI session.
        db = db_session or SessionLocal()
        should_close = db_session is None
        try:
            goal_id = f"goal_{state.turn_id}"
            existing_goal = db.get(AgentGoal, goal_id)
            if not existing_goal:
                db.add(AgentGoal(
                    id=goal_id,
                    user_id=state.user_id or "",
                    conversation_id=state.conversation_id or "",
                    turn_id=state.turn_id or "",
                    objective=state.user_message,
                    quality_mode=getattr(state, "quality_mode", "standard"),
                    budget_json=json.dumps({}),
                    status="completed" if state.status == "completed" else "running",
                ))

            db.add(AgentRunLog(
                id=decision.run_id,
                goal_id=goal_id,
                agent_id="orchestrator",
                status="completed",
                total_cost_usd=decision.cost_usd,
                latency_ms=decision.latency_ms,
                completed_at=datetime.now(timezone.utc),
            ))
            db.add(AgentStep(
                id=str(uuid.uuid4()),
                run_id=decision.run_id,
                step_type="llm_call",
                input_summary=state.user_message[:200],
                output_summary=decision.route,
                model_used=decision.model_used,
                latency_ms=decision.latency_ms,
                cost_usd=decision.cost_usd,
                metadata_json=json.dumps({"prompt_id": decision.prompt_id, "route": decision.route}),
            ))

            if da_result is not None:
                db.add(AgentRunLog(
                    id=da_result.run_id,
                    goal_id=goal_id,
                    agent_id="direct_answer_agent",
                    parent_run_id=decision.run_id,
                    status="completed",
                    total_cost_usd=da_result.cost_usd,
                    latency_ms=da_result.latency_ms,
                    completed_at=datetime.now(timezone.utc),
                ))
                db.add(AgentStep(
                    id=str(uuid.uuid4()),
                    run_id=da_result.run_id,
                    step_type="llm_call",
                    input_summary=state.user_message[:200],
                    output_summary=(state.final_answer or "")[:200],
                    model_used=da_result.model_used,
                    latency_ms=da_result.latency_ms,
                    cost_usd=da_result.cost_usd,
                    metadata_json=json.dumps({"prompt_id": da_result.prompt_id}),
                ))

            if ra_result is not None:
                db.add(AgentRunLog(
                    id=ra_result.run_id,
                    goal_id=goal_id,
                    agent_id="research_lead",
                    parent_run_id=decision.run_id,
                    status="completed",
                    total_cost_usd=ra_result.cost_usd,
                    latency_ms=ra_result.latency_ms,
                    completed_at=datetime.now(timezone.utc),
                ))
                for tool_call in ra_result.tool_calls:
                    db.add(AgentStep(
                        id=tool_call.step_id,
                        run_id=ra_result.run_id,
                        step_type="tool_call",
                        tool_name=tool_call.tool_name,
                        input_summary=tool_call.input_summary,
                        output_summary=f"{len(tool_call.output.get('sources') or [])} sources",
                        latency_ms=tool_call.latency_ms,
                        metadata_json=json.dumps({"tool_name": tool_call.tool_name}),
                    ))
                db.add(AgentStep(
                    id=str(uuid.uuid4()),
                    run_id=ra_result.run_id,
                    step_type="llm_call",
                    input_summary=state.user_message[:200],
                    output_summary=(state.final_answer or "")[:200],
                    model_used=ra_result.model_used,
                    latency_ms=ra_result.synthesis_latency_ms,
                    cost_usd=ra_result.cost_usd,
                    metadata_json=json.dumps({"prompt_id": ra_result.prompt_id}),
                ))

            if doc_result is not None:
                db.add(AgentRunLog(
                    id=doc_result.run_id,
                    goal_id=goal_id,
                    agent_id="document_lead",
                    parent_run_id=decision.run_id,
                    status="completed",
                    total_cost_usd=doc_result.cost_usd,
                    latency_ms=doc_result.latency_ms,
                    completed_at=datetime.now(timezone.utc),
                ))
                db.add(AgentStep(
                    id=str(uuid.uuid4()),
                    run_id=doc_result.run_id,
                    step_type="llm_call",
                    input_summary=state.user_message[:200],
                    output_summary="document_brief",
                    model_used=doc_result.model_used,
                    latency_ms=doc_result.planning_latency_ms,
                    cost_usd=0.0,
                    metadata_json=json.dumps({
                        "prompt_id": doc_result.prompt_id,
                        "step": "planning",
                    }),
                ))
                db.add(AgentStep(
                    id=str(uuid.uuid4()),
                    run_id=doc_result.run_id,
                    step_type="llm_call",
                    input_summary=state.user_message[:200],
                    output_summary=(doc_result.markdown or "")[:200],
                    model_used=doc_result.model_used,
                    latency_ms=doc_result.content_latency_ms,
                    cost_usd=doc_result.cost_usd,
                    metadata_json=json.dumps({
                        "step": "content_generation",
                        "doc_type": doc_result.doc_type,
                    }),
                ))
                is_presentation = (doc_result.doc_type or "").lower() == "presentation"
                tool_name_for_trace = "render_pptx" if is_presentation else "generate_document"
                db.add(AgentStep(
                    id=str(uuid.uuid4()),
                    run_id=doc_result.run_id,
                    step_type="tool_call",
                    tool_name=tool_name_for_trace,
                    input_summary=doc_result.title[:200],
                    output_summary=doc_result.filename,
                    latency_ms=max(
                        0,
                        doc_result.latency_ms
                        - doc_result.planning_latency_ms
                        - doc_result.content_latency_ms,
                    ),
                    metadata_json=json.dumps({
                        "doc_type": doc_result.doc_type,
                        "filename": doc_result.filename,
                    }),
                ))

            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Failed to write orchestrator trace; ignoring")
        finally:
            if should_close:
                db.close()
    except Exception:
        logger.exception("Failed to open DB session for orchestrator trace; ignoring")


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
            if tool_name in {"web_context", "web_search", "read_url", "generate_document", "render_pptx"}:
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
    if tool_name == "read_url":
        plan = state.plan or {}
        url = plan.get("url") if isinstance(plan, dict) else None
        return {"url": url} if isinstance(url, str) else {}
    if tool_name in {"generate_document", "render_pptx"}:
        plan = state.plan or {}
        document_brief = plan.get("document_brief") if isinstance(plan, dict) else None
        brand_profile = plan.get("brand_profile") if isinstance(plan, dict) else None
        tool_input: dict[str, Any] = {"document_brief": document_brief or {}}
        if isinstance(document_brief, dict) and isinstance(document_brief.get("template_id"), str):
            tool_input["template_id"] = document_brief["template_id"]
        elif isinstance(brand_profile, dict) and isinstance(brand_profile.get("template_id"), str):
            tool_input["template_id"] = brand_profile["template_id"]
        return tool_input
    return {}


def _shadow_tool_output(tool_name: str, state: TurnGraphState) -> dict[str, Any] | None:
    if tool_name in {"web_context", "web_search"} and isinstance(state.web_context, dict):
        return state.web_context
    if tool_name == "read_url" and isinstance(state.web_context, dict):
        return state.web_context
    if tool_name in {"generate_document", "render_pptx"} and isinstance(state.document_result, dict):
        return state.document_result
    return None


def _write_guardrail_events(rows: list[tuple[str, str | None, GuardrailDecision]], state: TurnGraphState) -> None:
    if not rows:
        return
    # TODO Phase E: replace standalone SessionLocal usage with request/job-scoped DI session.
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
