import json
import secrets
from datetime import datetime, date, timezone
from sqlalchemy import Boolean, create_engine, DateTime, Float, ForeignKey, Integer, String, Text, event, func
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
