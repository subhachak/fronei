from __future__ import annotations

import json
import logging
import re

from pydantic import BaseModel, Field

from app.services.agent_v3 import model_client
from app.services.agent_v3.models import AgentV3Request, Source

logger = logging.getLogger(__name__)


class ResearchPlan(BaseModel):
    questions: list[str] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list)
    max_sources: int = 6
    source: str = "llm"
    model_used: str = ""
    latency_ms: int = 0
    cost_usd: float = 0.0
    fallback_reason: str | None = None


class EvidenceItem(BaseModel):
    source_id: str
    title: str = ""
    url: str = ""
    evidence: str = ""


class EvidencePack(BaseModel):
    items: list[EvidenceItem] = Field(default_factory=list)


PLAN_PROMPT = """You are the Agent v3 research lead.

Create a compact research plan for the user request. Return only JSON:
{
  "questions": ["2-4 focused research questions"],
  "search_queries": ["2-4 precise web search queries"],
  "max_sources": 4-8
}
Prefer sourceable, specific questions. Do not answer the request.
"""


def plan_research(request: AgentV3Request) -> ResearchPlan:
    try:
        response = model_client.complete(
            [
                {"role": "system", "content": PLAN_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "message": request.message,
                            "quality_mode": request.quality_mode,
                            "output_format": request.output_format,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            max_tokens=600,
            timeout_s=20,
        )
        payload = _parse_json(response.text)
        plan = ResearchPlan.model_validate(payload)
        plan.model_used = response.model_used
        plan.latency_ms = response.latency_ms
        plan.cost_usd = response.cost_usd
        plan.source = "llm"
        return _normalize_plan(plan, request)
    except Exception as exc:
        logger.warning("agent_v3 research planning failed; using fallback plan: %s", exc)
        plan = _fallback_plan(request)
        plan.fallback_reason = str(exc)
        return plan


def bind_evidence(sources: list[Source], max_items: int = 8) -> EvidencePack:
    seen: set[str] = set()
    items: list[EvidenceItem] = []
    for source in sources:
        if not source.url or source.url in seen:
            continue
        seen.add(source.url)
        body = (source.content or source.snippet or "").strip()
        if not body:
            continue
        source_id = f"S{len(items) + 1}"
        items.append(
            EvidenceItem(
                source_id=source_id,
                title=source.title,
                url=source.url,
                evidence=body[:900],
            )
        )
        if len(items) >= max_items:
            break
    return EvidencePack(items=items)


def synthesize_answer(request: AgentV3Request, evidence: EvidencePack):
    evidence_context = "\n\n".join(
        f"[{item.source_id}] {item.title}\nURL: {item.url}\nEvidence: {item.evidence}"
        for item in evidence.items
    )
    if not evidence_context:
        evidence_context = "No source evidence was available. Be transparent about that."
    return model_client.simple_completion(
        (
            "You are the Agent v3 synthesis agent. Answer with clear structure. "
            "Use [S#] citations for claims supported by evidence. If evidence is thin, say so."
        ),
        f"User request:\n{request.message}\n\nEvidence pack:\n{evidence_context}",
        max_tokens=1800 if request.quality_mode == "executive" else 1200,
    )


def source_context_from_evidence(evidence: EvidencePack) -> str:
    return "\n\n".join(
        f"[{item.source_id}] {item.title}\nURL: {item.url}\n{item.evidence}"
        for item in evidence.items
    )


def _fallback_plan(request: AgentV3Request) -> ResearchPlan:
    return ResearchPlan(
        questions=[request.message],
        search_queries=[request.message],
        max_sources=8 if request.quality_mode == "executive" else 5,
        source="heuristic",
    )


def _normalize_plan(plan: ResearchPlan, request: AgentV3Request) -> ResearchPlan:
    if not plan.questions:
        plan.questions = [request.message]
    if not plan.search_queries:
        plan.search_queries = plan.questions
    plan.questions = _dedupe(plan.questions)[:4]
    plan.search_queries = _dedupe(plan.search_queries)[:4]
    plan.max_sources = max(2, min(8, int(plan.max_sources or 6)))
    return plan


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = " ".join(str(value).split())
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


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
