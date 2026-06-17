from __future__ import annotations

import time
from collections.abc import Iterator

from app.services.agent_v3 import model_client
from app.services.agent_v3.document_subtree import (
    build_artifact,
    choose_artifact_tool,
    judge_document,
    plan_document,
    write_document,
)
from app.services.agent_v3.orchestrator import OrchestratorDecision, decide_with_options
from app.services.agent_v3.research_subtree import (
    EvidencePack,
    bind_evidence,
    plan_research,
    synthesize_answer,
)
from app.services.agent_v3.models import (
    AgentV3Request,
    AgentV3Result,
    Goal,
    ProgressEvent,
    Source,
    StreamEnvelope,
    new_id,
)
from app.services.agent_v3.tool_registry import ToolRegistry
from app.services.agent_v3.tools import AgentV3Tools


class AgentV3Runtime:
    """Fresh isolated runtime with no dependency on the legacy/hybrid pipelines."""

    def __init__(self, tools: AgentV3Tools | None = None):
        self.tool_registry = ToolRegistry(tools or AgentV3Tools.from_settings())

    def run_stream(self, request: AgentV3Request, *, user_id: str) -> Iterator[StreamEnvelope]:
        turn_id = new_id("turn")
        started = time.perf_counter()
        available_routes = ["direct", "clarify", "research", "document", "research_document"]
        available_tools = [tool["name"] for tool in self.tool_registry.describe()]
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
            f"Fresh orchestrator selected the {route} route.",
            route=route,
            confidence=decision.confidence,
            reason=decision.reason,
            source=decision.source,
            model_used=decision.model_used,
            available_routes=decision.available_routes,
            available_tools=decision.available_tools,
            fallback_reason=decision.fallback_reason,
            route_tools=self.tool_registry.tool_names_for_route(route),
        )
        yield StreamEnvelope(type="progress", data=first.model_dump(mode="json"))

        try:
            if route == "clarify":
                result = self._run_clarify(request, goal, turn_id, events, decision)
            elif route == "direct":
                event = progress("direct_answer", "Drafting a direct response.")
                yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
                result = self._run_direct(request, goal, turn_id, events, progress)
            elif route == "research":
                research = yield from self._run_research_subtree(request, progress)
                response = research["response"]
                result = AgentV3Result(
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
                data={"turn_id": turn_id, "message": "Agent v3 failed.", "detail": str(exc)},
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

    def _run_research_subtree(self, request: AgentV3Request, progress):
        event = progress(
            "research_planning",
            "Planning focused research questions.",
            available_tools=self.tool_registry.tool_names_for_route("research"),
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        plan = plan_research(request)
        event = progress(
            "research_plan",
            f"Research plan ready with {len(plan.search_queries)} search worker(s).",
            questions=plan.questions,
            search_queries=plan.search_queries,
            max_sources=plan.max_sources,
            source=plan.source,
            model_used=plan.model_used,
            fallback_reason=plan.fallback_reason,
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))

        search_sources: list[Source] = []
        tool_calls = []
        per_query_max = max(2, min(5, plan.max_sources))
        for idx, query in enumerate(plan.search_queries, start=1):
            event = progress(
                "search_worker",
                f"Search worker {idx} running.",
                worker_index=idx,
                query=query,
                candidate_queries=plan.search_queries,
            )
            yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
            sources, call = yield from self._run_tool(
                progress,
                "web_search",
                {"query": query, "max_results": per_query_max},
            )
            tool_calls.append(call)
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
                worker_index=idx,
                query=query,
                provider=provider,
                ok=call.ok,
                source_count=len(sources),
                error=call.error,
            )
            yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))

        deduped = self._merge_sources(search_sources, [])
        event = progress(
            "source_selection",
            f"Selected {min(len(deduped), plan.max_sources)} unique source candidate(s).",
            candidate_count=len(search_sources),
            unique_count=len(deduped),
            selected_urls=[source.url for source in deduped[: plan.max_sources]],
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))

        selected = deduped[: plan.max_sources]
        event = progress("source_reader", "Reading selected source pages.", source_count=len(selected))
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        extracted, read_call = yield from self._run_tool(
            progress,
            "read_url",
            {"urls": [source.url for source in selected if source.url]},
        )
        tool_calls.append(read_call)
        merged = self._merge_sources(selected, extracted)

        evidence = bind_evidence(merged, max_items=plan.max_sources)
        event = progress(
            "evidence_binder",
            f"Bound {len(evidence.items)} evidence item(s).",
            evidence_items=[item.model_dump(mode="json") for item in evidence.items],
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))

        event = progress("synthesis", "Synthesizing source-grounded answer from evidence.")
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        response = synthesize_answer(request, evidence)
        return {
            "sources": merged,
            "tool_calls": tool_calls,
            "evidence": evidence,
            "response": response,
        }

    def _apply_decision(self, request: AgentV3Request, decision: OrchestratorDecision) -> AgentV3Request:
        updates = {}
        if decision.output_format in {"chat", "markdown", "docx"}:
            updates["output_format"] = decision.output_format
        if decision.rewritten_request:
            updates["message"] = decision.rewritten_request
        return request.model_copy(update=updates) if updates else request

    def _run_clarify(
        self,
        request,
        goal,
        turn_id,
        events,
        decision: OrchestratorDecision,
    ) -> AgentV3Result:
        question = decision.clarification_question or "Can you clarify what you want me to do?"
        return AgentV3Result(
            turn_id=turn_id,
            goal=goal,
            answer=question,
            route="clarify",
            model_used=decision.model_used,
            events=events,
            latency_ms=decision.latency_ms,
            cost_usd=decision.cost_usd,
        )

    def _run_direct(self, request, goal, turn_id, events, progress) -> AgentV3Result:
        response = model_client.simple_completion(
            "You are Fronei v3, a concise and helpful assistant. Answer directly.",
            request.message,
            max_tokens=900,
        )
        return AgentV3Result(
            turn_id=turn_id,
            goal=goal,
            answer=response.text,
            route=goal.route,
            model_used=response.model_used,
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
    ) -> AgentV3Result:
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
            fallback_reason=plan.fallback_reason,
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))

        event = progress("document_writer", "Writing document draft.", plan_title=plan.title)
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        draft = write_document(request, plan, sources=sources, research_answer=research_answer, evidence=evidence)

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

        tool_name = choose_artifact_tool(request, plan)
        event = progress(
            "artifact_builder",
            f"Building artifact with {tool_name}.",
            tool_name=tool_name,
            title=plan.title,
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        artifact, artifact_call = build_artifact(self.tool_registry, plan, draft, tool_name)
        event = progress(
            "artifact_result",
            f"Artifact builder produced {artifact.filename}.",
            tool_name=artifact_call.name,
            filename=artifact.filename,
            ok=artifact_call.ok,
            error=artifact_call.error,
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        answer = f"Done. I created `{artifact.filename}` with the fresh Agent v3 runtime."
        return AgentV3Result(
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
