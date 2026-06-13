from datetime import datetime

from pydantic import BaseModel, Field
from typing import Any, Literal

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


class DocumentGenerateRequest(BaseModel):
    title: str = Field(default="Fronei document", min_length=1, max_length=180)
    content: str = Field(min_length=1, max_length=120000)
    subtitle: str | None = Field(default=None, max_length=240)


class DocumentGenerateFromPromptRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=32000)
    title: str | None = Field(default=None, max_length=180)
    doc_type: str | None = Field(default=None, max_length=64)
    audience: str | None = Field(default=None, max_length=120)
    tone: str | None = Field(default=None, max_length=120)
    length: str | None = Field(default=None, max_length=80)
    output_formats: list[str] = Field(default_factory=list)
    profile: Profile | None = None
    force_model: str | None = None
    deep_research: bool = False
    research_mode: ResearchMode = "quick"
    allow_research_recommendation: bool = True
    web_search: bool = False
    allow_web_search_recommendation: bool = True
    attached_documents: list[AttachedDocument] = Field(default_factory=list)


class DocumentGenerateFromPromptResponse(BaseModel):
    title: str
    doc_type: str
    markdown: str
    filename: str
    docx_base64: str
    model_used: str
    estimated_cost_usd: float | None = None


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

class ConfirmedPlan(BaseModel):
    """User-edited decisions from the plan_proposed confirmation popup."""
    web_search: bool | None = None
    deep_research: bool | None = None
    research_mode: ResearchMode | None = None
    document: bool | None = None
    document_format: str | None = None
    document_brief: dict | None = None


class ConvChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=32000)
    client_request_id: str | None = Field(default=None, max_length=128)
    profile: Profile | None = None
    force_model: str | None = None
    conversation_id: str | None = None
    deep_research: bool = False
    research_mode: ResearchMode = "quick"
    web_search: bool = False
    document_requested: bool = False
    allow_research_recommendation: bool = True
    output_mode: OutputMode = "default"
    artifact_type: ArtifactType | None = None
    attached_documents: list[AttachedDocument] = []
    confirmed_plan: ConfirmedPlan | None = None


class ExecutePlanRequest(BaseModel):
    confirmed_plan: ConfirmedPlan = Field(default_factory=ConfirmedPlan)
    client_request_id: str | None = Field(default=None, max_length=128)


class ResearchSourceOut(BaseModel):
    id: int | None = None
    title: str
    url: str
    provider: str | None = None
    credibility_score: float | None = None
    relevance_score: float | None = None
    freshness_score: float | None = None
    source_type: str | None = None
    source_tier: str | None = None
    source_family: str | None = None
    source_role_prior: str | None = None
    published_at: datetime | None = None
    updated_at: datetime | None = None
    source_date_confidence: str | None = None
    admission_status: str | None = None
    admission_reason: str | None = None


class ResearchClaimOut(BaseModel):
    id: int | None = None
    claim: str
    quote: str | None = None
    confidence: str | None = None
    relevance_score: float | None = None
    claim_type: str | None = None
    claim_role: str | None = None
    freshness_risk: str | None = None
    source_id: int | None = None
    source_ref: str | None = None
    source_title: str | None = None
    source_url: str | None = None


class ResearchFindingOut(BaseModel):
    id: int | None = None
    finding: str
    evidence: list[dict] = []
    confidence: str | None = None


class ResearchQuestionOut(BaseModel):
    id: int | None = None
    question: str
    search_query: str | None = None
    status: str | None = None
    claim_type: str | None = None
    evidence_role: str | None = None
    freshness_requirement: str | None = None
    required_source_tiers: list[str] = []
    budget: dict[str, Any] = {}
    stop_reason: str | None = None
    confidence: str | None = None


class ResearchMeta(BaseModel):
    run_id: int
    mode: str
    sources: list[ResearchSourceOut] = []
    claims: list[ResearchClaimOut] = []
    findings: list[ResearchFindingOut] = []
    questions: list[str] = []
    question_threads: list[ResearchQuestionOut] = []
    rejected_sources: list[ResearchSourceOut] = []
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
    id: str
    title: str
    profile: str
    message_count: int
    total_cost_usd: float = 0.0
    created_at: str
    updated_at: str


class ConversationUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class ConversationTurnOut(BaseModel):
    id: str
    status: str
    turn_kind: str = "quick"
    progress: list[dict[str, Any]] = []
    lifecycle: list[dict[str, Any]] = []
    result: dict[str, Any] | None = None
    error_message: str | None = None
    user_message_id: int | None = None
    assistant_message_id: int | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None


class ConversationDetail(BaseModel):
    id: str
    title: str
    profile: str
    message_count: int
    created_at: str
    updated_at: str
    messages: list[MessageOut]
    active_turn: ConversationTurnOut | None = None


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
    conversation_id: str
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
    cost_by_day: list[DailyStat]
    model_usage: list[ModelUsageStat]
    task_distribution: list[TaskStat]
    model_stats: list[ModelDetailStat]


# ── Memory schemas ────────────────────────────────────────────────────────────

class MemoryItem(BaseModel):
    id: int
    content: str
    category: str
    scope: str = "global"
    confidence: float = 0.6
    source: str = "stated"
    seen_count: int = 1
    last_seen_at: str | None = None
    importance: float = 0.5
    pinned: bool = False
    status: str = "active"
    source_conversation_id: int | None = None
    created_at: str
    updated_at: str

class MemoryCreate(BaseModel):
    content: str = Field(min_length=1, max_length=500)
    category: str = Field(default="general", max_length=64)
    scope: str = Field(default="global", max_length=32)


class MemoryUpdate(BaseModel):
    pinned: bool | None = None
    content: str | None = Field(default=None, min_length=1, max_length=500)
    category: str | None = Field(default=None, max_length=64)
    scope: str | None = Field(default=None, max_length=32)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    status: Literal["active", "superseded", "archived"] | None = None


class PersonalProfileOut(BaseModel):
    profile: dict[str, Any] = {}
    last_consolidated_at: str | None = None


class PersonalProfileUpdate(BaseModel):
    overrides: dict[str, Any] = {}


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
