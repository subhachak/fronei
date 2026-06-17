from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from ipaddress import ip_address
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from app.config import get_settings
from app.services.agent_v3 import model_client
from app.services.agent_v3.models import AgentV3Request, Source, ToolCall, new_id

logger = logging.getLogger(__name__)

MAX_PARALLEL_READ_BATCHES = 4
MAX_PARALLEL_READ_BATCHES_DEEP = 6
MAX_URLS_PER_READ_BATCH = 6

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
    max_results_per_worker: int = 4
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
    discovery_domain: Literal["general", "academic", "repository", "documentation", "news", "primary"] = "general"

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
    query: str = ""
    provider: str = ""


class EvidenceClaim(BaseModel):
    claim_id: str = Field(default_factory=lambda: new_id("claim"))
    source_id: str
    text: str
    quote: str = ""
    claim_type: Literal[
        "policy",
        "timeline",
        "price",
        "statistic",
        "capability",
        "anecdote",
        "interpretation",
        "architecture",
        "implementation",
        "tradeoff",
        "failure",
        "unknown",
    ] = "unknown"
    claim_role: Literal[
        "official_policy",
        "operational_reality",
        "expert_interpretation",
        "anecdotal_case",
        "statistical_data",
        "technical_design",
        "implementation_detail",
        "background_context",
    ] = "background_context"
    freshness_risk: Literal["low", "medium", "high", "unknown"] = "unknown"
    confidence: float = 0.5
    source_title: str = ""
    source_url: str = ""


class EvidencePack(BaseModel):
    items: list[EvidenceItem] = Field(default_factory=list)
    claims: list[EvidenceClaim] = Field(default_factory=list)
    architecture_cards: list["ArchitectureExtractionCard"] = Field(default_factory=list)
    coverage: float = 0.0
    gaps: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)


class ArchitectureExtractionCard(BaseModel):
    card_id: str = Field(default_factory=lambda: new_id("archcard"))
    system: str
    source_id: str
    source_title: str = ""
    source_url: str = ""
    architecture_pattern: str = ""
    agent_roles: list[str] = Field(default_factory=list)
    state_objects: list[str] = Field(default_factory=list)
    tools_or_renderers: list[str] = Field(default_factory=list)
    validation_loop: str = ""
    failure_modes: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    lesson_for_agentdeck: str = ""
    quote: str = ""
    confidence: float = 0.5


class SearchWorkerReport(BaseModel):
    worker_id: str
    question: str
    query: str
    assigned_subject: str = ""
    assigned_dimension: str = ""
    sources: list[Source] = Field(default_factory=list)
    claims: list[EvidenceClaim] = Field(default_factory=list)
    self_assessed_confidence: float = 0.0
    missing_evidence: list[str] = Field(default_factory=list)
    retry_queries: list[str] = Field(default_factory=list)
    provider_attempts: list[dict[str, Any]] = Field(default_factory=list)


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
    worker_reports: list[SearchWorkerReport] = Field(default_factory=list)
    iteration: int = 0
    budget_ledger: ResearchBudgetLedger = Field(default_factory=lambda: ResearchBudgetLedger(budget=ResearchBudget()))

    def add_sources(self, sources: list[Source]) -> list[Source]:
        new_sources: list[Source] = []
        for source in sources:
            if not source.url:
                continue
            if source.url in self.source_inventory:
                existing = next((item for item in self.all_sources if item.url == source.url), None)
                if existing is not None:
                    _merge_source_detail(existing, source)
                continue
            if source.url:
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
For deep technical reports, write expansively: target 10-14 substantial sections,
include named examples from sources, compare patterns, and avoid compressing the
answer into an executive summary unless the user explicitly asks for brevity.
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
            max_results_per_worker=3,
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
            max_search_workers=10,
            max_results_per_worker=12,
            max_sources=32,
            min_evidence_items=14,
            repair_iterations=2,
            judge_threshold=0.78,
            max_tool_calls=72,
            max_model_calls=24,
            max_cost_usd=1.25,
            max_elapsed_ms=600_000,
            max_deep_links=28,
        )
    return ResearchBudget(
        max_search_workers=3,
        max_results_per_worker=6,
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
            role="research_brief",
            quality_mode=request.quality_mode,
            max_tokens=900 if request.research_level == "deep" else 600,
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
            role="coverage_contract",
            quality_mode=request.quality_mode,
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
                max_results=budget.max_results_per_worker,
            )
        )
        if len(workers) >= budget.max_search_workers:
            break
    if not workers:
        workers = _fallback_plan(request, create_research_goal(request)).workers

    # For technical_architecture + deep, include anchor queries that reliably
    # surface arxiv papers, GitHub repos, and engineering reference material.
    # Keep only a small fixed slice so contract-cell workers still execute.
    profile = infer_research_profile(request.message)
    anchor_workers: list[SearchWorkerPlan] = []
    if profile == "technical_architecture" and request.research_level == "deep":
        anchor_queries = _tech_arch_anchor_queries(request.message)
        existing_queries = {w.query for w in workers}
        anchor_workers = [
            SearchWorkerPlan(
                question=f"Anchor: {q}",
                query=q,
                rationale="Profile-level anchor to seed technically dense sources.",
                max_results=budget.max_results_per_worker,
                discovery_domain=_domain_for_query(q),
            )
            for q in anchor_queries
            if q not in existing_queries
        ]

    domain_workers: list[SearchWorkerPlan] = []
    if request.research_level == "deep":
        existing_queries = {w.query for w in workers} | {w.query for w in anchor_workers}
        domain_workers = [
            worker for worker in _domain_discovery_workers(request, profile, budget) if worker.query not in existing_queries
        ]

    if request.research_level == "deep":
        workers = _compose_deep_worker_wave(
            contract_workers=workers,
            anchor_workers=anchor_workers,
            domain_workers=domain_workers,
            max_workers=budget.max_search_workers,
        )
    elif anchor_workers:
        workers = _dedupe_workers(anchor_workers[:2] + workers)[: budget.max_search_workers]

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


def _dedupe_workers(workers: list[SearchWorkerPlan]) -> list[SearchWorkerPlan]:
    seen: set[str] = set()
    result: list[SearchWorkerPlan] = []
    for worker in workers:
        key = " ".join((worker.query or worker.question).lower().split())
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(worker)
    return result


def _compose_deep_worker_wave(
    *,
    contract_workers: list[SearchWorkerPlan],
    anchor_workers: list[SearchWorkerPlan],
    domain_workers: list[SearchWorkerPlan],
    max_workers: int,
) -> list[SearchWorkerPlan]:
    """Mix broad discovery with contract-targeted work without starving either."""
    if max_workers <= 0:
        return []
    if not contract_workers:
        return _dedupe_workers(domain_workers + anchor_workers)[:max_workers]

    discovery_cap = min(4, max(2, max_workers // 3))
    domain_cap = min(2, discovery_cap)
    anchor_cap = max(0, discovery_cap - domain_cap)
    selected = _dedupe_workers(
        domain_workers[:domain_cap]
        + anchor_workers[:anchor_cap]
        + contract_workers
        + domain_workers[domain_cap:]
        + anchor_workers[anchor_cap:]
    )
    return selected[:max_workers]


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
        worker_matches = [
            report
            for report in state.worker_reports
            if report.assigned_subject == cell.subject
            and report.assigned_dimension == cell.dimension
            and report.claims
            and report.self_assessed_confidence >= 0.42
        ]
        if worker_matches:
            best = max(worker_matches, key=lambda report: report.self_assessed_confidence)
            claim_source_ids = _dedupe([claim.source_id for claim in best.claims])
            cell.evidence_ids = claim_source_ids
            cell.confidence = best.self_assessed_confidence
            cell.status = "filled" if best.self_assessed_confidence >= 0.68 and len(best.claims) >= 2 else "partial"
            cell.notes = (
                f"Worker {best.worker_id} reported {len(best.claims)} typed claim(s); "
                f"confidence={best.self_assessed_confidence:.2f}."
            )
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
                            "worker_reports": [
                                {
                                    "question": report.question,
                                    "query": report.query,
                                    "assigned_subject": report.assigned_subject,
                                    "assigned_dimension": report.assigned_dimension,
                                    "confidence": report.self_assessed_confidence,
                                    "claim_count": len(report.claims),
                                    "missing_evidence": report.missing_evidence,
                                    "retry_queries": report.retry_queries,
                                }
                                for report in state.worker_reports[-10:]
                            ],
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
            role="reflection",
            quality_mode=request.quality_mode,
            max_tokens=900 if request.research_level == "deep" else 500,
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
        item.source_id: f"[{item.source_id}] {item.title}\nURL: {item.url}\nEvidence: {item.evidence[:1600]}"
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
                            "answer": answer[:10000],
                            "evidence_pack": list(evidence_index.values()),
                            "hallucinated_citations_detected": hallucinated,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            role="citation_verifier",
            quality_mode="standard",
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
        technical_claims = [
            claim
            for claim in state.evidence.claims
            if claim.claim_type in {"architecture", "implementation", "tradeoff", "failure", "statistic"}
        ]
        if len(answer or "") < 4500:
            score -= 0.28
            issues.append("Deep technical architecture report is far too short; expected a detailed multi-section report.")
        elif len(answer or "") < 9000:
            score -= 0.16
            issues.append("Deep technical architecture report is still short for deep mode; expand with concrete implementation detail.")
        if section_count < 10:
            score -= 0.12
            issues.append("Technical architecture report lacks enough concrete sections for deep mode.")
        if missing_terms:
            score -= min(0.16, 0.025 * len(missing_terms))
            issues.append("Technical architecture report misses required implementation concepts: " + ", ".join(missing_terms[:6]))
        if len(technical_sources) < 4:
            score -= 0.12
            issues.append("Evidence pack has too few technically dense sources.")
        if len(technical_claims) < max(8, state.plan.min_evidence_items):
            score -= 0.12
            issues.append("Evidence pack has too few typed technical claims for a deep architecture report.")
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
            role="research_planner",
            quality_mode=request.quality_mode,
            max_tokens=1000 if request.research_level == "deep" else 600,
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


def build_research_plan_preview(request: AgentV3Request) -> dict[str, Any]:
    """Build a human-reviewable research plan without executing web tools."""
    started = time.perf_counter()
    preview_request = request.model_copy(update={"research_level": "deep"})
    brief = generate_research_brief(preview_request)
    contract = generate_coverage_contract(preview_request, brief)
    budget = research_budget_for(preview_request)
    plan = plan_from_contract(preview_request, contract, budget)
    investigate = _plan_preview_investigation_items(brief, contract, plan)
    source_strategy = _plan_preview_source_strategy(brief.research_profile, plan)
    return {
        "title": _plan_preview_title(brief, request),
        "goal": brief.objective,
        "audience": brief.audience,
        "research_profile": brief.research_profile,
        "research_level": "deep",
        "output_format": request.output_format,
        "estimated_duration": "Ready in a few minutes",
        "workflow": [
            {"label": "Research websites", "description": "Run domain-specific discovery lanes and build a broad source candidate inventory."},
            {"label": "Analyze results", "description": "Rank sources, follow reference links, bind typed evidence, and retry weak worker results."},
            {"label": "Create report", "description": "Synthesize, fact-check, and tighten the final answer from verified evidence."},
        ],
        "investigate": investigate,
        "source_strategy": source_strategy,
        "workers": [worker.model_dump(mode="json") for worker in plan.workers],
        "coverage": {
            "subjects": contract.subjects,
            "dimensions": contract.dimensions,
            "required_cells": len([cell for cell in contract.cells if cell.required]),
        },
        "budget": budget.model_dump(mode="json"),
        "model_used": _dedupe([value for value in [brief.model_used, contract.model_used, plan.model_used] if value]),
        "latency_ms": int((time.perf_counter() - started) * 1000) + brief.latency_ms + contract.latency_ms + plan.latency_ms,
        "fallback_reasons": _dedupe(
            [value for value in [brief.fallback_reason, contract.fallback_reason, plan.fallback_reason] if value]
        ),
    }


def _plan_preview_title(brief: ResearchBrief, request: AgentV3Request) -> str:
    if brief.scope_in:
        return " ".join(brief.scope_in[:2])[:90]
    objective = re.sub(r"^(conduct|do|perform)\s+(deep\s+)?research\s+(on|about)?\s*", "", brief.objective, flags=re.I)
    return objective.strip(" .")[:90] or request.message[:90] or "Research plan"


def _plan_preview_investigation_items(
    brief: ResearchBrief,
    contract: CoverageContract,
    plan: ResearchPlan,
) -> list[str]:
    items: list[str] = []
    for criterion in brief.success_criteria:
        cleaned = " ".join(str(criterion).split()).strip()
        if cleaned:
            items.append(cleaned.rstrip(".") + ".")
    if len(items) < 5:
        for worker in plan.workers:
            cleaned = " ".join(worker.question.split()).strip()
            if cleaned and cleaned not in items:
                items.append(cleaned.rstrip(".") + ".")
    if len(items) < 5:
        for subject in contract.subjects:
            for dimension in contract.dimensions[:2]:
                items.append(f"Examine {subject} through the lens of {dimension}.")
                if len(items) >= 7:
                    break
            if len(items) >= 7:
                break
    return _dedupe(items)[:8]


def _plan_preview_source_strategy(profile: ResearchProfile, plan: ResearchPlan) -> list[str]:
    base = [
        "Domain-specific discovery lanes across academic, repository, docs, primary, and general web sources",
        "Web search across the configured provider chain",
        "Candidate source inventory before expensive page reading",
        "Source reading for high-value pages and documents",
        "Source graph expansion from high-value references and repository/docs links",
        "Worker self-evaluation and retry for weak result sets",
        "Typed claim extraction with citation provenance",
        "Fact-check/rewrite pass to replace vague claims with named-source specifics",
    ]
    if profile == "technical_architecture":
        base.extend(["Academic papers, GitHub repositories, framework docs, and engineering write-ups", "Implementation trade-offs, failure modes, and runtime patterns"])
    elif profile == "vendor_comparison":
        base.extend(["Official product docs, pricing pages, marketplace listings, and security/compliance material"])
    elif profile == "regulatory":
        base.extend(["Regulator, government, primary legal, and policy sources where available"])
    return _dedupe(base)


def bind_evidence(
    sources: list[Source],
    plan: ResearchPlan | None = None,
    max_items: int = 8,
    contract: CoverageContract | None = None,
) -> EvidencePack:
    seen: set[str] = set()
    items: list[EvidenceItem] = []
    questions = plan.questions if plan else []
    profile = plan.research_profile if plan else "general"
    for source in sources:
        if not source.url or source.url in seen:
            continue
        seen.add(source.url)
        body = (source.content or source.snippet or "").strip()
        if not body:
            continue
        source_type = classify_source_type(url=source.url)
        # Evidence body cap: academic papers and repos are the richest technical
        # sources — give them more room so synthesis has dense material to work with.
        # Generic web pages are capped lower to avoid diluting the context.
        if profile == "technical_architecture" and source_type in {"academic", "pdf"}:
            body_cap = 7000
        elif profile == "technical_architecture" and source_type in {"repository", "documentation"}:
            body_cap = 5600
        elif profile == "technical_architecture":
            body_cap = 3800
        elif source_type in {"academic", "repository"}:
            body_cap = 3200
        elif source_type in {"documentation", "pdf"}:
            body_cap = 2400
        else:
            body_cap = 900
        passages = _select_evidence_passages(
            source,
            body,
            plan=plan,
            contract=contract,
            body_cap=body_cap,
            max_passages=3 if profile == "technical_architecture" else 1,
        )
        for passage in passages:
            source_id = f"S{len(items) + 1}"
            items.append(
                EvidenceItem(
                    source_id=source_id,
                    question=questions[(len(items) % len(questions))] if questions else "",
                    title=source.title,
                    url=source.url,
                    source_type=source_type,
                    evidence=passage["text"],
                    relevance=max(_estimate_relevance(source, questions), float(passage["score"])),
                    confidence=_passage_confidence(source, passage_score=float(passage["score"])),
                    authority=score_source_authority(source.url),
                    supports_cells=list(passage["cell_ids"]),
                    quoted_text=str(passage["text"])[:500],
                    query=source.query,
                    provider=source.provider,
                )
            )
            if len(items) >= max_items:
                break
        if len(items) >= max_items:
            break
    min_items = plan.min_evidence_items if plan else 1
    coverage = min(1.0, len(items) / max(1, min_items))
    gaps = [] if len(items) >= min_items else [f"Only {len(items)} usable evidence item(s); target is {min_items}."]
    contradictions = detect_contradictions(items)
    pack = EvidencePack(items=items, coverage=coverage, gaps=gaps, contradictions=contradictions)
    pack.claims = extract_evidence_claims(pack, plan=plan)
    pack.architecture_cards = extract_architecture_cards(pack, plan=plan)
    return pack


def extract_evidence_claims(
    evidence: EvidencePack,
    *,
    plan: ResearchPlan | None = None,
    max_claims_per_item: int = 3,
) -> list[EvidenceClaim]:
    claims: list[EvidenceClaim] = []
    query_terms = _claim_query_terms(plan)
    for item in evidence.items:
        item_claim_limit = _max_claims_for_item(item, plan, default=max_claims_per_item)
        candidates: list[tuple[float, str]] = []
        for sentence in _claim_candidate_sentences(item.evidence):
            score = _score_claim_sentence(sentence, item, query_terms=query_terms, plan=plan)
            if score <= 0:
                continue
            candidates.append((score, sentence))
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        for score, sentence in candidates[:item_claim_limit]:
            claims.append(
                EvidenceClaim(
                    source_id=item.source_id,
                    text=sentence[:650],
                    quote=sentence[:500],
                    claim_type=_claim_type_for_text(sentence),
                    claim_role=_claim_role_for_text(sentence, item),
                    freshness_risk=_freshness_risk_for_text(sentence),
                    confidence=max(0.35, min(0.94, item.confidence + min(0.20, score * 0.05))),
                    source_title=item.title,
                    source_url=item.url,
                )
            )
    claims.sort(key=lambda claim: (claim.confidence, _claim_type_priority(claim.claim_type)), reverse=True)
    max_claims = 80 if plan and plan.research_profile == "technical_architecture" else 32
    return claims[:max_claims]


def _max_claims_for_item(item: EvidenceItem, plan: ResearchPlan | None, *, default: int) -> int:
    if not plan:
        return default
    if plan.research_profile == "technical_architecture":
        if item.source_type in {"academic", "repository"}:
            return 7
        if item.source_type in {"documentation", "pdf"}:
            return 6
        return 5
    if item.source_type in {"academic", "repository", "documentation", "pdf"}:
        return max(default, 5)
    return default


def extract_architecture_cards(
    evidence: EvidencePack,
    *,
    plan: ResearchPlan | None = None,
    max_cards: int = 18,
) -> list[ArchitectureExtractionCard]:
    if not plan or plan.research_profile != "technical_architecture":
        return []
    cards: list[ArchitectureExtractionCard] = []
    for item in evidence.items:
        text = item.evidence or ""
        system = _architecture_system_name(item, text)
        card = ArchitectureExtractionCard(
            system=system,
            source_id=item.source_id,
            source_title=item.title,
            source_url=item.url,
            architecture_pattern=_extract_architecture_pattern(text),
            agent_roles=_extract_architecture_terms(text, _AGENT_ROLE_TERMS, limit=8),
            state_objects=_extract_architecture_terms(text, _STATE_OBJECT_TERMS, limit=8),
            tools_or_renderers=_extract_architecture_terms(text, _TOOL_RENDERER_TERMS, limit=8),
            validation_loop=_extract_validation_loop(text),
            failure_modes=_extract_architecture_terms(text, _FAILURE_MODE_TERMS, limit=8),
            metrics=_extract_metric_snippets(text),
            lesson_for_agentdeck=_lesson_for_agentdeck(item, text),
            quote=_best_architecture_quote(text),
            confidence=_architecture_card_confidence(item, text),
        )
        if _architecture_card_has_signal(card):
            cards.append(card)
    cards.sort(key=lambda card: card.confidence, reverse=True)
    return cards[:max_cards]


_AGENT_ROLE_TERMS = [
    "orchestrator",
    "lead agent",
    "planner",
    "researcher",
    "worker",
    "subagent",
    "writer",
    "critic",
    "reviewer",
    "verifier",
    "citation agent",
    "formatter",
    "layout agent",
    "executor",
]

_STATE_OBJECT_TERMS = [
    "outline",
    "research brief",
    "coverage contract",
    "state graph",
    "memory",
    "scratchpad",
    "evidence pack",
    "citation map",
    "schema",
    "json",
    "slide spec",
    "render plan",
    "theme",
    "design tokens",
]

_TOOL_RENDERER_TERMS = [
    "pptxgenjs",
    "python-pptx",
    "python-docx",
    "openpyxl",
    "html",
    "css",
    "soffice",
    "pdftoppm",
    "vlm",
    "vision model",
    "mcp",
    "langgraph",
    "rag",
    "github",
]

_FAILURE_MODE_TERMS = [
    "hallucination",
    "overflow",
    "overlap",
    "truncation",
    "invalid json",
    "invalid code",
    "corrupt",
    "latency",
    "cost",
    "context",
    "incoherent",
    "disjoint",
    "security",
    "sandbox",
]


def _architecture_system_name(item: EvidenceItem, text: str) -> str:
    haystack = f"{item.title} {item.url} {text}".lower()
    known = [
        "AgentDeck",
        "PPTAgent",
        "AutoPresent",
        "STORM",
        "LongWriter",
        "AgentWrite",
        "SlideBot",
        "PPTEval",
        "PaperFit",
        "LangGraph",
        "Open Deep Research",
        "Gamma",
        "Microsoft Copilot",
        "Google Gemini",
        "Anthropic",
        "PptxGenJS",
        "Presenton",
        "MASFactory",
    ]
    for name in known:
        if name.lower() in haystack:
            return name
    host = urlparse(item.url or "").netloc.lower().replace("www.", "")
    return host or item.title[:80] or "Unknown system"


def _extract_architecture_pattern(text: str) -> str:
    candidates = _claim_candidate_sentences(text)
    pattern_terms = ("architecture", "orchestr", "workflow", "pipeline", "plan", "render", "critique", "revise", "agent")
    for sentence in candidates:
        lower = sentence.lower()
        if any(term in lower for term in pattern_terms):
            return sentence[:500]
    return candidates[0][:500] if candidates else ""


def _extract_architecture_terms(text: str, terms: list[str], *, limit: int) -> list[str]:
    lower = (text or "").lower()
    found = [term for term in terms if term in lower]
    return _dedupe(found)[:limit]


def _extract_validation_loop(text: str) -> str:
    candidates = _claim_candidate_sentences(text)
    validation_terms = ("validate", "verification", "verify", "judge", "critic", "render", "inspect", "qa", "feedback", "repair")
    for sentence in candidates:
        if any(term in sentence.lower() for term in validation_terms):
            return sentence[:500]
    return ""


def _extract_metric_snippets(text: str) -> list[str]:
    snippets: list[str] = []
    for sentence in _claim_candidate_sentences(text):
        lower = sentence.lower()
        if re.search(r"\b\d+(?:\.\d+)?\s*(?:%|percent|x|×|tokens|pearson|score|seconds|minutes|ms|calls)\b", lower):
            snippets.append(sentence[:300])
        elif any(term in lower for term in ("benchmark", "correlation", "evaluation", "outperform", "preferred by humans")):
            snippets.append(sentence[:300])
        if len(snippets) >= 5:
            break
    return snippets


def _lesson_for_agentdeck(item: EvidenceItem, text: str) -> str:
    lower = f"{item.title} {item.url} {text}".lower()
    if any(term in lower for term in ("overflow", "overlap", "render", "vision", "vlm", "pdftoppm", "soffice")):
        return "Use render-then-inspect QA with element-level repair before publishing."
    if any(term in lower for term in ("schema", "json", "structured output", "grammar", "validation")):
        return "Keep a schema-validated content contract before rendering."
    if any(term in lower for term in ("orchestrator", "subagent", "worker", "parallel")):
        return "Use a lead-agent plan with bounded specialist workers and a shared spine."
    if any(term in lower for term in ("theme", "brand", "design token", "template")):
        return "Treat the design system as a versioned contract, not a prompt hint."
    if any(term in lower for term in ("citation", "ground", "source", "rag")):
        return "Bind claims to source evidence before synthesis and verify citations after drafting."
    return "Extract the reusable architectural mechanism and map it to AgentDeck's pipeline."


def _best_architecture_quote(text: str) -> str:
    scored: list[tuple[float, str]] = []
    for sentence in _claim_candidate_sentences(text):
        score = 0.0
        lower = sentence.lower()
        if any(term in lower for term in ("architecture", "workflow", "orchestr", "schema", "render", "verify", "agent")):
            score += 2.0
        if re.search(r"\b\d+(?:\.\d+)?\s*(?:%|percent|x|×|tokens|pearson|score)\b", lower):
            score += 2.0
        if any(term in lower for term in ("implementation", "component", "state", "tool", "validation")):
            score += 1.0
        scored.append((score, sentence[:500]))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1] if scored and scored[0][0] > 0 else ""


def _architecture_card_confidence(item: EvidenceItem, text: str) -> float:
    signals = 0
    lower = text.lower()
    for terms in (_AGENT_ROLE_TERMS, _STATE_OBJECT_TERMS, _TOOL_RENDERER_TERMS, _FAILURE_MODE_TERMS):
        if any(term in lower for term in terms):
            signals += 1
    if _extract_metric_snippets(text):
        signals += 1
    return max(0.35, min(0.94, item.confidence + signals * 0.06 + score_technical_density(Source(title=item.title, url=item.url, content=text)) * 0.12))


def _architecture_card_has_signal(card: ArchitectureExtractionCard) -> bool:
    return bool(
        card.architecture_pattern
        or card.agent_roles
        or card.state_objects
        or card.tools_or_renderers
        or card.validation_loop
        or card.metrics
    )


def _claim_candidate_sentences(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    if not normalized:
        return []
    parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", normalized) if part.strip()]
    candidates: list[str] = []
    for part in parts:
        if 45 <= len(part) <= 520 and _looks_like_substantive_claim(part):
            candidates.append(part)
    if candidates:
        return candidates
    return [part for part in parts if 45 <= len(part) <= 520][:4]


def _claim_query_terms(plan: ResearchPlan | None) -> set[str]:
    if not plan:
        return set()
    text = " ".join(
        [
            plan.research_profile,
            *plan.questions,
            *plan.search_queries,
            *[worker.question for worker in plan.workers],
            *[worker.query for worker in plan.workers],
        ]
    )
    return set(_meaningful_tokens(text))


def _score_claim_sentence(
    sentence: str,
    item: EvidenceItem,
    *,
    query_terms: set[str],
    plan: ResearchPlan | None,
) -> float:
    lower = sentence.lower()
    query_hits = sum(1 for term in query_terms if term in lower)
    score = min(10.0, query_hits * 0.65)
    if re.search(r"\b\d+(?:\.\d+)?\s*(?:%|percent|ms|seconds|minutes|hours|days|tokens|calls|usd|\$)\b", lower):
        score += 2.2
    if any(term in lower for term in ("architecture", "orchestr", "workflow", "pipeline", "runtime", "state", "memory", "tool", "agent")):
        score += 1.8
    if any(term in lower for term in ("implementation", "data model", "schema", "queue", "trace", "event", "budget", "guardrail")):
        score += 1.8
    if any(term in lower for term in ("trade-off", "tradeoff", "latency", "cost", "failure", "risk", "limitation", "recovery")):
        score += 1.4
    if item.source_type in {"academic", "repository", "documentation", "pdf"}:
        score += 0.8
    if plan and plan.research_profile == "technical_architecture":
        score += score_technical_density(Source(title=item.title, url=item.url, content=sentence)) * 3.0
    return score


def _claim_type_for_text(text: str) -> str:
    lower = text.lower()
    if any(term in lower for term in ("architecture", "orchestr", "workflow", "pipeline", "component", "topology")):
        return "architecture"
    if any(term in lower for term in ("implementation", "schema", "data model", "queue", "state", "trace", "runtime")):
        return "implementation"
    if any(term in lower for term in ("trade-off", "tradeoff", "latency", "cost", "overhead", "performance")):
        return "tradeoff"
    if any(term in lower for term in ("fail", "failure", "risk", "limitation", "error", "recover", "timeout")):
        return "failure"
    if re.search(r"\b\d+(?:\.\d+)?\s*(?:%|percent|ms|seconds|minutes|hours|days|tokens|calls|\$|usd)\b", lower):
        return "statistic"
    if any(term in lower for term in ("price", "pricing", "costs", "plan", "$")):
        return "price"
    if any(term in lower for term in ("supports", "provides", "offers", "enables", "can ")):
        return "capability"
    if any(term in lower for term in ("according to", "argues", "suggests", "proposes", "observes")):
        return "interpretation"
    if any(term in lower for term in ("must", "required", "policy", "compliance", "shall")):
        return "policy"
    return "unknown"


def _claim_role_for_text(text: str, item: EvidenceItem) -> str:
    lower = text.lower()
    if item.source_type in {"academic", "repository", "documentation"} and any(
        term in lower for term in ("implementation", "schema", "workflow", "runtime", "architecture", "pipeline")
    ):
        return "technical_design"
    if any(term in lower for term in ("implementation", "code", "schema", "api", "runtime", "trace")):
        return "implementation_detail"
    if any(term in lower for term in ("benchmark", "study", "%", "percent", "measured", "dataset")):
        return "statistical_data"
    if any(term in lower for term in ("according to", "argues", "suggests", "we propose")):
        return "expert_interpretation"
    if item.source_type in {"primary", "documentation"}:
        return "official_policy"
    return "background_context"


def _freshness_risk_for_text(text: str) -> Literal["low", "medium", "high", "unknown"]:
    lower = text.lower()
    if re.search(r"\b20(?:2[4-9]|3\d)\b", lower) or any(term in lower for term in ("latest", "current", "recent")):
        return "low"
    if re.search(r"\b20(?:1\d|2[0-3])\b", lower):
        return "medium"
    return "unknown"


def _claim_type_priority(claim_type: str) -> int:
    return {
        "implementation": 7,
        "architecture": 7,
        "tradeoff": 6,
        "failure": 6,
        "statistic": 5,
        "policy": 4,
        "capability": 3,
        "interpretation": 2,
    }.get(claim_type, 1)


def _select_evidence_passages(
    source: Source,
    body: str,
    *,
    plan: ResearchPlan | None,
    contract: CoverageContract | None,
    body_cap: int,
    max_passages: int,
) -> list[dict[str, object]]:
    passages = _candidate_passages(body, max_chars=body_cap)
    if not passages:
        return [{"text": body[:body_cap], "score": 0.5, "cell_ids": []}]
    scored: list[dict[str, object]] = []
    for index, passage in enumerate(passages):
        score = _score_passage(source, passage, plan=plan, contract=contract)
        cell_ids = [
            cell.cell_id
            for cell in (contract.cells if contract else [])
            if _text_supports_cell(f"{source.title} {source.url} {passage}", cell)
        ]
        scored.append({"text": passage[:body_cap], "score": score, "cell_ids": cell_ids, "index": index})
    scored.sort(key=lambda item: (float(item["score"]), -int(item["index"])), reverse=True)
    selected: list[dict[str, object]] = []
    selected_signatures: set[str] = set()
    for passage in scored:
        signature = _passage_signature(str(passage["text"]))
        if signature in selected_signatures:
            continue
        selected.append(passage)
        selected_signatures.add(signature)
        if len(selected) >= max_passages:
            break
    if not selected:
        selected = [scored[0]]
    return selected


def _candidate_passages(body: str, *, max_chars: int) -> list[str]:
    text = re.sub(r"\s+", " ", body or "").strip()
    if not text:
        return []
    raw_parts = [part.strip() for part in re.split(r"(?:\n\s*){2,}", body) if part.strip()]
    if len(raw_parts) >= 2:
        passages: list[str] = []
        for part in raw_parts:
            normalized = re.sub(r"\s+", " ", part).strip()
            if len(normalized) > max_chars:
                passages.extend(_chunk_long_passage(normalized, max_chars=max_chars))
            elif normalized:
                passages.append(normalized)
        return [passage for passage in passages if len(passage) >= 60] or [text[:max_chars]]
    if len(raw_parts) <= 1:
        raw_parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
    passages: list[str] = []
    current = ""
    target_chars = max(650, min(max_chars, 1400))
    for part in raw_parts:
        normalized = re.sub(r"\s+", " ", part).strip()
        if not normalized:
            continue
        if len(normalized) > max_chars:
            for chunk in _chunk_long_passage(normalized, max_chars=max_chars):
                passages.append(chunk)
            current = ""
            continue
        if current and len(current) + len(normalized) + 1 > target_chars:
            passages.append(current)
            current = normalized
        else:
            current = f"{current} {normalized}".strip()
    if current:
        passages.append(current)
    return [passage for passage in passages if len(passage) >= 80] or [text[:max_chars]]


def _chunk_long_passage(text: str, *, max_chars: int) -> list[str]:
    chunks: list[str] = []
    start = 0
    stride = max(500, max_chars - 250)
    while start < len(text):
        chunk = text[start : start + max_chars].strip()
        if chunk:
            chunks.append(chunk)
        start += stride
    return chunks


def _score_passage(
    source: Source,
    passage: str,
    *,
    plan: ResearchPlan | None,
    contract: CoverageContract | None,
) -> float:
    haystack = f"{source.title} {source.url} {passage}".lower()
    query_text = " ".join(
        [
            *(plan.questions if plan else []),
            *(plan.search_queries if plan else []),
            *([worker.question + " " + worker.query for worker in plan.workers] if plan else []),
        ]
    )
    query_tokens = set(_meaningful_tokens(query_text))
    query_hits = sum(1 for token in query_tokens if token in haystack)
    query_score = min(0.30, query_hits * 0.018)
    cell_matches = 0
    if contract:
        cell_matches = sum(1 for cell in contract.cells if _text_supports_cell(haystack, cell))
    cell_score = min(0.30, cell_matches * 0.04)
    technical_score = 0.0
    if plan and plan.research_profile == "technical_architecture":
        technical_score = score_technical_density(Source(title=source.title, url=source.url, content=passage)) * 0.32
    type_score = {
        "academic": 0.12,
        "repository": 0.11,
        "documentation": 0.10,
        "pdf": 0.08,
        "primary": 0.08,
    }.get(classify_source_type(source.url), 0.03)
    length_score = 0.08 if len(passage) > 900 else 0.04 if len(passage) > 350 else 0.0
    return max(0.0, min(1.0, 0.16 + query_score + cell_score + technical_score + type_score + length_score))


def _passage_confidence(source: Source, *, passage_score: float) -> float:
    base = 0.66 if source.content else 0.50
    return max(0.45, min(0.9, base + min(0.18, passage_score * 0.18)))


def _passage_signature(text: str) -> str:
    tokens = _meaningful_tokens(text)[:28]
    return " ".join(tokens)


def synthesize_answer(request: AgentV3Request, plan: ResearchPlan, evidence: EvidencePack):
    architecture_context = _architecture_cards_context(evidence)
    claim_context = "\n".join(
        f"[{claim.source_id}] {claim.claim_type}/{claim.claim_role} "
        f"(confidence={claim.confidence:.2f}, freshness={claim.freshness_risk}): {claim.text}"
        for claim in evidence.claims[:40]
    )
    if not claim_context:
        claim_context = "No typed evidence claims were extracted. Lean on the evidence passages and disclose limits."
    evidence_context = "\n\n".join(
        f"[{item.source_id}] {item.title}\n"
        f"Question: {item.question}\n"
        f"Discovery query: {item.query or 'unknown'}\n"
        f"Provider: {item.provider or 'unknown'}\n"
        f"URL: {item.url}\n"
        f"Evidence: {item.evidence}"
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
            f"Architecture extraction cards:\n{architecture_context}\n\n"
            f"Typed evidence claims:\n{claim_context}\n\n"
            f"Evidence pack:\n{evidence_context}\n\n"
            f"Known gaps:\n{json.dumps(evidence.gaps, ensure_ascii=False)}"
        ),
        max_tokens=_synthesis_token_budget(request, plan),
        role="synthesis",
        quality_mode=request.quality_mode,
        timeout_s=_longform_timeout_s(),
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
        role="repair",
        quality_mode=request.quality_mode,
        timeout_s=_longform_timeout_s(),
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
            type_bonus = {
                "academic": 0.18,
                "repository": 0.17,
                "documentation": 0.14,
                "pdf": 0.13,
                "primary": 0.08,
                "news": -0.04,
            }.get(source_type, 0.0)
            host = urlparse(source.url or "").netloc.lower()
            if "medium.com" in host or "substack.com" in host:
                type_bonus -= 0.08
            score = max(
                0.0,
                min(
                    1.0,
                    (authority * 0.20)
                    + (relevance * 0.24)
                    + (technical_density * 0.38)
                    + type_bonus
                    + content_bonus,
                ),
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


def _select_diverse_ranked_sources(
    ranked: list[RankedSource],
    *,
    limit: int,
    research_level: str,
) -> list[Source]:
    if limit <= 0:
        return []
    host_cap = 5 if research_level == "deep" else 2
    type_targets = {"academic": 8, "repository": 8, "documentation": 8, "primary": 5, "pdf": 6}
    type_minimums = {"academic": 4, "repository": 4, "documentation": 4, "pdf": 2} if research_level == "deep" else {}
    selected: list[RankedSource] = []
    host_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}

    for source_type, minimum in type_minimums.items():
        for item in ranked:
            if len(selected) >= limit or type_counts.get(source_type, 0) >= minimum:
                break
            if item.source_type != source_type:
                continue
            host = urlparse(item.source.url or "").netloc.lower()
            if host_counts.get(host, 0) >= host_cap:
                continue
            selected.append(item)
            host_counts[host] = host_counts.get(host, 0) + 1
            type_counts[source_type] = type_counts.get(source_type, 0) + 1

    for item in ranked:
        if item in selected:
            continue
        host = urlparse(item.source.url or "").netloc.lower()
        source_type = item.source_type
        if host_counts.get(host, 0) >= host_cap:
            continue
        if research_level == "deep" and source_type in type_targets and type_counts.get(source_type, 0) >= type_targets[source_type]:
            continue
        selected.append(item)
        host_counts[host] = host_counts.get(host, 0) + 1
        type_counts[source_type] = type_counts.get(source_type, 0) + 1
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        selected_urls = {item.source.url for item in selected}
        for item in ranked:
            if item.source.url in selected_urls:
                continue
            selected.append(item)
            if len(selected) >= limit:
                break
    return [item.source for item in selected[:limit]]


def extract_deep_link_candidates(sources: list[Source], *, max_links: int = 4) -> list[DeepLinkCandidate]:
    candidates: list[DeepLinkCandidate] = []
    seen: set[str] = set()
    for source in sources:
        for candidate in _domain_specific_link_candidates(source):
            if candidate.url == source.url or candidate.url in seen or not is_public_source_url(candidate.url):
                continue
            seen.add(candidate.url)
            candidates.append(candidate)
            if len(candidates) >= max_links:
                return candidates
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


def _domain_specific_link_candidates(source: Source) -> list[DeepLinkCandidate]:
    url = source.url or ""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    candidates: list[DeepLinkCandidate] = []
    if "arxiv.org" in host:
        arxiv_id = _arxiv_id_from_url(url)
        if arxiv_id:
            candidates.extend(
                [
                    DeepLinkCandidate(url=f"https://arxiv.org/pdf/{arxiv_id}", parent_url=url, reason="arXiv PDF for full paper text."),
                    DeepLinkCandidate(url=f"https://arxiv.org/html/{arxiv_id}", parent_url=url, reason="arXiv HTML paper view when available."),
                ]
            )
    if "github.com" in host:
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2:
            repo = f"https://github.com/{parts[0]}/{parts[1]}"
            candidates.extend(
                [
                    DeepLinkCandidate(url=f"{repo}/blob/main/README.md", parent_url=url, reason="Repository README for implementation context."),
                    DeepLinkCandidate(url=f"{repo}/tree/main/docs", parent_url=url, reason="Repository docs folder for architecture details."),
                ]
            )
    return candidates


def _arxiv_id_from_url(url: str) -> str:
    match = re.search(r"arxiv\.org/(?:abs|pdf|html)/([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)", url)
    return match.group(1) if match else ""


def _source_inventory_summary(sources: list[Source]) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    by_host: dict[str, int] = {}
    for source in sources:
        source_type = classify_source_type(source.url)
        by_type[source_type] = by_type.get(source_type, 0) + 1
        host = urlparse(source.url or "").netloc.lower() or "unknown"
        by_host[host] = by_host.get(host, 0) + 1
    top_hosts = [
        {"host": host, "count": count}
        for host, count in sorted(by_host.items(), key=lambda item: item[1], reverse=True)[:12]
    ]
    return {
        "total": len([source for source in sources if source.url]),
        "by_type": by_type,
        "top_hosts": top_hosts,
    }


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


def _chunk_urls(urls: list[str], *, size: int) -> list[list[str]]:
    chunks: list[list[str]] = []
    for index in range(0, len(urls), max(1, size)):
        chunk = [url for url in urls[index : index + size] if url]
        if chunk:
            chunks.append(chunk)
    return chunks


def _max_parallel_read_batches_for(research_level: str) -> int:
    return MAX_PARALLEL_READ_BATCHES_DEEP if research_level == "deep" else MAX_PARALLEL_READ_BATCHES


def _read_cap_for_batch(urls: list[str], plan: ResearchPlan | None) -> int:
    if plan and plan.research_profile == "technical_architecture":
        if any(classify_source_type(url) in {"academic", "pdf"} for url in urls):
            return 14000
        if any(classify_source_type(url) in {"repository", "documentation"} for url in urls):
            return 10000
        return 6500
    if any(classify_source_type(url) in {"academic", "pdf", "documentation"} for url in urls):
        return 7000
    return 3500


def _assigned_cell_for_worker(worker: SearchWorkerPlan, contract: CoverageContract) -> CoverageCell | None:
    if not contract.cells:
        return None
    haystack = f"{worker.question} {worker.query} {worker.rationale}".lower()
    scored: list[tuple[int, CoverageCell]] = []
    for cell in contract.cells:
        terms = _cell_terms(cell.subject) + _cell_terms(cell.dimension)
        hits = sum(1 for term in terms if term in haystack)
        if hits:
            scored.append((hits, cell))
    if not scored:
        return None
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[0][1]


def _retry_query_for_worker(worker: SearchWorkerPlan, assigned_cell: CoverageCell | None, request: AgentV3Request) -> str:
    if assigned_cell:
        return _targeted_query(assigned_cell.subject, [assigned_cell.dimension], request.message)
    grounding = _tech_arch_grounding_term(worker.question) if "architecture" in request.message.lower() else ""
    return " ".join(part for part in [worker.question, grounding or "primary sources implementation evidence"] if part).strip()


def _source_relevance_for_worker(source: Source, worker: SearchWorkerPlan) -> float:
    question_score = _estimate_relevance(source, [worker.question])
    query_score = _estimate_relevance(source, [worker.query])
    density_bonus = min(0.18, score_technical_density(source) * 0.18)
    return max(question_score, query_score) + density_bonus


def _worker_confidence(
    worker: SearchWorkerPlan,
    sources: list[Source],
    claims: list[EvidenceClaim],
    assigned_cell: CoverageCell | None,
) -> float:
    if not sources:
        return 0.0
    relevance = sum(_source_relevance_for_worker(source, worker) for source in sources) / max(1, len(sources))
    claim_bonus = min(0.28, len(claims) * 0.045)
    authority = max((score_source_authority(source.url) for source in sources if source.url), default=0.0) * 0.14
    assignment_bonus = 0.08 if assigned_cell else 0.0
    return max(0.0, min(1.0, relevance * 0.55 + claim_bonus + authority + assignment_bonus))


def _worker_missing_evidence(
    worker: SearchWorkerPlan,
    sources: list[Source],
    claims: list[EvidenceClaim],
    confidence: float,
    assigned_cell: CoverageCell | None,
) -> list[str]:
    missing: list[str] = []
    if not sources:
        missing.append("No usable public sources found.")
    if confidence < 0.45:
        missing.append("Search results appear weak for the assigned question.")
    if not claims:
        missing.append("No typed evidence claims extracted from selected sources.")
    if assigned_cell and not any(
        _text_supports_cell(f"{claim.text} {claim.source_title} {claim.source_url}", assigned_cell)
        for claim in claims
    ):
        missing.append(f"No claim clearly supports {assigned_cell.subject}/{assigned_cell.dimension}.")
    return missing


def _worker_claim_pack(worker: SearchWorkerPlan, assigned_cell: CoverageCell | None, sources: list[Source], plan: ResearchPlan) -> EvidencePack:
    contract = None
    if assigned_cell:
        contract = CoverageContract(cells=[assigned_cell], subjects=[assigned_cell.subject], dimensions=[assigned_cell.dimension])
    worker_max_sources = 8 if plan.research_profile == "technical_architecture" else 6
    worker_max_items = 8 if plan.research_profile == "technical_architecture" else 6
    worker_plan = ResearchPlan(
        research_profile=plan.research_profile,
        questions=[worker.question],
        search_queries=[worker.query],
        workers=[worker],
        max_sources=min(worker_max_sources, plan.max_sources),
        min_evidence_items=1,
    )
    return bind_evidence(
        sources,
        plan=worker_plan,
        contract=contract,
        max_items=min(worker_max_items, max(1, len(sources) * 2)),
    )


def _worker_report_from_sources(
    worker: SearchWorkerPlan,
    *,
    assigned_cell: CoverageCell | None,
    sources: list[Source],
    plan: ResearchPlan,
    provider_attempts: list[dict[str, Any]],
    retry_queries: list[str] | None = None,
) -> SearchWorkerReport:
    pack = _worker_claim_pack(worker, assigned_cell, sources, plan)
    confidence = _worker_confidence(worker, sources, pack.claims, assigned_cell)
    return SearchWorkerReport(
        worker_id=worker.worker_id,
        question=worker.question,
        query=worker.query,
        assigned_subject=assigned_cell.subject if assigned_cell else "",
        assigned_dimension=assigned_cell.dimension if assigned_cell else "",
        sources=sources,
        claims=pack.claims,
        self_assessed_confidence=confidence,
        missing_evidence=_worker_missing_evidence(worker, sources, pack.claims, confidence, assigned_cell),
        retry_queries=retry_queries or [],
        provider_attempts=provider_attempts,
    )


def _worker_report_message(index: int, report: SearchWorkerReport) -> str:
    if report.self_assessed_confidence >= 0.70:
        strength = "strong"
    elif report.self_assessed_confidence >= 0.45:
        strength = "usable"
    else:
        strength = "weak"
    target = ""
    if report.assigned_subject or report.assigned_dimension:
        target = f" for {report.assigned_subject}/{report.assigned_dimension}".strip()
    retry_note = " after a retry" if report.retry_queries else ""
    return (
        f"Search worker {index} found {strength} evidence{target}{retry_note}: "
        f"{len(report.claims)} claim(s), {len(report.sources)} source(s)."
    )


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


def _specificity_rewrite_issues(answer: str) -> list[str]:
    text = answer or ""
    issues: list[str] = []
    hedged_patterns = [
        r"\b(?:many|some|several|various)\s+(?:systems|sources|providers|teams|organizations)\b",
        r"\b(?:may|might|could|can)\s+(?:help|support|enable|improve|reduce)\b",
        r"\b(?:it is important|it is crucial|it should be noted)\b",
        r"\b(?:generally|typically|often|commonly)\b",
    ]
    for pattern in hedged_patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            issues.append(f"Hedged language matched: {pattern}")
    uncited_substantive = 0
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if _looks_like_substantive_claim(sentence) and "[S" not in sentence:
            uncited_substantive += 1
    if uncited_substantive >= 3:
        issues.append(f"{uncited_substantive} substantive sentence(s) lack direct source citations.")
    return issues[:10]


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
        self._progress(
            "research_brief",
            "Scoping the research objective.",
            {
                "agent_id": "research_lead",
                **model_client.telemetry_for_role("research_brief", quality_mode=self.request.quality_mode),
            },
        )
        brief = generate_research_brief(self.request)
        self.ledger.record_model_call(cost_usd=brief.cost_usd, latency_ms=brief.latency_ms)
        self._progress(
            "research_brief_result",
            f"Research brief used {brief.model_used or 'the configured brief model'}.",
            {
                "agent_id": "research_lead",
                **model_client.telemetry_for_role(
                    "research_brief",
                    quality_mode=self.request.quality_mode,
                    model_used=brief.model_used,
                ),
                "source": brief.source,
                "latency_ms": brief.latency_ms,
                "cost_usd": brief.cost_usd,
            },
        )

        self._progress(
            "coverage_contract",
            "Building the evidence coverage matrix.",
            {
                "agent_id": "research_lead",
                **model_client.telemetry_for_role("coverage_contract", quality_mode=self.request.quality_mode),
            },
        )
        contract = generate_coverage_contract(self.request, brief)
        self.ledger.record_model_call(cost_usd=contract.cost_usd, latency_ms=contract.latency_ms)
        self._progress(
            "coverage_contract_result",
            f"Coverage contract used {contract.model_used or 'the configured contract model'}.",
            {
                "agent_id": "research_lead",
                **model_client.telemetry_for_role(
                    "coverage_contract",
                    quality_mode=self.request.quality_mode,
                    model_used=contract.model_used,
                ),
                "source": contract.source,
                "latency_ms": contract.latency_ms,
                "cost_usd": contract.cost_usd,
            },
        )

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
                contract=state.contract,
            )
            update_contract_from_evidence(state)
            self._progress(
                "coverage_check",
                f"Coverage is {state.contract.coverage_ratio():.0%}; {len(state.contract.open_cells())} cell(s) remain open.",
                {
                    "coverage_ratio": state.contract.coverage_ratio(),
                    "open_cells": [cell.model_dump(mode="json") for cell in state.contract.open_cells()[:10]],
                    "partial_cells": [cell.model_dump(mode="json") for cell in state.contract.partial_cells()[:8]],
                    "worker_reports": [report.model_dump(mode="json") for report in state.worker_reports[-8:]],
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
                    **model_client.telemetry_for_role(
                        "reflection",
                        quality_mode=self.request.quality_mode,
                        model_used=decision.model_used,
                    ),
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
                "worker_reports": len(state.worker_reports),
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
            "worker_reports": state.worker_reports,
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
        worker_sources: dict[str, list[Source]] = {}
        provider_attempts_by_worker: dict[str, list[dict[str, Any]]] = {}
        retry_queries_by_worker: dict[str, list[str]] = {}
        pending_workers: list[tuple[int, SearchWorkerPlan]] = []
        for index, worker in enumerate(state.plan.workers, start=1):
            if worker.query in state.query_history:
                continue
            pending_workers.append((index, worker))

        search_slots = min(
            max(1, state.budget_ledger.budget.max_search_workers),
            len(pending_workers),
            self.ledger.remaining_tool_calls(),
        )
        if search_slots <= 0 or not self.ledger.can_start_tool("web_search"):
            pending_workers = []
        else:
            pending_workers = pending_workers[:search_slots]

        for index, worker in pending_workers:
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
        if pending_workers:
            max_workers = max(1, min(state.budget_ledger.budget.max_search_workers, len(pending_workers)))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(self.tools.search_web, worker.query, max_results=worker.max_results): (index, worker)
                    for index, worker in pending_workers
                }
                for future in as_completed(futures):
                    index, worker = futures[future]
                    try:
                        sources, call = future.result()
                    except Exception as exc:
                        logger.warning("agent_v3 search worker failed for query=%r: %s", worker.query, exc)
                        sources = []
                        call = ToolCall(
                            name="web_search",
                            input={"query": worker.query, "max_results": worker.max_results},
                            output={},
                            ok=False,
                            error=str(exc),
                        )
                    self.ledger.record_tool_call(latency_ms=call.latency_ms, sources_seen=len(sources))
                    state.all_tool_calls.append(call)
                    provider = call.output.get("provider") if isinstance(call.output, dict) else ""
                    _ensure_source_provenance(sources, query=worker.query, provider=str(provider or ""))
                    public_sources = [source for source in sources if is_public_source_url(source.url)]
                    provider_attempts_by_worker.setdefault(worker.worker_id, []).append(
                        {
                            "query": worker.query,
                            "provider": provider,
                            "ok": call.ok,
                            "error": call.error,
                            "source_count": len(sources),
                            "public_source_count": len(public_sources),
                        }
                    )
                    assigned_cell = _assigned_cell_for_worker(worker, state.contract)
                    avg_relevance = (
                        sum(_source_relevance_for_worker(source, worker) for source in public_sources) / max(1, len(public_sources))
                    )
                    if (
                        self.request.research_level != "easy"
                        and avg_relevance < 0.35
                        and self.ledger.can_start_tool("web_search")
                    ):
                        retry_query = _retry_query_for_worker(worker, assigned_cell, self.request)
                        if retry_query and retry_query not in state.query_history:
                            state.add_queries([retry_query])
                            retry_queries_by_worker.setdefault(worker.worker_id, []).append(retry_query)
                            self._progress(
                                "search_worker_retry",
                                f"Search worker {index} is refining a weak result set.",
                                {
                                    "worker_index": index,
                                    "worker_id": worker.worker_id,
                                    "retry_query": retry_query,
                                    "initial_relevance": avg_relevance,
                                },
                            )
                            try:
                                retry_sources, retry_call = self.tools.search_web(retry_query, max_results=worker.max_results)
                            except Exception as exc:
                                logger.warning("agent_v3 retry search failed for query=%r: %s", retry_query, exc)
                                retry_sources = []
                                retry_call = ToolCall(
                                    name="web_search",
                                    input={"query": retry_query, "max_results": worker.max_results},
                                    output={},
                                    ok=False,
                                    error=str(exc),
                                )
                            self.ledger.record_tool_call(latency_ms=retry_call.latency_ms, sources_seen=len(retry_sources))
                            state.all_tool_calls.append(retry_call)
                            retry_provider = retry_call.output.get("provider") if isinstance(retry_call.output, dict) else ""
                            _ensure_source_provenance(retry_sources, query=retry_query, provider=str(retry_provider or provider or ""))
                            retry_public = [source for source in retry_sources if is_public_source_url(source.url)]
                            provider_attempts_by_worker.setdefault(worker.worker_id, []).append(
                                {
                                    "query": retry_query,
                                    "provider": retry_provider,
                                    "ok": retry_call.ok,
                                    "error": retry_call.error,
                                    "source_count": len(retry_sources),
                                    "public_source_count": len(retry_public),
                                }
                            )
                            public_sources.extend(retry_public)
                    added = state.add_sources(public_sources)
                    worker_sources.setdefault(worker.worker_id, []).extend(added)
                    wave_sources.extend(added)
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
                            "self_assessed_relevance": avg_relevance,
                            "retry_queries": retry_queries_by_worker.get(worker.worker_id, []),
                            "parallel_workers": len(pending_workers),
                            "budget_ledger": self.ledger.model_dump(mode="json"),
                        },
                    )

        ranked = rank_sources(wave_sources, state.plan)
        selected = _select_diverse_ranked_sources(
            ranked,
            limit=self.ledger.remaining_source_reads(),
            research_level=self.request.research_level,
        )
        inventory = _source_inventory_summary(state.all_sources)
        self._progress(
            "source_inventory",
            f"Built a candidate inventory with {inventory['total']} source candidate(s).",
            {
                "inventory": inventory,
                "candidate_count": inventory["total"],
                "read_budget_remaining": self.ledger.remaining_source_reads(),
                "tool_budget_remaining": self.ledger.remaining_tool_calls(),
            },
        )
        self._progress(
            "source_ranker",
            f"Ranked {len(wave_sources)} candidate source(s).",
            {
                "agent_id": "source_ranker",
                "ranked_sources": [item.model_dump(mode="json") for item in ranked[: state.plan.max_sources]],
                "selected_source_provenance": [
                    {
                        "title": source.title,
                        "url": source.url,
                        "query": source.query,
                        "provider": source.provider,
                    }
                    for source in selected
                ],
            },
        )
        if selected and self.ledger.can_start_tool("read_url") and self.ledger.can_read_more_sources():
            read_urls = [source.url for source in selected if source.url][: self.ledger.remaining_source_reads()]
            provenance_by_url = {
                source.url: {"query": source.query, "provider": source.provider}
                for source in selected
                if source.url
            }
            extracted_by_url: dict[str, Source] = {}
            read_batches = _chunk_urls(read_urls, size=MAX_URLS_PER_READ_BATCH)
            max_read_batches = _max_parallel_read_batches_for(self.request.research_level)
            read_batches = read_batches[: min(max_read_batches, self.ledger.remaining_tool_calls())]
            self._progress(
                "source_reader",
                f"Reading {sum(len(batch) for batch in read_batches)} selected source page(s).",
                {
                    "urls": read_urls,
                    "batch_count": len(read_batches),
                    "max_parallel_read_batches": max_read_batches,
                    "max_urls_per_read_batch": MAX_URLS_PER_READ_BATCH,
                    "read_ceiling": max_read_batches * MAX_URLS_PER_READ_BATCH,
                },
            )
            if read_batches:
                max_read_workers = max(1, min(max_read_batches, len(read_batches)))
                with ThreadPoolExecutor(max_workers=max_read_workers) as executor:
                    futures = {
                        executor.submit(self.tools.extract_urls, batch, max_chars_per_source=_read_cap_for_batch(batch, state.plan)): batch
                        for batch in read_batches
                    }
                    for future in as_completed(futures):
                        batch = futures[future]
                        try:
                            extracted, call = future.result()
                        except Exception as exc:
                            logger.warning("agent_v3 source reader failed for %d url(s): %s", len(batch), exc)
                            extracted = []
                            call = ToolCall(
                                name="read_url",
                                input={"urls": batch},
                                output={},
                                ok=False,
                                error=str(exc),
                            )
                        _apply_source_provenance(extracted, provenance_by_url)
                        self.ledger.record_tool_call(latency_ms=call.latency_ms, sources_read=len(batch))
                        state.all_tool_calls.append(call)
                        state.add_sources(extracted)
                        for source in extracted:
                            if source.url:
                                extracted_by_url[source.url] = source
                        provider = call.output.get("provider") if isinstance(call.output, dict) else None
                        self._progress(
                            "source_reader_result",
                            "Source reader finished extracting source text.",
                            {
                                "ok": call.ok,
                                "error": call.error,
                                "provider": provider,
                                "source_count": len(extracted),
                                "batch_size": len(batch),
                                "budget_ledger": self.ledger.model_dump(mode="json"),
                            },
                        )
            for index, worker in pending_workers:
                assigned_cell = _assigned_cell_for_worker(worker, state.contract)
                report_sources = [
                    extracted_by_url.get(source.url, source)
                    for source in worker_sources.get(worker.worker_id, [])
                    if source.url
                ]
                report = _worker_report_from_sources(
                    worker,
                    assigned_cell=assigned_cell,
                    sources=report_sources,
                    plan=state.plan,
                    provider_attempts=provider_attempts_by_worker.get(worker.worker_id, []),
                    retry_queries=retry_queries_by_worker.get(worker.worker_id, []),
                )
                state.worker_reports.append(report)
                self._progress(
                    "search_worker_report",
                    _worker_report_message(index, report),
                    {
                        "worker_index": index,
                        "report": report.model_dump(mode="json"),
                    },
                )
        self._expand_source_graph(state, selected)

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
        _apply_source_provenance(
            extracted,
            {
                candidate.url: {"query": "deep-link follow-up", "provider": "Tavily Extract", "parent_url": candidate.parent_url}
                for candidate in candidates
            },
        )
        self.ledger.record_tool_call(latency_ms=call.latency_ms, sources_read=len(urls))
        state.all_tool_calls.append(call)
        state.add_sources(extracted)

    def _expand_source_graph(self, state: ResearchStateStore, seeds: list[Source]) -> None:
        if self.budget.max_deep_links <= 0 or not self.ledger.can_read_more_sources():
            return
        max_depth = 2 if self.request.research_level == "deep" else 1
        frontier = [source for source in seeds if source.url]
        followed: set[str] = set()
        for depth in range(1, max_depth + 1):
            if not frontier or not self.ledger.can_read_more_sources() or not self.ledger.can_start_tool("read_url"):
                break
            candidate_limit = min(self.ledger.remaining_source_reads(), max(1, self.budget.max_deep_links - len(followed)))
            candidates = extract_deep_link_candidates(frontier, max_links=candidate_limit)
            urls = [
                candidate.url
                for candidate in candidates
                if candidate.url not in followed
                and candidate.url not in state.source_inventory
                and is_public_source_url(candidate.url)
            ][: self.ledger.remaining_source_reads()]
            self._progress(
                "source_graph_expansion",
                f"Following source graph layer {depth}: {len(urls)} reference link(s).",
                {
                    "depth": depth,
                    "candidate_count": len(candidates),
                    "selected_count": len(urls),
                    "links": [candidate.model_dump(mode="json") for candidate in candidates[: len(urls)]],
                    "inventory": _source_inventory_summary(state.all_sources),
                },
            )
            if not urls:
                break
            provenance_by_url = {
                candidate.url: {
                    "query": f"source graph layer {depth}",
                    "provider": "source_graph",
                    "parent_url": candidate.parent_url,
                }
                for candidate in candidates
            }
            try:
                extracted, call = self.tools.extract_urls(urls, max_chars_per_source=_read_cap_for_batch(urls, state.plan))
            except Exception as exc:
                logger.warning("agent_v3 source graph expansion failed at depth=%d: %s", depth, exc)
                extracted = []
                call = ToolCall(name="read_url", input={"urls": urls}, output={}, ok=False, error=str(exc))
            _apply_source_provenance(extracted, provenance_by_url)
            self.ledger.record_tool_call(latency_ms=call.latency_ms, sources_read=len(urls))
            state.all_tool_calls.append(call)
            state.add_sources(extracted)
            followed.update(urls)
            frontier = extracted
            self._progress(
                "source_graph_result",
                f"Source graph layer {depth} added {len(extracted)} readable source(s).",
                {
                    "depth": depth,
                    "ok": call.ok,
                    "error": call.error,
                    "source_count": len(extracted),
                    "inventory": _source_inventory_summary(state.all_sources),
                    "budget_ledger": self.ledger.model_dump(mode="json"),
                },
            )

    def _synthesize_verify_and_judge(self, state: ResearchStateStore) -> dict[str, Any]:
        self._progress(
            "evidence_binder",
            f"Bound {len(state.evidence.items)} evidence item(s).",
            {
                "agent_id": "evidence_binder",
                "coverage": state.evidence.coverage,
                "coverage_contract_ratio": state.contract.coverage_ratio(),
                "claim_count": len(state.evidence.claims),
                "gaps": state.evidence.gaps,
                "contradictions": state.evidence.contradictions,
                "evidence_items": [item.model_dump(mode="json") for item in state.evidence.items],
                "claims": [claim.model_dump(mode="json") for claim in state.evidence.claims[:20]],
                "architecture_cards": [card.model_dump(mode="json") for card in state.evidence.architecture_cards[:16]],
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
        self._progress(
            "synthesis",
            "Writing one coherent answer from the evidence.",
            {
                "agent_id": "synthesis_agent",
                **model_client.telemetry_for_role("synthesis", quality_mode=self.request.quality_mode),
            },
        )
        model_response = synthesize_answer(self.request, state.plan, state.evidence)
        self.ledger.record_model_call(cost_usd=model_response.cost_usd, latency_ms=model_response.latency_ms)
        self._progress(
            "synthesis_result",
            f"Synthesis used {model_response.model_used or 'the configured synthesis model'}.",
            {
                "agent_id": "synthesis_agent",
                **model_client.telemetry_for_response(model_response),
                "latency_ms": model_response.latency_ms,
                "cost_usd": model_response.cost_usd,
                "budget_ledger": self.ledger.model_dump(mode="json"),
            },
        )
        answer = model_response.text

        citation_result = verify_citations_semantically(answer, state.evidence)
        if citation_result.model_used:
            self.ledger.record_model_call(cost_usd=citation_result.cost_usd, latency_ms=citation_result.latency_ms)
        self._progress(
            "citation_verification",
            "Verified answer citations against source text.",
            {
                "agent_id": "claim_verifier",
                "verification": citation_result.model_dump(mode="json"),
                **model_client.telemetry_for_role(
                    "citation_verifier",
                    quality_mode="standard",
                    model_used=citation_result.model_used,
                ),
            },
        )
        repaired = False
        repair_attempts = 0
        if citation_result.repair_needed and self.ledger.can_start_model("repair_agent"):
            model_response = self._repair_answer(state, answer, citation_result.repair_instruction)
            answer = model_response.text
            repaired = True
            repair_attempts += 1

        specificity_issues = _specificity_rewrite_issues(answer)
        self._progress(
            "fact_check_rewrite",
            "Fact-checking the draft and tightening vague claims.",
            {
                "agent_id": "claim_verifier",
                "issue_count": len(specificity_issues),
                "issues": specificity_issues[:8],
            },
        )
        if specificity_issues and self.ledger.can_start_model("repair_agent"):
            instruction = (
                "Replace vague or hedged claims with named-source specifics using [S#] citations. "
                "If the evidence does not support a specific version, disclose the gap plainly. "
                "Issues to fix: " + "; ".join(specificity_issues[:8])
            )
            model_response = self._repair_answer(state, answer, instruction)
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
                contract=state.contract,
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
        self._progress(
            "research_repair",
            "Repairing the answer before publishing.",
            {
                "repair_instruction": instruction,
                **model_client.telemetry_for_role("repair", quality_mode=self.request.quality_mode),
            },
        )
        fake_judge = ResearchJudgeResult(
            status="repair",
            score=0.6,
            repair_instruction=instruction,
            can_publish=False,
        )
        repaired = repair_research_answer(self.request, state.plan, state.evidence, answer, fake_judge)
        self.ledger.record_model_call(cost_usd=repaired.cost_usd, latency_ms=repaired.latency_ms)
        self._progress(
            "research_repair_model",
            f"Repair used {repaired.model_used or 'the configured repair model'}.",
            {
                **model_client.telemetry_for_response(repaired),
                "latency_ms": repaired.latency_ms,
                "cost_usd": repaired.cost_usd,
                "budget_ledger": self.ledger.model_dump(mode="json"),
            },
        )
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


def _ensure_source_provenance(sources: list[Source], *, query: str, provider: str) -> None:
    for source in sources:
        if not source.query:
            source.query = query
        if provider and not source.provider:
            source.provider = provider


def _apply_source_provenance(sources: list[Source], provenance_by_url: dict[str, dict[str, str]]) -> None:
    for source in sources:
        provenance = provenance_by_url.get(source.url) or {}
        if not source.query:
            source.query = provenance.get("query", "")
        if not source.provider:
            source.provider = provenance.get("provider", "")


def _merge_source_detail(existing: Source, incoming: Source) -> None:
    """Upgrade a discovered candidate with read-page detail without losing provenance."""
    if incoming.title and (not existing.title or len(incoming.title) > len(existing.title)):
        existing.title = incoming.title
    if incoming.snippet and not existing.snippet:
        existing.snippet = incoming.snippet
    if incoming.content and len(incoming.content) > len(existing.content or ""):
        existing.content = incoming.content
    if incoming.query and not existing.query:
        existing.query = incoming.query
    if incoming.provider and not existing.provider:
        existing.provider = incoming.provider


def _synthesis_report_contract(profile: ResearchProfile, request: AgentV3Request) -> str:
    if profile == "technical_architecture":
        return (
            "Produce a detailed architectural report. "
            "Derive the section structure from the evidence — use the components, workflows, "
            "and architectural patterns that actually appear in the sources, not a generic template. "
            "Use the architecture extraction cards as the primary spine: compare named systems, their state objects, "
            "agent roles, renderers/tools, validation loops, metrics, and failure modes. "
            "Every section must be grounded in specific evidence with [S#] citations. "
            "Include concrete implementation details: data models, control flow, state transitions, "
            "failure handling, trade-offs, and design decisions. "
            "Add a compact ASCII or text diagram where it clarifies a component relationship or data flow. "
            "Where sources conflict or leave gaps, say so explicitly rather than filling with generic description. "
            "Avoid restating definitions unless the definition itself contains a design decision worth citing. "
            "For deep research, target 10-14 substantive sections and enough detail to stand alone as a technical "
            "architecture brief: concrete mechanisms, named systems, implementation patterns, trade-offs, failure "
            "modes, and source-backed examples. Do not compress the report into a short summary."
        )
    if request.output_format in {"docx", "markdown"} or "report" in request.message.lower():
        return "Produce a structured report with clear headings, evidence-backed findings, gaps, and recommendations."
    return "Produce a source-grounded answer with clear headings and cited findings."


def _synthesis_token_budget(request: AgentV3Request, plan: ResearchPlan) -> int:
    if plan.research_profile == "technical_architecture" and request.research_level == "deep":
        # Deep technical report: needs room for 10 detailed sections with citations,
        # diagrams, trade-off tables, and implementation specifics.
        return 14000 if request.quality_mode == "executive" else 12000
    if request.output_format in {"docx", "markdown"} or "report" in request.message.lower():
        return 6500 if request.quality_mode == "executive" else 5200
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
    architecture_context = _architecture_cards_context(evidence)
    claim_context = "\n".join(
        f"[{claim.source_id}] {claim.claim_type}/{claim.claim_role}: {claim.text}"
        for claim in evidence.claims[:30]
    )
    passage_context = "\n\n".join(
        f"[{item.source_id}] {item.title}\nURL: {item.url}\n{item.evidence}"
        for item in evidence.items
    )
    if claim_context and passage_context:
        return f"Architecture extraction cards:\n{architecture_context}\n\nTyped evidence claims:\n{claim_context}\n\nEvidence passages:\n{passage_context}"
    if architecture_context:
        return f"Architecture extraction cards:\n{architecture_context}\n\nEvidence passages:\n{passage_context}"
    return passage_context


def _architecture_cards_context(evidence: EvidencePack) -> str:
    if not evidence.architecture_cards:
        return "No architecture extraction cards were built. Use typed claims and passages, but disclose missing implementation detail."
    lines: list[str] = []
    for card in evidence.architecture_cards[:18]:
        lines.append(
            "\n".join(
                [
                    f"- [{card.source_id}] {card.system}",
                    f"  Pattern: {card.architecture_pattern or 'unknown'}",
                    f"  Agent roles: {', '.join(card.agent_roles) or 'unknown'}",
                    f"  State objects: {', '.join(card.state_objects) or 'unknown'}",
                    f"  Tools/renderers: {', '.join(card.tools_or_renderers) or 'unknown'}",
                    f"  Validation loop: {card.validation_loop or 'unknown'}",
                    f"  Failure modes: {', '.join(card.failure_modes) or 'unknown'}",
                    f"  Metrics: {' | '.join(card.metrics) or 'unknown'}",
                    f"  Lesson for AgentDeck: {card.lesson_for_agentdeck}",
                    f"  Quote: {card.quote or 'none extracted'}",
                    f"  URL: {card.source_url}",
                ]
            )
        )
    return "\n".join(lines)


def _fallback_plan(request: AgentV3Request, goal: ResearchGoal | None = None) -> ResearchPlan:
    goal = goal or create_research_goal(request)
    rationale = "Fallback worker from the original request."
    if request.research_level == "easy":
        rationale = "Easy research uses one narrow source-grounding search."
    worker = SearchWorkerPlan(
        question=request.message,
        query=request.message,
        rationale=rationale,
        max_results=goal.budget.max_results_per_worker,
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
                max_results=goal.budget.max_results_per_worker,
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
        worker.max_results = max(1, min(goal.budget.max_results_per_worker, int(worker.max_results or goal.budget.max_results_per_worker)))
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
            "LLM research agent planning loop evidence retrieval site:arxiv.org",
            "LLM research agent planning loop evidence retrieval site:github.com",
            "autonomous research agent orchestration evidence synthesis 2024",
        ]
    if "multi-agent" in msg_lower or "multi agent" in msg_lower:
        return [
            "multi-agent LLM orchestration architecture patterns",
            "multi-agent AI system design orchestrator planner executor",
            "agentic workflow multi-agent framework implementation site:github.com",
            "agentic workflow multi-agent framework implementation site:arxiv.org",
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
        f"{original_message[:60]} system design components site:arxiv.org",
        f"{original_message[:60]} system design components site:github.com",
    ]


def _domain_discovery_workers(request: AgentV3Request, profile: ResearchProfile, budget: ResearchBudget) -> list[SearchWorkerPlan]:
    subject = _compact_search_subject(request.message)
    workers: list[SearchWorkerPlan] = []
    specs: list[tuple[str, str, str, str]] = []
    if profile in {"technical_architecture", "academic_literature"}:
        specs.extend(
            [
                ("academic", f"{subject} site:arxiv.org", "Find academic papers and cited research."),
                ("academic", f"{subject} site:semanticscholar.org", "Find citation graph and related academic work."),
                ("repository", f"{subject} site:github.com implementation", "Find open implementations, repositories, and README architecture notes."),
                ("documentation", f"{subject} documentation architecture implementation", "Find framework docs and engineering references."),
            ]
        )
    elif profile == "vendor_comparison":
        specs.extend(
            [
                ("primary", f"{subject} official docs pricing security", "Find vendor-owned product, pricing, and security pages."),
                ("documentation", f"{subject} API docs integration guide", "Find implementation documentation."),
                ("general", f"{subject} comparison review limitations", "Find external comparisons and caveats."),
            ]
        )
    elif profile == "regulatory":
        specs.extend(
            [
                ("primary", f"{subject} regulator official guidance", "Find regulator and government material."),
                ("news", f"{subject} latest update analysis", "Find recent developments and practitioner commentary."),
            ]
        )
    else:
        specs.extend(
            [
                ("primary", f"{subject} official source documentation", "Find primary sources where available."),
                ("general", f"{subject} analysis evidence recent", "Find broad supporting sources."),
            ]
        )
    for domain, query, rationale in specs:
        workers.append(
            SearchWorkerPlan(
                question=f"Domain lane: {domain} evidence for {subject}",
                query=query[:220],
                rationale=rationale,
                max_results=budget.max_results_per_worker,
                discovery_domain=domain,  # type: ignore[arg-type]
            )
        )
    return workers[: max(0, min(4, budget.max_search_workers))]


def _compact_search_subject(message: str) -> str:
    tokens = [
        token
        for token in re.findall(r"[a-z0-9.]{2,}", (message or "").lower())
        if token
        not in {
            "a",
            "an",
            "and",
            "are",
            "as",
            "be",
            "by",
            "conduct",
            "create",
            "deep",
            "detailed",
            "do",
            "easy",
            "explaining",
            "explain",
            "for",
            "from",
            "generate",
            "give",
            "in",
            "into",
            "latest",
            "like",
            "me",
            "of",
            "on",
            "perform",
            "regular",
            "report",
            "research",
            "the",
            "to",
            "with",
        }
    ]
    cleaned = " ".join(_dedupe(tokens)[:16])
    return cleaned[:140] or (message or "")[:110] or "research topic"


def _longform_timeout_s() -> int:
    return max(30, int(get_settings().agent_v3_longform_timeout_s or 180))


def _domain_for_query(query: str) -> Literal["general", "academic", "repository", "documentation", "news", "primary"]:
    lower = (query or "").lower()
    if "arxiv" in lower or "semanticscholar" in lower or "paper" in lower:
        return "academic"
    if "github" in lower or "repo" in lower:
        return "repository"
    if "docs" in lower or "documentation" in lower or "api" in lower:
        return "documentation"
    if "official" in lower or "regulator" in lower or "government" in lower:
        return "primary"
    if "latest" in lower or "news" in lower or "recent" in lower:
        return "news"
    return "general"


def _targeted_query(subject: str, dimensions: list[str], original: str) -> str:
    raw_subject = " ".join(str(subject or "").split())
    subject = _public_technical_subject(raw_subject) if infer_research_profile(original) == "technical_architecture" else raw_subject
    # Pick the single most specific dimension to keep the query tight
    primary_dim = dimensions[0] if dimensions else ""
    primary_dim = " ".join(str(primary_dim or "").split())

    if infer_research_profile(original) == "technical_architecture":
        # Tight subject-focused query — don't pad with keyword lists.
        # The search engine needs a natural, specific query, not a keyword dump.
        # Append one grounding term to bias toward technical sources.
        grounding = _tech_arch_grounding_term(raw_subject)
        query = f"{subject} {primary_dim} {grounding}".strip()
        return query[:180]

    if subject and any(token in subject.lower() for token in ("tavily", "nimble", "you.com", "youcom")):
        return f"{subject} {primary_dim} official docs pricing security API enterprise".strip()[:180]

    base = f"{subject} {primary_dim}".strip()
    return f"{base} {original}".strip()[:220]


def _public_technical_subject(subject: str) -> str:
    text = subject.lower()
    mappings = [
        (("lead agent", "orchestration"), "multi-agent research orchestrator planner executor"),
        (("research planning", "coverage contract"), "research agent query planning coverage evaluation"),
        (("search worker", "provider"), "LLM search worker web retrieval provider routing"),
        (("source reading", "deep-link"), "web research agent source extraction crawling"),
        (("evidence binder", "citation"), "evidence grounding citation verification research agent"),
        (("reflection", "gap", "repair"), "agent reflection gap detection repair loop"),
        (("synthesis", "judge", "quality"), "LLM judge synthesis critic quality gate"),
        (("runtime", "durability", "budget", "observability"), "agent runtime tracing budget ledger observability"),
        (("guardrail", "security"), "LLM agent guardrails tool security policy"),
    ]
    for needles, replacement in mappings:
        if any(needle in text for needle in needles):
            return replacement
    return subject


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
    if cell.cell_id in item.supports_cells:
        return True
    return _text_supports_cell(f"{item.title} {item.url} {item.evidence}", cell)


def _text_supports_cell(text: str, cell: CoverageCell) -> bool:
    haystack = text.lower()
    subject_terms = _cell_terms(cell.subject)
    dimension_terms = _cell_terms(cell.dimension)
    subject_hit = any(term in haystack for term in subject_terms) if subject_terms else False
    dimension_hit = any(term in haystack for term in dimension_terms) if dimension_terms else False
    if subject_hit and dimension_hit:
        return True
    if subject_hit and cell.dimension.lower() in {"evidence", "coverage", "capabilities"}:
        return True
    if dimension_hit and any(term in haystack for term in ("architecture", "agent", "research", "system")):
        return True
    return False


def _cell_terms(value: str) -> list[str]:
    tokens = _meaningful_tokens(value)
    lowered = value.lower()
    aliases: list[str] = []
    alias_groups = [
        (("lead", "orchestrat"), ["orchestrator", "supervisor", "controller", "coordinator", "planner"]),
        (("planning", "coverage"), ["plan", "planning", "decomposition", "coverage", "evaluation", "query"]),
        (("search", "provider"), ["search", "retrieval", "provider", "browser", "crawl", "query"]),
        (("source", "reading", "deep-link"), ["source", "extract", "crawl", "read", "parse", "document"]),
        (("evidence", "citation"), ["evidence", "citation", "grounding", "provenance", "attribution", "source"]),
        (("reflection", "gap", "repair"), ["reflection", "critic", "judge", "repair", "gap", "feedback"]),
        (("synthesis", "judge", "quality"), ["synthesis", "generate", "judge", "critic", "quality", "evaluation"]),
        (("runtime", "budget", "observability"), ["runtime", "state", "trace", "telemetry", "budget", "cost", "latency", "durable"]),
        (("guardrail", "security"), ["guardrail", "security", "safety", "policy", "permission", "validation"]),
        (("responsibility",), ["role", "responsibility", "function", "owns", "manage"]),
        (("implementation", "pattern"), ["implementation", "architecture", "design", "pattern", "component"]),
        (("data", "model"), ["schema", "state", "data", "model", "store", "object"]),
        (("workflow",), ["workflow", "flow", "pipeline", "process", "loop", "sequence"]),
        (("failure", "handling"), ["failure", "error", "retry", "fallback", "timeout", "recovery"]),
        (("trade",), ["trade-off", "tradeoff", "latency", "cost", "quality", "accuracy", "complexity"]),
    ]
    for triggers, terms in alias_groups:
        if any(trigger in lowered for trigger in triggers):
            aliases.extend(terms)
    return _dedupe([*tokens, *aliases])


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
    if request.quality_mode == "executive" and request.research_level == "deep":
        return 5
    if request.research_level == "deep":
        return 4
    return 3


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
