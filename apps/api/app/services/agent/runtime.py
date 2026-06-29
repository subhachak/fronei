from __future__ import annotations

import json
import time
from collections.abc import Iterator

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
from app.services.agent.research_subtree import (
    EvidencePack,
    ResearchBudgetLedger,
    ResearchFeedbackLoop,
    bind_evidence,
    build_gap_followup_workers,
    build_research_plan_preview,
    build_synthesis_prompt,
    create_research_goal,
    extract_deep_link_candidates,
    get_research_registry,
    is_public_source_url,
    judge_research,
    plan_research,
    rank_sources,
    repair_research_answer,
    _synthesis_token_budget,
    verify_claims,
)
from app.services.agent.research_planner import _longform_timeout_s
from app.services.agent.tool_registry import ToolRegistry
from app.services.agent.tools import Tools, source_context


def _is_owner_reliability_research(message: str) -> bool:
    text = (message or "").lower()
    owner_terms = (
        "owner review",
        "owner reviews",
        "owner report",
        "owner reports",
        "owner experience",
        "owner experiences",
        "owners say",
        "user reviews",
        "customer reviews",
        "reddit",
        "forum",
        "community",
        "real-world",
        "real world",
    )
    reliability_terms = (
        "reliability",
        "failure rate",
        "failure rates",
        "failures",
        "degradation",
        "capacity retention",
        "long-term",
        "long term",
        "after 1",
        "after 2",
        "1-2 years",
        "1–2 years",
        "warranty claim",
    )
    return any(term in text for term in owner_terms) and any(term in text for term in reliability_terms)


def _add_owner_reliability_gaps(request: TurnRequest, evidence: EvidencePack) -> None:
    if not _is_owner_reliability_research(request.message):
        return
    combined = "\n".join(
        f"{item.title} {item.url} {item.evidence}"
        for item in evidence.items
    ).lower()
    gaps: list[str] = []
    if not any(term in combined for term in ("reddit", "forum", "community", "owner review", "owner report", "verified purchase", "customer review")):
        gaps.append("Missing actual owner/community/forum evidence; policy or warranty pages do not answer owner reliability.")
    if not any(term in combined for term in ("12 month", "12-month", "1 year", "one year", "18 month", "18-month", "24 month", "24-month", "2 year", "two year", "long term", "long-term")):
        gaps.append("Missing 12-24 month longitudinal owner evidence for field reliability.")
    if any(term in (request.message or "").lower() for term in ("failure rate", "degradation", "capacity retention")) and not any(
        term in combined for term in ("failure rate", "degradation", "capacity retention", "capacity loss", "warranty claim", "replacement")
    ):
        gaps.append("Missing quantified or outcome-based evidence for failure rate, degradation, or claim outcomes.")
    for gap in gaps:
        if gap not in evidence.gaps:
            evidence.gaps.append(gap)
    if gaps and evidence.coverage >= 1.0:
        evidence.coverage = 0.55


def _gap_followup_read_reserve(request: TurnRequest, ledger: ResearchBudgetLedger) -> int:
    if not _is_owner_reliability_research(request.message):
        return 0
    return min(4, ledger.budget.max_deep_links, max(0, ledger.remaining_source_reads()))


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
                result = TurnResult(
                    turn_id=turn_id,
                    goal=goal,
                    answer=response.text,
                    route=goal.route,
                    model_used=response.model_used,
                    sources=research["sources"],
                    tool_calls=research["tool_calls"],
                    events=events,
                    latency_ms=response.latency_ms + sum(call.latency_ms for call in research["tool_calls"]),
                    cost_usd=response.cost_usd,
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

    def _run_research_subtree(self, request: TurnRequest, progress):
        if request.research_level == "deep":
            from queue import Empty, Queue
            from threading import Thread

            from app.services.agent.research_subtree import lead_research_loop

            event_queue: Queue[ProgressEvent | object] = Queue()
            done = object()
            result_holder: dict[str, object] = {}

            def lead_progress(stage: str, message: str, data: dict):
                event = progress(stage, message, **data)
                event_queue.put(event)

            def run_lead_loop() -> None:
                try:
                    result_holder["result"] = lead_research_loop(request, self.tool_registry.tools, lead_progress)
                except BaseException as exc:  # pragma: no cover - defensive streaming bridge.
                    result_holder["error"] = exc
                finally:
                    event_queue.put(done)

            thread = Thread(target=run_lead_loop, name="lead-research", daemon=True)
            thread.start()
            while True:
                try:
                    item = event_queue.get(timeout=0.25)
                except Empty:
                    if not thread.is_alive():
                        break
                    continue
                if item is done:
                    break
                yield StreamEnvelope(type="progress", data=item.model_dump(mode="json"))
            thread.join(timeout=1)
            if "error" in result_holder:
                raise result_holder["error"]  # type: ignore[misc]
            return result_holder["result"]

        research_started = time.perf_counter()
        registry = get_research_registry()
        research_goal = create_research_goal(request)
        ledger = ResearchBudgetLedger(budget=research_goal.budget)
        event = progress(
            "research_registry",
            "Research team is ready.",
            registry=registry.public_summary(),
            agent_count=len(registry.agents),
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        event = progress(
            "research_goal",
            "Research goal and safety limits are set.",
            goal=research_goal.model_dump(mode="json"),
            budget_ledger=ledger.model_dump(mode="json"),
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        event = progress(
            "research_planning",
            "Planning focused research questions.",
            available_tools=self.tool_registry.tool_names_for_route("research"),
            agent_id="research_lead",
            prompt_template_id=registry.agent("research_lead").prompt_template_id,
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        plan = plan_research(request)
        if plan.model_used:
            ledger.record_model_call(cost_usd=plan.cost_usd, latency_ms=plan.latency_ms)
        ledger.refresh_elapsed(int((time.perf_counter() - research_started) * 1000))
        event = progress(
            "research_plan",
            f"Research plan ready with {len(plan.workers)} search worker(s).",
            questions=plan.questions,
            search_queries=plan.search_queries,
            workers=[worker.model_dump(mode="json") for worker in plan.workers],
            max_sources=plan.max_sources,
            min_evidence_items=plan.min_evidence_items,
            judge_threshold=plan.judge_threshold,
            repair_iterations=plan.repair_iterations,
            guardrails=plan.guardrails,
            source=plan.source,
            model_used=plan.model_used,
            **model_client.telemetry_for_role(
                "research_planner",
                quality_mode=request.quality_mode,
                model_used=plan.model_used,
                overrides=request.model_overrides,
            ),
            fallback_reason=plan.fallback_reason,
            agent_id="research_lead",
            budget_ledger=ledger.model_dump(mode="json"),
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        event = progress(
            "research_guardrails",
            "Research guardrails are active.",
            guardrails=plan.guardrails,
            max_sources=plan.max_sources,
            max_workers=len(plan.workers),
            budget_ledger=ledger.model_dump(mode="json"),
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))

        search_sources: list[Source] = []
        tool_calls = []
        for idx, worker in enumerate(plan.workers, start=1):
            ledger.refresh_elapsed(int((time.perf_counter() - research_started) * 1000))
            if not ledger.can_start_tool("web_search"):
                event = progress(
                    "research_budget",
                    f"Budget stopped search worker {idx}.",
                    stop_reason=ledger.stop_reason,
                    budget_ledger=ledger.model_dump(mode="json"),
                )
                yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
                break
            event = progress(
                "search_worker",
                f"Search worker {idx} running.",
                agent_id=worker.agent_id,
                worker_id=worker.worker_id,
                worker_index=idx,
                question=worker.question,
                query=worker.query,
                rationale=worker.rationale,
                candidate_queries=plan.search_queries,
            )
            yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
            sources, call = yield from self._run_tool(
                progress,
                "web_search",
                {"query": worker.query, "max_results": worker.max_results},
            )
            tool_calls.append(call)
            ledger.record_tool_call(latency_ms=call.latency_ms, sources_seen=len(sources))
            search_sources.extend(sources)
            provider = call.output.get("provider") if isinstance(call.output, dict) else None
            provider_message = (
                f"Search worker {idx} used {provider}."
                if provider
                else f"Search worker {idx} completed without a provider result."
            )
            event = progress(
                "search_worker_provider",
                provider_message,
                agent_id=worker.agent_id,
                worker_id=worker.worker_id,
                worker_index=idx,
                query=worker.query,
                provider=provider,
                ok=call.ok,
                source_count=len(sources),
                error=call.error,
                budget_ledger=ledger.model_dump(mode="json"),
            )
            yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
            if ledger.stopped:
                event = progress(
                    "research_budget",
                    "Budget stopped additional search work.",
                    stop_reason=ledger.stop_reason,
                    budget_ledger=ledger.model_dump(mode="json"),
                )
                yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
                break

        deduped_all = self._merge_sources(search_sources, [])
        public_candidates = [source for source in deduped_all if is_public_source_url(source.url)]
        blocked_source_urls = [source.url for source in deduped_all if source.url and not is_public_source_url(source.url)]
        ranked_sources = rank_sources(public_candidates, plan)
        deduped = [item.source for item in ranked_sources]
        event = progress(
            "source_ranker",
            "Ranking source candidates.",
            agent_id="source_ranker",
            prompt_template_id=registry.agent("source_ranker").prompt_template_id,
            ranked_sources=[item.model_dump(mode="json") for item in ranked_sources[: plan.max_sources]],
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        event = progress(
            "source_selection",
            f"Selected {min(len(deduped), plan.max_sources)} unique source candidate(s).",
            candidate_count=len(search_sources),
            unique_count=len(deduped),
            selected_urls=[source.url for source in deduped[: plan.max_sources]],
            blocked_source_urls=blocked_source_urls,
            guardrail="public_source_urls",
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))

        selected = deduped[: plan.max_sources]
        if ledger.remaining_source_reads() < len(selected):
            selected = selected[: ledger.remaining_source_reads()]
        event = progress(
            "source_reader",
            "Reading selected source pages.",
            agent_id="source_reader",
            prompt_template_id=registry.agent("source_reader").prompt_template_id,
            source_count=len(selected),
            budget_ledger=ledger.model_dump(mode="json"),
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        extracted = []
        if selected and ledger.can_start_tool("read_url") and ledger.can_read_more_sources():
            extracted, read_call = yield from self._run_tool(
                progress,
                "read_url",
                {"urls": [source.url for source in selected if source.url]},
            )
            tool_calls.append(read_call)
            ledger.record_tool_call(latency_ms=read_call.latency_ms, sources_read=len(selected))
        elif selected:
            event = progress(
                "research_budget",
                "Budget skipped source reading.",
                stop_reason=ledger.stop_reason,
                budget_ledger=ledger.model_dump(mode="json"),
            )
            yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        merged = self._merge_sources(selected, extracted)
        gap_read_reserve = _gap_followup_read_reserve(request, ledger)
        deep_link_budget = min(
            ledger.budget.max_deep_links,
            max(0, ledger.remaining_source_reads() - gap_read_reserve),
        )
        deep_links = extract_deep_link_candidates(merged, max_links=deep_link_budget)
        event = progress(
            "deep_link_agent",
            f"Found {len(deep_links)} useful deep link(s).",
            agent_id="deep_link_agent",
            prompt_template_id=registry.agent("deep_link_agent").prompt_template_id,
            link_budget=deep_link_budget,
            links=[link.model_dump(mode="json") for link in deep_links],
            budget_ledger=ledger.model_dump(mode="json"),
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        if deep_links and ledger.can_start_tool("read_url") and ledger.can_read_more_sources():
            deep_extracted, deep_read_call = yield from self._run_tool(
                progress,
                "read_url",
                {"urls": [link.url for link in deep_links], "max_chars_per_source": 1800},
            )
            tool_calls.append(deep_read_call)
            ledger.record_tool_call(latency_ms=deep_read_call.latency_ms, sources_read=len(deep_links))
            merged = self._merge_sources(merged, deep_extracted)
        elif deep_links:
            event = progress(
                "research_budget",
                "Budget skipped deep-link reading.",
                stop_reason=ledger.stop_reason,
                budget_ledger=ledger.model_dump(mode="json"),
            )
            yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))

        evidence = bind_evidence(merged, plan=plan, max_items=plan.max_sources)
        _add_owner_reliability_gaps(request, evidence)
        if evidence.gaps and not ledger.stopped and ledger.remaining_tool_calls() >= 2:
            followups = build_gap_followup_workers(request, plan, evidence)
            event = progress(
                "gap_agent",
                f"Gap agent created {len(followups)} follow-up search worker(s).",
                agent_id="gap_agent",
                prompt_template_id=registry.agent("gap_agent").prompt_template_id,
                gaps=evidence.gaps,
                workers=[worker.model_dump(mode="json") for worker in followups],
                budget_ledger=ledger.model_dump(mode="json"),
            )
            yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
            for idx, worker in enumerate(followups, start=1):
                if not ledger.can_start_tool("web_search"):
                    break
                sources, call = yield from self._run_tool(
                    progress,
                    "web_search",
                    {"query": worker.query, "max_results": worker.max_results},
                )
                tool_calls.append(call)
                ledger.record_tool_call(latency_ms=call.latency_ms, sources_seen=len(sources))
                followup_public = [source for source in sources if is_public_source_url(source.url)]
                followup_ranked = rank_sources(followup_public, plan)
                remaining_workers = max(1, len(followups) - idx + 1)
                per_worker_read_limit = max(1, ledger.remaining_source_reads() // remaining_workers)
                followup_selected = [item.source for item in followup_ranked[:per_worker_read_limit]]
                if followup_selected and ledger.can_start_tool("read_url") and ledger.can_read_more_sources():
                    followup_selected = followup_selected[: ledger.remaining_source_reads()]
                    followup_extracted, followup_read_call = yield from self._run_tool(
                        progress,
                        "read_url",
                        {"urls": [source.url for source in followup_selected], "max_chars_per_source": 1800},
                    )
                    tool_calls.append(followup_read_call)
                    ledger.record_tool_call(latency_ms=followup_read_call.latency_ms, sources_read=len(followup_selected))
                    merged = self._merge_sources(merged, self._merge_sources(followup_selected, followup_extracted))
            evidence = bind_evidence(merged, plan=plan, max_items=plan.max_sources)
            _add_owner_reliability_gaps(request, evidence)
        elif evidence.gaps:
            event = progress(
                "research_budget",
                "Budget skipped gap follow-up search.",
                stop_reason=ledger.stop_reason,
                remaining_tool_calls=ledger.remaining_tool_calls(),
                budget_ledger=ledger.model_dump(mode="json"),
            )
            yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        event = progress(
            "evidence_binder",
            f"Bound {len(evidence.items)} evidence item(s).",
            agent_id="evidence_binder",
            prompt_template_id=registry.agent("evidence_binder").prompt_template_id,
            coverage=evidence.coverage,
            gaps=evidence.gaps,
            contradictions=evidence.contradictions,
            evidence_items=[item.model_dump(mode="json") for item in evidence.items],
            budget_ledger=ledger.model_dump(mode="json"),
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))

        if not ledger.can_start_model("synthesis_agent"):
            response = model_client.ModelResponse(
                text=(
                    "I gathered the available evidence, but stopped before synthesis because the research "
                    f"budget was exhausted ({ledger.stop_reason})."
                ),
                model_used="budget-ledger",
                latency_ms=0,
                cost_usd=0.0,
            )
            judge = judge_research(request, plan, evidence, response.text)
            feedback = ResearchFeedbackLoop(judge=judge, final_score=judge.score)
            return {
                "sources": merged,
                "tool_calls": tool_calls,
                "evidence": evidence,
                "response": response,
                "plan": plan,
                "feedback": feedback,
            }
        event = progress(
            "synthesis",
            "Synthesizing source-grounded answer from evidence.",
            agent_id="synthesis_agent",
            prompt_template_id=registry.agent("synthesis_agent").prompt_template_id,
            **model_client.telemetry_for_role("synthesis", quality_mode=request.quality_mode, overrides=request.model_overrides),
            budget_ledger=ledger.model_dump(mode="json"),
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        system_prompt, user_prompt = build_synthesis_prompt(request, plan, evidence)
        response = yield from self._stream_model_response(
            progress,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            role="synthesis",
            quality_mode=request.quality_mode,
            overrides=request.model_overrides,
            max_tokens=_synthesis_token_budget(request, plan),
            timeout_s=_longform_timeout_s(),
        )
        ledger.record_model_call(cost_usd=response.cost_usd, latency_ms=response.latency_ms)
        event = progress(
            "synthesis_result",
            f"Synthesis used {response.model_used or 'the configured synthesis model'}.",
            agent_id="synthesis_agent",
            **model_client.telemetry_for_role(
                "synthesis",
                quality_mode=request.quality_mode,
                model_used=response.model_used,
                overrides=request.model_overrides,
            ),
            latency_ms=response.latency_ms,
            cost_usd=response.cost_usd,
            budget_ledger=ledger.model_dump(mode="json"),
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        judge = judge_research(request, plan, evidence, response.text)
        event = progress(
            "research_judge",
            "Checking research quality before publishing.",
            agent_id="research_judge",
            prompt_template_id=registry.agent("research_judge").prompt_template_id,
            **model_client.telemetry_for_role("research_judge", quality_mode=request.quality_mode, overrides=request.model_overrides),
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        event = progress(
            "research_judge_result",
            f"Research judge returned {judge.status}.",
            status=judge.status,
            score=judge.score,
            issues=judge.issues,
            repair_instruction=judge.repair_instruction,
            can_publish=judge.can_publish,
            agent_id="research_judge",
            budget_ledger=ledger.model_dump(mode="json"),
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        verification = verify_claims(response.text, evidence)
        event = progress(
            "claim_verifier",
            f"Claim verifier returned {verification.status}.",
            agent_id="claim_verifier",
            prompt_template_id=registry.agent("claim_verifier").prompt_template_id,
            verification=verification.model_dump(mode="json"),
            **model_client.telemetry_for_role(
                "citation_verifier",
                quality_mode="standard",
                model_used=getattr(verification, "model_used", ""),
                overrides=request.model_overrides,
            ),
            budget_ledger=ledger.model_dump(mode="json"),
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        if verification.status == "repair" and judge.status == "pass":
            judge.status = "repair"
            judge.issues.extend(verification.notes)
            judge.repair_instruction = "Add citations to unsupported substantive claims."
            judge.can_publish = False
        feedback = ResearchFeedbackLoop(judge=judge, final_score=judge.score)
        if judge.status == "repair" and plan.repair_iterations > 0 and ledger.can_start_model("repair_agent"):
            event = progress(
                "research_repair",
                "Repairing the research answer.",
                agent_id="repair_agent",
                prompt_template_id=registry.agent("repair_agent").prompt_template_id,
                repair_instruction=judge.repair_instruction,
                issues=judge.issues,
                **model_client.telemetry_for_role("repair", quality_mode=request.quality_mode, overrides=request.model_overrides),
                budget_ledger=ledger.model_dump(mode="json"),
            )
            yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
            repaired = repair_research_answer(request, plan, evidence, response.text, judge)
            ledger.record_model_call(cost_usd=repaired.cost_usd, latency_ms=repaired.latency_ms)
            repaired_judge = judge_research(request, plan, evidence, repaired.text)
            response = repaired
            feedback = ResearchFeedbackLoop(
                judge=repaired_judge,
                repaired=True,
                repair_attempts=1,
                final_score=repaired_judge.score,
            )
            event = progress(
                "research_repair_result",
                f"Repair complete; judge now returned {repaired_judge.status}.",
                status=repaired_judge.status,
                score=repaired_judge.score,
                issues=repaired_judge.issues,
                can_publish=repaired_judge.can_publish,
                agent_id="repair_agent",
                **model_client.telemetry_for_role(
                    "repair",
                    quality_mode=request.quality_mode,
                    model_used=repaired.model_used,
                    overrides=request.model_overrides,
                ),
                budget_ledger=ledger.model_dump(mode="json"),
            )
            yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        elif judge.status == "repair" and plan.repair_iterations > 0:
            event = progress(
                "research_budget",
                "Budget skipped repair.",
                stop_reason=ledger.stop_reason,
                budget_ledger=ledger.model_dump(mode="json"),
            )
            yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        event = progress(
            "research_budget",
            "Research budget ledger closed.",
            stop_reason=ledger.stop_reason,
            budget_ledger=ledger.model_dump(mode="json"),
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        return {
            "sources": merged,
            "tool_calls": tool_calls,
            "evidence": evidence,
            "response": response,
            "plan": plan,
            "feedback": feedback,
        }

    def _apply_decision(self, request: TurnRequest, decision: OrchestratorDecision) -> TurnRequest:
        updates = {}
        if decision.output_format in {"chat", "markdown", "docx", "pptx"}:
            updates["output_format"] = decision.output_format
        if decision.research_level in {"easy", "regular", "deep"}:
            updates["research_level"] = decision.research_level
        if decision.rewritten_request:
            updates["message"] = decision.rewritten_request
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
                by_url[source.url].content = source.content
                if source.title:
                    by_url[source.url].title = source.title
            elif source.url:
                by_url[source.url] = source
        return list(by_url.values())
