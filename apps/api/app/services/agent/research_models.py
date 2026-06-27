"""Pydantic models for the Fronei research pipeline.

Extracted from research_subtree.py (TD-01) to reduce that file's size and
make the data contracts discoverable without reading 5k lines of logic.

Everything here is a pure data class or type alias — no I/O, no LLM calls,
no imports from other agent sub-modules (except the shared models layer).
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.services.agent.models import Source, ToolCall, new_id

# ---------------------------------------------------------------------------
# Research profile
# ---------------------------------------------------------------------------

ResearchProfile = Literal[
    "general",
    "technical_architecture",
    "vendor_comparison",
    "market_landscape",
    "policy_regulatory",
    "strategy_brief",
    "implementation_plan",
    "academic_literature",
]

_RESEARCH_PROFILES: tuple[ResearchProfile, ...] = (
    "general",
    "technical_architecture",
    "vendor_comparison",
    "market_landscape",
    "policy_regulatory",
    "strategy_brief",
    "implementation_plan",
    "academic_literature",
)


class ResearchProfilePolicy(BaseModel):
    profile: ResearchProfile
    source_lanes: list[str] = Field(default_factory=list)
    domain_specs: list[tuple[str, str, str]] = Field(default_factory=list)
    type_weights: dict[str, float] = Field(default_factory=dict)
    allowed_gaps: list[str] = Field(default_factory=list)


PROFILE_POLICIES: dict[ResearchProfile, ResearchProfilePolicy] = {
    "general": ResearchProfilePolicy(
        profile="general",
        source_lanes=[
            "balanced web discovery",
            "official or primary sources when obvious",
            "reputable explainers for background",
        ],
        domain_specs=[
            ("primary", "{subject} official source documentation", "Find primary sources where available."),
            ("general", "{subject} analysis evidence recent", "Find broad supporting sources."),
        ],
        type_weights={"primary": 0.08, "documentation": 0.05, "academic": 0.05, "web": 0.02},
        allowed_gaps=["Specialized source quotas are not enforced for general research."],
    ),
    "technical_architecture": ResearchProfilePolicy(
        profile="technical_architecture",
        source_lanes=[
            "arXiv and academic papers",
            "GitHub repositories and READMEs",
            "official framework docs",
            "engineering blogs and postmortems",
        ],
        domain_specs=[
            ("academic", "{subject} site:arxiv.org", "Find academic papers and cited research."),
            ("academic", "{subject} site:semanticscholar.org", "Find citation graph and related academic work."),
            ("repository", "{subject} site:github.com implementation", "Find open implementations, repositories, and README architecture notes."),
            ("documentation", "{subject} documentation architecture implementation", "Find framework docs and engineering references."),
        ],
        type_weights={"academic": 0.18, "repository": 0.17, "documentation": 0.14, "pdf": 0.13, "primary": 0.08, "news": -0.04},
    ),
    "academic_literature": ResearchProfilePolicy(
        profile="academic_literature",
        source_lanes=["arXiv", "Semantic Scholar", "publisher pages", "benchmark repositories"],
        domain_specs=[
            ("academic", "{subject} site:arxiv.org", "Find academic papers and cited research."),
            ("academic", "{subject} site:semanticscholar.org", "Find citation graph and related academic work."),
            ("repository", "{subject} benchmark implementation site:github.com", "Find benchmark code and replications."),
        ],
        type_weights={"academic": 0.18, "pdf": 0.15, "repository": 0.10, "documentation": 0.05},
    ),
    "vendor_comparison": ResearchProfilePolicy(
        profile="vendor_comparison",
        source_lanes=[
            "official product docs",
            "pricing pages",
            "API/reference docs",
            "security and compliance pages",
            "marketplaces and credible review sites",
        ],
        domain_specs=[
            ("primary", "{subject} official docs pricing security", "Find vendor-owned product, pricing, and security pages."),
            ("documentation", "{subject} API docs integration guide", "Find implementation documentation."),
            ("primary", "{subject} SOC 2 security compliance marketplace", "Find security, compliance, and marketplace evidence."),
            ("general", "{subject} comparison review limitations", "Find external comparisons and caveats."),
        ],
        type_weights={"primary": 0.18, "documentation": 0.16, "marketplace": 0.12, "web": 0.02, "news": 0.02},
        allowed_gaps=["Enterprise pricing may be sales-gated, but the gap must be explicit."],
    ),
    "policy_regulatory": ResearchProfilePolicy(
        profile="policy_regulatory",
        source_lanes=[
            "regulator sites",
            "statutes and official guidance",
            "enforcement actions",
            "reputable legal analysis as secondary context",
        ],
        domain_specs=[
            ("primary", "{subject} regulator official guidance", "Find regulator and government material."),
            ("primary", "{subject} enforcement action penalty compliance requirement", "Find enforcement and penalty specifics."),
            ("news", "{subject} latest update legal analysis", "Find recent developments and practitioner commentary."),
        ],
        type_weights={"primary": 0.22, "pdf": 0.14, "news": 0.04, "web": -0.02},
    ),
    "market_landscape": ResearchProfilePolicy(
        profile="market_landscape",
        source_lanes=[
            "analyst/research reports",
            "company filings and investor relations",
            "press releases",
            "industry associations",
            "credible industry media",
        ],
        domain_specs=[
            ("primary", "{subject} market size growth forecast", "Find market sizing and growth data."),
            ("primary", "{subject} investor relations earnings market", "Find company filings and earnings evidence."),
            ("news", "{subject} competitive landscape industry analysis", "Find credible industry coverage."),
        ],
        type_weights={"primary": 0.14, "news": 0.08, "pdf": 0.08, "web": 0.02},
        allowed_gaps=["Analyst estimates may conflict; cite ranges and source disagreements."],
    ),
    "strategy_brief": ResearchProfilePolicy(
        profile="strategy_brief",
        source_lanes=[
            "case studies",
            "benchmarks",
            "financial and operational evidence",
            "analyst views",
            "competitor examples",
        ],
        domain_specs=[
            ("primary", "{subject} case study ROI outcomes", "Find named case studies and outcome data."),
            ("general", "{subject} strategic analysis business case", "Find decision-grade analysis."),
            ("news", "{subject} competitor example benchmark", "Find comparable examples and benchmarks."),
        ],
        type_weights={"primary": 0.12, "documentation": 0.08, "news": 0.06, "web": 0.02},
    ),
    "implementation_plan": ResearchProfilePolicy(
        profile="implementation_plan",
        source_lanes=[
            "official implementation docs",
            "migration guides",
            "reference architectures",
            "playbooks",
            "engineering postmortems",
        ],
        domain_specs=[
            ("documentation", "{subject} implementation guide best practices", "Find official implementation guidance."),
            ("documentation", "{subject} migration guide reference architecture", "Find migration and reference architecture material."),
            ("news", "{subject} rollout lessons learned postmortem", "Find lessons learned and failure modes."),
        ],
        type_weights={"documentation": 0.16, "primary": 0.10, "repository": 0.08, "news": 0.04},
    ),
}

# ---------------------------------------------------------------------------
# Agent registry types
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Goal, plan, evidence
# ---------------------------------------------------------------------------

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
    secondary_profiles: list[ResearchProfile] = Field(default_factory=list)
    source_lanes: list[str] = Field(default_factory=list)
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
    # Phase 4 — retrieval diversity hint, set from query heuristics at plan-creation time
    expected_primary_role: str | None = None


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
    # Phase 3 — source metadata for independence proxy and freshness tracking
    date_confidence: Literal["known", "unknown", "inferred"] = "unknown"
    published_date: str | None = None
    source_family: str = ""        # registrable domain, e.g. "reddit.com" not "old.reddit.com/r/x"
    content_fingerprint: str = ""  # normalized-title hash for repost detection


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
    # Phase 3 — count of items with unique (source_family, content_fingerprint) pairs
    independent_source_count: int = 0


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
    secondary_profiles: list[ResearchProfile] = Field(default_factory=list)
    profile_confidence: float = 0.0
    classification_reason: str = ""
    domain_strategy_hints: list[str] = Field(default_factory=list)
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
    # Phase 5 — last citation verification result, consumed by judge_research_final
    last_citation_verification: CitationVerification | None = None

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
    # Phase 5 — role-appropriateness and conflict signals from the verifier
    role_mismatch_issues: list[str] = Field(default_factory=list)
    unresolved_conflicts: list[str] = Field(default_factory=list)


__all__ = [
    # Type aliases
    "ResearchProfile",
    "ResearchAgentId",
    "_RESEARCH_PROFILES",
    # Profile policies
    "ResearchProfilePolicy",
    "PROFILE_POLICIES",
    # Agent registry
    "ResearchPromptTemplate",
    "ResearchAgentDefinition",
    "ResearchAgentRegistry",
    # Budget
    "ResearchBudget",
    "ResearchBudgetLedger",
    # Core models
    "ResearchGoal",
    "SearchWorkerPlan",
    "ResearchPlan",
    "EvidenceItem",
    "EvidenceClaim",
    "EvidencePack",
    "ArchitectureExtractionCard",
    "SearchWorkerReport",
    "ResearchJudgeResult",
    "RankedSource",
    "DeepLinkCandidate",
    "ClaimVerification",
    "ResearchFeedbackLoop",
    "ResearchBrief",
    "CoverageCell",
    "CoverageContract",
    "ResearchStateStore",
    "ReflectionDecision",
    "JudgeVerdict",
    "CitationVerification",
    # Helpers used by ResearchStateStore
    "_merge_source_detail",
]
