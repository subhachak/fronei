from __future__ import annotations

import json
import logging
import re

from pydantic import BaseModel, Field

from app.services.agent_v3 import model_client
from app.services.agent_v3.models import AgentV3Request, RouteName

logger = logging.getLogger(__name__)


class OrchestratorDecision(BaseModel):
    route: RouteName
    confidence: float = Field(ge=0.0, le=1.0, default=0.6)
    reason: str = ""
    clarification_question: str | None = None
    output_format: str | None = None
    rewritten_request: str | None = None
    model_used: str = ""
    latency_ms: int = 0
    cost_usd: float = 0.0
    source: str = "llm"


SYSTEM_PROMPT = """You are the fresh Fronei Agent v3 orchestrator.

Choose exactly one route:
- direct: answer from general knowledge; no tool required.
- clarify: the user request is ambiguous, risky, under-specified, or missing a required target.
- research: the answer needs current, source-grounded, market, pricing, legal, financial, product, vendor, or time-sensitive information.
- document: the user primarily wants a document/report/memo/deck, and enough content is already provided.
- research_document: the user wants a document/report/memo/deck and the contents need source-grounded research first.

Return only compact JSON with this schema:
{
  "route": "direct|clarify|research|document|research_document",
  "confidence": 0.0-1.0,
  "reason": "short reason",
  "clarification_question": "required only for clarify",
  "output_format": "chat|markdown|docx|null",
  "rewritten_request": "optional clearer version of the user request"
}
"""


def decide(request: AgentV3Request) -> OrchestratorDecision:
    if request.force_route:
        return OrchestratorDecision(
            route=request.force_route,
            confidence=1.0,
            reason="User explicitly forced the route.",
            output_format=request.output_format,
            source="forced",
        )

    user_payload = json.dumps(
        {
            "message": request.message,
            "quality_mode": request.quality_mode,
            "requested_output_format": request.output_format,
        },
        ensure_ascii=False,
    )
    try:
        response = model_client.complete(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_payload},
            ],
            max_tokens=400,
            timeout_s=18,
        )
        parsed = _parse_json(response.text)
        decision = OrchestratorDecision.model_validate(parsed)
        decision.model_used = response.model_used
        decision.latency_ms = response.latency_ms
        decision.cost_usd = response.cost_usd
        decision.source = "llm"
        if decision.route == "clarify" and not decision.clarification_question:
            decision.clarification_question = "Can you clarify what outcome you want and any constraints I should follow?"
        return decision
    except Exception as exc:
        logger.warning("agent_v3 orchestrator failed; using fallback route: %s", exc)
        fallback = heuristic_decide(request)
        fallback.reason = f"{fallback.reason} Orchestrator fallback after model failure."
        return fallback


def heuristic_decide(request: AgentV3Request) -> OrchestratorDecision:
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
    if _looks_too_vague(text):
        return OrchestratorDecision(
            route="clarify",
            confidence=0.72,
            reason="The request is too vague to execute safely.",
            clarification_question="What topic or outcome should I focus on?",
            source="heuristic",
        )
    if asks_research and asks_doc:
        route: RouteName = "research_document"
    elif asks_research:
        route = "research"
    elif asks_doc or request.output_format in {"docx", "markdown"}:
        route = "document"
    else:
        route = "direct"
    return OrchestratorDecision(
        route=route,
        confidence=0.64,
        reason="Deterministic fallback route based on request shape.",
        output_format=request.output_format,
        source="heuristic",
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
