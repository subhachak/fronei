from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from app.services.agent_runtime.adapters import model_policy_to_route
from app.services.agent_runtime.guardrails import GuardrailService
from app.services.agent_runtime.registry import RuntimeRegistry
from app.services.agent_runtime.tool_runner import (
    ToolCallResult,
    ToolExecutionError,
    ToolNotPermittedError,
    ToolRunner,
)
from app.services.turn_graph.state import TurnGraphState


logger = logging.getLogger(__name__)
MAX_SEARCH_QUERIES = 3


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
    """Phase-E research_lead agent: web_search -> synthesize."""

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
        queries = _extract_queries(decision.plan, state.user_message)
        tool_calls: list[ToolCallResult] = []
        all_sources: list[dict[str, str]] = []

        for query in queries[:MAX_SEARCH_QUERIES]:
            try:
                result = tool_runner.run("web_search", {"query": query, "max_results": 5}, state=state)
                tool_calls.append(result)
                all_sources.extend(_source_citations(result.output))
            except (ToolNotPermittedError, ToolExecutionError) as exc:
                logger.warning("Research tool call failed for query=%r: %s", query, exc)

        try:
            synthesis = self._synthesize(state, all_sources, decision)
        except Exception:
            logger.exception("Research synthesis failed; returning fail-soft response")
            synthesis = SimpleNamespace(
                answer=(
                    "I couldn't complete the research synthesis right now. "
                    "Please retry the research request."
                ),
                model_used="unavailable",
                latency_ms=0,
                estimated_cost_usd=0.0,
            )
        return ResearchResult(
            answer=synthesis.answer,
            sources=all_sources[:10],
            tool_calls=tool_calls,
            model_used=synthesis.model_used,
            prompt_id=self.prompt.id,
            latency_ms=sum(call.latency_ms for call in tool_calls) + synthesis.latency_ms,
            synthesis_latency_ms=synthesis.latency_ms,
            cost_usd=synthesis.estimated_cost_usd or 0.0,
        )

    def _synthesize(self, state: TurnGraphState, sources: list[dict], decision) -> Any:
        from app.services.llm_gateway import invoke_llm

        return invoke_llm(
            message=state.user_message,
            route=model_policy_to_route(self.model_policy),
            history=state.history[-8:] if state.history else [],
            web_context=_format_sources(sources) if sources else None,
            planner_context=state.running_summary or None,
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
