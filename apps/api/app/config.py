import logging
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    app_env: str = "local"
    database_url: str = "sqlite:///./fronei.db"
    allowed_origins: str = "http://localhost:3000"
    default_profile: str = "balanced"
    # Default per-user monthly budget cap (USD). Admins (env allowlist) are exempt.
    monthly_budget_usd: float = 5.0
    log_level: str = "INFO"
    log_json: bool = False
    sentry_dsn: str | None = None
    sentry_traces_sample_rate: float = 0.05
    app_release: str = ""

    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None
    openrouter_api_key: str | None = None
    you_api_key: str | None = None
    tavily_api_key: str | None = None
    nimble_api_key: str | None = None
    nimble_api_endpoint: str = "https://sdk.nimbleway.com/v1/search"
    planner_model: str = "openrouter/qwen/qwen3.7-max"
    planner_fallback_models: str = "claude-sonnet-4-6,gemini/gemini-2.5-flash"
    # Agent v3 model assignment (which model handles each role: fast_router,
    # orchestrator, direct_answer, research_planner, synthesis, document_writer,
    # etc.) is no longer configured here. It is DB-backed and admin-editable at
    # runtime via GET/PATCH /admin/model-policy — see
    # app/services/agent/model_policy.py for the defaults and the full role
    # list. Moving it out of .env removed a second, harder-to-discover place
    # that controlled the same thing.
    document_writer_concurrency: int = 3
    longform_timeout_s: int = 300
    clerk_issuer: str = ""
    # Required in production. When unset, JWT audience verification (`verify_aud`)
    # is disabled in app/auth.py — acceptable for local dev only.
    clerk_audience: str = ""
    admin_user_ids: str = ""
    admin_emails: str = ""

    # Clerk Backend API secret key (sk_...). Used to look up a user's email/name
    # by clerk_id for the admin Users tab when the JWT doesn't carry those claims.
    clerk_secret_key: str = ""

    # New-user approval gate: when enabled, accounts created after first sign-in
    # default to status="pending" and cannot use the app until an admin sets
    # them to "active". Admins (env allowlist) are always exempt.
    require_user_approval: bool = True

    # Outbound email (admin notification on new signups). If smtp_host is unset,
    # notifications are logged only (no email sent) — safe default for local dev.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    # Comma-separated recipients for new-signup notifications. Falls back to
    # admin_emails when unset.
    notification_emails: str = ""

    # Shared secret for internal task endpoints, e.g. scheduled profile
    # consolidation. Leave unset locally unless you need to exercise the route.
    internal_task_secret: str = ""

    # Per-user rate limits (sliding window). Admins are exempt.
    rate_limit_chat_per_minute: int = 20
    rate_limit_documents_per_minute: int = 10
    rate_limit_research_per_hour: int = 10
    rate_limit_extraction_per_hour: int = 5

    # Concurrency caps for parallel LLM/extraction work. Each concurrent worker
    # holds its own request/response buffers in memory at the same time, so on
    # memory-constrained instances (e.g. Railway starter plan, 512MB) lower these.
    max_question_workers: int = 4
    max_claim_extract_workers: int = 6
    max_document_workers: int = 5
    max_decompose_workers: int = 4
    max_pptx_render_qa_workers: int = 4

    # Durable turn worker. API requests only enqueue work; a bounded set of
    # database-backed workers claims turns with renewable leases. Expired
    # leases are eligible for retry after a deploy or process crash.
    turn_worker_concurrency: int = 2
    turn_worker_poll_seconds: float = 0.5
    turn_worker_lease_seconds: int = 90
    turn_worker_max_attempts: int = 3

    # Persistent user-uploaded document templates. In production this should
    # point at a mounted volume, e.g. /data/fronei/document_templates.
    document_template_storage_dir: str = "./data/document_templates"

    # Agent v3 generated artifacts. In production this should point at a
    # mounted volume when using the local backend. Production should prefer
    # the S3 backend (AWS S3, Cloudflare R2, MinIO, or another compatible API).
    artifact_storage_backend: str = "local"
    artifact_storage_dir: str = "./data/artifacts"
    artifact_s3_bucket: str = ""
    artifact_s3_endpoint_url: str = ""
    artifact_s3_region: str = "us-east-1"
    artifact_s3_access_key_id: str = ""
    artifact_s3_secret_access_key: str = ""
    artifact_s3_key_prefix: str = "artifacts"
    artifact_download_url_ttl_seconds: int = 300

    # Whether to run LibreOffice/poppler-based PPTX render QA synchronously on
    # the document-generation request path. This can take up to ~60s per deck
    # (see pptx_render_qa.CONVERT_TIMEOUT_SECONDS). Disabled by default in
    # production to avoid adding tens of seconds of latency to every PPTX
    # generation request; enable for local/staging diagnostics.
    pptx_render_qa_enabled: bool = False

    # AgentDeck v2: keep component-usage logging passive until enough real
    # samples exist to make the signal reliable. When false, the planner still
    # logs component usage/QA outcomes but does not use history to rank
    # component candidates.
    agentdeck_usage_stats_weighting_enabled: bool = False

    # AgentDeck executive-mode visual judge. This is intentionally gated behind
    # both quality_mode="executive" and this flag because it sends rendered
    # slide thumbnails to a vision-capable model and adds cost/latency.
    agentdeck_vision_judge_enabled: bool = True
    agentdeck_vision_judge_model: str = "gemini/gemini-2.5-flash"
    agentdeck_vision_judge_max_slides: int = 12
    # Reuse a persistent Node/PptxGenJS process for AgentDeck rendering.
    # Falls back to one-shot subprocess rendering if the warm process fails.
    agentdeck_warm_renderer_enabled: bool = True

    # Agentic runtime migration. Keep disabled until the graph shell is wired
    # in shadow mode and admin traces can show graph events beside the current
    # execution log.
    turn_graph_enabled: bool = False
    # Cutover switch: when true and turn_graph_enabled is also true, graph
    # research/document agents become authoritative instead of shadow wrappers.
    turn_graph_authoritative: bool = False
    # Phase D: enable the LLM-backed orchestrator agent node in the turn graph
    # shell. Existing pipeline remains the fallback on every error.
    orchestrator_enabled: bool = False
    # Temporary operational diagnostics for rollout/cutover. Emits structured
    # INFO logs with prefix "turn_graph_debug" when enabled.
    turn_graph_debug_enabled: bool = False
    # Seed the DB-backed agent registry from file defaults on startup. When
    # unset, this defaults on for local/dev/CI and off for production.
    seed_registry_on_startup: bool | None = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def admin_id_set(self) -> set[str]:
        return {v.strip() for v in self.admin_user_ids.split(",") if v.strip()}

    @property
    def admin_email_set(self) -> set[str]:
        return {v.strip().lower() for v in self.admin_emails.split(",") if v.strip()}

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() in {"prod", "production"}

    @property
    def notification_email_list(self) -> list[str]:
        raw = self.notification_emails or self.admin_emails
        return [v.strip() for v in raw.split(",") if v.strip()]

    @property
    def planner_fallback_model_list(self) -> list[str]:
        return [v.strip() for v in self.planner_fallback_models.split(",") if v.strip()]

    @property
    def should_seed_registry_on_startup(self) -> bool:
        if self.seed_registry_on_startup is not None:
            return self.seed_registry_on_startup
        return not self.is_production


@lru_cache
def get_settings() -> Settings:
    return Settings()


def check_production_config() -> None:
    """Fail fast on startup if production is misconfigured for Clerk audience
    verification. CLERK_AUDIENCE controls `verify_aud` in app/auth.py — running
    production without it means tokens are accepted without audience checks.
    """
    settings = get_settings()
    if not settings.is_production:
        return
    if not settings.clerk_issuer:
        raise RuntimeError("CLERK_ISSUER must be set when APP_ENV=production.")
    if not settings.clerk_audience:
        raise RuntimeError(
            "CLERK_AUDIENCE must be set when APP_ENV=production. "
            "Without it, JWT audience verification is disabled. "
            "Set CLERK_AUDIENCE to your Clerk app's API audience, "
            "or configure an audience claim in your Clerk JWT template."
        )
    if not settings.admin_id_set and not settings.admin_email_set:
        logger.warning(
            "ADMIN_USER_IDS / ADMIN_EMAILS are both empty in production — "
            "no user can access /admin endpoints."
        )
    backend = settings.artifact_storage_backend.strip().lower()
    if backend not in {"local", "s3"}:
        raise RuntimeError("ARTIFACT_STORAGE_BACKEND must be 'local' or 's3'.")
    if backend == "s3" and not settings.artifact_s3_bucket:
        raise RuntimeError("ARTIFACT_S3_BUCKET must be set when ARTIFACT_STORAGE_BACKEND=s3.")
    if backend == "local":
        logger.warning(
            "Production artifact storage uses the local filesystem. "
            "Use a persistent volume or set ARTIFACT_STORAGE_BACKEND=s3."
        )
