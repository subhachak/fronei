from __future__ import annotations

import json
import logging
import re
from ipaddress import ip_address
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from app.services.agent_v3 import model_client
from app.services.agent_v3.models import AgentV3Request, Source, new_id

logger = logging.getLogger(__name__)


ResearchAgentId = Literal[
    "research_lead",
    "search_worker",
    "source_ranker",
    "source_reader",
    "deep_link_agent",
    "evidence_binder",
    "gap_agent",
    "synthesis_agent",
    "research_judge",
    "claim_verifier",
    "repair_agent",
]


class ResearchPromptTemplate(BaseModel):
    id: str
    agent_id: ResearchAgentId
    system_prompt: str
    variables: list[str] = Field(default_factory=list)
    version: str = "1.0.0"


class ResearchAgentDefinition(BaseModel):
    id: ResearchAgentId
    name: str
    role: str
    prompt_template_id: str
    allowed_tools: list[str] = Field(default_factory=list)
    guardrails: list[str] = Field(default_factory=list)
    max_iterations: int = 1
    version: str = "1.0.0"


class ResearchAgentRegistry(BaseModel):
    agents: dict[ResearchAgentId, ResearchAgentDefinition]
    prompts: dict[str, ResearchPromptTemplate]

    def agent(self, agent_id: ResearchAgentId) -> ResearchAgentDefinition:
        return self.agents[agent_id]

    def prompt_for(self, agent_id: ResearchAgentId) -> ResearchPromptTemplate:
        agent = self.agent(agent_id)
        return self.prompts[agent.prompt_template_id]

    def public_summary(self) -> dict[str, Any]:
        return {
            "agents": [
                {
                    "id": agent.id,
                    "name": agent.name,
                    "role": agent.role,
                    "tools": agent.allowed_tools,
                    "guardrails": agent.guardrails,
                    "prompt_template_id": agent.prompt_template_id,
                    "version": agent.version,
                }
                for agent in self.agents.values()
            ]
        }


class ResearchBudget(BaseModel):
    max_search_workers: int = 3
    max_sources: int = 6
    min_evidence_items: int = 2
    repair_iterations: int = 1
    judge_threshold: float = 0.72


class ResearchGoal(BaseModel):
    id: str = Field(default_factory=lambda: new_id("rgoal"))
    objective: str
    quality_mode: str = "standard"
    output_format: str = "chat"
    budget: ResearchBudget = Field(default_factory=ResearchBudget)
    guardrails: list[str] = Field(default_factory=list)
    status: Literal["created", "planned", "running", "judging", "complete", "repaired"] = "created"


class SearchWorkerPlan(BaseModel):
    worker_id: str = Field(default_factory=lambda: new_id("worker"))
    agent_id: ResearchAgentId = "search_worker"
    question: str
    query: str
    rationale: str = ""
    max_results: int = 4

    @field_validator("question", "query", mode="before")
    @classmethod
    def _clean_required_text(cls, value):
        return " ".join(str(value or "").split())


class ResearchPlan(BaseModel):
    goal_id: str = ""
    questions: list[str] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list)
    workers: list[SearchWorkerPlan] = Field(default_factory=list)
    max_sources: int = 6
    min_evidence_items: int = 2
    judge_threshold: float = 0.72
    repair_iterations: int = 1
    guardrails: list[str] = Field(default_factory=list)
    source: str = "llm"
    model_used: str = ""
    latency_ms: int = 0
    cost_usd: float = 0.0
    fallback_reason: str | None = None


class EvidenceItem(BaseModel):
    source_id: str
    question: str = ""
    title: str = ""
    url: str = ""
    source_type: str = "web"
    evidence: str = ""
    relevance: float = 0.5
    confidence: float = 0.5
    authority: float = 0.5


class EvidencePack(BaseModel):
    items: list[EvidenceItem] = Field(default_factory=list)
    coverage: float = 0.0
    gaps: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)


class ResearchJudgeResult(BaseModel):
    agent_id: ResearchAgentId = "research_judge"
    status: Literal["pass", "repair", "fail"] = "pass"
    score: float = 1.0
    issues: list[str] = Field(default_factory=list)
    repair_instruction: str = ""
    can_publish: bool = True


class RankedSource(BaseModel):
    source: Source
    rank: int
    score: float
    source_type: str = "web"
    authority: float = 0.5
    relevance: float = 0.5
    rationale: str = ""


class DeepLinkCandidate(BaseModel):
    url: str
    parent_url: str = ""
    reason: str = ""


class ClaimVerification(BaseModel):
    status: Literal["pass", "repair"] = "pass"
    checked_claims: int = 0
    cited_claims: int = 0
    unsupported_claims: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ResearchFeedbackLoop(BaseModel):
    judge: ResearchJudgeResult
    repaired: bool = False
    repair_attempts: int = 0
    final_score: float = 1.0


PLAN_PROMPT = """You are the Agent v3 research lead.

Create a compact multi-agent research plan for the user request. Return only JSON:
{
  "questions": ["2-4 focused research questions"],
  "search_queries": ["2-4 precise web search queries"],
  "workers": [
    {"question": "focused question", "query": "precise search query", "rationale": "why this worker is useful", "max_results": 3-5}
  ],
  "max_sources": 4-8,
  "min_evidence_items": 2-4,
  "judge_threshold": 0.65-0.85,
  "repair_iterations": 0-2
}
Prefer sourceable, specific questions. Do not answer the request.
"""


SYNTHESIS_PROMPT = """You are the Agent v3 synthesis agent.

Write a source-grounded answer using only the evidence pack. Use clear structure,
specific findings, and [S#] citations for claims tied to evidence. If evidence is
thin, say what is missing instead of pretending certainty.
"""


REPAIR_PROMPT = """You are the Agent v3 repair agent.

Revise the answer according to the judge feedback. Preserve useful content, add
source citations where evidence supports a claim, and be transparent about gaps.
Return only the improved answer.
"""


def get_research_registry() -> ResearchAgentRegistry:
    return ResearchAgentRegistry(
        agents={
            "research_lead": ResearchAgentDefinition(
                id="research_lead",
                name="Research Lead",
                role="Decomposes the user objective into sourceable questions and search workers.",
                prompt_template_id="research.lead.v1",
                guardrails=["query_specificity", "budget_limits"],
            ),
            "search_worker": ResearchAgentDefinition(
                id="search_worker",
                name="Search Worker",
                role="Runs one focused web search and reports provider/source coverage.",
                prompt_template_id="research.search_worker.v1",
                allowed_tools=["web_search"],
                guardrails=["provider_trace_required", "max_results"],
            ),
            "source_ranker": ResearchAgentDefinition(
                id="source_ranker",
                name="Source Ranker",
                role="Ranks candidate sources by relevance, authority, source type, and usefulness.",
                prompt_template_id="research.source_ranker.v1",
                guardrails=["prefer_primary_sources", "dedupe_sources"],
            ),
            "source_reader": ResearchAgentDefinition(
                id="source_reader",
                name="Source Reader",
                role="Reads selected source pages and normalizes extractable evidence.",
                prompt_template_id="research.source_reader.v1",
                allowed_tools=["read_url"],
                guardrails=["public_urls_only", "source_limit"],
            ),
            "deep_link_agent": ResearchAgentDefinition(
                id="deep_link_agent",
                name="Deep Link Agent",
                role="Discovers bounded follow-on URLs from high-value sources.",
                prompt_template_id="research.deep_link.v1",
                allowed_tools=["read_url"],
                guardrails=["public_urls_only", "link_budget"],
            ),
            "evidence_binder": ResearchAgentDefinition(
                id="evidence_binder",
                name="Evidence Binder",
                role="Scores source extracts, removes duplicates, and builds an evidence pack.",
                prompt_template_id="research.evidence_binder.v1",
                guardrails=["source_manifest_required", "dedupe_sources"],
            ),
            "gap_agent": ResearchAgentDefinition(
                id="gap_agent",
                name="Gap Agent",
                role="Inspects evidence gaps and spawns focused follow-up searches when budget allows.",
                prompt_template_id="research.gap_agent.v1",
                allowed_tools=["web_search"],
                guardrails=["single_gap_pass", "budget_limits"],
            ),
            "synthesis_agent": ResearchAgentDefinition(
                id="synthesis_agent",
                name="Synthesis Agent",
                role="Writes the answer from bound evidence with citations and gap disclosure.",
                prompt_template_id="research.synthesis.v1",
                guardrails=["cite_evidence", "no_unsupported_claims"],
            ),
            "research_judge": ResearchAgentDefinition(
                id="research_judge",
                name="Research Judge",
                role="Evaluates evidence coverage, citation use, and answer publishability.",
                prompt_template_id="research.judge.v1",
                guardrails=["deterministic_first", "publish_threshold"],
            ),
            "claim_verifier": ResearchAgentDefinition(
                id="claim_verifier",
                name="Claim Verifier",
                role="Checks final answer claims for citation markers and evidence support.",
                prompt_template_id="research.claim_verifier.v1",
                guardrails=["citation_required_for_claims", "unsupported_claim_detection"],
            ),
            "repair_agent": ResearchAgentDefinition(
                id="repair_agent",
                name="Repair Agent",
                role="Improves a judged answer when evidence or citations are insufficient.",
                prompt_template_id="research.repair.v1",
                guardrails=["preserve_sources", "repair_iteration_cap"],
            ),
        },
        prompts={
            "research.lead.v1": ResearchPromptTemplate(
                id="research.lead.v1",
                agent_id="research_lead",
                system_prompt=PLAN_PROMPT,
                variables=["message", "quality_mode", "output_format"],
            ),
            "research.search_worker.v1": ResearchPromptTemplate(
                id="research.search_worker.v1",
                agent_id="search_worker",
                system_prompt="Run one focused web search. Return provider, source count, and source candidates.",
                variables=["query", "max_results"],
            ),
            "research.source_reader.v1": ResearchPromptTemplate(
                id="research.source_reader.v1",
                agent_id="source_reader",
                system_prompt="Read selected public source URLs and extract relevant text.",
                variables=["urls"],
            ),
            "research.source_ranker.v1": ResearchPromptTemplate(
                id="research.source_ranker.v1",
                agent_id="source_ranker",
                system_prompt="Rank public source candidates by authority, source type, recency cues, and relevance.",
                variables=["sources", "questions"],
            ),
            "research.deep_link.v1": ResearchPromptTemplate(
                id="research.deep_link.v1",
                agent_id="deep_link_agent",
                system_prompt="Follow a small number of useful public links from high-value source pages.",
                variables=["sources", "link_budget"],
            ),
            "research.evidence_binder.v1": ResearchPromptTemplate(
                id="research.evidence_binder.v1",
                agent_id="evidence_binder",
                system_prompt="Bind sources into a concise evidence pack with confidence and gaps.",
                variables=["sources", "questions"],
            ),
            "research.gap_agent.v1": ResearchPromptTemplate(
                id="research.gap_agent.v1",
                agent_id="gap_agent",
                system_prompt="Turn evidence gaps into one focused follow-up search worker.",
                variables=["gaps", "message"],
            ),
            "research.synthesis.v1": ResearchPromptTemplate(
                id="research.synthesis.v1",
                agent_id="synthesis_agent",
                system_prompt=SYNTHESIS_PROMPT,
                variables=["message", "evidence_pack"],
            ),
            "research.judge.v1": ResearchPromptTemplate(
                id="research.judge.v1",
                agent_id="research_judge",
                system_prompt="Judge research quality, evidence coverage, citation use, and publishability.",
                variables=["answer", "evidence_pack", "plan"],
            ),
            "research.claim_verifier.v1": ResearchPromptTemplate(
                id="research.claim_verifier.v1",
                agent_id="claim_verifier",
                system_prompt="Verify final claims have citations and evidence support.",
                variables=["answer", "evidence_pack"],
            ),
            "research.repair.v1": ResearchPromptTemplate(
                id="research.repair.v1",
                agent_id="repair_agent",
                system_prompt=REPAIR_PROMPT,
                variables=["answer", "judge", "evidence_pack"],
            ),
        },
    )


def research_budget_for(request: AgentV3Request) -> ResearchBudget:
    if request.quality_mode == "executive":
        return ResearchBudget(max_search_workers=4, max_sources=8, min_evidence_items=3, repair_iterations=2, judge_threshold=0.78)
    if request.quality_mode == "draft":
        return ResearchBudget(max_search_workers=2, max_sources=4, min_evidence_items=1, repair_iterations=0, judge_threshold=0.62)
    return ResearchBudget(max_search_workers=3, max_sources=6, min_evidence_items=2, repair_iterations=1, judge_threshold=0.72)


def create_research_goal(request: AgentV3Request) -> ResearchGoal:
    budget = research_budget_for(request)
    return ResearchGoal(
        objective=request.message,
        quality_mode=request.quality_mode,
        output_format=request.output_format,
        budget=budget,
        guardrails=[
            "bounded_search_workers",
            "public_source_urls",
            "source_manifest_required",
            "citation_required_for_claims",
            "judge_before_publish",
        ],
    )


def plan_research(request: AgentV3Request) -> ResearchPlan:
    goal = create_research_goal(request)
    try:
        registry = get_research_registry()
        prompt = registry.prompt_for("research_lead")
        response = model_client.complete(
            [
                {"role": "system", "content": prompt.system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "message": request.message,
                            "quality_mode": request.quality_mode,
                            "output_format": request.output_format,
                            "budget": goal.budget.model_dump(mode="json"),
                            "guardrails": goal.guardrails,
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
        return _normalize_plan(plan, request, goal)
    except Exception as exc:
        logger.warning("agent_v3 research planning failed; using fallback plan: %s", exc)
        plan = _fallback_plan(request, goal)
        plan.fallback_reason = str(exc)
        return plan


def bind_evidence(sources: list[Source], plan: ResearchPlan | None = None, max_items: int = 8) -> EvidencePack:
    seen: set[str] = set()
    items: list[EvidenceItem] = []
    questions = plan.questions if plan else []
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
                question=questions[(len(items) % len(questions))] if questions else "",
                title=source.title,
                url=source.url,
                source_type=classify_source_type(source.url),
                evidence=body[:900],
                relevance=_estimate_relevance(source, questions),
                confidence=0.75 if source.content else 0.55,
                authority=score_source_authority(source.url),
            )
        )
        if len(items) >= max_items:
            break
    min_items = plan.min_evidence_items if plan else 1
    coverage = min(1.0, len(items) / max(1, min_items))
    gaps = [] if len(items) >= min_items else [f"Only {len(items)} usable evidence item(s); target is {min_items}."]
    contradictions = detect_contradictions(items)
    return EvidencePack(items=items, coverage=coverage, gaps=gaps, contradictions=contradictions)


def synthesize_answer(request: AgentV3Request, plan: ResearchPlan, evidence: EvidencePack):
    evidence_context = "\n\n".join(
        f"[{item.source_id}] {item.title}\nQuestion: {item.question}\nURL: {item.url}\nEvidence: {item.evidence}"
        for item in evidence.items
    )
    if not evidence_context:
        evidence_context = "No source evidence was available. Be transparent about that."
    return model_client.simple_completion(
        SYNTHESIS_PROMPT,
        (
            f"User request:\n{request.message}\n\n"
            f"Research questions:\n{json.dumps(plan.questions, ensure_ascii=False)}\n\n"
            f"Evidence pack:\n{evidence_context}\n\n"
            f"Known gaps:\n{json.dumps(evidence.gaps, ensure_ascii=False)}"
        ),
        max_tokens=1800 if request.quality_mode == "executive" else 1200,
    )


def judge_research(request: AgentV3Request, plan: ResearchPlan, evidence: EvidencePack, answer: str) -> ResearchJudgeResult:
    issues: list[str] = []
    score = 0.45
    if evidence.items:
        score += min(0.25, 0.05 * len(evidence.items))
    if evidence.coverage >= 1.0:
        score += 0.15
    citation_count = len(re.findall(r"\[S\d+\]", answer or ""))
    if citation_count:
        score += min(0.2, 0.05 * citation_count)
    else:
        issues.append("The answer does not include source citations.")
    if len(answer or "") < 180:
        score -= 0.1
        issues.append("The answer is too short for the requested research depth.")
    if evidence.gaps:
        score -= 0.08
        issues.extend(evidence.gaps)
    score = max(0.0, min(1.0, score))
    threshold = plan.judge_threshold or 0.72
    if score >= threshold:
        return ResearchJudgeResult(status="pass", score=score, issues=issues, can_publish=True)
    if score >= max(0.45, threshold - 0.2):
        return ResearchJudgeResult(
            status="repair",
            score=score,
            issues=issues,
            repair_instruction=(
                "Strengthen the answer with explicit [S#] citations, acknowledge source gaps, "
                "and make the structure more useful to the user."
            ),
            can_publish=False,
        )
    return ResearchJudgeResult(
        status="fail",
        score=score,
        issues=issues or ["Research quality is below publish threshold."],
        repair_instruction="Redo the research plan with better source coverage.",
        can_publish=False,
    )


def repair_research_answer(
    request: AgentV3Request,
    plan: ResearchPlan,
    evidence: EvidencePack,
    answer: str,
    judge: ResearchJudgeResult,
):
    evidence_context = source_context_from_evidence(evidence) or "No source evidence was available."
    return model_client.simple_completion(
        REPAIR_PROMPT,
        (
            f"User request:\n{request.message}\n\n"
            f"Original answer:\n{answer}\n\n"
            f"Judge feedback:\n{judge.model_dump_json()}\n\n"
            f"Evidence pack:\n{evidence_context}\n\n"
            f"Research questions:\n{json.dumps(plan.questions, ensure_ascii=False)}"
        ),
        max_tokens=1800 if request.quality_mode == "executive" else 1200,
    )


def rank_sources(sources: list[Source], plan: ResearchPlan) -> list[RankedSource]:
    ranked: list[RankedSource] = []
    seen: set[str] = set()
    for source in sources:
        if not source.url or source.url in seen:
            continue
        seen.add(source.url)
        source_type = classify_source_type(source.url)
        authority = score_source_authority(source.url)
        relevance = _estimate_relevance(source, plan.questions)
        content_bonus = 0.08 if source.content else 0.0
        score = max(0.0, min(1.0, (authority * 0.45) + (relevance * 0.45) + content_bonus))
        ranked.append(
            RankedSource(
                source=source,
                rank=0,
                score=score,
                source_type=source_type,
                authority=authority,
                relevance=relevance,
                rationale=f"{source_type} source; authority={authority:.2f}; relevance={relevance:.2f}",
            )
        )
    ranked.sort(key=lambda item: item.score, reverse=True)
    for index, item in enumerate(ranked, start=1):
        item.rank = index
    return ranked


def extract_deep_link_candidates(sources: list[Source], *, max_links: int = 4) -> list[DeepLinkCandidate]:
    candidates: list[DeepLinkCandidate] = []
    seen: set[str] = set()
    for source in sources:
        text = f"{source.snippet}\n{source.content}"
        for url in _extract_urls_from_text(text):
            if url == source.url or url in seen or not is_public_source_url(url):
                continue
            seen.add(url)
            candidates.append(
                DeepLinkCandidate(
                    url=url,
                    parent_url=source.url,
                    reason="Linked from a selected high-value source.",
                )
            )
            if len(candidates) >= max_links:
                return candidates
    return candidates


def build_gap_followup_workers(request: AgentV3Request, plan: ResearchPlan, evidence: EvidencePack) -> list[SearchWorkerPlan]:
    if not evidence.gaps:
        return []
    gap_text = " ".join(evidence.gaps)[:240]
    query = f"{request.message} {gap_text}".strip()
    return [
        SearchWorkerPlan(
            question=f"Close evidence gap: {gap_text}",
            query=query,
            rationale="Gap agent follow-up search from evidence coverage review.",
            max_results=min(3, plan.max_sources),
        )
    ]


def verify_claims(answer: str, evidence: EvidencePack) -> ClaimVerification:
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", answer or "") if len(part.strip()) > 30]
    checked = min(12, len(sentences))
    unsupported: list[str] = []
    cited = 0
    valid_source_ids = {item.source_id for item in evidence.items}
    for sentence in sentences[:checked]:
        markers = set(re.findall(r"\[(S\d+)\]", sentence))
        if markers & valid_source_ids:
            cited += 1
        elif _looks_like_substantive_claim(sentence):
            unsupported.append(sentence[:180])
    status: Literal["pass", "repair"] = "pass" if not unsupported else "repair"
    notes = [] if not unsupported else ["Some substantive claims lack [S#] citations."]
    return ClaimVerification(
        status=status,
        checked_claims=checked,
        cited_claims=cited,
        unsupported_claims=unsupported,
        notes=notes,
    )


def classify_source_type(url: str) -> str:
    parsed = urlparse(url or "")
    path = parsed.path.lower()
    host = (parsed.hostname or "").lower()
    if path.endswith(".pdf"):
        return "pdf"
    if host.endswith(".gov") or ".gov." in host:
        return "government"
    if host.endswith(".edu") or ".edu." in host:
        return "academic"
    if any(token in host for token in ("sec.gov", "who.int", "oecd.org", "worldbank.org", "imf.org")):
        return "primary"
    if any(token in host for token in ("docs.", "developer.", "support.", "help.")):
        return "documentation"
    if any(token in host for token in ("reuters.com", "apnews.com", "bloomberg.com", "ft.com", "wsj.com")):
        return "news"
    return "web"


def score_source_authority(url: str) -> float:
    source_type = classify_source_type(url)
    scores = {
        "government": 0.95,
        "primary": 0.92,
        "academic": 0.88,
        "documentation": 0.84,
        "pdf": 0.76,
        "news": 0.68,
        "web": 0.52,
    }
    return scores.get(source_type, 0.5)


def detect_contradictions(items: list[EvidenceItem]) -> list[str]:
    text = " ".join(item.evidence.lower() for item in items)
    pairs = [("increase", "decrease"), ("growth", "decline"), ("approved", "rejected"), ("profit", "loss")]
    found: list[str] = []
    for left, right in pairs:
        if left in text and right in text:
            found.append(f"Evidence contains both '{left}' and '{right}' signals; synthesis should avoid overclaiming.")
    return found[:3]


def is_public_source_url(url: str) -> bool:
    parsed = urlparse(url or "")
    if parsed.scheme not in {"http", "https"}:
        return False
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        return False
    if hostname in {"localhost", "metadata.google.internal"} or hostname.endswith(".local"):
        return False
    try:
        ip = ip_address(hostname)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast)
    except ValueError:
        return True


def source_context_from_evidence(evidence: EvidencePack) -> str:
    return "\n\n".join(
        f"[{item.source_id}] {item.title}\nURL: {item.url}\n{item.evidence}"
        for item in evidence.items
    )


def _fallback_plan(request: AgentV3Request, goal: ResearchGoal | None = None) -> ResearchPlan:
    goal = goal or create_research_goal(request)
    worker = SearchWorkerPlan(
        question=request.message,
        query=request.message,
        rationale="Fallback worker from the original request.",
        max_results=min(5, goal.budget.max_sources),
    )
    return ResearchPlan(
        goal_id=goal.id,
        questions=[request.message],
        search_queries=[request.message],
        workers=[worker],
        max_sources=goal.budget.max_sources,
        min_evidence_items=goal.budget.min_evidence_items,
        judge_threshold=goal.budget.judge_threshold,
        repair_iterations=goal.budget.repair_iterations,
        guardrails=goal.guardrails,
        source="heuristic",
    )


def _normalize_plan(plan: ResearchPlan, request: AgentV3Request, goal: ResearchGoal | None = None) -> ResearchPlan:
    goal = goal or create_research_goal(request)
    if not plan.questions:
        plan.questions = [request.message]
    plan.questions = _dedupe(plan.questions)[:4]
    if not plan.workers:
        queries = _dedupe(plan.search_queries or plan.questions)[: goal.budget.max_search_workers]
        plan.workers = [
            SearchWorkerPlan(
                question=plan.questions[idx] if idx < len(plan.questions) else query,
                query=query,
                rationale="LLM-selected focused research worker.",
                max_results=min(5, goal.budget.max_sources),
            )
            for idx, query in enumerate(queries)
        ]
    else:
        plan.workers = [worker for worker in plan.workers if worker.query][: goal.budget.max_search_workers]
    if not plan.workers:
        plan.workers = _fallback_plan(request, goal).workers
    plan.search_queries = [worker.query for worker in plan.workers]
    plan.questions = _dedupe([worker.question for worker in plan.workers] or plan.questions)[:4]
    plan.max_sources = max(2, min(goal.budget.max_sources, int(plan.max_sources or goal.budget.max_sources)))
    plan.min_evidence_items = max(1, min(plan.max_sources, int(plan.min_evidence_items or goal.budget.min_evidence_items)))
    plan.judge_threshold = max(0.45, min(0.9, float(plan.judge_threshold or goal.budget.judge_threshold)))
    plan.repair_iterations = max(0, min(goal.budget.repair_iterations, int(plan.repair_iterations or 0)))
    plan.guardrails = plan.guardrails or goal.guardrails
    plan.goal_id = plan.goal_id or goal.id
    for worker in plan.workers:
        worker.max_results = max(1, min(5, int(worker.max_results or 4)))
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


def _estimate_relevance(source: Source, questions: list[str]) -> float:
    haystack = f"{source.title} {source.snippet} {source.content}".lower()
    if not haystack or not questions:
        return 0.5
    tokens = {token for question in questions for token in re.findall(r"[a-z0-9]{4,}", question.lower())}
    if not tokens:
        return 0.5
    hits = sum(1 for token in tokens if token in haystack)
    return max(0.25, min(0.95, 0.35 + hits / max(4, len(tokens))))


def _extract_urls_from_text(text: str) -> list[str]:
    markdown_urls = re.findall(r"\[[^\]]+\]\((https?://[^)\s]+)\)", text or "")
    plain_urls = re.findall(r"https?://[^\s)>\]\"']+", text or "")
    return _dedupe([*_clean_urls(markdown_urls), *_clean_urls(plain_urls)])


def _clean_urls(urls: list[str]) -> list[str]:
    return [url.rstrip(".,;:") for url in urls if url]


def _looks_like_substantive_claim(sentence: str) -> bool:
    lowered = sentence.lower()
    return any(
        token in lowered
        for token in (
            "%",
            "$",
            "million",
            "billion",
            "increase",
            "decrease",
            "growth",
            "decline",
            "market",
            "revenue",
            "cost",
            "risk",
            "announced",
            "reported",
        )
    )
