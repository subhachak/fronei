from __future__ import annotations

import time
from collections.abc import Iterator

from app.services.agent_v3 import model_client
from app.services.agent_v3.models import (
    AgentV3Request,
    AgentV3Result,
    Goal,
    ProgressEvent,
    RouteName,
    Source,
    StreamEnvelope,
    new_id,
)
from app.services.agent_v3.tools import AgentV3Tools, source_context


class AgentV3Runtime:
    """Fresh isolated runtime with no dependency on the legacy/hybrid pipelines."""

    def __init__(self, tools: AgentV3Tools | None = None):
        self.tools = tools or AgentV3Tools.from_settings()

    def run_stream(self, request: AgentV3Request, *, user_id: str) -> Iterator[StreamEnvelope]:
        turn_id = new_id("turn")
        started = time.perf_counter()
        route = request.force_route or self._route(request)
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

        first = progress("orchestrator", f"Fresh runtime selected the {route} route.", route=route)
        yield StreamEnvelope(type="progress", data=first.model_dump(mode="json"))

        try:
            if route == "direct":
                event = progress("direct_answer", "Drafting a direct response.")
                yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
                result = self._run_direct(request, goal, turn_id, events, progress)
            elif route == "research":
                event = progress("research", "Searching the web with the fresh v3 tool runner.")
                yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
                sources, search_call = self.tools.search_web(
                    request.message,
                    max_results=8 if request.quality_mode == "executive" else 5,
                )
                event = progress("research", f"Found {len(sources)} candidate sources.", source_count=len(sources))
                yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
                extracted, read_call = self.tools.extract_urls([s.url for s in sources if s.url])
                merged = self._merge_sources(sources, extracted)
                event = progress("research", f"Read {len(extracted)} source pages.", source_count=len(extracted))
                yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
                event = progress("synthesis", "Synthesizing source-grounded answer.")
                yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
                response = self._synthesize_research(request, merged)
                result = AgentV3Result(
                    turn_id=turn_id,
                    goal=goal,
                    answer=response.text,
                    route=goal.route,
                    model_used=response.model_used,
                    sources=merged,
                    tool_calls=[search_call, read_call],
                    events=events,
                    latency_ms=response.latency_ms + search_call.latency_ms + read_call.latency_ms,
                    cost_usd=response.cost_usd,
                )
            elif route == "document":
                event = progress("document", "Composing a standalone document artifact.")
                yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
                result = self._run_document(request, goal, turn_id, events, progress, sources=[])
            else:
                event = progress("research", "Searching before writing the document.")
                yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
                sources, search_call = self.tools.search_web(request.message, max_results=8)
                event = progress("research", f"Found {len(sources)} candidate sources.", source_count=len(sources))
                yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
                extracted, read_call = self.tools.extract_urls([s.url for s in sources if s.url])
                merged = self._merge_sources(sources, extracted)
                event = progress("research", f"Read {len(extracted)} source pages.", source_count=len(extracted))
                yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
                event = progress("synthesis", "Synthesizing research before drafting.")
                yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
                research_response = self._synthesize_research(request, merged)
                event = progress("document", "Writing the downloadable document.")
                yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
                result = self._run_document(
                    request,
                    goal,
                    turn_id,
                    events,
                    progress,
                    sources=merged,
                    research_answer=research_response.text,
                )
                result.tool_calls = [search_call, read_call]
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

    def _route(self, request: AgentV3Request) -> RouteName:
        text = request.message.lower()
        asks_doc = any(term in text for term in ["document", "report", "docx", "memo", "briefing", "deck", "ppt"])
        asks_research = any(
            term in text
            for term in [
                "research",
                "sources",
                "current",
                "latest",
                "market",
                "compare",
                "benchmark",
                "recent",
                "citations",
            ]
        )
        if asks_research and asks_doc:
            return "research_document"
        if asks_research:
            return "research"
        if asks_doc or request.output_format in {"docx", "markdown"}:
            return "document"
        return "direct"

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

    def _synthesize_research(self, request: AgentV3Request, sources: list[Source]):
        context = source_context(sources)
        return model_client.simple_completion(
            (
                "You are a source-grounded research analyst. Use the supplied sources, "
                "cite claims with [S#], and say when evidence is thin."
            ),
            f"Question:\n{request.message}\n\nSources:\n{context or 'No sources available.'}",
            max_tokens=1800 if request.quality_mode == "executive" else 1200,
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
    ) -> AgentV3Result:
        context = source_context(sources)
        prompt = (
            f"User request:\n{request.message}\n\n"
            f"Research summary:\n{research_answer or ''}\n\n"
            f"Sources:\n{context}\n\n"
            "Write a polished, structured markdown document with clear headings."
        )
        response = model_client.simple_completion(
            "You are a document-writing agent. Produce only the document body in markdown.",
            prompt,
            max_tokens=2200,
        )
        title = self._title_from_message(request.message)
        artifact = (
            self.tools.make_docx_artifact(title, response.text)
            if request.output_format in {"docx", "chat"} or "docx" in request.message.lower()
            else self.tools.make_markdown_artifact(title, response.text)
        )
        answer = f"Done. I created `{artifact.filename}` with the fresh Agent v3 runtime."
        return AgentV3Result(
            turn_id=turn_id,
            goal=goal,
            answer=answer,
            route=goal.route,
            model_used=response.model_used,
            sources=sources,
            artifacts=[artifact],
            events=events,
            latency_ms=response.latency_ms,
            cost_usd=response.cost_usd,
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

    def _title_from_message(self, message: str) -> str:
        cleaned = " ".join(message.replace("\n", " ").split())
        return cleaned[:80].strip(" .") or "Agent v3 document"
