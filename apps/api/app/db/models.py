import json
from datetime import datetime, date, timezone
from sqlalchemy import Boolean, create_engine, DateTime, Float, ForeignKey, Integer, String, Text, event, func, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from app.config import get_settings


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    clerk_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


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


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True, default="")
    title: Mapped[str] = mapped_column(String(120), default="New conversation")
    profile: Mapped[str] = mapped_column(String(32), default="balanced")
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    # Conversation memory (step 2)
    running_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    active_task_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    messages: Mapped[list["ConversationMessage"]] = relationship(
        "ConversationMessage",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="ConversationMessage.id",
    )


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    task_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    complexity: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model_used: Mapped[str | None] = mapped_column(String(128), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    execution_log_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    research_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    plan_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    conversation: Mapped["Conversation"] = relationship("Conversation", back_populates="messages")


class RequestLog(Base):
    __tablename__ = "request_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    message: Mapped[str] = mapped_column(Text)
    task_type: Mapped[str] = mapped_column(String(64))
    complexity: Mapped[str] = mapped_column(String(32))
    profile: Mapped[str] = mapped_column(String(32))
    selected_model: Mapped[str] = mapped_column(String(128))
    model_used: Mapped[str] = mapped_column(String(128))
    latency_ms: Mapped[int] = mapped_column(Integer)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="success")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


DEFAULT_DECAY_RATES: dict[str, float] = {
    "bio": 0.005,
    "work": 0.005,
    "project": 0.08,
    "preference": 0.03,
    "communication_style": 0.01,
    "relationship": 0.03,
    "constraint": 0.03,
    "temporary_plan": 0.15,
    "tool": 0.03,
    "personal": 0.03,
    "general": 0.05,
}


class UserMemory(Base):
    __tablename__ = "user_memories"

    id:                     Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id:                Mapped[str]      = mapped_column(String(128), nullable=False, index=True)
    content:                Mapped[str]      = mapped_column(Text)
    category:               Mapped[str]      = mapped_column(String(64), default="general")
    scope:                  Mapped[str]      = mapped_column(String(32), default="global")
    confidence:             Mapped[float]    = mapped_column(Float, default=0.6)
    source:                 Mapped[str]      = mapped_column(String(16), default="stated")
    seen_count:             Mapped[int]      = mapped_column(Integer, default=1)
    last_seen_at:           Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    importance:             Mapped[float]    = mapped_column(Float, default=0.5)
    decay_rate:             Mapped[float]    = mapped_column(Float, default=0.05)
    pinned:                 Mapped[bool]     = mapped_column(Boolean, default=False)
    status:                 Mapped[str]      = mapped_column(String(16), default="active")
    superseded_by_id:       Mapped[int|None] = mapped_column(Integer, nullable=True)
    source_conversation_id: Mapped[int|None] = mapped_column(Integer, nullable=True)
    created_at:             Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at:             Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class UserProfile(Base):
    __tablename__ = "user_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    profile_json: Mapped[str] = mapped_column(Text, default="{}")
    last_consolidated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class WritingSample(Base):
    __tablename__ = "writing_samples"

    id:         Mapped[int]        = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id:    Mapped[str]        = mapped_column(String(128), nullable=False, index=True)
    content:    Mapped[str]        = mapped_column(Text, nullable=False)
    label:      Mapped[str | None] = mapped_column(String(120), nullable=True)
    char_count: Mapped[int]        = mapped_column(Integer, default=0)
    created_at: Mapped[datetime]   = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class TwinProfile(Base):
    __tablename__ = "twin_profiles"

    id:               Mapped[int]             = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id:          Mapped[str]             = mapped_column(String(128), unique=True, nullable=False, index=True)
    fingerprint_json: Mapped[str | None]      = mapped_column(Text, nullable=True)
    rewrite_prompt:   Mapped[str | None]      = mapped_column(Text, nullable=True)
    extracted_at:     Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    prefs_json:       Mapped[str | None]      = mapped_column(Text, nullable=True)
    created_at:       Mapped[datetime]        = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at:       Mapped[datetime]        = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class ResearchRun(Base):
    __tablename__ = "research_runs"

    id:                      Mapped[int]             = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id:                 Mapped[str]             = mapped_column(String(128), nullable=False, index=True)
    conversation_id:         Mapped[int | None]      = mapped_column(Integer, nullable=True, index=True)
    query:                   Mapped[str]             = mapped_column(Text, nullable=False)
    mode:                    Mapped[str]             = mapped_column(String(32), default="deep")
    status:                  Mapped[str]             = mapped_column(String(32), default="running")
    iterations:              Mapped[int]             = mapped_column(Integer, default=0)
    max_sources:             Mapped[int]             = mapped_column(Integer, default=12)
    source_count:            Mapped[int]             = mapped_column(Integer, default=0)
    claim_count:             Mapped[int]             = mapped_column(Integer, default=0)
    confidence:              Mapped[str | None]      = mapped_column(String(32), nullable=True)
    gaps_json:               Mapped[str | None]      = mapped_column(Text, nullable=True)
    contradictions_json:     Mapped[str | None]      = mapped_column(Text, nullable=True)
    verifier_notes:          Mapped[str | None]      = mapped_column(Text, nullable=True)
    final_answer:            Mapped[str | None]      = mapped_column(Text, nullable=True)
    created_at:              Mapped[datetime]        = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at:              Mapped[datetime]        = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class ResearchQuestion(Base):
    __tablename__ = "research_questions"

    id:              Mapped[int]        = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id:          Mapped[int]        = mapped_column(Integer, ForeignKey("research_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    question:        Mapped[str]        = mapped_column(Text, nullable=False)
    search_query:    Mapped[str | None] = mapped_column(Text, nullable=True)
    status:          Mapped[str]        = mapped_column(String(32), default="pending")
    created_at:      Mapped[datetime]   = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class ResearchSource(Base):
    __tablename__ = "research_sources"

    id:                  Mapped[int]        = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id:              Mapped[int]        = mapped_column(Integer, ForeignKey("research_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    question_id:         Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    title:               Mapped[str]        = mapped_column(Text, nullable=False)
    url:                 Mapped[str]        = mapped_column(Text, nullable=False)
    provider:            Mapped[str]        = mapped_column(String(64), default="")
    excerpt:             Mapped[str | None] = mapped_column(Text, nullable=True)
    credibility_score:   Mapped[float]      = mapped_column(Float, default=0.0)
    relevance_score:     Mapped[float]      = mapped_column(Float, default=0.0)
    freshness_score:     Mapped[float]      = mapped_column(Float, default=0.0)
    source_type:         Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at:          Mapped[datetime]   = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class ResearchClaim(Base):
    __tablename__ = "research_claims"

    id:                Mapped[int]        = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id:            Mapped[int]        = mapped_column(Integer, ForeignKey("research_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    source_id:         Mapped[int]        = mapped_column(Integer, ForeignKey("research_sources.id", ondelete="CASCADE"), nullable=False, index=True)
    claim:             Mapped[str]        = mapped_column(Text, nullable=False)
    quote:             Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence:        Mapped[str]        = mapped_column(String(32), default="medium")
    relevance_score:   Mapped[float]      = mapped_column(Float, default=0.0)
    created_at:        Mapped[datetime]   = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class ResearchFinding(Base):
    __tablename__ = "research_findings"

    id:            Mapped[int]        = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id:        Mapped[int]        = mapped_column(Integer, ForeignKey("research_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    finding:       Mapped[str]        = mapped_column(Text, nullable=False)
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence:    Mapped[str]        = mapped_column(String(32), default="medium")
    created_at:    Mapped[datetime]   = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


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


def init_db() -> None:
    # Dev fallback: create tables that don't exist yet (no-op on first run after
    # `alembic upgrade head`).  In production run: alembic upgrade head
    Base.metadata.create_all(bind=engine)
    _ensure_sqlite_schema(engine)


def _ensure_sqlite_schema(bind) -> None:
    """Additive SQLite schema repair for DBs bootstrapped before Alembic ran."""
    if "sqlite" not in str(bind.url):
        return

    inspector = inspect(bind)

    def has_table(table: str) -> bool:
        return table in inspector.get_table_names()

    def has_column(table: str, column: str) -> bool:
        if not has_table(table):
            return False
        return column in {c["name"] for c in inspector.get_columns(table)}

    statements: list[str] = []
    if has_table("users") and not has_column("users", "last_login_at"):
        statements.append("ALTER TABLE users ADD COLUMN last_login_at DATETIME")

    if has_table("conversations"):
        if not has_column("conversations", "user_id"):
            statements.append("ALTER TABLE conversations ADD COLUMN user_id VARCHAR(128) NOT NULL DEFAULT ''")
        if not has_column("conversations", "running_summary"):
            statements.append("ALTER TABLE conversations ADD COLUMN running_summary TEXT")
        if not has_column("conversations", "active_task_json"):
            statements.append("ALTER TABLE conversations ADD COLUMN active_task_json TEXT")

    if has_table("request_logs") and not has_column("request_logs", "user_id"):
        statements.append("ALTER TABLE request_logs ADD COLUMN user_id VARCHAR(128) NOT NULL DEFAULT ''")

    if has_table("user_memories"):
        memory_columns = [
            ("scope", "VARCHAR(32) DEFAULT 'global'"),
            ("confidence", "FLOAT DEFAULT 0.6"),
            ("source", "VARCHAR(16) DEFAULT 'stated'"),
            ("seen_count", "INTEGER DEFAULT 1"),
            ("last_seen_at", "DATETIME"),
            ("importance", "FLOAT DEFAULT 0.5"),
            ("decay_rate", "FLOAT DEFAULT 0.05"),
            ("pinned", "BOOLEAN DEFAULT 0"),
            ("status", "VARCHAR(16) DEFAULT 'active'"),
            ("superseded_by_id", "INTEGER"),
        ]
        for column, ddl in memory_columns:
            if not has_column("user_memories", column):
                statements.append(f"ALTER TABLE user_memories ADD COLUMN {column} {ddl}")
        statements.append("CREATE INDEX IF NOT EXISTS ix_user_memories_user_id_status ON user_memories (user_id, status)")

    if not has_table("user_profiles"):
        statements.append("""
            CREATE TABLE user_profiles (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                user_id VARCHAR(128) NOT NULL,
                profile_json TEXT DEFAULT '{}',
                last_consolidated_at DATETIME,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
        """)
    if has_table("user_profiles"):
        statements.append("CREATE UNIQUE INDEX IF NOT EXISTS ix_user_profiles_user_id ON user_profiles (user_id)")

    admin_table_sql = {
        "user_admin_controls": """
            CREATE TABLE user_admin_controls (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                user_id VARCHAR(128) NOT NULL,
                status VARCHAR(32) DEFAULT 'active',
                role VARCHAR(32) DEFAULT 'user',
                monthly_budget_usd FLOAT,
                notes TEXT,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
        """,
        "admin_audit_logs": """
            CREATE TABLE admin_audit_logs (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                admin_user_id VARCHAR(128) NOT NULL,
                action VARCHAR(120) NOT NULL,
                target_user_id VARCHAR(128),
                details_json TEXT,
                created_at DATETIME NOT NULL
            )
        """,
        "admin_settings": """
            CREATE TABLE admin_settings (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                key VARCHAR(128) NOT NULL,
                value_json TEXT DEFAULT '{}',
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
        """,
    }
    for table, statement in admin_table_sql.items():
        if not has_table(table):
            statements.append(statement)

    if has_table("user_admin_controls") and not has_column("user_admin_controls", "role"):
        statements.append("ALTER TABLE user_admin_controls ADD COLUMN role VARCHAR(32) DEFAULT 'user'")
    if has_table("user_admin_controls") and not has_column("user_admin_controls", "monthly_budget_usd"):
        statements.append("ALTER TABLE user_admin_controls ADD COLUMN monthly_budget_usd FLOAT")
    if has_table("user_admin_controls"):
        statements.append("CREATE UNIQUE INDEX IF NOT EXISTS ix_user_admin_controls_user_id ON user_admin_controls (user_id)")
    if has_table("admin_audit_logs"):
        statements.append("CREATE INDEX IF NOT EXISTS ix_admin_audit_logs_admin_user_id ON admin_audit_logs (admin_user_id)")
        statements.append("CREATE INDEX IF NOT EXISTS ix_admin_audit_logs_target_user_id ON admin_audit_logs (target_user_id)")
    if has_table("admin_settings"):
        statements.append("CREATE UNIQUE INDEX IF NOT EXISTS ix_admin_settings_key ON admin_settings (key)")

    if has_table("conversation_messages") and not has_column("conversation_messages", "execution_log_json"):
        statements.append("ALTER TABLE conversation_messages ADD COLUMN execution_log_json TEXT")
    if has_table("conversation_messages") and not has_column("conversation_messages", "research_run_id"):
        statements.append("ALTER TABLE conversation_messages ADD COLUMN research_run_id INTEGER")

    research_table_sql = {
        "research_runs": """
            CREATE TABLE research_runs (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                user_id VARCHAR(128) NOT NULL,
                conversation_id INTEGER,
                query TEXT NOT NULL,
                mode VARCHAR(32) DEFAULT 'deep',
                status VARCHAR(32) DEFAULT 'running',
                iterations INTEGER DEFAULT 0,
                max_sources INTEGER DEFAULT 12,
                source_count INTEGER DEFAULT 0,
                claim_count INTEGER DEFAULT 0,
                confidence VARCHAR(32),
                gaps_json TEXT,
                contradictions_json TEXT,
                verifier_notes TEXT,
                final_answer TEXT,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
        """,
        "research_questions": """
            CREATE TABLE research_questions (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                question TEXT NOT NULL,
                search_query TEXT,
                status VARCHAR(32) DEFAULT 'pending',
                created_at DATETIME NOT NULL,
                FOREIGN KEY(run_id) REFERENCES research_runs (id) ON DELETE CASCADE
            )
        """,
        "research_sources": """
            CREATE TABLE research_sources (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                question_id INTEGER,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                provider VARCHAR(64) DEFAULT '',
                excerpt TEXT,
                credibility_score FLOAT DEFAULT 0.0,
                relevance_score FLOAT DEFAULT 0.0,
                freshness_score FLOAT DEFAULT 0.0,
                source_type VARCHAR(64),
                created_at DATETIME NOT NULL,
                FOREIGN KEY(run_id) REFERENCES research_runs (id) ON DELETE CASCADE
            )
        """,
        "research_claims": """
            CREATE TABLE research_claims (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                source_id INTEGER NOT NULL,
                claim TEXT NOT NULL,
                quote TEXT,
                confidence VARCHAR(32) DEFAULT 'medium',
                relevance_score FLOAT DEFAULT 0.0,
                created_at DATETIME NOT NULL,
                FOREIGN KEY(run_id) REFERENCES research_runs (id) ON DELETE CASCADE,
                FOREIGN KEY(source_id) REFERENCES research_sources (id) ON DELETE CASCADE
            )
        """,
        "research_findings": """
            CREATE TABLE research_findings (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                finding TEXT NOT NULL,
                evidence_json TEXT,
                confidence VARCHAR(32) DEFAULT 'medium',
                created_at DATETIME NOT NULL,
                FOREIGN KEY(run_id) REFERENCES research_runs (id) ON DELETE CASCADE
            )
        """,
    }
    for table, statement in research_table_sql.items():
        if not has_table(table):
            statements.append(statement)

    for table, column in [
        ("research_runs", "user_id"),
        ("research_runs", "conversation_id"),
        ("research_questions", "run_id"),
        ("research_sources", "run_id"),
        ("research_sources", "question_id"),
        ("research_claims", "run_id"),
        ("research_claims", "source_id"),
        ("research_findings", "run_id"),
        ("conversation_messages", "research_run_id"),
    ]:
        if has_table(table):
            statements.append(f"CREATE INDEX IF NOT EXISTS ix_{table}_{column} ON {table} ({column})")

    if not statements:
        return

    with bind.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def get_all_memories(db, user_id: str) -> str:
    """Deprecated: return active memories for a user as a formatted block.

    New prompt paths should use app.services.personal_context.build_context().
    """
    mems = (
        db.query(UserMemory)
        .filter(UserMemory.user_id == user_id, UserMemory.status == "active")
        .order_by(UserMemory.updated_at.desc())
        .all()
    )
    if not mems:
        return ""
    return "\n".join(f"- [{m.category}] {m.content}" for m in mems)


def get_twin_profile(db, user_id: str) -> "TwinProfile | None":
    """Return the TwinProfile for a user, or None if not yet created."""
    return db.query(TwinProfile).filter(TwinProfile.user_id == user_id).first()


def get_user_control(db, user_id: str) -> "UserAdminControl | None":
    return db.query(UserAdminControl).filter(UserAdminControl.user_id == user_id).first()


def is_user_suspended(db, user_id: str) -> bool:
    control = get_user_control(db, user_id)
    return bool(control and control.status == "suspended")


def is_user_pending(db, user_id: str) -> bool:
    """True if the user has signed up but is awaiting admin activation."""
    control = get_user_control(db, user_id)
    return bool(control and control.status == "pending")


def get_effective_monthly_budget(db, user_id: str) -> float:
    settings = get_settings()
    control = get_user_control(db, user_id)
    if control and control.monthly_budget_usd is not None:
        return float(control.monthly_budget_usd)
    return settings.monthly_budget_usd


GLOBAL_BUDGET_SETTING_KEY = "global_budget"


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


def get_global_budget_config(db) -> dict:
    raw = get_admin_setting(db, GLOBAL_BUDGET_SETTING_KEY)
    cap = raw.get("monthly_budget_usd")
    try:
        cap = float(cap) if cap is not None else None
    except (TypeError, ValueError):
        cap = None
    return {
        "monthly_budget_usd": cap if cap is not None and cap >= 0 else None,
        "admin_override_enabled": bool(raw.get("admin_override_enabled", True)),
    }


def set_global_budget_config(db, monthly_budget_usd: float | None, admin_override_enabled: bool) -> AdminSetting:
    return set_admin_setting(db, GLOBAL_BUDGET_SETTING_KEY, {
        "monthly_budget_usd": monthly_budget_usd,
        "admin_override_enabled": admin_override_enabled,
    })


def get_global_monthly_spend(db) -> float:
    month_start = datetime.combine(date.today().replace(day=1), datetime.min.time()).replace(tzinfo=timezone.utc)
    msg_spend = (
        db.query(func.sum(ConversationMessage.estimated_cost_usd))
        .join(Conversation, ConversationMessage.conversation_id == Conversation.id)
        .filter(ConversationMessage.role == "assistant")
        .filter(ConversationMessage.created_at >= month_start)
        .scalar() or 0.0
    )
    log_spend = (
        db.query(func.sum(RequestLog.estimated_cost_usd))
        .filter(RequestLog.status == "success")
        .filter(RequestLog.created_at >= month_start)
        .scalar() or 0.0
    )
    return float(msg_spend) + float(log_spend)


def get_monthly_spend(db, user_id: str) -> float:
    month_start = datetime.combine(date.today().replace(day=1), datetime.min.time()).replace(tzinfo=timezone.utc)
    msg_spend = (
        db.query(func.sum(ConversationMessage.estimated_cost_usd))
        .join(Conversation, ConversationMessage.conversation_id == Conversation.id)
        .filter(ConversationMessage.role == "assistant")
        .filter(Conversation.user_id == user_id)
        .filter(ConversationMessage.created_at >= month_start)
        .scalar() or 0.0
    )
    log_spend = (
        db.query(func.sum(RequestLog.estimated_cost_usd))
        .filter(RequestLog.status == "success")
        .filter(RequestLog.user_id == user_id)
        .filter(RequestLog.created_at >= month_start)
        .scalar() or 0.0
    )
    return float(msg_spend) + float(log_spend)
