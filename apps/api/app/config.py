import logging
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    app_env: str = "local"
    database_url: str = "sqlite:///./fronei.db"
    allowed_origins: str = "http://localhost:3000"
    default_profile: str = "balanced"
    daily_budget_usd: float = 10.0

    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None
    openrouter_api_key: str | None = None
    tavily_api_key: str | None = None
    brave_api_key: str | None = None
    planner_model: str = "claude-sonnet-4-6"
    clerk_issuer: str = ""
    # Required in production. When unset, JWT audience verification (`verify_aud`)
    # is disabled in app/auth.py — acceptable for local dev only.
    clerk_audience: str = ""
    admin_user_ids: str = ""
    admin_emails: str = ""

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

    # Per-user rate limits (sliding window). Admins are exempt.
    rate_limit_chat_per_minute: int = 20
    rate_limit_documents_per_minute: int = 10
    rate_limit_research_per_hour: int = 10
    rate_limit_extraction_per_hour: int = 5

    # Concurrency caps for parallel LLM/extraction work. Each concurrent worker
    # holds its own request/response buffers in memory at the same time, so on
    # memory-constrained instances (e.g. Render free tier, 512MB) lower these.
    max_question_workers: int = 4
    max_claim_extract_workers: int = 6
    max_document_workers: int = 5
    max_decompose_workers: int = 4

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
