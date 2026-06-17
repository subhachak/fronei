from __future__ import annotations

import json
import logging
import re
from ipaddress import ip_address
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from app.services.agent_v3 import model_client
from app.services.agent_v3.models import AgentV3Request, Source, ToolCall, new_id

logger = logging.getLogger(__name__)

ResearchProfile = Literal[
    "general",
    "technical_architecture",
    "vendor_comparison",
    "market_research",
    "regulatory",
    "academic_literature",
]


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
    max_tool_calls: int = 8
    max_model_calls: int = 4
    max_cost_usd: float = 0.08
    max_elapsed_ms: int = 90_000
    max_deep_links: int = 2


class ResearchBudgetLedger(BaseModel):
    budget: ResearchBudget
    tool_calls: int = 0
    model_calls: int = 0
    sources_seen: int = 0
    sources_read: int = 0
    cost_usd: float = 0.0
    elapsed_ms: int = 0
    stopped: bool = False
    stop_reason: str | None = None
    decisions: list[str] = Field(default_factory=list)

    def refresh_elapsed(self, elapsed_ms: int) -> None:
        self.elapsed_ms = max(0, int(elapsed_ms))
        if self.elapsed_ms >= self.budget.max_elapsed_ms and not self.stopped:
            self.stop("elapsed time budget exhausted")

    def record_model_call(self, *, cost_usd: float = 0.0, latency_ms: int = 0) -> None:
        self.model_calls += 1
        self.cost_usd += max(0.0, float(cost_usd or 0.0))
        self.elapsed_ms += max(0, int(latency_ms or 0))
        self._check_limits()

    def record_tool_call(
        self,
        *,
        latency_ms: int = 0,
        sources_seen: int = 0,
        sources_read: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        self.tool_calls += 1
        self.sources_seen += max(0, int(sources_seen or 0))
        self.sources_read += max(0, int(sources_read or 0))
        self.cost_usd += max(0.0, float(cost_usd or 0.0))
        self.elapsed_ms += max(0, int(latency_ms or 0))
        self._check_limits()

    def can_start_tool(self, tool_name: str) -> bool:
        if self.stopped:
            return False
        if self.tool_calls >= self.budget.max_tool_calls:
            self.stop(f"tool budget exhausted before {tool_name}")
            return False
        return True

    def can_start_model(self, agent_id: str) -> bool:
        if self.model_calls >= self.budget.max_model_calls:
            self.stop(f"model budget exhausted before {agent_id}")
            return False
        if self.cost_usd >= self.budget.max_cost_usd:
            self.stop(f"cost budget exhausted before {agent_id}")
            return False
        if self.elapsed_ms >= self.budget.max_elapsed_ms:
            self.stop(f"elapsed time budget exhausted before {agent_id}")
            return False
        return True

    def can_read_more_sources(self) -> bool:
        if self.stopped:
            return False
        if self.sources_read >= self.budget.max_sources + self.budget.max_deep_links:
            self.stop("source read budget exhausted")
            return False
        return True

    def remaining_tool_calls(self) -> int:
        return max(0, self.budget.max_tool_calls - self.tool_calls)

    def remaining_source_reads(self) -> int:
        return max(0, self.budget.max_sources + self.budget.max_deep_links - self.sources_read)

    def stop(self, reason: str) -> None:
        self.stopped = True
        self.stop_reason = self.stop_reason or reason
        if reason not in self.decisions:
            self.decisions.append(reason)

    def _check_limits(self) -> None:
        if self.stopped:
            return
        if self.tool_calls >= self.budget.max_tool_calls:
            self.stop("tool budget exhausted")
        elif self.model_calls >= self.budget.max_model_calls:
            self.stop("model budget exhausted")
        elif self.cost_usd >= self.budget.max_cost_usd:
            self.stop("cost budget exhausted")
        elif self.elapsed_ms >= self.budget.max_elapsed_ms:
            self.stop("elapsed time budget exhausted")


class ResearchGoal(BaseModel):
    id: str = Field(default_factory=lambda: new_id("rgoal"))
    objective: str
    research_level: str = "regular"
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
    research_profile: ResearchProfile = "general"
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
    supports_cells: list[str] = Field(default_factory=list)
    quoted_text: str = ""


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


class ResearchBrief(BaseModel):
    objective: str
    audience: str = "general business"
    research_profile: ResearchProfile = "general"
    scope_in: list[str] = Field(default_factory=list)
    scope_out: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    output_type: str = "answer"
    assumptions: list[str] = Field(default_factory=list)
    research_level: str = "regular"
    quality_mode: str = "standard"
    model_used: str = ""
    latency_ms: int = 0
    cost_usd: float = 0.0
    source: str = "llm"
    fallback_reason: str | None = None


class CoverageCell(BaseModel):
    cell_id: str = Field(default_factory=lambda: new_id("cell"))
    dimension: str
    subject: str
    required: bool = True
    status: Literal["empty", "partial", "filled", "not_applicable"] = "empty"
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    notes: str = ""
    attempts: int = 0


class CoverageContract(BaseModel):
    cells: list[CoverageCell] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    subjects: list[str] = Field(default_factory=list)
    model_used: str = ""
    latency_ms: int = 0
    cost_usd: float = 0.0
    source: str = "llm"
    fallback_reason: str | None = None

    def coverage_ratio(self) -> float:
        required = [cell for cell in self.cells if cell.required and cell.status != "not_applicable"]
        if not required:
            return 1.0
        filled = [cell for cell in required if cell.status in {"filled", "partial"}]
        return len(filled) / len(required)

    def open_cells(self) -> list[CoverageCell]:
        return [cell for cell in self.cells if cell.required and cell.status == "empty"]

    def partial_cells(self) -> list[CoverageCell]:
        return [cell for cell in self.cells if cell.required and cell.status == "partial"]


class ResearchStateStore(BaseModel):
    brief: ResearchBrief
    contract: CoverageContract
    plan: ResearchPlan
    evidence: EvidencePack = Field(default_factory=EvidencePack)
    source_inventory: list[str] = Field(default_factory=list)
    query_history: list[str] = Field(default_factory=list)
    all_sources: list[Source] = Field(default_factory=list)
    all_tool_calls: list[ToolCall] = Field(default_factory=list)
    iteration: int = 0
    budget_ledger: ResearchBudgetLedger = Field(default_factory=lambda: ResearchBudgetLedger(budget=ResearchBudget()))

    def add_sources(self, sources: list[Source]) -> list[Source]:
        new_sources: list[Source] = []
        for source in sources:
            if source.url and source.url not in self.source_inventory:
                self.source_inventory.append(source.url)
                self.all_sources.append(source)
                new_sources.append(source)
        return new_sources

    def add_queries(self, queries: list[str]) -> None:
        for query in queries:
            cleaned = " ".join(str(query or "").split())
            if cleaned and cleaned not in self.query_history:
                self.query_history.append(cleaned)


class ReflectionDecision(BaseModel):
    sufficient: bool = False
    open_dimensions: list[str] = Field(default_factory=list)
    open_subjects: list[str] = Field(default_factory=list)
    targeted_queries: list[str] = Field(default_factory=list)
    terminate_reason: str | None = None
    coverage_ratio: float = 0.0
    next_action: Literal["continue", "publish", "stop_with_gaps"] = "continue"
    model_used: str = ""
    latency_ms: int = 0
    cost_usd: float = 0.0
    source: str = "llm"


class JudgeVerdict(BaseModel):
    can_publish: bool = True
    repair_needed: bool = False
    repair_instruction: str = ""
    specific_gaps: list[str] = Field(default_factory=list)
    score: float = 1.0
    issues: list[str] = Field(default_factory=list)
    next_action: Literal["publish", "repair_answer", "research_more", "stop_with_gaps"] = "publish"


class CitationVerification(BaseModel):
    verified_claims: int = 0
    unsupported_claims: list[str] = Field(default_factory=list)
    hallucinated_citations: list[str] = Field(default_factory=list)
    repair_needed: bool = False
    repair_instruction: str = ""
    model_used: str = ""
    latency_ms: int = 0
    cost_usd: float = 0.0
    source: str = "llm"


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

For technical architecture research, produce a real architectural report, not a
short overview. Include concrete components, control flow, data flow, agent
roles, state/memory, tool boundaries, guardrails, failure handling, observability,
latency/cost trade-offs, and implementation guidance. Prefer precise technical
language over marketing phrasing. Include a compact text diagram when useful.
"""


REPAIR_PROMPT = """You are the Agent v3 repair agent.

Revise the answer according to the judge feedback. Preserve useful content, add
source citations where evidence supports a claim, and be transparent about gaps.
Return only the improved answer.
"""


BRIEF_PROMPT = """You are the Fronei research briefing agent.

Convert the user request into a compact, frozen research brief. Return only JSON:
{
  "objective": "precise one-sentence research objective",
  "research_profile": "general|technical_architecture|vendor_comparison|market_research|regulatory|academic_literature",
  "audience": "intended audience",
  "scope_in": ["2-4 topics, entities, or dimensions explicitly in scope"],
  "scope_out": ["0-2 things explicitly out of scope"],
  "success_criteria": ["2-4 measurable conditions that define complete research"],
  "output_type": "answer|report|comparison|briefing",
  "assumptions": ["0-2 assumptions the research makes"]
}
Infer carefully from the request. Do not invent facts.
"""


COVERAGE_CONTRACT_PROMPT = """You are the Fronei coverage contract agent.

Given a research brief, generate the evidence matrix that defines when research is complete.
For comparison or vendor research, subjects are the entities being compared and dimensions are the attributes.
For topic research, subjects are major subtopics and dimensions are analytical angles.

Return only JSON:
{
  "subjects": ["2-6 subjects"],
  "dimensions": ["3-7 dimensions"],
  "cells": [
    {"dimension": "dimension name", "subject": "subject name", "required": true}
  ]
}
Generate one cell per dimension × subject combination. Mark required=false only when obviously not applicable.
"""


REFLECTION_PROMPT = """You are the Fronei lead research agent.

Review the current research state and decide whether to continue or terminate.
Return only JSON:
{
  "sufficient": true|false,
  "open_dimensions": ["dimensions not yet covered"],
  "open_subjects": ["subjects not yet covered"],
  "targeted_queries": ["2-5 specific search queries to close remaining gaps; empty if sufficient"],
  "terminate_reason": "reason to stop if sufficient=true or budget exhausted",
  "coverage_ratio": 0.0-1.0,
  "next_action": "continue|publish|stop_with_gaps"
}
Be specific in targeted_queries. Prefer site-specific queries for vendor docs, pricing, compliance, APIs, and official pages.
If a cell has repeated targeted attempts and still has no public evidence, stop with an explicit gap rather than hallucinating.
"""


CITATION_VERIFICATION_PROMPT = """You are the Fronei citation verification agent.

You will be given a synthesized answer and an evidence pack. For each factual claim with a [S#] citation, verify:
1. The source [S#] exists in the evidence pack.
2. The quoted source text supports the specific claim.

Return only JSON:
{
  "verified_claims": 0,
  "unsupported_claims": ["claims where citation does not support the claim"],
  "hallucinated_citations": ["S# references that appear in the answer but are not in the evidence pack"],
  "repair_needed": true|false,
  "repair_instruction": "specific repair instruction if needed"
}
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
    if request.research_level == "easy":
        return ResearchBudget(
            max_search_workers=1,
            max_sources=1,
            min_evidence_items=1,
            repair_iterations=0,
            judge_threshold=0.60,
            max_tool_calls=2,
            max_model_calls=2,
            max_cost_usd=0.01,
            max_elapsed_ms=15_000,
            max_deep_links=0,
        )
    if request.research_level == "deep":
        return ResearchBudget(
            max_search_workers=6,
            max_sources=18,
            min_evidence_items=8,
            repair_iterations=2,
            judge_threshold=0.78,
            max_tool_calls=36,
            max_model_calls=14,
            max_cost_usd=0.50,
            max_elapsed_ms=300_000,
            max_deep_links=12,
        )
    return ResearchBudget(
        max_search_workers=3,
        max_sources=6,
        min_evidence_items=2,
        repair_iterations=1,
        judge_threshold=0.72,
        max_tool_calls=8,
        max_model_calls=4,
        max_cost_usd=0.08,
        max_elapsed_ms=90_000,
        max_deep_links=2,
    )


def create_research_goal(request: AgentV3Request) -> ResearchGoal:
    budget = research_budget_for(request)
    return ResearchGoal(
        objective=request.message,
        research_level=request.research_level if request.research_level != "auto" else "regular",
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


def generate_research_brief(request: AgentV3Request) -> ResearchBrief:
    try:
        response = model_client.complete(
            [
                {"role": "system", "content": BRIEF_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "message": request.message,
                            "conversation_context": request.conversation_context[-3000:] if request.conversation_context else "",
                            "quality_mode": request.quality_mode,
                            "research_level": request.research_level,
                            "output_format": request.output_format,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            max_tokens=600,
            timeout_s=15,
        )
        payload = _parse_json(response.text)
        brief = ResearchBrief.model_validate(payload)
        brief.objective = brief.objective or request.message
        if brief.research_profile == "general":
            brief.research_profile = infer_research_profile(request.message)
        brief.research_level = request.research_level if request.research_level != "auto" else "regular"
        brief.quality_mode = request.quality_mode
        brief.model_used = response.model_used
        brief.latency_ms = response.latency_ms
        brief.cost_usd = response.cost_usd
        brief.source = "llm"
        return brief
    except Exception as exc:
        logger.warning("agent_v3 brief generation failed; using fallback: %s", exc)
        profile = infer_research_profile(request.message)
        return ResearchBrief(
            objective=request.message,
            research_profile=profile,
            scope_in=[request.message[:160]],
            success_criteria=_fallback_success_criteria(profile),
            research_level=request.research_level if request.research_level != "auto" else "regular",
            quality_mode=request.quality_mode,
            source="heuristic",
            fallback_reason=str(exc),
        )


def infer_research_profile(message: str) -> ResearchProfile:
    text = (message or "").lower()
    technical_terms = [
        "architecture",
        "system design",
        "components",
        "workflow",
        "workflows",
        "orchestration",
        "multi-agent",
        "multi agent",
        "agentic",
        "pipeline",
        "runtime",
        "implementation",
        "data flow",
        "stateful",
        "mcp",
        "guardrails",
        "evidence binder",
        "planner",
        "critic",
        "judge",
    ]
    if any(term in text for term in technical_terms):
        return "technical_architecture"
    if any(term in text for term in ("compare", "vendor", "pricing", "versus", " vs ", "tavily", "nimble", "you.com")):
        return "vendor_comparison"
    if any(term in text for term in ("regulation", "regulatory", "compliance", "law", "policy", "guideline")):
        return "regulatory"
    if any(term in text for term in ("market", "industry", "tam", "forecast", "share", "growth")):
        return "market_research"
    if any(term in text for term in ("paper", "literature", "academic", "arxiv", "benchmark")):
        return "academic_literature"
    return "general"


def _fallback_success_criteria(profile: ResearchProfile) -> list[str]:
    if profile == "technical_architecture":
        return [
            "Identify concrete system components and their responsibilities.",
            "Explain end-to-end workflows, control loops, state, and data flow.",
            "Cover implementation trade-offs, failure handling, guardrails, and evaluation.",
            "Prioritize technically dense sources over high-level overview pages.",
        ]
    return ["Answer the user's question with source-grounded evidence."]


def _technical_architecture_contract() -> CoverageContract:
    subjects = [
        "Lead agent and orchestration",
        "Research planning and coverage contract",
        "Search workers and provider strategy",
        "Source reading and deep-link crawling",
        "Evidence binder and citation map",
        "Reflection, gap detection, and repair loop",
        "Synthesis, judge, and quality gates",
        "Runtime durability, budget ledger, and observability",
        "Guardrails and security controls",
    ]
    dimensions = [
        "responsibility",
        "implementation pattern",
        "data model",
        "workflow",
        "failure handling",
        "trade-offs",
    ]
    cells = [
        CoverageCell(subject=subject, dimension=dimension, required=True)
        for subject in subjects
        for dimension in dimensions
    ]
    return CoverageContract(
        cells=cells,
        subjects=subjects,
        dimensions=dimensions,
        source="profile:technical_architecture",
    )


def generate_coverage_contract(request: AgentV3Request, brief: ResearchBrief) -> CoverageContract:
    if brief.research_profile == "technical_architecture":
        return _technical_architecture_contract()
    try:
        response = model_client.complete(
            [
                {"role": "system", "content": COVERAGE_CONTRACT_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"message": request.message, "brief": brief.model_dump(mode="json")},
                        ensure_ascii=False,
                    ),
                },
            ],
            max_tokens=1000,
            timeout_s=20,
        )
        payload = _parse_json(response.text)
        subjects = [str(item) for item in (payload.get("subjects") or []) if str(item).strip()][:6]
        dimensions = [str(item) for item in (payload.get("dimensions") or []) if str(item).strip()][:7]
        cells = [
            CoverageCell.model_validate(cell)
            for cell in payload.get("cells", [])
            if isinstance(cell, dict)
        ]
        if subjects:
            cells = [cell for cell in cells if cell.subject in subjects]
        if dimensions:
            cells = [cell for cell in cells if cell.dimension in dimensions]
        cells = cells[:42]
        if not cells:
            raise ValueError("empty coverage contract")
        if not subjects:
            subjects = _dedupe([cell.subject for cell in cells])[:6]
        if not dimensions:
            dimensions = _dedupe([cell.dimension for cell in cells])[:7]
        return CoverageContract(
            cells=cells,
            subjects=subjects,
            dimensions=dimensions,
            model_used=response.model_used,
            latency_ms=response.latency_ms,
            cost_usd=response.cost_usd,
            source="llm",
        )
    except Exception as exc:
        logger.warning("agent_v3 coverage contract failed; using fallback: %s", exc)
        criteria = brief.success_criteria or [brief.objective]
        subjects = _derive_fallback_subjects(request.message, brief)
        dimensions = _derive_fallback_dimensions(criteria)
        cells = [
            CoverageCell(subject=subject, dimension=dimension, required=True)
            for subject in subjects
            for dimension in dimensions
        ][:24]
        return CoverageContract(
            cells=cells or [CoverageCell(subject=brief.objective[:80], dimension="coverage")],
            subjects=subjects or [brief.objective[:80]],
            dimensions=dimensions or ["coverage"],
            source="heuristic",
            fallback_reason=str(exc),
        )


def plan_from_contract(
    request: AgentV3Request,
    contract: CoverageContract,
    budget: ResearchBudget | None = None,
) -> ResearchPlan:
    budget = budget or research_budget_for(request)
    open_cells = contract.open_cells()
    workers: list[SearchWorkerPlan] = []
    for subject in _dedupe([cell.subject for cell in open_cells]):
        subject_cells = [cell for cell in open_cells if cell.subject == subject]
        dimensions = _dedupe([cell.dimension for cell in subject_cells])[:4]
        query = _targeted_query(subject, dimensions, request.message)
        workers.append(
            SearchWorkerPlan(
                question=f"Research {subject}: {', '.join(dimensions)}",
                query=query,
                rationale=f"Cover open contract cells for {subject}.",
                max_results=4,
            )
        )
        if len(workers) >= budget.max_search_workers:
            break
    if not workers:
        workers = _fallback_plan(request, create_research_goal(request)).workers

    # For technical_architecture + deep, prepend anchor queries that reliably
    # surface arxiv papers, GitHub repos, and engineering reference material.
    # These run before the contract-cell workers so wave 1 seeds the evidence
    # pool with dense sources before the coverage check fires.
    profile = infer_research_profile(request.message)
    if profile == "technical_architecture" and request.research_level == "deep":
        anchor_queries = _tech_arch_anchor_queries(request.message)
        existing_queries = {w.query for w in workers}
        anchor_workers = [
            SearchWorkerPlan(
                question=f"Anchor: {q}",
                query=q,
                rationale="Profile-level anchor to seed technically dense sources.",
                max_results=5,
            )
            for q in anchor_queries
            if q not in existing_queries
        ]
        workers = (anchor_workers + workers)[: budget.max_search_workers]

    return ResearchPlan(
        research_profile=profile,
        questions=[worker.question for worker in workers],
        search_queries=[worker.query for worker in workers],
        workers=workers,
        max_sources=budget.max_sources,
        min_evidence_items=budget.min_evidence_items,
        judge_threshold=budget.judge_threshold,
        repair_iterations=budget.repair_iterations,
        guardrails=create_research_goal(request).guardrails,
        source="contract",
    )


def plan_from_targeted_queries(targeted_queries: list[str], state: ResearchStateStore) -> ResearchPlan:
    new_queries = [
        " ".join(query.split())
        for query in targeted_queries
        if query and " ".join(query.split()) not in state.query_history
    ][: state.budget_ledger.budget.max_search_workers]
    if not new_queries:
        return state.plan
    workers = [
        SearchWorkerPlan(
            question=f"Follow-up: {query}",
            query=query[:220],
            rationale="Lead agent targeted follow-up to fill coverage gaps.",
            max_results=4,
        )
        for query in new_queries
    ]
    return ResearchPlan(
        research_profile=state.plan.research_profile,
        questions=[worker.question for worker in workers],
        search_queries=[worker.query for worker in workers],
        workers=workers,
        max_sources=state.plan.max_sources,
        min_evidence_items=state.plan.min_evidence_items,
        judge_threshold=state.plan.judge_threshold,
        repair_iterations=state.plan.repair_iterations,
        guardrails=state.plan.guardrails,
        source="reflection",
    )


def update_contract_from_evidence(state: ResearchStateStore) -> None:
    for cell in state.contract.cells:
        if not cell.required or cell.status == "not_applicable":
            continue
        matches: list[EvidenceItem] = []
        for item in state.evidence.items:
            if _evidence_supports_cell(item, cell):
                matches.append(item)
        if not matches:
            if cell.status not in {"partial", "filled"}:
                cell.status = "empty"
                cell.evidence_ids = []
                cell.confidence = 0.0
            continue
        cell.evidence_ids = [item.source_id for item in matches]
        cell.confidence = max(item.confidence for item in matches)
        cell.status = "filled" if len(matches) >= 2 or cell.confidence >= 0.72 else "partial"
        for item in matches:
            if cell.cell_id not in item.supports_cells:
                item.supports_cells.append(cell.cell_id)


def reflect(request: AgentV3Request, state: ResearchStateStore) -> ReflectionDecision:
    open_cells = state.contract.open_cells()
    partial_cells = state.contract.partial_cells()
    coverage = state.contract.coverage_ratio()
    if not open_cells and coverage >= 1.0:
        return ReflectionDecision(
            sufficient=True,
            terminate_reason="Coverage contract fully satisfied.",
            coverage_ratio=coverage,
            next_action="publish",
            source="heuristic",
        )
    if state.budget_ledger.stopped:
        return ReflectionDecision(
            sufficient=True,
            terminate_reason=state.budget_ledger.stop_reason or "budget exhausted",
            coverage_ratio=coverage,
            next_action="stop_with_gaps" if open_cells else "publish",
            source="heuristic",
        )
    exhausted_cells = [cell for cell in open_cells if cell.attempts >= _max_attempts_per_cell(request)]
    if open_cells and len(exhausted_cells) == len(open_cells):
        return ReflectionDecision(
            sufficient=True,
            open_dimensions=_dedupe([cell.dimension for cell in open_cells]),
            open_subjects=_dedupe([cell.subject for cell in open_cells]),
            terminate_reason="Remaining cells have already had targeted follow-up attempts.",
            coverage_ratio=coverage,
            next_action="stop_with_gaps",
            source="heuristic",
        )
    try:
        response = model_client.complete(
            [
                {"role": "system", "content": REFLECTION_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "objective": state.brief.objective,
                            "coverage_ratio": coverage,
                            "open_cells": [
                                {"subject": cell.subject, "dimension": cell.dimension, "attempts": cell.attempts}
                                for cell in open_cells[:14]
                            ],
                            "partial_cells": [
                                {"subject": cell.subject, "dimension": cell.dimension, "notes": cell.notes}
                                for cell in partial_cells[:8]
                            ],
                            "queries_already_tried": state.query_history[-10:],
                            "source_count": len(state.source_inventory),
                            "iteration": state.iteration,
                            "budget_remaining": {
                                "tool_calls": state.budget_ledger.remaining_tool_calls(),
                                "source_reads": state.budget_ledger.remaining_source_reads(),
                            },
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            max_tokens=500,
            timeout_s=15,
        )
        payload = _parse_json(response.text)
        decision = ReflectionDecision.model_validate(payload)
        decision.coverage_ratio = coverage
        decision.model_used = response.model_used
        decision.latency_ms = response.latency_ms
        decision.cost_usd = response.cost_usd
        decision.source = "llm"
        if decision.sufficient and decision.next_action == "continue":
            decision.next_action = "publish" if not open_cells else "stop_with_gaps"
        return decision
    except Exception as exc:
        logger.warning("agent_v3 reflection failed; using deterministic follow-up: %s", exc)
        queries = [_targeted_query(cell.subject, [cell.dimension], request.message) for cell in open_cells[:4]]
        return ReflectionDecision(
            sufficient=not queries,
            open_dimensions=_dedupe([cell.dimension for cell in open_cells]),
            open_subjects=_dedupe([cell.subject for cell in open_cells]),
            targeted_queries=queries,
            terminate_reason=f"Reflection agent failed: {exc}" if not queries else None,
            coverage_ratio=coverage,
            next_action="continue" if queries else "stop_with_gaps",
            source="heuristic",
        )


def verify_citations_semantically(answer: str, evidence: EvidencePack) -> CitationVerification:
    if not answer or not evidence.items:
        return CitationVerification(source="skipped")
    evidence_index = {
        item.source_id: f"[{item.source_id}] {item.title}\nURL: {item.url}\nEvidence: {item.evidence[:500]}"
        for item in evidence.items
    }
    cited_ids = set(re.findall(r"\[(S\d+)\]", answer))
    hallucinated = sorted(cited_ids - set(evidence_index))
    if not cited_ids:
        return CitationVerification(
            repair_needed=True,
            repair_instruction="The answer contains no [S#] citations. Add citations for factual claims.",
            source="heuristic",
        )
    try:
        response = model_client.complete(
            [
                {"role": "system", "content": CITATION_VERIFICATION_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "answer": answer[:3500],
                            "evidence_pack": list(evidence_index.values()),
                            "hallucinated_citations_detected": hallucinated,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            max_tokens=700,
            timeout_s=20,
        )
        payload = _parse_json(response.text)
        result = CitationVerification.model_validate(payload)
        result.hallucinated_citations = sorted(set(result.hallucinated_citations) | set(hallucinated))
        result.repair_needed = bool(result.repair_needed or result.unsupported_claims or result.hallucinated_citations)
        if result.repair_needed and not result.repair_instruction:
            result.repair_instruction = _citation_repair_instruction(result)
        result.model_used = response.model_used
        result.latency_ms = response.latency_ms
        result.cost_usd = response.cost_usd
        result.source = "llm"
        return result
    except Exception as exc:
        logger.warning("agent_v3 citation verification failed; using heuristic fallback: %s", exc)
        return CitationVerification(
            hallucinated_citations=hallucinated,
            repair_needed=bool(hallucinated),
            repair_instruction="Remove or fix hallucinated citation markers." if hallucinated else "",
            source="heuristic",
        )


def judge_research_final(request: AgentV3Request, state: ResearchStateStore, answer: str) -> JudgeVerdict:
    issues: list[str] = []
    score = 0.35
    if state.evidence.items:
        score += min(0.20, 0.035 * len(state.evidence.items))
    coverage = state.contract.coverage_ratio()
    score += coverage * 0.25
    citation_count = len(re.findall(r"\[S\d+\]", answer or ""))
    if citation_count:
        score += min(0.15, 0.035 * citation_count)
    else:
        issues.append("No source citations in answer.")
    if len(answer or "") < 250:
        score -= 0.10
        issues.append("Answer too short for deep research.")
    if state.plan.research_profile == "technical_architecture" and request.research_level == "deep":
        section_count = len(re.findall(r"(?m)^(?:#{1,3}\s+|\d+\.\s+)[A-Z0-9][^\n]{3,}", answer or ""))
        required_terms = [
            "orchestr",
            "workflow",
            "evidence",
            "guardrail",
            "judge",
            "runtime",
            "failure",
            "budget",
            "trace",
            "source",
        ]
        missing_terms = [term for term in required_terms if term not in (answer or "").lower()]
        technical_sources = [
            item for item in state.evidence.items if score_technical_density(Source(title=item.title, url=item.url, content=item.evidence)) >= 0.35
        ]
        if len(answer or "") < 4500:
            score -= 0.18
            issues.append("Deep technical architecture report is too short; expected a detailed multi-section report.")
        if section_count < 8:
            score -= 0.12
            issues.append("Technical architecture report lacks enough concrete sections.")
        if missing_terms:
            score -= min(0.16, 0.025 * len(missing_terms))
            issues.append("Technical architecture report misses required implementation concepts: " + ", ".join(missing_terms[:6]))
        if len(technical_sources) < 4:
            score -= 0.12
            issues.append("Evidence pack has too few technically dense sources.")
    open_cells = state.contract.open_cells()
    if open_cells:
        score -= min(0.18, 0.025 * len(open_cells))
        issues.append(
            f"{len(open_cells)} required coverage cell(s) remain empty: "
            + ", ".join(f"{cell.subject}/{cell.dimension}" for cell in open_cells[:5])
        )
    if state.evidence.contradictions:
        score -= 0.05
        issues.append("Contradictions in evidence should be surfaced.")
    score = max(0.0, min(1.0, score))
    threshold = state.plan.judge_threshold or 0.78
    if score >= threshold and not open_cells:
        return JudgeVerdict(can_publish=True, repair_needed=False, score=score, issues=issues, next_action="publish")
    if open_cells and not state.budget_ledger.stopped and state.iteration < _max_iterations_for(request):
        return JudgeVerdict(
            can_publish=False,
            repair_needed=False,
            score=score,
            issues=issues,
            specific_gaps=[f"{cell.subject}/{cell.dimension}" for cell in open_cells[:8]],
            next_action="research_more",
        )
    repair_instruction = " ".join(issues) or "Improve the answer with better citation use and explicit caveats."
    if open_cells:
        repair_instruction += " Explicitly disclose unresolved public-evidence gaps: " + "; ".join(
            f"{cell.subject} {cell.dimension}" for cell in open_cells[:6]
        )
    return JudgeVerdict(
        can_publish=score >= max(0.55, threshold - 0.20),
        repair_needed=True,
        repair_instruction=repair_instruction,
        specific_gaps=[f"{cell.subject}/{cell.dimension}" for cell in open_cells[:8]],
        score=score,
        issues=issues,
        next_action="repair_answer" if score >= 0.45 else "stop_with_gaps",
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
                            "conversation_context": request.conversation_context[-5000:] if request.conversation_context else "",
                            "quality_mode": request.quality_mode,
                            "research_level": request.research_level,
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
        plan.max_sources = min(plan.max_sources, goal.budget.max_sources)
        plan.min_evidence_items = min(plan.min_evidence_items, goal.budget.min_evidence_items)
        plan.judge_threshold = goal.budget.judge_threshold
        plan.repair_iterations = goal.budget.repair_iterations
        plan.workers = plan.workers[: goal.budget.max_search_workers]
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
    report_contract = _synthesis_report_contract(plan.research_profile, request)
    return model_client.simple_completion(
        SYNTHESIS_PROMPT,
        (
            f"{request.conversation_context}\n\n" if request.conversation_context else ""
        )
        + (
            f"User request:\n{request.message}\n\n"
            f"Research profile: {plan.research_profile}\n\n"
            f"Required deliverable shape:\n{report_contract}\n\n"
            f"Research questions:\n{json.dumps(plan.questions, ensure_ascii=False)}\n\n"
            f"Evidence pack:\n{evidence_context}\n\n"
            f"Known gaps:\n{json.dumps(evidence.gaps, ensure_ascii=False)}"
        ),
        max_tokens=_synthesis_token_budget(request, plan),
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
            f"{request.conversation_context}\n\n" if request.conversation_context else ""
        )
        + (
            f"User request:\n{request.message}\n\n"
            f"Original answer:\n{answer}\n\n"
            f"Judge feedback:\n{judge.model_dump_json()}\n\n"
            f"Evidence pack:\n{evidence_context}\n\n"
            f"Research questions:\n{json.dumps(plan.questions, ensure_ascii=False)}"
        ),
        max_tokens=_synthesis_token_budget(request, plan),
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
        technical_density = score_technical_density(source) if plan.research_profile == "technical_architecture" else 0.0
        content_bonus = 0.08 if source.content else 0.0
        if plan.research_profile == "technical_architecture":
            score = max(
                0.0,
                min(1.0, (authority * 0.25) + (relevance * 0.30) + (technical_density * 0.40) + content_bonus),
            )
        else:
            score = max(0.0, min(1.0, (authority * 0.45) + (relevance * 0.45) + content_bonus))
        ranked.append(
            RankedSource(
                source=source,
                rank=0,
                score=score,
                source_type=source_type,
                authority=authority,
                relevance=relevance,
                rationale=(
                    f"{source_type} source; authority={authority:.2f}; relevance={relevance:.2f}; "
                    f"technical_density={technical_density:.2f}"
                ),
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


class LeadResearchAgent:
    """Lead-agent controller for deep research.

    Workers collect and read sources, but this lead owns the research state,
    coverage contract, budget ledger, reflection loop, and publish decision.
    """

    def __init__(
        self,
        request: AgentV3Request,
        tools: Any,
        progress: Callable[[str, str, dict[str, Any]], None] | None = None,
    ):
        self.request = request
        self.tools = tools
        self.progress = progress or (lambda _stage, _message, _data: None)
        self.budget = research_budget_for(request)
        self.ledger = ResearchBudgetLedger(budget=self.budget)

    def run(self) -> dict[str, Any]:
        registry = get_research_registry()
        self._progress(
            "research_registry",
            "Research team is ready.",
            {"registry": registry.public_summary(), "agent_count": len(registry.agents), "mode": "lead_loop"},
        )
        self._progress("research_brief", "Scoping the research objective.", {"agent_id": "research_lead"})
        brief = generate_research_brief(self.request)
        self.ledger.record_model_call(cost_usd=brief.cost_usd, latency_ms=brief.latency_ms)

        self._progress("coverage_contract", "Building the evidence coverage matrix.", {"agent_id": "research_lead"})
        contract = generate_coverage_contract(self.request, brief)
        self.ledger.record_model_call(cost_usd=contract.cost_usd, latency_ms=contract.latency_ms)

        plan = plan_from_contract(self.request, contract, self.budget)
        goal = create_research_goal(self.request)
        state = ResearchStateStore(brief=brief, contract=contract, plan=plan, budget_ledger=self.ledger)
        self._progress(
            "research_goal",
            "Lead research goal and safety limits are set.",
            {
                "goal": goal.model_dump(mode="json"),
                "brief": brief.model_dump(mode="json"),
                "contract": contract.model_dump(mode="json"),
                "budget_ledger": self.ledger.model_dump(mode="json"),
            },
        )

        for iteration in range(1, _max_iterations_for(self.request) + 1):
            if self.ledger.stopped:
                break
            state.iteration = iteration
            self._dispatch_worker_wave(state)
            state.evidence = bind_evidence(
                state.all_sources,
                plan=state.plan,
                max_items=self.budget.max_sources + self.budget.max_deep_links,
            )
            update_contract_from_evidence(state)
            self._progress(
                "coverage_check",
                f"Coverage is {state.contract.coverage_ratio():.0%}; {len(state.contract.open_cells())} cell(s) remain open.",
                {
                    "coverage_ratio": state.contract.coverage_ratio(),
                    "open_cells": [cell.model_dump(mode="json") for cell in state.contract.open_cells()[:10]],
                    "partial_cells": [cell.model_dump(mode="json") for cell in state.contract.partial_cells()[:8]],
                    "iteration": iteration,
                    "budget_ledger": self.ledger.model_dump(mode="json"),
                },
            )
            decision = reflect(self.request, state)
            if decision.model_used:
                self.ledger.record_model_call(cost_usd=decision.cost_usd, latency_ms=decision.latency_ms)
            self._progress(
                "lead_reflection",
                self._reflection_message(decision),
                {
                    "decision": decision.model_dump(mode="json"),
                    "targeted_queries": decision.targeted_queries,
                    "budget_ledger": self.ledger.model_dump(mode="json"),
                },
            )
            if decision.next_action != "continue" or decision.sufficient or not decision.targeted_queries:
                break
            self._mark_attempts_for_open_cells(state)
            state.plan = plan_from_targeted_queries(decision.targeted_queries, state)

        response = self._synthesize_verify_and_judge(state)
        feedback = ResearchFeedbackLoop(
            judge=ResearchJudgeResult(
                status="pass" if response["verdict"].can_publish else "repair",
                score=response["verdict"].score,
                issues=response["verdict"].issues,
                repair_instruction=response["verdict"].repair_instruction,
                can_publish=response["verdict"].can_publish,
            ),
            repaired=response["repaired"],
            repair_attempts=response["repair_attempts"],
            final_score=response["verdict"].score,
        )
        self._progress(
            "research_budget",
            "Lead research budget ledger closed.",
            {
                "stop_reason": self.ledger.stop_reason,
                "coverage_ratio": state.contract.coverage_ratio(),
                "open_cells": len(state.contract.open_cells()),
                "evidence_items": len(state.evidence.items),
                "iterations": state.iteration,
                "budget_ledger": self.ledger.model_dump(mode="json"),
            },
        )
        return {
            "sources": state.all_sources,
            "tool_calls": state.all_tool_calls,
            "evidence": state.evidence,
            "response": response["model_response"],
            "plan": state.plan,
            "feedback": feedback,
        }

    def _dispatch_worker_wave(self, state: ResearchStateStore) -> None:
        self._progress(
            "lead_research_dispatch",
            f"Dispatching worker wave {state.iteration} with {len(state.plan.workers)} worker(s).",
            {
                "iteration": state.iteration,
                "workers": [worker.model_dump(mode="json") for worker in state.plan.workers],
                "agent_id": "research_lead",
            },
        )
        wave_sources: list[Source] = []
        for index, worker in enumerate(state.plan.workers, start=1):
            if not self.ledger.can_start_tool("web_search"):
                break
            if worker.query in state.query_history:
                continue
            state.add_queries([worker.query])
            self._progress(
                "search_worker",
                f"Search worker {index} is looking for evidence.",
                {
                    "agent_id": worker.agent_id,
                    "worker_id": worker.worker_id,
                    "worker_index": index,
                    "query": worker.query,
                    "question": worker.question,
                    "rationale": worker.rationale,
                },
            )
            sources, call = self.tools.search_web(worker.query, max_results=worker.max_results)
            self.ledger.record_tool_call(latency_ms=call.latency_ms, sources_seen=len(sources))
            state.all_tool_calls.append(call)
            public_sources = [source for source in sources if is_public_source_url(source.url)]
            added = state.add_sources(public_sources)
            wave_sources.extend(added)
            provider = call.output.get("provider") if isinstance(call.output, dict) else None
            self._progress(
                "search_worker_provider",
                f"Search worker {index} used {provider or 'the configured search provider chain'}.",
                {
                    "worker_index": index,
                    "provider": provider,
                    "ok": call.ok,
                    "error": call.error,
                    "source_count": len(sources),
                    "public_source_count": len(public_sources),
                    "budget_ledger": self.ledger.model_dump(mode="json"),
                },
            )

        ranked = rank_sources(wave_sources, state.plan)
        selected = [item.source for item in ranked[: self.ledger.remaining_source_reads()]]
        self._progress(
            "source_ranker",
            f"Ranked {len(wave_sources)} candidate source(s).",
            {
                "agent_id": "source_ranker",
                "ranked_sources": [item.model_dump(mode="json") for item in ranked[: state.plan.max_sources]],
            },
        )
        if selected and self.ledger.can_start_tool("read_url") and self.ledger.can_read_more_sources():
            read_urls = [source.url for source in selected if source.url][: self.ledger.remaining_source_reads()]
            self._progress("source_reader", f"Reading {len(read_urls)} selected source page(s).", {"urls": read_urls})
            extracted, call = self.tools.extract_urls(read_urls, max_chars_per_source=3500)
            self.ledger.record_tool_call(latency_ms=call.latency_ms, sources_read=len(read_urls))
            state.all_tool_calls.append(call)
            state.add_sources(extracted)
            provider = call.output.get("provider") if isinstance(call.output, dict) else None
            self._progress(
                "source_reader_result",
                "Source reader finished extracting source text.",
                {"ok": call.ok, "error": call.error, "provider": provider, "source_count": len(extracted)},
            )
        self._follow_deep_links(state, [*wave_sources, *selected])

    def _follow_deep_links(self, state: ResearchStateStore, sources: list[Source]) -> None:
        if self.budget.max_deep_links <= 0 or not self.ledger.can_read_more_sources():
            return
        candidates = extract_deep_link_candidates(sources, max_links=self.budget.max_deep_links)
        urls = [
            candidate.url
            for candidate in candidates
            if candidate.url not in state.source_inventory and is_public_source_url(candidate.url)
        ][: self.ledger.remaining_source_reads()]
        self._progress(
            "deep_link_agent",
            f"Found {len(urls)} useful deep link(s) to inspect.",
            {"links": [candidate.model_dump(mode="json") for candidate in candidates[: len(urls)]]},
        )
        if not urls or not self.ledger.can_start_tool("read_url"):
            return
        extracted, call = self.tools.extract_urls(urls, max_chars_per_source=2500)
        self.ledger.record_tool_call(latency_ms=call.latency_ms, sources_read=len(urls))
        state.all_tool_calls.append(call)
        state.add_sources(extracted)

    def _synthesize_verify_and_judge(self, state: ResearchStateStore) -> dict[str, Any]:
        self._progress(
            "evidence_binder",
            f"Bound {len(state.evidence.items)} evidence item(s).",
            {
                "agent_id": "evidence_binder",
                "coverage": state.evidence.coverage,
                "coverage_contract_ratio": state.contract.coverage_ratio(),
                "gaps": state.evidence.gaps,
                "contradictions": state.evidence.contradictions,
                "evidence_items": [item.model_dump(mode="json") for item in state.evidence.items],
            },
        )
        if not self.ledger.can_start_model("synthesis_agent"):
            answer = self._budget_stopped_answer(state)
            return {
                "model_response": model_client.ModelResponse(
                    text=answer,
                    model_used="budget-ledger",
                    latency_ms=0,
                    cost_usd=0.0,
                ),
                "verdict": judge_research_final(self.request, state, answer),
                "repaired": False,
                "repair_attempts": 0,
            }
        self._progress("synthesis", "Writing one coherent answer from the evidence.", {"agent_id": "synthesis_agent"})
        model_response = synthesize_answer(self.request, state.plan, state.evidence)
        self.ledger.record_model_call(cost_usd=model_response.cost_usd, latency_ms=model_response.latency_ms)
        answer = model_response.text

        citation_result = verify_citations_semantically(answer, state.evidence)
        if citation_result.model_used:
            self.ledger.record_model_call(cost_usd=citation_result.cost_usd, latency_ms=citation_result.latency_ms)
        self._progress(
            "citation_verification",
            "Verified answer citations against source text.",
            {"agent_id": "claim_verifier", "verification": citation_result.model_dump(mode="json")},
        )
        repaired = False
        repair_attempts = 0
        if citation_result.repair_needed and self.ledger.can_start_model("repair_agent"):
            model_response = self._repair_answer(state, answer, citation_result.repair_instruction)
            answer = model_response.text
            repaired = True
            repair_attempts += 1

        verdict = judge_research_final(self.request, state, answer)
        self._progress(
            "research_judge_result",
            f"Research judge recommends {verdict.next_action}.",
            {"agent_id": "research_judge", "verdict": verdict.model_dump(mode="json")},
        )
        if verdict.next_action == "research_more" and not self.ledger.stopped:
            state.plan = plan_from_targeted_queries(
                [_targeted_query(cell.subject, [cell.dimension], self.request.message) for cell in state.contract.open_cells()[:4]],
                state,
            )
            self._mark_attempts_for_open_cells(state)
            self._dispatch_worker_wave(state)
            state.evidence = bind_evidence(
                state.all_sources,
                plan=state.plan,
                max_items=self.budget.max_sources + self.budget.max_deep_links,
            )
            update_contract_from_evidence(state)
            model_response = synthesize_answer(self.request, state.plan, state.evidence)
            self.ledger.record_model_call(cost_usd=model_response.cost_usd, latency_ms=model_response.latency_ms)
            answer = model_response.text
            verdict = judge_research_final(self.request, state, answer)
        if verdict.repair_needed and state.plan.repair_iterations > repair_attempts and self.ledger.can_start_model("repair_agent"):
            model_response = self._repair_answer(state, answer, verdict.repair_instruction)
            repaired = True
            repair_attempts += 1
            verdict = judge_research_final(self.request, state, model_response.text)
        return {
            "model_response": model_response,
            "verdict": verdict,
            "repaired": repaired,
            "repair_attempts": repair_attempts,
        }

    def _repair_answer(self, state: ResearchStateStore, answer: str, instruction: str):
        self._progress("research_repair", "Repairing the answer before publishing.", {"repair_instruction": instruction})
        fake_judge = ResearchJudgeResult(
            status="repair",
            score=0.6,
            repair_instruction=instruction,
            can_publish=False,
        )
        repaired = repair_research_answer(self.request, state.plan, state.evidence, answer, fake_judge)
        self.ledger.record_model_call(cost_usd=repaired.cost_usd, latency_ms=repaired.latency_ms)
        return repaired

    def _mark_attempts_for_open_cells(self, state: ResearchStateStore) -> None:
        for cell in state.contract.open_cells():
            cell.attempts += 1

    def _budget_stopped_answer(self, state: ResearchStateStore) -> str:
        open_cells = state.contract.open_cells()
        gap_text = ""
        if open_cells:
            gap_text = "\n\nUnresolved public-evidence gaps:\n" + "\n".join(
                f"- {cell.subject} / {cell.dimension}" for cell in open_cells[:10]
            )
        return (
            "I gathered evidence but stopped before synthesis because the research budget was exhausted"
            f" ({self.ledger.stop_reason or 'budget stopped'}).{gap_text}"
        )

    def _reflection_message(self, decision: ReflectionDecision) -> str:
        if decision.next_action == "publish":
            return "Lead researcher judged the evidence sufficient."
        if decision.next_action == "stop_with_gaps":
            return "Lead researcher stopped with explicit unresolved gaps."
        return "Lead researcher found gaps and prepared targeted follow-up searches."

    def _progress(self, stage: str, message: str, data: dict[str, Any] | None = None) -> None:
        self.progress(stage, message, data or {})


def lead_research_loop(
    request: AgentV3Request,
    tools: Any,
    progress: Callable[[str, str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    return LeadResearchAgent(request, tools, progress).run()


def classify_source_type(url: str) -> str:
    parsed = urlparse(url or "")
    path = parsed.path.lower()
    host = (parsed.hostname or "").lower()
    if path.endswith(".pdf"):
        return "pdf"
    if "arxiv.org" in host or "papers.ssrn.com" in host or "aclanthology.org" in host:
        return "academic"
    if "github.com" in host or "gitlab.com" in host:
        return "repository"
    if host.endswith(".gov") or ".gov." in host:
        return "government"
    if host.endswith(".edu") or ".edu." in host:
        return "academic"
    if any(token in host for token in ("sec.gov", "who.int", "oecd.org", "worldbank.org", "imf.org")):
        return "primary"
    if any(token in host for token in ("docs.", "developer.", "support.", "help.", "readthedocs", "langchain", "llamaindex")):
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
        "repository": 0.86,
        "documentation": 0.84,
        "pdf": 0.76,
        "news": 0.68,
        "web": 0.52,
    }
    return scores.get(source_type, 0.5)


def score_technical_density(source: Source) -> float:
    text = f"{source.title} {source.url} {source.snippet} {source.content}".lower()
    signals = [
        "architecture",
        "component",
        "workflow",
        "orchestrator",
        "planner",
        "executor",
        "critic",
        "judge",
        "guardrail",
        "retrieval",
        "citation",
        "evidence",
        "schema",
        "state",
        "memory",
        "tool",
        "mcp",
        "api",
        "latency",
        "cost",
        "evaluation",
        "benchmark",
        "failure",
        "retry",
        "queue",
        "event",
        "trace",
        "github",
        "arxiv",
        "implementation",
    ]
    hits = sum(1 for signal in signals if signal in text)
    type_bonus = {
        "academic": 0.28,
        "repository": 0.26,
        "documentation": 0.22,
        "pdf": 0.16,
        "primary": 0.12,
    }.get(classify_source_type(source.url), 0.0)
    content_bonus = 0.12 if len(source.content or "") > 1200 else 0.0
    return max(0.0, min(1.0, type_bonus + min(0.60, hits * 0.035) + content_bonus))


def _synthesis_report_contract(profile: ResearchProfile, request: AgentV3Request) -> str:
    if profile == "technical_architecture":
        return "\n".join(
            [
                "Produce a detailed architectural report with these sections:",
                "1. Executive summary and scope",
                "2. Reference architecture and component map",
                "3. Lead-agent orchestration and planning loop",
                "4. Search/source acquisition workers and provider strategy",
                "5. Source reading, deep-link crawling, and artifact handling",
                "6. Evidence model, coverage contract, citation map, and contradiction handling",
                "7. Reflection loop, gap repair, judge/critic gates, and termination rules",
                "8. Runtime durability, event streaming, budgets, observability, and trace model",
                "9. Guardrails, security controls, and failure modes",
                "10. Implementation roadmap and trade-offs",
                "Use [S#] citations throughout. Avoid generic definitions unless they support a concrete design decision.",
            ]
        )
    if request.output_format in {"docx", "markdown"} or "report" in request.message.lower():
        return "Produce a structured report with clear headings, evidence-backed findings, gaps, and recommendations."
    return "Produce a source-grounded answer with clear headings and cited findings."


def _synthesis_token_budget(request: AgentV3Request, plan: ResearchPlan) -> int:
    if plan.research_profile == "technical_architecture" and request.research_level == "deep":
        return 6500 if request.quality_mode == "executive" else 5200
    if request.output_format in {"docx", "markdown"} or "report" in request.message.lower():
        return 4200 if request.quality_mode == "executive" else 3200
    return 1800 if request.quality_mode == "executive" else 1200


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
    rationale = "Fallback worker from the original request."
    if request.research_level == "easy":
        rationale = "Easy research uses one narrow source-grounding search."
    worker = SearchWorkerPlan(
        question=request.message,
        query=request.message,
        rationale=rationale,
        max_results=min(5, goal.budget.max_sources),
    )
    return ResearchPlan(
        goal_id=goal.id,
        research_profile=infer_research_profile(request.message),
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
    if plan.research_profile == "general":
        plan.research_profile = infer_research_profile(request.message)
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
    minimum_sources = 1 if request.research_level == "easy" else 2
    plan.max_sources = max(minimum_sources, min(goal.budget.max_sources, int(plan.max_sources or goal.budget.max_sources)))
    plan.min_evidence_items = max(1, min(plan.max_sources, int(plan.min_evidence_items or goal.budget.min_evidence_items)))
    plan.judge_threshold = max(0.45, min(0.9, float(plan.judge_threshold or goal.budget.judge_threshold)))
    plan.repair_iterations = max(0, min(goal.budget.repair_iterations, int(plan.repair_iterations or 0)))
    plan.guardrails = plan.guardrails or goal.guardrails
    plan.goal_id = plan.goal_id or goal.id
    for worker in plan.workers:
        worker.max_results = max(1, min(5, int(worker.max_results or 4)))
    return plan


def _derive_fallback_subjects(message: str, brief: ResearchBrief) -> list[str]:
    scoped = [item for item in brief.scope_in if len(item.strip()) > 1]
    if scoped:
        return _dedupe(scoped)[:4]
    candidates = re.split(r"\b(?:vs\.?|versus|and|,|/)\b", message, flags=re.IGNORECASE)
    subjects = [candidate.strip(" .:-") for candidate in candidates if 2 <= len(candidate.strip()) <= 80]
    if len(subjects) >= 2 and any(token in message.lower() for token in ("compare", " vs", "versus")):
        return _dedupe(subjects)[:4]
    return [brief.objective[:80] or message[:80]]


def _derive_fallback_dimensions(criteria: list[str]) -> list[str]:
    text = " ".join(criteria).lower()
    standard = []
    for dimension in ("capabilities", "pricing", "security", "data quality", "risks", "recent developments"):
        if dimension.split()[0] in text:
            standard.append(dimension)
    if standard:
        return _dedupe(standard)[:5]
    return ["capabilities", "evidence", "risks"]


def _tech_arch_anchor_queries(original_message: str) -> list[str]:
    """Return 2-3 tight anchor queries for technical_architecture profile.

    These are designed to surface arxiv papers, GitHub repos, and engineering
    reference material — sources that generic keyword queries miss.
    The queries are short and natural so search engines treat them well.
    """
    msg_lower = (original_message or "").lower()
    # Determine the core subject from the user's message
    if "deep research" in msg_lower or "deep_research" in msg_lower:
        return [
            "agentic deep research multi-agent architecture implementation",
            "LLM research agent planning loop evidence retrieval site:arxiv.org OR site:github.com",
            "autonomous research agent orchestration evidence synthesis 2024",
        ]
    if "multi-agent" in msg_lower or "multi agent" in msg_lower:
        return [
            "multi-agent LLM orchestration architecture patterns",
            "multi-agent AI system design orchestrator planner executor",
            "agentic workflow multi-agent framework implementation site:github.com OR site:arxiv.org",
        ]
    if "rag" in msg_lower or "retrieval" in msg_lower:
        return [
            "RAG architecture retrieval augmented generation production implementation",
            "agentic RAG planning retrieval evidence grounding 2024",
            "retrieval augmented generation system design components site:arxiv.org",
        ]
    # Generic technical architecture fallback
    return [
        f"{original_message[:80]} architecture implementation",
        f"{original_message[:60]} system design components site:arxiv.org OR site:github.com",
    ]


def _targeted_query(subject: str, dimensions: list[str], original: str) -> str:
    subject = " ".join(str(subject or "").split())
    # Pick the single most specific dimension to keep the query tight
    primary_dim = dimensions[0] if dimensions else ""
    primary_dim = " ".join(str(primary_dim or "").split())

    if infer_research_profile(original) == "technical_architecture":
        # Tight subject-focused query — don't pad with keyword lists.
        # The search engine needs a natural, specific query, not a keyword dump.
        # Append one grounding term to bias toward technical sources.
        grounding = _tech_arch_grounding_term(subject)
        query = f"{subject} {primary_dim} {grounding}".strip()
        return query[:180]

    if subject and any(token in subject.lower() for token in ("tavily", "nimble", "you.com", "youcom")):
        return f"{subject} {primary_dim} official docs pricing security API enterprise".strip()[:180]

    base = f"{subject} {primary_dim}".strip()
    return f"{base} {original}".strip()[:220]


def _tech_arch_grounding_term(subject: str) -> str:
    """Return a short grounding suffix that biases search toward technical sources
    without keyword-stuffing. One term only."""
    s = subject.lower()
    if any(t in s for t in ("orchestrat", "lead agent", "planner")):
        return "implementation"
    if any(t in s for t in ("evidence", "citation", "binder")):
        return "architecture"
    if any(t in s for t in ("search", "worker", "provider", "retrieval")):
        return "multi-agent"
    if any(t in s for t in ("reflect", "gap", "repair", "judge", "critic")):
        return "agentic loop"
    if any(t in s for t in ("guardrail", "security", "safety")):
        return "LLM guardrails"
    if any(t in s for t in ("budget", "ledger", "observ", "latency", "cost")):
        return "production"
    if any(t in s for t in ("memory", "state", "stateful")):
        return "stateful agent"
    if any(t in s for t in ("synthesis", "synthesiz")):
        return "RAG synthesis"
    return "agentic AI"


def _evidence_supports_cell(item: EvidenceItem, cell: CoverageCell) -> bool:
    haystack = f"{item.title} {item.url} {item.evidence}".lower()
    subject_tokens = _meaningful_tokens(cell.subject)
    dimension_tokens = _meaningful_tokens(cell.dimension)
    subject_hit = any(token in haystack for token in subject_tokens) if subject_tokens else False
    dimension_hit = any(token in haystack for token in dimension_tokens) if dimension_tokens else False
    if subject_hit and dimension_hit:
        return True
    if subject_hit and cell.dimension.lower() in {"evidence", "coverage", "capabilities"}:
        return True
    return False


def _meaningful_tokens(value: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9.]{3,}", value.lower())
        if token not in {"the", "and", "for", "with", "from", "about", "latest", "research"}
    ]


def _max_attempts_per_cell(request: AgentV3Request) -> int:
    if request.quality_mode == "executive":
        return 4
    return 3 if request.research_level == "deep" else 1


def _max_iterations_for(request: AgentV3Request) -> int:
    return 4 if request.quality_mode == "executive" else 3


def _citation_repair_instruction(result: CitationVerification) -> str:
    parts: list[str] = []
    if result.unsupported_claims:
        parts.append("Correct or remove unsupported claims: " + "; ".join(result.unsupported_claims[:3]))
    if result.hallucinated_citations:
        parts.append("Remove or fix hallucinated citation markers: " + ", ".join(result.hallucinated_citations[:6]))
    return " ".join(parts) or "Repair citation support."


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
