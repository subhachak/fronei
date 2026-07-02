from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)

LANGGRAPH_STREAM_HEARTBEAT_SECONDS = 2.5
LANGGRAPH_STREAM_QUEUE_POLL_SECONDS = 0.25

from app.services.agent import model_client
from app.services.agent.deck_subtree import plan_deck
from app.services.agent.document_subtree import (
    build_artifact,
    choose_artifact_tool,
    judge_document,
    plan_document,
    write_document,
)
from app.services.agent.fast_path import (
    DIRECT_FAST_PROMPT,
    WEB_FAST_PROMPT,
    decide_fast_path,
)
from app.services.agent.models import (
    Goal,
    ProgressEvent,
    Source,
    StreamEnvelope,
    TurnRequest,
    TurnResult,
    new_id,
)
from app.services.agent.orchestrator import OrchestratorDecision, decide_with_options
from app.services.agent.research_models import _looks_like_low_value_extraction
from app.services.agent.research_subtree import (
    EvidencePack,
    build_research_plan_preview,
)
from app.services.agent.tool_registry import ToolRegistry
from app.services.agent.tools import Tools, source_context

_LANGGRAPH_NODE_MESSAGES: dict[str, str] = {
    "brief": "Understanding what you're asking...",
    "subject_derivation": "Identifying what to compare...",
    "contract": "Mapping out what needs to be covered...",
    "plan": "Planning the research approach...",
    "dispatch_search": "Starting the searches...",
    "search_worker": "Searching the web...",
    "rank": "Ranking the best sources...",
    "read": "Reading the source pages...",
    "classify_claims": "Reviewing claims for accuracy...",
    "expand_source_graph": "Following up on related links...",
    "bind": "Pulling the evidence together...",
    "budget_gate_pre_synthesis": "Checking the research budget...",
    "budget_gate_pre_repair": "Checking the research budget...",
    "synthesize": "Writing the answer...",
    "verify": "Double-checking citations...",
    "judge": "Reviewing answer quality...",
    "repair": "Improving the answer...",
}


class Runtime:
    """Canonical Fronei turn runtime."""

    def __init__(self, tools: Tools | None = None):
        self.tool_registry = ToolRegistry(tools or Tools.from_settings())

    def run_stream(self, request: TurnRequest, *, user_id: str, turn_id: str | None = None) -> Iterator[StreamEnvelope]:
        turn_id = turn_id or new_id("turn")
        started = time.perf_counter()
        available_routes = ["direct", "clarify", "research", "document", "research_document"]
        available_tools = [tool["name"] for tool in self.tool_registry.describe()]

        fast_decision = decide_fast_path(request)
        if fast_decision.path in {"direct_fast", "web_fast"}:
            goal_route = "research" if fast_decision.path == "web_fast" else "direct"
            goal = Goal(
                user_id=user_id,
                conversation_id=request.conversation_id,
                objective=request.message,
                route=goal_route,
                quality_mode=request.quality_mode,
            )
            events: list[ProgressEvent] = []

            yield StreamEnvelope(type="start", data={"turn_id": turn_id, "goal": goal.model_dump(mode="json")})

            def progress(stage: str, message: str, **data) -> ProgressEvent:
                event = ProgressEvent(turn_id=turn_id, stage=stage, message=message, data=data)
                events.append(event)
                return event

            first = progress(
                "fast_router",
                (
                    "I can answer this directly."
                    if fast_decision.path == "direct_fast"
                    else "I'm checking the web quickly before answering."
                ),
                path=fast_decision.path,
                confidence=fast_decision.confidence,
                reason=fast_decision.reason,
                source=fast_decision.source,
                web_query=fast_decision.web_query,
                model_used=fast_decision.model_used,
                fallback_reason=fast_decision.fallback_reason,
                matched_signal_groups=fast_decision.matched_signal_groups,
                matched_signals=fast_decision.matched_signals,
                adaptive_policy_version="bootstrap_v1",
                **model_client.telemetry_for_role(
                    "fast_router",
                    quality_mode=request.quality_mode,
                    model_used=fast_decision.model_used,
                    overrides=request.model_overrides,
                ),
            )
            yield StreamEnvelope(type="progress", data=first.model_dump(mode="json"))

            if fast_decision.path == "direct_fast":
                event = progress("direct_fast_answer", "Answering from the current conversation context.")
                yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
                user_prompt = request.message
                if request.conversation_context:
                    user_prompt = f"{request.conversation_context}\n\nCurrent user request:\n{request.message}"
                response = yield from self._stream_model_response(
                    progress,
                    [
                        {"role": "system", "content": DIRECT_FAST_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    role="direct_answer",
                    quality_mode=request.quality_mode,
                    overrides=request.model_overrides,
                    max_tokens=1600,
                    timeout_s=14,
                )
                event = progress(
                    "direct_fast_result",
                    f"Direct answer used {response.model_used or 'the configured direct model'}.",
                    latency_ms=response.latency_ms,
                    cost_usd=response.cost_usd,
                    **model_client.telemetry_for_response(response, overrides=request.model_overrides),
                )
                yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
                result = TurnResult(
                    turn_id=turn_id,
                    goal=goal,
                    answer=response.text,
                    route=goal.route,
                    model_used=response.model_used,
                    tool_calls=[],
                    sources=[],
                    events=events,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    cost_usd=response.cost_usd + fast_decision.cost_usd,
                )
                yield StreamEnvelope(type="result", data=result.model_dump(mode="json"))
                yield StreamEnvelope(type="done", data={"turn_id": turn_id, "latency_ms": result.latency_ms})
                return

            web_query = fast_decision.web_query or request.message
            sources, search_call = yield from self._run_tool(
                progress,
                "web_search",
                {"query": web_query, "max_results": 3},
            )
            public_urls = [source.url for source in sources[:2] if source.url]
            extracted_sources: list[Source] = []
            read_call = None
            if public_urls:
                extracted_sources, read_call = yield from self._run_tool(
                    progress,
                    "read_url",
                    {"urls": public_urls, "max_chars_per_source": 1800},
                )
            event = progress(
                "web_fast_answer",
                "Answering from the quick web check.",
                source_count=len(sources),
                read_count=len(extracted_sources),
            )
            yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
            merged_web_sources = self._merge_sources(sources, extracted_sources)
            response = yield from self._stream_model_response(
                progress,
                [
                    {"role": "system", "content": WEB_FAST_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "message": request.message,
                                "web_query": web_query,
                                "source_context": source_context(merged_web_sources[:3]),
                                "conversation_context": request.conversation_context[-1800:] if request.conversation_context else "",
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                role="direct_answer",
                quality_mode=request.quality_mode,
                overrides=request.model_overrides,
                max_tokens=1000,
                timeout_s=16,
            )
            event = progress(
                "web_fast_result",
                f"Quick web answer used {response.model_used or 'the configured direct model'}.",
                latency_ms=response.latency_ms,
                cost_usd=response.cost_usd,
                **model_client.telemetry_for_response(response, overrides=request.model_overrides),
            )
            yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
            tool_calls = [search_call, *([read_call] if read_call is not None else [])]
            result = TurnResult(
                turn_id=turn_id,
                goal=goal,
                answer=response.text,
                route=goal.route,
                model_used=response.model_used,
                sources=merged_web_sources,
                tool_calls=tool_calls,
                events=events,
                latency_ms=int((time.perf_counter() - started) * 1000),
                cost_usd=response.cost_usd + fast_decision.cost_usd,
            )
            yield StreamEnvelope(type="result", data=result.model_dump(mode="json"))
            yield StreamEnvelope(type="done", data={"turn_id": turn_id, "latency_ms": result.latency_ms})
            return

        decision = decide_with_options(request, available_routes=available_routes, available_tools=available_tools)
        request = self._apply_decision(request, decision)
        route = decision.route
        goal = Goal(
            user_id=user_id,
            conversation_id=request.conversation_id,
            objective=request.message,
            route=route,
            quality_mode=request.quality_mode,
        )
        events: list[ProgressEvent] = []

        yield StreamEnvelope(type="start", data={"turn_id": turn_id, "goal": goal.model_dump(mode="json")})

        def progress(stage: str, message: str, **data) -> ProgressEvent:
            event = ProgressEvent(turn_id=turn_id, stage=stage, message=message, data=data)
            events.append(event)
            return event

        first = progress(
            "orchestrator",
            f"Fronei selected the {route} route.",
            route=route,
            research_level=decision.research_level if route in {"research", "research_document"} else None,
            requires_confirmation=decision.requires_confirmation,
            confidence=decision.confidence,
            reason=decision.reason,
            source=decision.source,
            model_used=decision.model_used,
            **model_client.telemetry_for_role(
                "orchestrator",
                quality_mode=request.quality_mode,
                model_used=decision.model_used,
                overrides=request.model_overrides,
            ),
            available_routes=decision.available_routes,
            available_tools=decision.available_tools,
            fallback_reason=decision.fallback_reason,
            route_tools=self.tool_registry.tool_names_for_route(route),
        )
        yield StreamEnvelope(type="progress", data=first.model_dump(mode="json"))

        try:
            if route in {"research", "research_document"} and decision.requires_confirmation and not request.confirm_deep_research:
                result, preview_event = self._run_deep_research_confirmation(request, goal, turn_id, events, decision, progress)
                yield StreamEnvelope(type="progress", data=preview_event.model_dump(mode="json"))
                result.latency_ms = int((time.perf_counter() - started) * 1000)
                result.events = events
                yield StreamEnvelope(type="result", data=result.model_dump(mode="json"))
                yield StreamEnvelope(type="done", data={"turn_id": turn_id, "latency_ms": result.latency_ms})
                return
            if route == "clarify":
                result = self._run_clarify(request, goal, turn_id, events, decision)
            elif route == "direct":
                event = progress("direct_answer", "Drafting a direct response.")
                yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
                result = yield from self._run_direct(request, goal, turn_id, events, progress)
            elif route == "research":
                research = yield from self._run_research_subtree(request, progress)
                response = research["response"]
                langgraph_state = research.get("langgraph_state") or {}
                is_paused = bool(langgraph_state.get("interrupted"))
                is_langgraph_streamed = (
                    research.get("orchestrator") == "langgraph"
                    and research.get("answer_streamed")
                )
                if request.research_level == "deep" and (
                    not research.get("answer_streamed")
                    or (research.get("replay_final_answer") and not is_langgraph_streamed)
                ) and not is_paused:
                    yield from self._emit_buffered_answer(response, progress)
                result = self._langgraph_research_turn_result(
                    turn_id=turn_id,
                    goal=goal,
                    research=research,
                    events=events,
                )
            elif route == "document":
                event = progress("document", "Composing a standalone document artifact.")
                yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
                result = yield from self._run_document(request, goal, turn_id, events, progress, sources=[])
            elif route == "research_document":
                research = yield from self._run_research_subtree(request, progress)
                event = progress("document", "Writing the downloadable document.")
                yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
                result = yield from self._run_document(
                    request,
                    goal,
                    turn_id,
                    events,
                    progress,
                    sources=research["sources"],
                    research_answer=research["response"].text,
                    evidence=research["evidence"],
                )
                result.tool_calls = [*research["tool_calls"], *result.tool_calls]
            else:
                result = self._run_clarify(
                    request,
                    goal,
                    turn_id,
                    events,
                    OrchestratorDecision(
                        route="clarify",
                        reason="Unknown route selected.",
                        clarification_question="Can you clarify what you want me to do?",
                    ),
                )
            result.latency_ms = int((time.perf_counter() - started) * 1000)
            result.events = events
            yield StreamEnvelope(type="result", data=result.model_dump(mode="json"))
            yield StreamEnvelope(type="done", data={"turn_id": turn_id, "latency_ms": result.latency_ms})
        except Exception as exc:
            yield StreamEnvelope(
                type="error",
                data={
                    "turn_id": turn_id,
                    "message": "I couldn't complete this request. Please try again.",
                    "detail": str(exc),
                },
            )
            yield StreamEnvelope(type="done", data={"turn_id": turn_id, "failed": True})

    def _run_tool(self, progress, name: str, inputs: dict):
        selected = progress(
            "tool_selection",
            f"Selected tool {name}.",
            tool_name=name,
            tool_input=inputs,
            available_tools=[tool["name"] for tool in self.tool_registry.describe()],
        )
        yield StreamEnvelope(type="progress", data=selected.model_dump(mode="json"))
        output, call = self.tool_registry.run(name, inputs)
        result = progress(
            "tool_result",
            f"Tool {name} {'completed' if call.ok else 'failed'}.",
            tool_name=name,
            ok=call.ok,
            error=call.error,
            latency_ms=call.latency_ms,
            output_summary=call.output,
        )
        yield StreamEnvelope(type="progress", data=result.model_dump(mode="json"))
        return output or [], call

    def _stream_model_response(
        self,
        progress,
        messages: list[dict[str, str]],
        *,
        role: str,
        quality_mode: str,
        overrides: dict[str, str] | None,
        max_tokens: int,
        timeout_s: int,
    ):
        buffered = ""
        response: model_client.ModelResponse | None = None
        for item in model_client.stream_complete(
            messages,
            role=role,
            quality_mode=quality_mode,
            overrides=overrides,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
        ):
            if isinstance(item, model_client.ModelDelta):
                if not item.text:
                    continue
                buffered += item.text
                event = progress(
                    "answer_delta",
                    "Streaming answer.",
                    delta=item.text,
                    char_count=len(buffered),
                    ephemeral_ui=True,
                )
                yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
            else:
                response = item
        if response is None:
            response = model_client.ModelResponse(
                text=buffered.strip(),
                model_used="",
                latency_ms=0,
                cost_usd=0.0,
                model_role=role,
            )
        event = progress(
            "answer_complete",
            "Answer stream complete.",
            char_count=len(response.text),
            model_used=response.model_used,
            latency_ms=response.latency_ms,
            cost_usd=response.cost_usd,
            ephemeral_ui=True,
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        return response

    # Tuning for _emit_buffered_answer's replay pacing — see its docstring.
    _BUFFERED_REPLAY_CHUNK_CHARS = 120
    _BUFFERED_REPLAY_CHUNK_DELAY_S = 0.03
    _BUFFERED_REPLAY_MAX_TOTAL_DELAY_S = 6.0

    def _emit_buffered_answer(self, response: model_client.ModelResponse, progress) -> Iterator[StreamEnvelope]:
        """Emit answer stream events for non-streaming synthesis paths.

        Deep lead-loop research uses internally retriable complete() calls for synthesis and
        repair, so there is no token stream to forward live. Replay the final answer in
        chunks before the result event so clients that render answer_delta/answer_complete
        do not appear to stall at the research ledger.

        The client (useTurnRunner.ts) buffers incoming deltas and drains them on its own
        steady-cadence timer to produce a typing animation — but it flushes that buffer
        immediately when answer_complete arrives. Since the full answer text already
        exists here (this is a non-streaming synthesis path), emitting every chunk plus
        answer_complete back-to-back with no delay means the whole burst lands within the
        same tick on the client and the animation never gets a chance to run before the
        flush empties it. A small per-chunk sleep gives genuine wall-clock spacing between
        deltas, matching what the live-streaming legacy path produces naturally. Total
        added delay is capped so very long reports don't make users wait artificially long.
        """
        text = response.text or ""
        if not text:
            return
        chunk_chars = self._BUFFERED_REPLAY_CHUNK_CHARS
        max_chunks = max(1, len(text) // chunk_chars + 1)
        delay_s = min(
            self._BUFFERED_REPLAY_CHUNK_DELAY_S,
            self._BUFFERED_REPLAY_MAX_TOTAL_DELAY_S / max_chunks,
        )
        char_count = 0
        for start in range(0, len(text), chunk_chars):
            delta = text[start : start + chunk_chars]
            char_count += len(delta)
            event = progress(
                "answer_delta",
                "Streaming answer.",
                delta=delta,
                char_count=char_count,
                ephemeral_ui=True,
            )
            yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
            if delay_s > 0:
                time.sleep(delay_s)
        event = progress(
            "answer_complete",
            "Answer stream complete.",
            char_count=len(text),
            model_used=response.model_used,
            latency_ms=response.latency_ms,
            cost_usd=response.cost_usd,
            ephemeral_ui=True,
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))

    def _forward_langgraph_stream(self, gen, progress):
        buffered_answer = ""
        last_source_node: str | None = None
        result = None
        try:
            while True:
                next_result = yield from self._next_langgraph_stream_item(gen, progress)
                if next_result["status"] == "stop":
                    result = next_result["value"]
                    break
                kind, payload = next_result["value"]
                if kind == "delta":
                    delta = payload.get("text", "") if isinstance(payload, dict) else str(payload)
                    source_node = payload.get("source_node", "") if isinstance(payload, dict) else ""
                    if last_source_node is not None and source_node and source_node != last_source_node and delta:
                        buffered_answer = ""
                        message = _LANGGRAPH_NODE_MESSAGES.get(
                            source_node,
                            f"{source_node.replace('_', ' ').capitalize()}...",
                        )
                        reset_event = progress(source_node, message, reset=True, ephemeral_ui=True)
                        yield StreamEnvelope(type="progress", data=reset_event.model_dump(mode="json"))
                    last_source_node = source_node
                    buffered_answer += delta
                    event = progress(
                        "answer_delta",
                        "Streaming answer.",
                        delta=delta,
                        char_count=len(buffered_answer),
                        source_node=source_node,
                        ephemeral_ui=True,
                    )
                    yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
                elif kind == "node":
                    node_payload = dict(payload)
                    node_name = str(node_payload.pop("node_name", "") or "")
                    message = (
                        _LANGGRAPH_NODE_MESSAGES.get(node_name)
                        or node_payload.pop("message", None)
                        or f"{node_name.replace('_', ' ').capitalize()}..."
                    )
                    event = progress(node_name, message, **node_payload)
                    yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        except StopIteration as stop:
            result = stop.value
        if buffered_answer and result is not None:
            result["answer_streamed"] = True
            if not result.get("replay_final_answer"):
                result["replay_final_answer"] = False
            response = result.get("response")
            event = progress(
                "answer_complete",
                "Answer stream complete.",
                char_count=len(buffered_answer),
                model_used=getattr(response, "model_used", ""),
                latency_ms=getattr(response, "latency_ms", 0),
                cost_usd=getattr(response, "cost_usd", 0.0),
                ephemeral_ui=True,
            )
            yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        return result

    def _next_langgraph_stream_item(self, gen, progress):
        out: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

        def advance() -> None:
            try:
                out.put(("item", next(gen)))
            except StopIteration as stop:
                out.put(("stop", stop.value))
            except BaseException as exc:
                out.put(("error", exc))

        worker = threading.Thread(target=advance, daemon=True, name="langgraph-stream-next")
        worker.start()
        started = time.monotonic()
        last_heartbeat = started

        while True:
            try:
                status, value = out.get(timeout=LANGGRAPH_STREAM_QUEUE_POLL_SECONDS)
            except queue.Empty:
                now = time.monotonic()
                if now - last_heartbeat >= LANGGRAPH_STREAM_HEARTBEAT_SECONDS:
                    last_heartbeat = now
                    event = progress(
                        "research_progress",
                        "Still working...",
                        quiet_seconds=round(now - started, 1),
                        ephemeral_ui=True,
                    )
                    yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
                continue

            worker.join(timeout=0)
            if status == "error":
                raise value
            return {"status": status, "value": value}

    def _langgraph_research_turn_result(
        self,
        *,
        turn_id: str,
        goal: Goal,
        research: dict[str, Any],
        events: list[ProgressEvent],
    ) -> TurnResult:
        response = research["response"]
        langgraph_state = research.get("langgraph_state") or {}
        is_paused = bool(langgraph_state.get("interrupted"))
        pause_contract = langgraph_state.get("pause_contract") or {}
        return TurnResult(
            turn_id=turn_id,
            goal=goal,
            answer=response.text,
            route=goal.route,
            turn_status="paused" if is_paused else "completed",
            langgraph_run_id=research.get("langgraph_run_id") if is_paused else None,
            pause_reason=pause_contract.get("pause_reason") if is_paused else None,
            required_additional_budget_usd=(
                pause_contract.get("required_additional_budget_usd") if is_paused else None
            ),
            model_used=response.model_used,
            sources=research["sources"],
            tool_calls=research["tool_calls"],
            events=events,
            latency_ms=response.latency_ms + sum(call.latency_ms for call in research["tool_calls"]),
            cost_usd=response.cost_usd,
        )

    def resume_langgraph_turn_stream(
        self,
        turn_id: str,
        langgraph_run_id: str,
        *,
        approved_by: str,
        updated_budget_ceiling_usd: float | None,
        user_id: str,
    ):
        from app.services.agent import persistence
        from app.services.agent.langgraph_runtime import stream_resume_langgraph_research

        existing = persistence.load_turn(turn_id, user_id)
        if existing is None:
            yield StreamEnvelope(
                type="error",
                data={"turn_id": turn_id, "message": "Paused turn not found for LangGraph resume."},
            )
            return
        events: list[ProgressEvent] = []

        def progress(stage: str, message: str, **data) -> ProgressEvent:
            event = ProgressEvent(turn_id=turn_id, stage=stage, message=message, data=data)
            events.append(event)
            return event

        gen = stream_resume_langgraph_research(
            langgraph_run_id,
            approved_by=approved_by,
            updated_budget_ceiling_usd=updated_budget_ceiling_usd,
            progress=progress,
            already_claimed=True,
        )
        research = yield from self._forward_langgraph_stream(gen, progress)
        result = self._langgraph_research_turn_result(
            turn_id=turn_id,
            goal=existing.goal,
            research=research,
            events=events,
        )
        yield StreamEnvelope(type="result", data=result.model_dump(mode="json"))

    def _run_research_subtree(self, request: TurnRequest, progress):
        from app.config import get_settings
        from app.services.agent.langgraph_runtime import stream_langgraph_research
        from app.services.agent.models import new_id

        audit_id = new_id("lgaudit")
        logger.info(
            "langgraph_orchestrator_dispatch",
            extra={
                "audit_id": audit_id,
                "orchestrator": "langgraph",
                "env": get_settings().app_env,
                "research_level": getattr(request, "research_level", None),
                "message_preview": (getattr(request, "message", "") or "")[:60],
            },
        )
        gen = stream_langgraph_research(request, self.tool_registry.tools, progress)
        return (yield from self._forward_langgraph_stream(gen, progress))

    def _apply_decision(self, request: TurnRequest, decision: OrchestratorDecision) -> TurnRequest:
        updates = {}
        if decision.output_format in {"chat", "markdown", "docx", "pptx"}:
            updates["output_format"] = decision.output_format
        if decision.research_level in {"easy", "regular", "deep"}:
            updates["research_level"] = decision.research_level
        return request.model_copy(update=updates) if updates else request

    def _run_deep_research_confirmation(
        self,
        request: TurnRequest,
        goal: Goal,
        turn_id: str,
        events: list[ProgressEvent],
        decision: OrchestratorDecision,
        progress,
    ) -> tuple[TurnResult, ProgressEvent]:
        try:
            preview = build_research_plan_preview(request)
        except Exception as exc:
            preview = {
                "title": "Deep research plan",
                "goal": request.message,
                "research_level": "deep",
                "estimated_duration": "Ready in a few minutes",
                "workflow": [
                    {"label": "Research websites", "description": "Search and read source candidates."},
                    {"label": "Analyze results", "description": "Evaluate evidence quality and gaps."},
                    {"label": "Create report", "description": "Synthesize the final answer."},
                ],
                "investigate": [request.message],
                "source_strategy": ["Web search", "Source reading", "Evidence extraction"],
                "fallback_reasons": [str(exc)],
            }
        preview_event = progress(
            "research_plan_preview",
            "I drafted a deep research plan for review.",
            research_plan_preview=preview,
        )
        result = TurnResult(
            turn_id=turn_id,
            goal=goal,
            answer=decision.confirmation_message
            or (
                "I drafted a deep research plan. Review it, then start deep research when you are ready."
            ),
            route="clarify",
            model_used=decision.model_used,
            events=events,
            latency_ms=decision.latency_ms,
            cost_usd=decision.cost_usd,
            research_plan_preview=preview,
            follow_up_options=[
                {
                    "label": "Start research",
                    "message": request.message,
                    "force_route": decision.route,
                    "research_level": "deep",
                    "confirm_deep_research": True,
                    "output_format": request.output_format,
                },
                {
                    "label": "Use regular research",
                    "message": request.message,
                    "force_route": decision.route,
                    "research_level": "regular",
                    "confirm_deep_research": False,
                    "output_format": request.output_format,
                },
                {
                    "label": "Answer directly",
                    "message": request.message,
                    "force_route": "direct",
                    "research_level": "easy",
                    "confirm_deep_research": False,
                    "output_format": "chat",
                },
            ],
        )
        return result, preview_event

    def _run_clarify(
        self,
        request,
        goal,
        turn_id,
        events,
        decision: OrchestratorDecision,
    ) -> TurnResult:
        question = decision.clarification_question or "Can you clarify what you want me to do?"
        return TurnResult(
            turn_id=turn_id,
            goal=goal,
            answer=question,
            route="clarify",
            model_used=decision.model_used,
            events=events,
            latency_ms=decision.latency_ms,
            cost_usd=decision.cost_usd,
        )

    def _run_direct(self, request, goal, turn_id, events, progress) -> TurnResult:
        user_prompt = request.message
        if request.conversation_context:
            user_prompt = f"{request.conversation_context}\n\nCurrent user request:\n{request.message}"
        response = yield from self._stream_model_response(
            progress,
            [
                {"role": "system", "content": "You are Fronei v3, a concise and helpful assistant. Answer directly."},
                {"role": "user", "content": user_prompt},
            ],
            role="direct_answer",
            quality_mode=request.quality_mode,
            overrides=request.model_overrides,
            max_tokens=900,
            timeout_s=30,
        )
        return TurnResult(
            turn_id=turn_id,
            goal=goal,
            answer=response.text,
            route=goal.route,
            model_used=response.model_used,
            **model_client.telemetry_for_role(
                "direct_answer",
                quality_mode=request.quality_mode,
                model_used=response.model_used,
                overrides=request.model_overrides,
            ),
            events=events,
            latency_ms=response.latency_ms,
            cost_usd=response.cost_usd,
        )

    def _run_document(
        self,
        request,
        goal,
        turn_id,
        events,
        progress,
        *,
        sources: list[Source],
        research_answer: str | None = None,
        evidence: EvidencePack | None = None,
    ) -> TurnResult:
        event = progress(
            "document_planner",
            "Planning document structure.",
            source_count=len(sources),
            has_research=bool(research_answer),
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        # This subtree is synchronous but deliberately stage-explicit so the
        # UI/admin trace shows every decision the document path makes.
        plan = plan_document(request, sources=sources, research_answer=research_answer, evidence=evidence)
        event = progress(
            "document_plan",
            f"Document plan ready with {len(plan.sections)} section(s).",
            title=plan.title,
            format=plan.format,
            audience=plan.audience,
            sections=plan.sections,
            source=plan.source,
            model_used=plan.model_used,
            **model_client.telemetry_for_role(
                "document_planner",
                quality_mode=request.quality_mode,
                model_used=plan.model_used,
                overrides=request.model_overrides,
            ),
            fallback_reason=plan.fallback_reason,
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))

        tool_name = choose_artifact_tool(request, plan)
        if tool_name == "make_pptx_artifact":
            event = progress(
                "deck_planner",
                "Planning a native slide storyboard and visual structure.",
                template_id=request.template_id,
                planned_sections=list(plan.sections or []),
            )
            event.data.update(model_client.telemetry_for_role("document_planner", quality_mode=request.quality_mode, overrides=request.model_overrides))
            yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
            deck = plan_deck(
                request,
                plan,
                sources=sources,
                research_answer=research_answer,
                evidence=evidence,
                user_id=goal.user_id,
            )
            event = progress(
                "deck_plan",
                f"Deck plan ready with {len(deck.render_plan.slides)} slide(s).",
                title=deck.title,
                design_system=deck.design_system_id,
                slide_count=len(deck.render_plan.slides),
                template_mode=deck.template_grammar.get("mode"),
                template_slide_types=deck.template_grammar.get("available_slide_types"),
                template_preferred_layouts=deck.template_grammar.get("preferred_v3_layouts"),
                repair_actions=deck.repair_actions,
                **model_client.telemetry_for_response(
                    model_client.ModelResponse(
                        text="",
                        model_used=deck.model_used,
                        latency_ms=deck.latency_ms,
                        cost_usd=deck.cost_usd,
                        model_role="document_planner",
                        preferred_model=deck.preferred_model,
                        attempted_models=deck.attempted_models,
                        failed_model_attempts=deck.failed_model_attempts,
                    ),
                    overrides=request.model_overrides,
                ),
                latency_ms=deck.latency_ms,
                cost_usd=deck.cost_usd,
            )
            yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
            event = progress(
                "pptx_design_plan",
                "Composing validated design-system slides.",
                template_id=request.template_id,
                design_system=deck.design_system_id,
                template_mode=deck.template_grammar.get("mode"),
                template_layout_inventory=deck.template_grammar.get("layout_inventory"),
                design_ledger=deck.design_ledger,
            )
            yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
            event = progress("artifact_builder", "Building artifact with make_pptx_artifact.", tool_name=tool_name, title=deck.title)
            yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
            artifact, artifact_call = self.tool_registry.run(
                "make_pptx_artifact",
                {
                    "title": deck.title,
                    "markdown": deck.summary_markdown,
                    "expected_slides": [slide.section_title or slide.closing_text or slide.hero_title or "" for slide in deck.doc_plan.sections],
                    "template_id": request.template_id,
                    "user_id": goal.user_id,
                    "render_plan": deck.render_plan.to_payload(),
                    "design_system_id": deck.design_system_id,
                    "repair_actions": deck.repair_actions,
                },
            )
            if artifact.kind == "markdown":
                event = progress(
                    "chat_renderer",
                    "PPTX rendering fell back to a deck summary in chat.",
                    title=deck.title,
                    format="markdown",
                    markdown_chars=len(deck.summary_markdown or ""),
                    error=artifact_call.error,
                )
                yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
                return TurnResult(
                    turn_id=turn_id,
                    goal=goal,
                    answer=deck.summary_markdown,
                    route=goal.route,
                    model_used=deck.model_used,
                    sources=sources,
                    tool_calls=[artifact_call],
                    artifacts=[],
                    events=events,
                    latency_ms=plan.latency_ms + deck.latency_ms + artifact_call.latency_ms,
                    cost_usd=plan.cost_usd + deck.cost_usd,
                )
            event = progress(
                "artifact_result",
                f"Artifact builder produced {artifact.filename}.",
                tool_name=artifact_call.name,
                filename=artifact.filename,
                ok=artifact_call.ok,
                error=artifact_call.error,
                deck_source=artifact_call.output.get("deck_source"),
                design_system=artifact_call.output.get("design_system"),
            )
            yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
            return TurnResult(
                turn_id=turn_id,
                goal=goal,
                answer=f"Done. I created `{artifact.filename}` from a native deck plan.",
                route=goal.route,
                model_used=deck.model_used,
                sources=sources,
                tool_calls=[artifact_call],
                artifacts=[artifact],
                events=events,
                latency_ms=plan.latency_ms + deck.latency_ms + artifact_call.latency_ms,
                cost_usd=plan.cost_usd + deck.cost_usd,
            )

        event = progress("document_writer", "Writing document draft.", plan_title=plan.title)
        event.data.update(model_client.telemetry_for_role("document_writer", quality_mode=request.quality_mode, overrides=request.model_overrides))
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        draft = write_document(request, plan, sources=sources, research_answer=research_answer, evidence=evidence)
        event = progress(
            "document_writer_result",
            f"Document writer used {draft.model_used or 'the configured document writer model'}.",
            plan_title=plan.title,
            **model_client.telemetry_for_response(
                model_client.ModelResponse(
                    text="",
                    model_used=draft.model_used,
                    latency_ms=draft.latency_ms,
                    cost_usd=draft.cost_usd,
                    model_role=draft.model_role,
                    preferred_model=draft.preferred_model,
                    attempted_models=draft.attempted_models,
                    failed_model_attempts=draft.failed_model_attempts,
                ),
                overrides=request.model_overrides,
            ),
            latency_ms=draft.latency_ms,
            cost_usd=draft.cost_usd,
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))

        event = progress("document_judge", "Checking document draft.", plan_title=plan.title)
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        judge = judge_document(draft, plan, source_count=len(sources))
        event = progress(
            "document_judge_result",
            f"Document judge returned {judge.status}.",
            status=judge.status,
            score=judge.score,
            issues=judge.issues,
            repair_instruction=judge.repair_instruction,
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))

        if judge.status == "repair":
            event = progress(
                "document_repair",
                "Repairing document draft.",
                issues=judge.issues,
                repair_instruction=judge.repair_instruction,
            )
            yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
            repaired = write_document(
                request,
                plan,
                sources=sources,
                research_answer=research_answer,
                evidence=evidence,
                repair_instruction=judge.repair_instruction,
            )
            draft.markdown = repaired.markdown
            draft.model_used = repaired.model_used or draft.model_used
            draft.latency_ms += repaired.latency_ms
            draft.cost_usd += repaired.cost_usd

        if plan.format == "markdown" or request.output_format == "markdown":
            event = progress(
                "chat_renderer",
                "Rendering markdown in the chat response.",
                title=plan.title,
                format="markdown",
                markdown_chars=len(draft.markdown or ""),
            )
            yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
            return TurnResult(
                turn_id=turn_id,
                goal=goal,
                answer=draft.markdown,
                route=goal.route,
                model_used=draft.model_used,
                sources=sources,
                tool_calls=[],
                artifacts=[],
                events=events,
                latency_ms=plan.latency_ms + draft.latency_ms,
                cost_usd=plan.cost_usd + draft.cost_usd,
            )

        event = progress(
            "artifact_builder",
            f"Building artifact with {tool_name}.",
            tool_name=tool_name,
            title=plan.title,
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        artifact, artifact_call = build_artifact(self.tool_registry, plan, draft, tool_name, request, user_id=goal.user_id)
        if artifact.kind == "markdown":
            event = progress(
                "chat_renderer",
                "Artifact rendering fell back to markdown, so I am rendering it in chat.",
                title=plan.title,
                format="markdown",
                fallback_from=tool_name,
                markdown_chars=len(draft.markdown or ""),
            )
            yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
            return TurnResult(
                turn_id=turn_id,
                goal=goal,
                answer=draft.markdown,
                route=goal.route,
                model_used=draft.model_used,
                sources=sources,
                tool_calls=[artifact_call],
                artifacts=[],
                events=events,
                latency_ms=plan.latency_ms + draft.latency_ms + artifact_call.latency_ms,
                cost_usd=plan.cost_usd + draft.cost_usd,
            )
        event = progress(
            "artifact_result",
            f"Artifact builder produced {artifact.filename}.",
            tool_name=artifact_call.name,
            filename=artifact.filename,
            ok=artifact_call.ok,
            error=artifact_call.error,
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        answer = f"Done. I created `{artifact.filename}`."
        return TurnResult(
            turn_id=turn_id,
            goal=goal,
            answer=answer,
            route=goal.route,
            model_used=draft.model_used,
            sources=sources,
            tool_calls=[artifact_call],
            artifacts=[artifact],
            events=events,
            latency_ms=plan.latency_ms + draft.latency_ms + artifact_call.latency_ms,
            cost_usd=plan.cost_usd + draft.cost_usd,
        )

    def _merge_sources(self, search_sources: list[Source], extracted_sources: list[Source]) -> list[Source]:
        by_url = {source.url: source for source in search_sources if source.url}
        for source in extracted_sources:
            if source.url in by_url:
                if source.content and not _looks_like_low_value_extraction(source.content):
                    by_url[source.url].content = source.content
                if source.title:
                    by_url[source.url].title = source.title
            elif source.url:
                by_url[source.url] = source
        return list(by_url.values())
