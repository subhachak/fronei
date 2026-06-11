from pydantic import BaseModel, Field
from typing import Literal

TaskType = Literal[
    "coding", "reasoning", "architecture", "writing", "summarization",
    "research", "document_qa", "math", "email", "planning", "unknown"
]
Complexity = Literal["low", "medium", "high"]
Profile = Literal["cost_saver", "balanced", "best_quality"]
ResearchMode = Literal["quick", "deep", "expert"]
OutputMode = Literal[
    "raw",
    "default",
    "client_ready",
    "exec_ready",
    "email",
    "proposal",
    "architecture",
    "pushback",
]
ArtifactType = Literal[
    "adr",                  # Architecture Decision Record
    "solution_comparison",  # Compare 2-3 options with trade-offs
    "trade_off_matrix",     # Structured matrix across dimensions
    "exec_brief",           # Executive briefing / C-suite summary
    "risk_register",        # Risks, probability, impact, mitigation
    "nfr_analysis",         # Non-functional requirements analysis
    "steering_update",      # Steering committee status update
]


class AttachedDocument(BaseModel):
    name:       str = Field(max_length=255)
    text:       str
    char_count: int
    pages:      int = 1
    method:     str = "parser"   # "vision" | "parser"


class DocumentExtractResponse(BaseModel):
    name:            str
    char_count:      int
    pages_extracted: int
    pages_total:     int
    truncated:       bool
    method:          str
    text:            str           # full extracted text for send body
    text_preview:    str           # first 300 chars for UI display


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=32000)
    profile: Profile | None = None
    force_model: str | None = None
    deep_research: bool = False
    research_mode: ResearchMode = "quick"
    web_search: bool = False
    attached_documents: list[AttachedDocument] = []


class RouteDecision(BaseModel):
    task_type: TaskType
    complexity: Complexity
    profile: Profile
    primary_model: str
    fallbacks: list[str]
    reason: str


class ChatResponse(BaseModel):
    answer: str
    route: RouteDecision
    model_used: str
    latency_ms: int
    estimated_cost_usd: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


# ── Conversation schemas ──────────────────────────────────────────────────────

class ConvChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=32000)
    profile: Profile | None = None
    force_model: str | None = None
    conversation_id: int | None = None
    deep_research: bool = False
    research_mode: ResearchMode = "quick"
    web_search: bool = False
    allow_research_recommendation: bool = True
    output_mode: OutputMode = "default"
    artifact_type: ArtifactType | None = None
    attached_documents: list[AttachedDocument] = []


class ResearchSourceOut(BaseModel):
    id: int | None = None
    title: str
    url: str
    provider: str | None = None
    credibility_score: float | None = None
    relevance_score: float | None = None
    freshness_score: float | None = None
    source_type: str | None = None


class ResearchClaimOut(BaseModel):
    id: int | None = None
    claim: str
    quote: str | None = None
    confidence: str | None = None
    relevance_score: float | None = None
    source_id: int | None = None
    source_ref: str | None = None
    source_title: str | None = None
    source_url: str | None = None


class ResearchFindingOut(BaseModel):
    id: int | None = None
    finding: str
    evidence: list[dict] = []
    confidence: str | None = None


class ResearchMeta(BaseModel):
    run_id: int
    mode: str
    sources: list[ResearchSourceOut] = []
    claims: list[ResearchClaimOut] = []
    findings: list[ResearchFindingOut] = []
    questions: list[str] = []
    gaps: list[str] = []
    contradictions: list[str] = []
    verifier_notes: str | None = None
    confidence: str | None = None


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    task_type: str | None = None
    complexity: str | None = None
    model_used: str | None = None
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    estimated_cost_usd: float | None = None
    execution_log: "ExecutionLog | None" = None
    research_run_id: int | None = None
    research: ResearchMeta | None = None
    created_at: str


class ConversationSummary(BaseModel):
    id: int
    title: str
    profile: str
    message_count: int
    total_cost_usd: float = 0.0
    created_at: str
    updated_at: str


class ConversationUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class ConversationDetail(BaseModel):
    id: int
    title: str
    profile: str
    message_count: int
    created_at: str
    updated_at: str
    messages: list[MessageOut]


class PlannerSubQuery(BaseModel):
    query: str
    task_type: str | None = None
    preferred_model: str | None = None

class SubQueryLog(BaseModel):
    query: str
    task_type: str | None = None
    model_requested: str | None = None  # primary model attempted (may differ from model_used on fallback)
    model_used: str
    fallback_error: str | None = None   # set when model_requested != model_used
    cost_usd: float | None = None
    latency_ms: int = 0

class PlannerLog(BaseModel):
    model: str
    latency_ms: int
    cost_usd: float
    turn_type: str = "new_task"
    action: str = "use_workers"
    intent: str
    enriched_prompt: str
    needs_web_search: bool
    search_query: str | None = None
    sub_queries: list[PlannerSubQuery] = []
    context_summary: str = ""

class WebContextLog(BaseModel):
    enabled: bool
    provider: str = ""
    sources_count: int = 0
    search_query: str | None = None
    status: str = ""

class WorkerLog(BaseModel):
    model: str
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None
    sub_queries_count: int = 0
    sub_query_logs: list[SubQueryLog] = []

class ExecutionLog(BaseModel):
    planner: PlannerLog
    web_context: WebContextLog
    worker: WorkerLog
    total_cost_usd: float
    total_latency_ms: int

class ConvChatResponse(BaseModel):
    conversation_id: int
    message_id: int
    answer: str
    route: RouteDecision
    model_used: str
    latency_ms: int
    estimated_cost_usd: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    execution_log: ExecutionLog | None = None


# ── Analytics schemas ─────────────────────────────────────────────────────────

class DailyStat(BaseModel):
    date: str
    cost: float
    requests: int

class ModelUsageStat(BaseModel):
    model: str
    requests: int
    total_cost: float
    avg_latency_ms: float

class TaskStat(BaseModel):
    task_type: str
    count: int

class ModelDetailStat(BaseModel):
    model: str
    requests: int
    avg_latency_ms: float
    p50_latency_ms: int
    p95_latency_ms: int
    avg_prompt_tokens: float
    avg_completion_tokens: float
    total_cost: float

class AnalyticsSummary(BaseModel):
    total_cost: float
    total_requests: int
    total_tokens: int
    avg_latency_ms: float

class AnalyticsResponse(BaseModel):
    range: str
    summary: AnalyticsSummary
    daily_budget_usd: float
    cost_by_day: list[DailyStat]
    model_usage: list[ModelUsageStat]
    task_distribution: list[TaskStat]
    model_stats: list[ModelDetailStat]


# ── Memory schemas ────────────────────────────────────────────────────────────

class MemoryItem(BaseModel):
    id: int
    content: str
    category: str
    source_conversation_id: int | None = None
    created_at: str
    updated_at: str

class MemoryCreate(BaseModel):
    content: str = Field(min_length=1, max_length=500)
    category: str = Field(default="general", max_length=64)


# ── Writing samples ───────────────────────────────────────────────────

class WritingSampleIn(BaseModel):
    content: str = Field(min_length=50, max_length=8000)
    label: str | None = Field(default=None, max_length=120)


class WritingSampleOut(BaseModel):
    id: int
    content: str
    label: str | None
    char_count: int
    created_at: str


# ── Twin profile ──────────────────────────────────────────────────────

class FingerprintOut(BaseModel):
    """Structured fingerprint extracted from writing samples."""
    sentence_length:    str = "medium"
    formality:          str = "professional"
    directness:         str = "high"
    hedging:            str = "low"
    structure:          str = "mixed"
    technical_depth:    str = "high"
    preferred_phrases:  list[str] = []
    forbidden_phrases:  list[str] = []
    avoid_patterns:     list[str] = []
    signature_patterns: list[str] = []
    tone_by_audience:   dict[str, str] = {}


class TwinProfileOut(BaseModel):
    user_id:        str
    fingerprint:    FingerprintOut | None = None
    rewrite_prompt: str | None = None
    prefs:          dict = {}
    extracted_at:   str | None = None
    sample_count:   int = 0


class TwinProfilePrefsUpdate(BaseModel):
    preferred_phrases: list[str] | None = None
    forbidden_phrases: list[str] | None = None
    tone_by_audience:  dict[str, str] | None = None
