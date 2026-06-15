import pytest
from sqlalchemy import create_engine
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker
from app.db.models import Base, Conversation, ConversationMessage, RequestLog, UserAdminControl, _ensure_sqlite_schema
from datetime import datetime, timezone


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_get_monthly_spend_empty(db):
    from app.db.models import get_monthly_spend
    assert get_monthly_spend(db, "u1") == 0.0


def test_get_monthly_spend_with_data(db):
    from app.db.models import get_monthly_spend
    conv = Conversation(user_id="u1", title="t", profile="balanced", message_count=2)
    db.add(conv); db.flush()
    msg = ConversationMessage(
        conversation_id=conv.id, role="assistant", content="hi",
        estimated_cost_usd=0.05,
        created_at=datetime.now(timezone.utc),
    )
    db.add(msg); db.commit()
    assert get_monthly_spend(db, "u1") == pytest.approx(0.05)


def test_get_monthly_spend_is_user_scoped(db):
    from app.db.models import get_monthly_spend

    conv1 = Conversation(user_id="u1", title="u1", profile="balanced", message_count=2)
    conv2 = Conversation(user_id="u2", title="u2", profile="balanced", message_count=2)
    db.add_all([conv1, conv2]); db.flush()
    db.add_all([
        ConversationMessage(
            conversation_id=conv1.id, role="assistant", content="u1",
            estimated_cost_usd=0.05,
            created_at=datetime.now(timezone.utc),
        ),
        ConversationMessage(
            conversation_id=conv2.id, role="assistant", content="u2",
            estimated_cost_usd=0.20,
            created_at=datetime.now(timezone.utc),
        ),
        RequestLog(
            user_id="u1", message="hi", task_type="unknown", complexity="low",
            profile="balanced", selected_model="gpt-4.1-mini",
            model_used="gpt-4.1-mini", latency_ms=10,
            estimated_cost_usd=0.01, status="success",
            created_at=datetime.now(timezone.utc),
        ),
        RequestLog(
            user_id="u2", message="hi", task_type="unknown", complexity="low",
            profile="balanced", selected_model="gpt-4.1-mini",
            model_used="gpt-4.1-mini", latency_ms=10,
            estimated_cost_usd=0.30, status="success",
            created_at=datetime.now(timezone.utc),
        ),
    ])
    db.commit()

    assert get_monthly_spend(db, "u1") == pytest.approx(0.06)
    assert get_monthly_spend(db, "u2") == pytest.approx(0.50)


def test_admin_budget_override_and_suspension_helpers(db):
    from app.db.models import get_effective_monthly_budget, is_user_suspended

    db.add(UserAdminControl(
        user_id="u1",
        status="suspended",
        monthly_budget_usd=2.5,
    ))
    db.commit()

    assert get_effective_monthly_budget(db, "u1") == pytest.approx(2.5)
    assert is_user_suspended(db, "u1") is True
    assert is_user_suspended(db, "missing") is False


def test_ensure_sqlite_schema_adds_missing_legacy_columns():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE conversations (
                id INTEGER PRIMARY KEY,
                title VARCHAR(120) NOT NULL,
                profile VARCHAR(32) NOT NULL,
                message_count INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE conversation_messages (
                id INTEGER PRIMARY KEY,
                conversation_id INTEGER NOT NULL,
                role VARCHAR(16) NOT NULL,
                content TEXT NOT NULL,
                created_at DATETIME NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE request_logs (
                id INTEGER PRIMARY KEY,
                created_at DATETIME NOT NULL,
                message TEXT NOT NULL,
                task_type VARCHAR(64) NOT NULL,
                complexity VARCHAR(32) NOT NULL,
                profile VARCHAR(32) NOT NULL,
                selected_model VARCHAR(128) NOT NULL,
                model_used VARCHAR(128) NOT NULL,
                latency_ms INTEGER NOT NULL,
                status VARCHAR(32) NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE document_templates (
                id INTEGER PRIMARY KEY,
                public_id VARCHAR(32) NOT NULL,
                user_id VARCHAR(128) NOT NULL,
                name VARCHAR(160) NOT NULL,
                storage_key VARCHAR(512) NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
        """))

    _ensure_sqlite_schema(engine)

    inspector = inspect(engine)
    conversation_cols = {c["name"] for c in inspector.get_columns("conversations")}
    message_cols = {c["name"] for c in inspector.get_columns("conversation_messages")}
    request_cols = {c["name"] for c in inspector.get_columns("request_logs")}
    admin_control_cols = {c["name"] for c in inspector.get_columns("user_admin_controls")}
    audit_cols = {c["name"] for c in inspector.get_columns("admin_audit_logs")}
    template_cols = {c["name"] for c in inspector.get_columns("document_templates")}

    assert {"user_id", "running_summary", "active_task_json"} <= conversation_cols
    assert "execution_log_json" in message_cols
    assert "user_id" in request_cols
    assert {"user_id", "status", "monthly_budget_usd", "notes"} <= admin_control_cols
    assert {"admin_user_id", "action", "target_user_id", "details_json"} <= audit_cols
    assert "design_system_id" in template_cols
