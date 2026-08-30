import json
import secrets
from datetime import datetime, date, timezone
from sqlalchemy import Boolean, create_engine, Date, DateTime, Float, ForeignKey, Integer, String, Text, event, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _strip_postgres_nul(value: str) -> str:
    """PostgreSQL rejects NUL bytes in text/varchar values.

    Web pages, PDFs, and model outputs can occasionally carry `\x00` through
    otherwise-valid Unicode strings. SQLite accepts those values, so local
    tests may pass while production fails at commit time with:
    "A string literal cannot contain NUL (0x00) characters."
    Strip them at the ORM boundary for every textual column.
    """
    return value.replace("\x00", "") if "\x00" in value else value


@event.listens_for(Base, "before_insert", propagate=True)
@event.listens_for(Base, "before_update", propagate=True)
def _sanitize_text_columns(_mapper, _connection, target) -> None:
    for attr in target.__mapper__.column_attrs:
        column = attr.columns[0]
        if not isinstance(column.type, (String, Text)):
            continue
        value = getattr(target, attr.key, None)
        if isinstance(value, str) and "\x00" in value:
            setattr(target, attr.key, _strip_postgres_nul(value))


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    clerk_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Periodically-consolidated "preferences" (how this person likes
    # responses -- tone, format, recurring asks), distilled by
    # app/services/agent/profile_consolidator.py from the user's recent
    # turns across all their workspaces. Workspace-specific "current
    # priorities" live on Workspace.priorities_json instead -- see that
    # model and profile_consolidator.py for why the split matters. Distinct
    # from the per-conversation/per-workspace rolling context in
    # persistence.py: this is a deliberate, LLM-summarized profile refreshed
    # periodically rather than appended to on every turn.
    profile_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    profile_consolidated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Explicit, user-set persistent defaults (quality_mode, output_format,
    # research_level) for new turns. Unlike profile_json, this is never
    # written by the consolidator -- only by the user themselves via
    # PATCH /profile/settings.
    settings_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


def get_or_create_user(db, clerk_id: str, email: str | None = None, name: str | None = None) -> tuple["User", bool]:
    """Upsert the local profile row for a Clerk user. Called on every
    authenticated session bootstrap so a User record exists from first login,
    even before the user starts a conversation.

    Returns (user, created) where `created` is True only the first time this
    clerk_id is seen — used to gate new-signup approval/notification logic."""
    now = datetime.now(timezone.utc)
    user = db.query(User).filter(User.clerk_id == clerk_id).first()
    created = False
    if not user:
        user = User(clerk_id=clerk_id, email=email, name=name, created_at=now, last_login_at=now)
        db.add(user)
        created = True
    else:
        if email and user.email != email:
            user.email = email
        if name and user.name != name:
            user.name = name
        user.last_login_at = now
    db.commit()
    db.refresh(user)
    return user, created


class UserAdminControl(Base):
    __tablename__ = "user_admin_controls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    role: Mapped[str] = mapped_column(String(32), default="user")
    monthly_budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    target_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class AdminSetting(Base):
    __tablename__ = "admin_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    value_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class EvalCase(Base):
    """An admin-managed evaluation case for testing the research pipeline."""
    __tablename__ = "eval_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # JSON list of strings describing what a good answer should include.
    expected_criteria_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Primary evidence role expected ("official_policy", "operational_reality", etc.)
    expected_primary_role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    min_independent_sources: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Structured benchmark thresholds, scored deterministically (not by the LLM judge)
    # against the actual run's evidence_count and criteria.score.
    min_evidence_items: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_criteria_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Expected orchestrator route ("direct"|"clarify"|"research"|"document"|
    # "research_document"); null means the case doesn't assert on routing
    # (only graded on its answer once a route is whatever it is).
    expected_route: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # JSON blob holding the v2 scoring schema's optional nested sections
    # (routing.expected_gate_fires/expected_gate_silent, retrieval_requirements,
    # synthesis_requirements, document_requirements, cost_latency_budget,
    # adversarial_properties, harness_integrity_checks — see eval_case_schema.json
    # case_template). A single JSON column rather than ~15 new individual columns:
    # the schema is still evolving (scoring_spec.md has open implementation
    # questions), and every section is independently optional/sparse by design —
    # scoring functions check each subsection's presence and skip the axis if
    # absent, same pattern as the existing min_evidence_items/min_criteria_score
    # benchmark fields. Null/missing means "this case doesn't assert on that axis."
    v2_spec_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Soft-delete: False = deactivated (hidden from normal queries, never erased).
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class EvalRun(Base):
    """A single admin-triggered evaluation run over a set of EvalCases."""
    __tablename__ = "eval_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="running")  # running|complete|error
    started_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # JSON list of case IDs run; null means all cases at the time of the run.
    case_ids_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON-serialised list of EvalCaseRunResult dicts.
    results_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MaintenanceJob(Base):
    __tablename__ = "maintenance_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(160), unique=True, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued", index=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    result_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False, default="Personal workspace")
    context_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    context_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Periodically-consolidated "what's actively being worked on in this
    # workspace" -- see app/services/agent/profile_consolidator.py. Scoped
    # to the workspace (not the user) so an active project in one workspace
    # doesn't bleed into another workspace's context. Durable preferences
    # (how the user likes responses, not what they're working on) live on
    # User.profile_json instead, since those genuinely are workspace-agnostic.
    priorities_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    priorities_consolidated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # User-curated, durable facts this workspace should always remember.
    # Unlike priorities_json, this is never overwritten by the nightly
    # consolidator; only the profile facts endpoint edits it.
    pinned_facts_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    workspace_id: Mapped[str] = mapped_column(String(64), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(180), nullable=False, default="New conversation")
    context_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    context_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class Turn(Base):
    __tablename__ = "turns"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    route: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    quality_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="standard")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="running", index=True)
    answer: Mapped[str] = mapped_column(Text, nullable=False, default="")
    model_used: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    sources_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # User feedback: "positive" | "negative" | None (not yet rated)
    feedback: Mapped[str | None] = mapped_column(String(16), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    langgraph_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    pause_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Token-budget governance (see context_contracts.ContextTokenBudget). Best-
    # effort estimates via research_utils.estimate_tokens(), not exact
    # provider-billed usage -- see docs note on complete_turn().
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Per-layer breakdown, e.g. {"conversation": 412, "facts": 890, "evidence": 3120}.
    context_tokens_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # True if this turn's research (evidence.gaps) left something unresolved --
    # e.g. couldn't confirm a schedule/fixture/price for a requested date. Lets a
    # later turn in the same conversation know the prior "couldn't confirm X" was
    # an open gap, not a verified negative, so it isn't restated as settled fact.
    had_unresolved_gaps: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # True if this turn's answer was a deep-research confirmation offer. Lets a
    # later turn's orchestrator distinguish "the user is confirming a deep-
    # research offer" from any other clarify exchange, so a short reply like
    # "Yes" can restore research_level="deep" instead of it being recomputed
    # from the reply text alone.
    offered_deep_research: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class LangGraphRunContext(Base):
    __tablename__ = "langgraph_run_contexts"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    tool_config_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # Valid values: running, paused, resuming, completed, failed, orphaned.
    # "resuming" is a short-lived transitional state set atomically by the
    # idempotency guard in resume_langgraph_research before the graph is
    # invoked, closing the double-resume race (see LangGraphResumeConflict).
    # "orphaned" is set by the startup reconciliation job (mirrors
    # _mark_orphaned_eval_runs in app/main.py) for rows still "running" or
    # "resuming" from a process that crashed/restarted.
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="running", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Set atomically together with status="resuming" by the check-and-set
    # guard in resume_langgraph_research. Distinguishes "someone is/has
    # resumed this run" from created_at/updated_at, which get touched by
    # other status transitions too.
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resumed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    turn_id: Mapped[str] = mapped_column(String(64), ForeignKey("turns.id", ondelete="CASCADE"), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    data_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class ToolCall(Base):
    __tablename__ = "tool_calls"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    turn_id: Mapped[str] = mapped_column(String(64), ForeignKey("turns.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    input_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    output_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    turn_id: Mapped[str] = mapped_column(String(64), ForeignKey("turns.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    base64_data: Mapped[str] = mapped_column(Text, nullable=False)
    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class PromptTemplate(Base):
    __tablename__ = "prompt_templates"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    profile: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0.0")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft", index=True)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    developer_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    variables_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class RoutingSignalCandidate(Base):
    __tablename__ = "routing_signal_candidates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    phrase: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_phrase: Mapped[str] = mapped_column(String(240), nullable=False, index=True)
    signal_group: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    suggested_route: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    support_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    false_positive_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    example_turn_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="candidate", index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="learned")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class RoutingDecisionFeedback(Base):
    __tablename__ = "routing_decision_feedback"

    turn_id: Mapped[str] = mapped_column(String(64), ForeignKey("turns.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    selected_route: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    final_route: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    matched_signals_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, default="completed", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class BlogPost(Base):
    __tablename__ = "blog_posts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    slug: Mapped[str] = mapped_column(String(160), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    author: Mapped[str] = mapped_column(String(120), nullable=False, default="Subh Chakraborty")
    # "personal" (byline voice, no badge) or "product" (shown with a Product
    # Update badge) -- see the blog design plan: content can be personal
    # opinion or a Fronei-the-product update, and readers need to be able to
    # tell which at a glance.
    voice: Mapped[str] = mapped_column(String(16), nullable=False, default="personal")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", index=True)
    cover_image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    # Bounded stack of prior body_markdown snapshots, most recent last --
    # see blog.py's _push_revision(). Populated before every body_markdown
    # change (manual save or LLM edit-with-instruction) so any edit is
    # undoable, not just LLM ones. Each entry:
    # {id, body_markdown, label, changes, created_at}.
    revisions_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    @property
    def tags(self) -> list[str]:
        try:
            return json.loads(self.tags_json)
        except (TypeError, ValueError):
            return []

    @property
    def revisions(self) -> list[dict]:
        try:
            return json.loads(self.revisions_json)
        except (TypeError, ValueError):
            return []


class DocumentTemplate(Base):
    __tablename__ = "document_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False, default=lambda: secrets.token_hex(12)
    )
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    doc_type: Mapped[str] = mapped_column(String(64), default="presentation")
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # #182: id of a generated brand design_system (design_systems/<id>/spec.json)
    # produced from this template's BrandProfile (#181). Null until generated,
    # and null for built-in templates that don't get a brand variant.
    design_system_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))



# --- CELPIP preparation app -----------------------------------------------
#
# The CELPIP workspace owns its own tables and service layer rather than
# routing through Fronei's conversational agent stack: a timed exam has a
# server-authoritative clock, a fixed item schema, and a scoring pipeline that
# has nothing in common with a chat turn. See docs/celpip-app-plan.md.
#
# Every row is scoped by user_id (the Clerk id) even though the section is
# admin-only today -- a scored attempt belongs to the person who sat it, and
# a table that assumes a single user is expensive to un-assume later.


class CelpipProfile(Base):
    """Per-user preparation settings and onboarding state."""

    __tablename__ = "celpip_profiles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    # "general" (all four components) or "general_ls" (Listening + Speaking).
    test_type: Mapped[str] = mapped_column(String(16), nullable=False, default="general")
    test_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    target_level: Mapped[int] = mapped_column(Integer, nullable=False, default=9)
    weekday_hours: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    weekend_hours: Mapped[float] = mapped_column(Float, nullable=False, default=2.0)
    # Task keys or weakness tags the learner named for themselves at onboarding.
    # Kept distinct from measured weaknesses (which come from evaluations) --
    # self-report seeds the first plan, measurement replaces it.
    self_reported_weaknesses_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # "pending" until onboarding is completed or explicitly skipped.
    onboarding_state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    diagnostic_attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Last computed readiness composite plus its component sub-scores, so the
    # dashboard can explain why the number moved instead of just showing it.
    readiness_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


class CelpipLesson(Base):
    """Learn-library content.

    Authored in the repo and seeded into the DB rather than rendered straight
    from files, because plan items and result feedback link to lessons by id
    ("review the lesson for this weakness") and a DB row is what makes that
    link resolvable and orderable.
    """

    __tablename__ = "celpip_lessons"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    slug: Mapped[str] = mapped_column(String(160), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # "overview" | "format" | "strategy" | "scoring" | "vocabulary"
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="overview", index=True)
    # Null for general lessons; set for a lesson that teaches one official task.
    skill: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    task_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Weakness tags this lesson addresses -- the join that lets a scored
    # weakness surface "review this" without a hand-maintained mapping table.
    weakness_tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    estimated_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Content hash of the authored source, so reseeding only rewrites lessons
    # whose source actually changed and never clobbers nothing-to-do rows.
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


class CelpipQuestion(Base):
    """One self-contained task instance in the item bank.

    `payload_json` carries the whole item -- stimulus, questions, keyed
    answers, evidence spans, and per-distractor rationale -- validated against
    the per-task schema in services/celpip/schemas.py before the row is
    written. Keeping it as one JSON document rather than normalised question
    rows is deliberate: an item is only ever served, scored, and retired as a
    unit, and the shape differs sharply between task types.
    """

    __tablename__ = "celpip_questions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    skill: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    task_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    part: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # Target CELPIP level this item was written for (1-12).
    difficulty: Mapped[int] = mapped_column(Integer, nullable=False, default=9)
    topic: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    # "generated" | "authored"
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="generated")
    # "draft" -> validation pending; "awaiting_assets" -> validated but its
    # audio or image is still being built; "ready" -> servable; "rejected" ->
    # failed validation; "disabled" -> manually withdrawn; "retired" ->
    # superseded. Only "ready" is ever served.
    # Note "ready" is set by automated validation, not human approval: a review
    # queue that blocks practice would cost more preparation time than it saves
    # (docs/celpip-app-plan.md section 5).
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", index=True)
    # Full validator output: verdict, per-check results, rejection reasons.
    validation_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    validated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Set when a human explicitly approved it in the Question Bank. Never
    # required for serving -- purely a quality signal for assembly preference.
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    generation_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    generator_model: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    validator_model: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    spec_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    # Normalised shingle fingerprint of the stimulus, used to reject items that
    # substantially duplicate something already in the bank.
    content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    times_served: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_served_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


class CelpipQuestionAsset(Base):
    """Generated media belonging to a question: listening audio, the diagram
    image for Reading Part 2, or the scene image for Speaking 3/4/5/8."""

    __tablename__ = "celpip_question_assets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    question_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    # "audio" | "image" | "transcript"
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="audio")
    # Which segment of a multi-part listening item this belongs to (0-based).
    segment_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # blob_store location, e.g. "local:celpip/audio/<id>.mp3" or "s3:...".
    blob_location: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # The exact script the audio was synthesised from. Stored so the validator
    # can confirm audio and transcript agree, and so a failed synthesis can be
    # retried without regenerating the item.
    text_content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    voice: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # "pending" | "ready" | "failed"
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


class CelpipTest(Base):
    """An assembled set of items: a full mock, a component test, a custom
    drill set, or a diagnostic."""

    __tablename__ = "celpip_tests"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # "full" | "full_ls" | "component" | "custom" | "diagnostic" | "single_task"
    mode: Mapped[str] = mapped_column(String(24), nullable=False, default="custom", index=True)
    # Skills covered, in delivery order.
    components_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # "learn" | "timed" | "simulation" -- fixed at assembly because it changes
    # which items are eligible and how the runner behaves.
    practice_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="timed")
    target_level: Mapped[int] = mapped_column(Integer, nullable=False, default=9)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class CelpipTestItem(Base):
    """Ordered membership of a question in a test."""

    __tablename__ = "celpip_test_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    test_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    question_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    skill: Mapped[str] = mapped_column(String(16), nullable=False)
    task_key: Mapped[str] = mapped_column(String(64), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Unscored content appears in a real full mock and is not identified during
    # the attempt. Excluded from raw score at evaluation time.
    is_unscored: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # The official practice task at the head of Listening and Reading.
    is_practice_task: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class CelpipAttempt(Base):
    """One sitting.

    The clock is server-authoritative: section deadlines are computed from
    `section_state_json`'s recorded server start times, never from anything the
    browser reports. That is what makes timer recovery after a refresh correct
    rather than exploitable.
    """

    __tablename__ = "celpip_attempts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    test_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    practice_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="timed")
    # not_started | in_progress | submitted | evaluating | completed | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="not_started", index=True)
    # Per-skill: {started_at, deadline_at, completed_at, auto_submitted}.
    section_state_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    current_skill: Mapped[str | None] = mapped_column(String(16), nullable=True)
    current_position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Question ids the learner flagged to revisit, where the task allows it.
    flagged_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Rolled-up per-component level estimates once evaluation finishes.
    results_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


class CelpipResponse(Base):
    """One answer within an attempt. Autosaved server-side on every change."""

    __tablename__ = "celpip_responses"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    question_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    skill: Mapped[str] = mapped_column(String(16), nullable=False)
    task_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # Index of the sub-question within a receptive item; 0 for writing/speaking.
    question_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Selected option key for multiple choice.
    selected_option: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Written response for writing tasks.
    response_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Speaking capture.
    audio_blob_location: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    audio_duration_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    transcript: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Word-level timing from the transcription provider, when available. Drives
    # the deterministic pace/pause/filler metrics an LLM cannot infer from a
    # cleaned-up transcript.
    transcript_words_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    transcription_status: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    time_spent_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    flagged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Set when the server recorded the answer after the section deadline plus
    # its grace window (reachable through clock skew or a late autosave).
    # Receptive answers flagged here are scored as unanswered -- see
    # scoring.score_receptive_question, which also reports how many were
    # dropped so the learner is told rather than silently docked. Written and
    # spoken responses are NOT dropped on this flag: their text accumulates
    # through autosave, so one late save would discard the whole essay.
    late: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


class CelpipEvaluation(Base):
    """Scoring output for one response (productive) or one task (receptive).

    Writing and Speaking are scored twice by independent passes and reconciled
    when they materially disagree. All three outputs persist: a level estimate
    the learner is meant to trust has to be auditable, and a disagreement
    between passes is itself the honest signal that the estimate is soft.
    """

    __tablename__ = "celpip_evaluations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    response_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    question_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    skill: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    task_key: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # "deterministic" for keyed L/R scoring, "rubric" for W/S.
    method: Mapped[str] = mapped_column(String(16), nullable=False, default="rubric")
    # pending | scoring | complete | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    # Reconciled per-dimension levels and the overall estimate.
    level_low: Mapped[int | None] = mapped_column(Integer, nullable=True)
    level_high: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dimensions_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # 0-1. Falls as the two evaluator passes diverge.
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # The two independent passes, kept verbatim.
    evaluator_a_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    evaluator_b_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    reconciliation_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # Learner-facing bundle: evidence, missing requirements, corrections,
    # outline, patterns. The improved sample response is generated separately
    # and on demand so it cannot bias the score.
    feedback_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    exemplar_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # Deterministic delivery metrics for speaking (pace, pauses, fillers).
    delivery_metrics_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    weakness_tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    evaluator_a_model: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    evaluator_b_model: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    reconciler_model: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    rubric_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CelpipStudyPlanItem(Base):
    """One scheduled activity in the one-month plan."""

    __tablename__ = "celpip_study_plan_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    scheduled_for: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # 1-4, for the week-level view.
    week_index: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # "diagnostic" | "lesson" | "drill" | "timed_component" | "full_mock" |
    # "simulation" | "review" | "vocabulary"
    activity_type: Mapped[str] = mapped_column(String(24), nullable=False, default="drill")
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    skill: Mapped[str | None] = mapped_column(String(16), nullable=True)
    task_keys_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    weakness_tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    lesson_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    estimated_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    # pending | in_progress | completed | skipped | deferred
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Append-only log of {from_date, to_date, reason} so a plan that keeps
    # sliding is visible as such instead of silently rewriting itself.
    reschedule_history_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # Which planner pass created this item, so a rebalance can replace its own
    # prior output without touching items the learner has already started.
    plan_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


class CelpipGenerationRun(Base):
    """Audit of one generation batch, including everything it rejected.

    Rejections are the useful half: a task type whose items keep failing
    independent validation is a prompt problem, and without this table that
    shows up only as a mysteriously empty bank.
    """

    __tablename__ = "celpip_generation_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    task_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    requested_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    difficulty: Mapped[int] = mapped_column(Integer, nullable=False, default=9)
    topic_hint: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # queued | running | complete | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", index=True)
    accepted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # [{reason, detail, task_key}] -- one entry per rejected candidate.
    rejections_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    question_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    generator_model: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    validator_model: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    spec_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


def build_engine():
    settings = get_settings()
    if settings.database_url.startswith("sqlite"):
        # timeout=30 → SQLite driver retries for up to 30 s before raising
        # OperationalError("database is locked"), covering transient contention.
        connect_args = {"check_same_thread": False, "timeout": 30}
        return create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
    return create_engine(settings.database_url, pool_pre_ping=True)


engine = build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    if "sqlite" in str(engine.url):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        # WAL allows concurrent readers + one writer; dramatically reduces lock contention
        # vs the default DELETE journal mode which holds an exclusive lock for the full write.
        cursor.execute("PRAGMA journal_mode=WAL")
        # Belt-and-suspenders: if another writer holds the lock, wait up to 5 s before failing.
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


def get_user_control(db, user_id: str) -> "UserAdminControl | None":
    return db.query(UserAdminControl).filter(UserAdminControl.user_id == user_id).first()


def is_user_suspended(db, user_id: str) -> bool:
    control = get_user_control(db, user_id)
    return bool(control and control.status == "suspended")


def is_user_pending(db, user_id: str) -> bool:
    """True if the user has signed up but is awaiting admin activation."""
    control = get_user_control(db, user_id)
    return bool(control and control.status == "pending")


def bootstrap_user_and_control(
    db,
    user_id: str,
    email: str | None,
    name: str | None,
    *,
    is_admin: bool,
    require_approval: bool,
) -> tuple["User", "UserAdminControl | None", bool]:
    """Single source of truth for "does this account need a control row yet".

    Ensures a local User row exists, and — for non-admins, when approval is
    required — ensures a UserAdminControl row exists too, defaulting brand
    new accounts to status="pending" and notifying admins exactly once.

    This is called both from GET /me (normal first-login bootstrap) and from
    the get_current_active_user_id auth dependency in app/auth.py, so the
    same thing happens even if a client reaches some other endpoint first
    without ever calling /me — closing the gap where a scripted client could
    skip the bootstrap call and stay "fail open" (no control row -> treated
    as not-pending -> full access) forever.

    Returns (user, control, control_just_created). `control` is None only
    when approval is not required or the caller is an admin.
    """
    user, _ = get_or_create_user(db, user_id, email=email, name=name)
    control = get_user_control(db, user_id)
    control_created = False
    if control is None and require_approval and not is_admin:
        now = datetime.now(timezone.utc)
        control = UserAdminControl(user_id=user_id, status="pending", role="user", created_at=now, updated_at=now)
        db.add(control)
        try:
            db.commit()
            control_created = True
        except Exception:
            # Lost a create race with a concurrent request for the same
            # brand-new user (unique constraint on user_id) — fall back to
            # whatever the other request already committed.
            db.rollback()
            control = get_user_control(db, user_id)
        if control_created:
            from app.services.notifications import notify_new_signup  # local import: avoid import cycle
            notify_new_signup(user_id, email, name)
    return user, control, control_created


def get_effective_monthly_budget(db, user_id: str) -> float:
    settings = get_settings()
    control = get_user_control(db, user_id)
    if control and control.monthly_budget_usd is not None:
        return float(control.monthly_budget_usd)
    return settings.monthly_budget_usd


def get_admin_setting(db, key: str) -> dict:
    row = db.query(AdminSetting).filter(AdminSetting.key == key).first()
    if not row or not row.value_json:
        return {}
    try:
        data = json.loads(row.value_json)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def set_admin_setting(db, key: str, value: dict) -> AdminSetting:
    now = datetime.now(timezone.utc)
    row = db.query(AdminSetting).filter(AdminSetting.key == key).first()
    if not row:
        row = AdminSetting(key=key, created_at=now, updated_at=now)
        db.add(row)
    row.value_json = json.dumps(value)
    row.updated_at = now
    return row


def get_global_monthly_spend(db) -> float:
    month_start = datetime.combine(date.today().replace(day=1), datetime.min.time()).replace(tzinfo=timezone.utc)
    spend = (
        db.query(func.sum(Turn.cost_usd))
        .filter(Turn.created_at >= month_start)
        .scalar() or 0.0
    )
    return float(spend)


def get_monthly_spend(db, user_id: str) -> float:
    month_start = datetime.combine(date.today().replace(day=1), datetime.min.time()).replace(tzinfo=timezone.utc)
    spend = (
        db.query(func.sum(Turn.cost_usd))
        .filter(Turn.user_id == user_id)
        .filter(Turn.created_at >= month_start)
        .scalar() or 0.0
    )
    return float(spend)
