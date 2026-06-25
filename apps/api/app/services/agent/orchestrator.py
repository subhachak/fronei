from __future__ import annotations

import json
import logging
import re
from typing import Literal

from pydantic import BaseModel, Field

from app.services.agent import model_client
from app.services.agent.models import TurnRequest, ResearchLevel, RouteName

logger = logging.getLogger(__name__)


class OrchestratorDecision(BaseModel):
    route: RouteName
    confidence: float = Field(ge=0.0, le=1.0, default=0.6)
    reason: str = ""
    clarification_question: str | None = None
    output_format: str | None = None
    research_level: Literal["easy", "regular", "deep"] = "regular"
    requires_confirmation: bool = False
    confirmation_message: str | None = None
    fallback_research_level: Literal["easy", "regular"] = "regular"
    rewritten_request: str | None = None
    model_used: str = ""
    latency_ms: int = 0
    cost_usd: float = 0.0
    source: str = "llm"
    available_routes: list[str] = Field(default_factory=list)
    available_tools: list[str] = Field(default_factory=list)
    fallback_reason: str | None = None


SYSTEM_PROMPT = """You are Fronei's orchestration agent.

Choose exactly one route:
- direct: answer from general knowledge; no tool required.
- clarify: the user request is ambiguous, risky, under-specified, or missing a required target.
- research: the answer needs current, source-grounded, market, pricing, legal, financial, product, vendor, or time-sensitive information.
- document: the user primarily wants a document/report/memo/deck, and enough content is already provided.
- research_document: the user wants a document/report/memo/deck and the contents need source-grounded research first.

If the route is research or research_document, also choose exactly one research_level:
- easy: very narrow freshness check or simple sourced lookup; minimal web use.
- regular: normal source-grounded research, comparison, recommendation, or briefing.
- deep: broad/high-stakes/document-grade investigation with many sources, verification, and repair.

Deep research is expensive and slower. Use deep only for explicit deep/comprehensive asks, high-stakes domains,
strategic/business/legal/financial/regulatory work, vendor/investment decisions, or broad document-grade research.
When research_level is deep, set requires_confirmation=true.

Return only compact JSON with this schema:
{
  "route": "direct|clarify|research|document|research_document",
  "confidence": 0.0-1.0,
  "reason": "short reason",
  "clarification_question": "required only for clarify",
  "output_format": "chat|markdown|docx|pptx|null",
  "research_level": "easy|regular|deep",
  "requires_confirmation": true|false,
  "confirmation_message": "required only when requires_confirmation is true",
  "fallback_research_level": "easy|regular",
  "rewritten_request": "optional clearer version of the user request"
}
"""


def decide(request: TurnRequest) -> OrchestratorDecision:
    return decide_with_options(
        request,
        available_routes=["direct", "clarify", "research", "document", "research_document"],
        available_tools=[],
    )


def decide_with_options(
    request: TurnRequest,
    *,
    available_routes: list[str],
    available_tools: list[str],
) -> OrchestratorDecision:
    if request.force_route:
        return _normalize_research_decision(request, OrchestratorDecision(
            route=request.force_route,
            confidence=1.0,
            reason="User explicitly forced the route.",
            output_format=request.output_format,
            source="forced",
            available_routes=available_routes,
            available_tools=available_tools,
        ))

    user_payload = json.dumps(
        {
            "message": request.message,
            "conversation_context": request.conversation_context[-5000:] if request.conversation_context else "",
            "quality_mode": request.quality_mode,
            "requested_research_level": request.research_level,
            "deep_research_confirmed": request.confirm_deep_research,
            "requested_output_format": request.output_format,
            "available_routes": available_routes,
            "available_tools": available_tools,
        },
        ensure_ascii=False,
    )
    try:
        response = model_client.complete(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_payload},
            ],
            role="orchestrator",
            quality_mode=request.quality_mode,
            overrides=request.model_overrides,
            max_tokens=400,
            timeout_s=18,
        )
        parsed = _parse_json(response.text)
        if parsed.get("route") in {"research", "research_document"} and not parsed.get("research_level"):
            parsed["research_level"] = choose_research_level(request, parsed["route"])
        decision = OrchestratorDecision.model_validate(parsed)
        decision.model_used = response.model_used
        decision.latency_ms = response.latency_ms
        decision.cost_usd = response.cost_usd
        decision.source = "llm"
        decision.available_routes = available_routes
        decision.available_tools = available_tools
        decision = _normalize_research_decision(request, decision)
        if decision.route == "clarify" and not decision.clarification_question:
            decision.clarification_question = "Can you clarify what outcome you want and any constraints I should follow?"
        return decision
    except Exception as exc:
        logger.warning("agent orchestrator failed; using fallback route: %s", exc)
        fallback = heuristic_decide(request, available_routes=available_routes, available_tools=available_tools)
        fallback.reason = f"{fallback.reason} Orchestrator fallback after model failure."
        fallback.fallback_reason = str(exc)
        return fallback


def heuristic_decide(
    request: TurnRequest,
    *,
    available_routes: list[str] | None = None,
    available_tools: list[str] | None = None,
) -> OrchestratorDecision:
    available_routes = available_routes or ["direct", "clarify", "research", "document", "research_document"]
    available_tools = available_tools or []
    text = request.message.lower()
    asks_doc = any(term in text for term in ["document", "report", "docx", "memo", "briefing", "deck", "ppt", "slides", "presentation", "powerpoint"])
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
    if _looks_too_vague(text):
        return OrchestratorDecision(
            route="clarify",
            confidence=0.72,
            reason="The request is too vague to execute safely.",
            clarification_question="What topic or outcome should I focus on?",
            source="heuristic",
            available_routes=available_routes,
            available_tools=available_tools,
        )
    if asks_research and asks_doc:
        route: RouteName = "research_document"
    elif asks_research:
        route = "research"
    elif asks_doc or request.output_format in {"docx", "markdown", "pptx"}:
        route = "document"
    else:
        route = "direct"
    research_level = choose_research_level(request, route)
    return OrchestratorDecision(
        route=route,
        confidence=0.64,
        reason="Deterministic fallback route based on request shape.",
        output_format=request.output_format,
        research_level=research_level,
        requires_confirmation=route in {"research", "research_document"} and research_level == "deep",
        confirmation_message=_deep_confirmation_message() if research_level == "deep" else None,
        fallback_research_level="regular",
        source="heuristic",
        available_routes=available_routes,
        available_tools=available_tools,
    )


def choose_research_level(request: TurnRequest, route: RouteName) -> Literal["easy", "regular", "deep"]:
    if route not in {"research", "research_document"}:
        return "regular"
    if request.research_level in {"easy", "regular", "deep"}:
        return request.research_level  # type: ignore[return-value]
    text = request.message.lower()
    high_stakes_terms = [
        "legal",
        "regulatory",
        "compliance",
        "financial",
        "investment",
        "vendor selection",
        "board",
        "ciso",
        "cio",
        "risk",
        "strategy",
        "market analysis",
        "operating model",
        "business model",
    ]
    deep_terms = [
        "deep",
        "comprehensive",
        "full analysis",
        "detailed report",
        "exhaustive",
        "thorough",
        "investment memo",
        "board-ready",
    ]
    easy_terms = ["quick", "briefly", "check", "current", "latest", "what is", "when is", "find out"]
    asks_doc = any(term in text for term in ["document", "report", "docx", "memo", "briefing", "deck", "ppt", "slides", "presentation", "powerpoint"])
    if any(term in text for term in deep_terms) or any(term in text for term in high_stakes_terms):
        return "deep"
    explicit_research = "research" in text
    if asks_doc and route == "research_document":
        return "regular"
    if not explicit_research and any(term in text for term in easy_terms) and len(text.split()) <= 18:
        return "easy"
    return "regular"


def _normalize_research_decision(request: TurnRequest, decision: OrchestratorDecision) -> OrchestratorDecision:
    text = request.message.lower()
    asks_research = "research" in text or decision.route in {"research", "research_document"}
    asks_doc = any(
        term in text
        for term in ["document", "report", "docx", "memo", "briefing", "deck", "ppt", "slides", "presentation", "powerpoint"]
    )
    if decision.route == "research" and asks_research and asks_doc:
        decision.route = "research_document"
        decision.output_format = decision.output_format or request.output_format
    if request.research_level in {"easy", "regular", "deep"}:
        decision.research_level = request.research_level  # type: ignore[assignment]
    elif decision.route in {"research", "research_document"}:
        decision.research_level = choose_research_level(request, decision.route)
    else:
        decision.research_level = "regular"
    if decision.route in {"research", "research_document"} and decision.research_level == "deep":
        decision.requires_confirmation = True
        decision.confirmation_message = decision.confirmation_message or _deep_confirmation_message()
    elif decision.research_level != "deep":
        decision.requires_confirmation = False
        decision.confirmation_message = None
    return decision


def _deep_confirmation_message() -> str:
    return (
        "This looks like deep research because it needs broader source coverage, evidence checking, "
        "and synthesis. I can run it in deep mode, which may take a few minutes. Continue with deep "
        "research, use regular research instead, or answer directly?"
    )


def _parse_json(raw: str) -> dict:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def _looks_too_vague(text: str) -> bool:
    words = [w for w in re.split(r"\W+", text) if w]
    if len(words) <= 2 and any(w in {"it", "this", "that", "them", "better", "fix", "research"} for w in words):
        return True
    return text.strip() in {"help", "do it", "make it better", "research it", "create it"}
