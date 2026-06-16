from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from app.services.agent_runtime.adapters import model_policy_to_route
from app.services.agent_runtime.guardrails import GuardrailService
from app.services.agent_runtime.judge_service import JudgeService
from app.services.agent_runtime.registry import RuntimeRegistry
from app.services.agent_runtime.tool_runner import (
    ToolCallResult,
    ToolExecutionError,
    ToolNotPermittedError,
    ToolRunner,
)
from app.services.agent_runtime.utils import effective_max_repair_iters, strip_json_fence
from app.services.turn_graph.research import (
    crawl_research_node,
    decompose_research_node,
    extract_research_node,
    search_research_node,
    sufficiency_research_node,
    synthesize_research_node,
    verify_research_node,
)
from app.services.turn_graph.state import TurnGraphState


logger = logging.getLogger(__name__)
MAX_SEARCH_QUERIES = 3
MAX_CRAWL_SOURCES = 3
MAX_CLAIMS_PER_SOURCE = 5
SUFFICIENCY_MIN_CLAIMS = 2
SUFFICIENCY_MIN_SOURCES = 2


@dataclass
class ResearchResult:
    answer: str
    sources: list[dict[str, str]]
    tool_calls: list[ToolCallResult]
    model_used: str
    prompt_id: str
    latency_ms: int
    cost_usd: float
    synthesis_latency_ms: int = 0
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))


class ResearchAgent:
    """Phase-K research_lead agent: decompose -> evidence -> synthesize."""

    def __init__(self, registry: RuntimeRegistry) -> None:
        self.registry = registry
        self.agent_def = registry.agent("research_lead")
        self.model_policy = registry.model_policy(self.agent_def.model_policy_id)
        self.prompt = registry.prompt(self.agent_def.prompt_template_id)

    def run(self, state: TurnGraphState, decision) -> ResearchResult:
        tool_runner = ToolRunner(
            registry=self.registry,
            agent_id="research_lead",
            guardrail_service=GuardrailService(self.registry),
        )
        tool_calls: list[ToolCallResult] = []

        state = decompose_research_node(
            state,
            fn=lambda s: self._decompose(s, decision, tool_calls),
        )
        queries = list(state.research_queries) or [state.user_message]

        state = search_research_node(
            state,
            fn=lambda s: self._scout(s, queries, tool_runner, tool_calls),
        )

        state = crawl_research_node(
            state,
            fn=lambda s: self._crawl(s, tool_runner, tool_calls),
        )

        state = extract_research_node(
            state,
            fn=lambda s: self._extract(s),
        )

        state = sufficiency_research_node(
            state,
            fn=lambda s: self._check_sufficiency(s),
        )

        synthesis_holder: list[Any] = []
        state = synthesize_research_node(
            state,
            fn=lambda s: self._synthesize_from_claims(s, decision, synthesis_holder),
        )

        self._judge_synthesis_loop(state, decision, synthesis_holder)
        synthesis_obj = synthesis_holder[0] if synthesis_holder else None

        state = verify_research_node(state)

        return self._build_result(state, tool_calls, synthesis_obj)

    def _decompose(
        self,
        state: TurnGraphState,
        decision,
        _tool_calls_log: list[ToolCallResult],
    ) -> dict[str, Any]:
        """Call the fast model to split the user query into focused sub-queries."""

        from app.services.llm_gateway import invoke_llm

        fallback = _extract_queries(decision.plan, state.user_message)
        try:
            fast_policy = self.registry.model_policy("model.fast")
            result = invoke_llm(
                message=state.user_message,
                route=model_policy_to_route(fast_policy),
                history=[],
                system_prompt=(
                    "You are a research planning assistant. "
                    "Given a user question, output a JSON object with a single key "
                    '"search_queries" whose value is an array of 2 to 5 concise, distinct '
                    "web search queries that together fully cover the question. "
                    "Output ONLY valid JSON with no other text."
                ),
            )
            raw = strip_json_fence((getattr(result, "answer", "") or "").strip())
            queries = [
                str(query)
                for query in json.loads(raw).get("search_queries", [])
                if query
            ][:MAX_SEARCH_QUERIES]
            state.research_queries = queries if queries else fallback
        except Exception:
            logger.warning("Query decomposition failed; using fallback queries for %r", state.user_message[:80])
            state.research_queries = fallback

        return {"queries": state.research_queries}

    def _scout(
        self,
        state: TurnGraphState,
        queries: list[str],
        tool_runner: ToolRunner,
        tool_calls_log: list[ToolCallResult],
    ) -> dict[str, Any]:
        """Run web_search for each sub-query; deduplicate sources by URL."""

        seen_urls: set[str] = set()
        all_sources: list[dict[str, Any]] = []

        for query in queries[:MAX_SEARCH_QUERIES]:
            try:
                result = tool_runner.run(
                    "web_search",
                    {"query": query, "max_results": 5},
                    state=state,
                )
                tool_calls_log.append(result)
                for source in _source_citations(result.output):
                    url = source.get("url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        all_sources.append(source)
            except (ToolNotPermittedError, ToolExecutionError) as exc:
                logger.warning("web_search failed for query=%r: %s", query, exc)

        state.research_sources = all_sources
        return {"sources_found": len(all_sources)}

    def _crawl(
        self,
        state: TurnGraphState,
        tool_runner: ToolRunner,
        tool_calls_log: list[ToolCallResult],
    ) -> dict[str, Any]:
        """Call read_url for the top MAX_CRAWL_SOURCES sources."""

        sources = list(state.research_sources or [])
        enriched: list[dict[str, Any]] = []

        for source in sources[:MAX_CRAWL_SOURCES]:
            url = source.get("url", "")
            if not url:
                enriched.append(source)
                continue
            try:
                result = tool_runner.run("read_url", {"url": url}, state=state)
                tool_calls_log.append(result)
                enriched.append({
                    **source,
                    "content": str(result.output.get("content") or "")[:4_000],
                })
            except (ToolNotPermittedError, ToolExecutionError) as exc:
                logger.warning("read_url failed for url=%r: %s", url, exc)
                enriched.append(source)

        enriched_urls = {source.get("url") for source in enriched}
        remaining = [
            source
            for source in sources[MAX_CRAWL_SOURCES:]
            if source.get("url") not in enriched_urls
        ]
        state.research_sources = enriched + remaining

        return {"sources_crawled": len(enriched)}

    def _extract(self, state: TurnGraphState) -> dict[str, Any]:
        """Call executive model to extract structured claims from crawled content."""

        from app.services.llm_gateway import invoke_llm

        content_blocks: list[str] = []
        for index, source in enumerate(state.research_sources or [], 1):
            content = source.get("content", "")
            if not content:
                continue
            title = source.get("title") or source.get("url", "")
            url = source.get("url", "")
            content_blocks.append(f"[Source {index}] {title} ({url})\n{content[:2_000]}")

        if not content_blocks:
            state.research_claims = []
            return {"claims_extracted": 0}

        combined = "\n\n---\n\n".join(content_blocks)
        try:
            exec_policy = self.registry.model_policy("model.executive")
            result = invoke_llm(
                message=(
                    f"Research question: {state.user_message}\n\n"
                    f"Source content:\n{combined}"
                ),
                route=model_policy_to_route(exec_policy),
                history=[],
                system_prompt=(
                    "You are an evidence extractor. "
                    "Given source content and a research question, extract the most relevant "
                    "factual claims. Output a JSON object with key \"claims\" whose value is an "
                    "array of objects, each with: "
                    "\"text\" (the claim as a complete sentence), "
                    "\"source_url\" (exact URL it came from), "
                    "\"confidence\" (float 0.0-1.0). "
                    f"Extract at most {MAX_CLAIMS_PER_SOURCE * MAX_CRAWL_SOURCES} total claims. "
                    "Output ONLY valid JSON with no other text."
                ),
            )
            raw = strip_json_fence((getattr(result, "answer", "") or "").strip())
            claims = [
                {
                    "text": str(claim.get("text", "")),
                    "source_url": str(claim.get("source_url", "")),
                    "confidence": float(claim.get("confidence", 0.5)),
                }
                for claim in (json.loads(raw).get("claims") or [])
                if isinstance(claim, dict) and claim.get("text")
            ]
            state.research_claims = claims
            return {"claims_extracted": len(claims)}
        except Exception:
            logger.warning("Evidence extraction failed; continuing without claims")
            state.research_claims = []
            return {"claims_extracted": 0}

    def _check_sufficiency(self, state: TurnGraphState) -> dict[str, Any]:
        """Heuristic sufficiency check. No model call."""

        claims_count = len(state.research_claims or [])
        sources_with_content = len([
            source for source in (state.research_sources or []) if source.get("content")
        ])
        sufficient = (
            claims_count >= SUFFICIENCY_MIN_CLAIMS
            and sources_with_content >= SUFFICIENCY_MIN_SOURCES
        )
        return {
            "sufficient": sufficient,
            "claims_count": claims_count,
            "sources_with_content": sources_with_content,
        }

    def _synthesize_from_claims(
        self,
        state: TurnGraphState,
        _decision,
        holder: list[Any],
    ) -> dict[str, Any]:
        """Synthesize final answer from extracted claims."""

        from app.services.llm_gateway import invoke_llm

        claims = state.research_claims or []
        sources = state.research_sources or []

        if claims:
            claims_text = "\n".join(
                f"- {claim['text']} (confidence: {claim['confidence']:.0%}, source: {claim['source_url']})"
                for claim in claims
            )
            web_context: str | None = f"Evidence:\n{claims_text}"
        else:
            web_context = _format_sources(sources) if sources else None

        try:
            result = invoke_llm(
                message=state.user_message,
                route=model_policy_to_route(self.model_policy),
                history=state.history[-8:] if state.history else [],
                web_context=web_context,
                planner_context=state.running_summary or None,
            )
            holder.append(result)
            return {
                "model_used": getattr(result, "model_used", ""),
                "latency_ms": getattr(result, "latency_ms", 0),
            }
        except Exception:
            logger.exception("Research synthesis failed; returning fail-soft response")
            fallback = SimpleNamespace(
                answer=(
                    "I couldn't complete the research synthesis right now. "
                    "Please retry the research request."
                ),
                model_used="unavailable",
                latency_ms=0,
                estimated_cost_usd=0.0,
            )
            holder.append(fallback)
            return {"model_used": "unavailable"}

    def _judge_synthesis_loop(
        self,
        state: TurnGraphState,
        decision,
        synthesis_holder: list[Any],
    ) -> None:
        """Evaluate synthesis and repair in place when the judge requests it."""

        judge_policy_id = self.agent_def.judge_policy_id
        if not judge_policy_id:
            return

        synthesis_obj = synthesis_holder[0] if synthesis_holder else None
        answer = getattr(synthesis_obj, "answer", "") or ""
        sources_summary = _format_sources([
            {"title": source.get("title", ""), "url": source.get("url", "")}
            for source in (state.research_sources or [])[:10]
            if source.get("url")
        ])

        judge_result = JudgeService(self.registry).evaluate(
            judge_policy_id,
            content=answer,
            context={
                "user_question": state.user_message,
                "sources_summary": sources_summary,
            },
            target_id=str(getattr(state, "turn_id", "") or ""),
        )
        logger.info(
            "Research judge [0]: policy=%s status=%s score=%.2f",
            judge_policy_id,
            judge_result.status,
            judge_result.score,
        )
        if judge_result.status != "repair":
            return

        quality_mode = getattr(state, "quality_mode", None) or "standard"
        policy = self.registry.judges.get(judge_policy_id)
        max_iters = effective_max_repair_iters(quality_mode, policy)
        if max_iters == 0:
            logger.info("Research judge repair skipped: quality_mode=%s", quality_mode)
            return

        for attempt in range(max_iters):
            logger.info(
                "Research judge repair %d/%d: re-synthesizing (repairs=%s)",
                attempt + 1,
                max_iters,
                judge_result.required_repairs,
            )
            repaired = self._resynthesize_with_repairs(
                state,
                decision,
                judge_result.required_repairs,
            )
            if repaired is not None:
                if synthesis_holder:
                    synthesis_holder[0] = repaired
                else:
                    synthesis_holder.append(repaired)
                answer = getattr(repaired, "answer", "") or ""

            judge_result = JudgeService(self.registry).evaluate(
                judge_policy_id,
                content=answer,
                context={
                    "user_question": state.user_message,
                    "sources_summary": sources_summary,
                },
                target_id=str(getattr(state, "turn_id", "") or ""),
            )
            logger.info(
                "Research judge [%d]: policy=%s status=%s score=%.2f",
                attempt + 1,
                judge_policy_id,
                judge_result.status,
                judge_result.score,
            )
            if judge_result.status != "repair":
                break

    def _resynthesize_with_repairs(
        self,
        state: TurnGraphState,
        _decision,
        required_repairs: list[dict[str, Any]],
    ) -> Any | None:
        """Re-run research synthesis with repair instructions. Never raises."""

        from app.services.llm_gateway import invoke_llm

        claims = state.research_claims or []
        sources = state.research_sources or []

        if claims:
            claims_text = "\n".join(
                f"- {claim['text']} (confidence: {claim['confidence']:.0%}, source: {claim['source_url']})"
                for claim in claims
            )
            web_context: str | None = f"Evidence:\n{claims_text}"
        else:
            web_context = _format_sources(sources) if sources else None

        repair_note = (
            "REVISION REQUIRED. The previous synthesis was evaluated and needs improvement:\n"
            + "\n".join(f"- {_repair_instruction_text(repair)}" for repair in required_repairs)
            + "\nAddress each point in your revised synthesis."
        )
        web_context = f"{repair_note}\n\n{web_context}" if web_context else repair_note

        try:
            # TODO(new-phase-m): Replace this direct LLM call with
            # SubAgentRunner("research_synthesizer", self.registry) once the
            # true sub-agent runtime is in place.
            return invoke_llm(
                message=state.user_message,
                route=model_policy_to_route(self.model_policy),
                history=state.history[-8:] if state.history else [],
                web_context=web_context,
                planner_context=state.running_summary or None,
            )
        except Exception:
            logger.exception("Research re-synthesis failed; retaining previous answer")
            return None

    def _build_result(
        self,
        state: TurnGraphState,
        tool_calls: list[ToolCallResult],
        synthesis: Any,
    ) -> ResearchResult:
        answer = getattr(synthesis, "answer", "") or ""
        model_used = getattr(synthesis, "model_used", "") or ""
        synthesis_latency = getattr(synthesis, "latency_ms", 0) or 0
        cost = getattr(synthesis, "estimated_cost_usd", 0.0) or 0.0
        tool_latency = sum(call.latency_ms for call in tool_calls)

        sources = [
            {"title": source.get("title", ""), "url": source.get("url", "")}
            for source in (state.research_sources or [])
            if source.get("url")
        ]

        return ResearchResult(
            answer=answer,
            sources=sources[:10],
            tool_calls=tool_calls,
            model_used=model_used,
            prompt_id=self.prompt.id,
            latency_ms=tool_latency + synthesis_latency,
            synthesis_latency_ms=synthesis_latency,
            cost_usd=cost,
        )


def _extract_queries(plan: dict[str, Any], fallback: str) -> list[str]:
    if not isinstance(plan, dict):
        return [fallback]
    queries = plan.get("search_queries") or plan.get("queries") or []
    if isinstance(queries, list) and queries:
        return [str(query) for query in queries if query]
    return [fallback]


def _source_citations(output: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"title": str(source.get("title", "")), "url": str(source.get("url", ""))}
        for source in (output.get("sources") or [])
        if isinstance(source, dict) and source.get("url")
    ]


def _format_sources(sources: list[dict[str, str]]) -> str:
    lines: list[str] = []
    for index, source in enumerate(sources[:10], 1):
        title = source.get("title") or source.get("url", "")
        url = source.get("url", "")
        lines.append(f"[{index}] {title} - {url}")
    return "\n".join(lines)


def _repair_instruction_text(repair: dict[str, Any] | str) -> str:
    if isinstance(repair, dict):
        section = str(repair.get("section", "")).strip()
        instruction = str(repair.get("instruction") or repair.get("message") or "").strip()
        if section and instruction:
            return f"{section}: {instruction}"
        return instruction or section or json.dumps(repair, sort_keys=True)
    return str(repair)
