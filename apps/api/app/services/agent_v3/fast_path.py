from __future__ import annotations

import json
import logging
import re
from typing import Literal

from pydantic import BaseModel, Field

from app.services.agent_v3 import model_client
from app.services.agent_v3.models import AgentV3Request, Source
from app.services.agent_v3.tools import source_context

logger = logging.getLogger(__name__)


FastPathName = Literal["direct_fast", "web_fast", "agentic"]


class FastPathDecision(BaseModel):
    path: FastPathName = "agentic"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""
    web_query: str | None = None
    needs_clarification: bool = False
    clarification_question: str | None = None
    model_used: str = ""
    latency_ms: int = 0
    cost_usd: float = 0.0
    source: str = "llm"
    fallback_reason: str | None = None


FAST_ROUTER_PROMPT = """You are Fronei's fast path router.

Choose the fastest safe path:
- direct_fast: answer from general knowledge plus the current conversation context.
- web_fast: one quick web lookup is useful for current/light factual information.
- agentic: use the full agentic runtime for ambiguity, high stakes, deep research, documents, artifacts, tools, or multi-step work.

Rules:
- If requested_output_format is not "chat", choose agentic.
- If the user asks for a document, deck, report file, DOCX, PPTX, deep research, broad comparison, strategy, legal/regulatory/financial advice, or high-stakes decision support, choose agentic.
- Use web_fast only for quick current facts or one narrow lookup. Do not use it for full research.
- For vague follow-ups, use direct_fast only if the current conversation context clearly contains the target. Otherwise choose agentic.

Return only compact JSON:
{
  "path": "direct_fast|web_fast|agentic",
  "confidence": 0.0-1.0,
  "reason": "short reason",
  "web_query": "only for web_fast",
  "needs_clarification": false,
  "clarification_question": null
}
"""


DIRECT_FAST_PROMPT = """You are Fronei, a fast and helpful general-purpose assistant.

Answer the current user request directly. Use the provided current-conversation context for pronouns and follow-ups, but do not import unrelated workspace topics. Be concise unless the user asks for depth.
"""


WEB_FAST_PROMPT = """You are Fronei, answering with a quick web check.

Use only the provided source context for current factual claims. Keep the answer concise and include source links naturally. If the sources are insufficient, say what is missing and avoid pretending this was full deep research.
"""


def decide_fast_path(request: AgentV3Request) -> FastPathDecision:
    if request.force_route:
        return FastPathDecision(
            path="agentic",
            confidence=1.0,
            reason="A route was explicitly forced.",
            source="guardrail",
        )
    if request.output_format != "chat":
        return FastPathDecision(
            path="agentic",
            confidence=1.0,
            reason="Non-chat output should use the full agentic runtime.",
            source="guardrail",
        )
    if request.confirm_deep_research or request.research_level == "deep":
        return FastPathDecision(
            path="agentic",
            confidence=1.0,
            reason="Deep research confirmation or explicit deep mode requires the full runtime.",
            source="guardrail",
        )
    payload = json.dumps(
        {
            "message": request.message,
            "conversation_context": request.conversation_context[-3500:] if request.conversation_context else "",
            "quality_mode": request.quality_mode,
            "requested_research_level": request.research_level,
            "requested_output_format": request.output_format,
        },
        ensure_ascii=False,
    )
    try:
        response = model_client.complete(
            [
                {"role": "system", "content": FAST_ROUTER_PROMPT},
                {"role": "user", "content": payload},
            ],
            role="fast_router",
            quality_mode=request.quality_mode,
            max_tokens=260,
            timeout_s=8,
        )
        parsed = _parse_json(response.text)
        decision = FastPathDecision.model_validate(parsed)
        decision.model_used = response.model_used
        decision.latency_ms = response.latency_ms
        decision.cost_usd = response.cost_usd
        decision.source = "llm"
        return _normalize_fast_decision(request, decision)
    except Exception as exc:
        logger.warning("agent_v3 fast router failed; using fallback: %s", exc)
        fallback = heuristic_fast_path(request)
        fallback.fallback_reason = str(exc)
        return fallback


def heuristic_fast_path(request: AgentV3Request) -> FastPathDecision:
    text = request.message.lower()
    if request.output_format != "chat" or request.force_route:
        return FastPathDecision(path="agentic", confidence=1.0, reason="Fast path guardrail.", source="heuristic")
    if any(term in text for term in _agentic_terms()):
        return FastPathDecision(path="agentic", confidence=0.72, reason="The request looks multi-step or work-product oriented.", source="heuristic")
    if any(term in text for term in _web_terms()):
        return FastPathDecision(
            path="web_fast",
            confidence=0.68,
            reason="The request asks for a narrow current lookup.",
            web_query=_clean_web_query(request.message),
            source="heuristic",
        )
    return FastPathDecision(path="direct_fast", confidence=0.7, reason="The request looks like ordinary chat.", source="heuristic")


def answer_direct_fast(request: AgentV3Request) -> model_client.ModelResponse:
    user_prompt = request.message
    if request.conversation_context:
        user_prompt = f"{request.conversation_context}\n\nCurrent user request:\n{request.message}"
    return model_client.simple_completion(
        DIRECT_FAST_PROMPT,
        user_prompt,
        max_tokens=900,
        role="direct_answer",
        quality_mode=request.quality_mode,
        timeout_s=14,
    )


def answer_web_fast(
    request: AgentV3Request,
    *,
    web_query: str,
    sources: list[Source],
    extracted_sources: list[Source],
) -> model_client.ModelResponse:
    merged_sources = _merge_sources(sources, extracted_sources)
    context = source_context(merged_sources[:3])
    user_prompt = json.dumps(
        {
            "message": request.message,
            "web_query": web_query,
            "source_context": context,
            "conversation_context": request.conversation_context[-1800:] if request.conversation_context else "",
        },
        ensure_ascii=False,
    )
    return model_client.simple_completion(
        WEB_FAST_PROMPT,
        user_prompt,
        max_tokens=1000,
        role="direct_answer",
        quality_mode=request.quality_mode,
        timeout_s=16,
    )


def _normalize_fast_decision(request: AgentV3Request, decision: FastPathDecision) -> FastPathDecision:
    if request.output_format != "chat":
        decision.path = "agentic"
        decision.reason = "Non-chat output should use the full agentic runtime."
        return decision
    text = request.message.lower()
    if decision.path in {"direct_fast", "web_fast"} and any(term in text for term in _agentic_terms()):
        decision.path = "agentic"
        decision.reason = "Escalated because the request asks for agentic work."
        return decision
    if decision.path == "web_fast" and not decision.web_query:
        decision.web_query = _clean_web_query(request.message)
    if decision.path in {"direct_fast", "web_fast"} and decision.confidence < 0.55:
        decision.path = "agentic"
        decision.reason = "Fast router confidence was too low."
    return decision


def _parse_json(raw: str) -> dict:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    return json.loads(cleaned)


def _clean_web_query(message: str) -> str:
    return " ".join((message or "").replace("\n", " ").split())[:240]


def _web_terms() -> list[str]:
    return [
        "latest",
        "current",
        "today",
        "recent",
        "pricing",
        "price",
        "who is",
        "who's",
        "release",
        "announced",
        "news",
    ]


def _agentic_terms() -> list[str]:
    return [
        "deep research",
        "research",
        "investigate",
        "comprehensive",
        "detailed report",
        "report",
        "docx",
        "document",
        "deck",
        "ppt",
        "compare",
        "benchmark",
        "vendor selection",
        "legal",
        "regulatory",
        "compliance",
        "financial",
        "investment",
        "board",
        "risk matrix",
    ]


def _merge_sources(search_sources: list[Source], extracted_sources: list[Source]) -> list[Source]:
    by_url = {source.url: source for source in search_sources if source.url}
    for source in extracted_sources:
        if source.url in by_url:
            by_url[source.url].content = source.content
            if source.title:
                by_url[source.url].title = source.title
        elif source.url:
            by_url[source.url] = source
    return list(by_url.values())
