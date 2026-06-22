"""Coverage for the admin-approval access gate (app/auth.py:get_current_active_user_id).

This is the dependency every resource-consuming endpoint uses instead of the
bare `get_current_user_id`. It must fail closed: a brand-new account with no
UserAdminControl row yet, a "pending" account, and a "suspended" account
must all be rejected, while admins and "active" accounts pass through.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.db.models as db_models
from app.auth import get_current_active_user_id
from app.config import get_settings
from app.db.models import Base, UserAdminControl


@pytest.fixture
def gated_db(monkeypatch):
    """Isolated in-memory DB wired into app.db.models.SessionLocal, plus
    REQUIRE_USER_APPROVAL=true for the duration of the test (the wider test
    suite defaults this off; this is the one place that needs it on)."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(db_models, "SessionLocal", Session)
    monkeypatch.setenv("REQUIRE_USER_APPROVAL", "true")
    monkeypatch.setenv("ADMIN_USER_IDS", "admin-1")
    get_settings.cache_clear()
    yield Session
    get_settings.cache_clear()


def test_brand_new_user_is_auto_pending_and_denied(gated_db):
    """The core fix: a client that never calls /me first must still be
    blocked on its very first request, not fail open because no control
    row exists yet."""
    Session = gated_db
    with pytest.raises(HTTPException) as exc_info:
        get_current_active_user_id(user_id="brand-new-user")
    assert exc_info.value.status_code == 403
    assert "pending" in exc_info.value.detail.lower()

    with Session() as session:
        control = session.query(UserAdminControl).filter(
            UserAdminControl.user_id == "brand-new-user"
        ).first()
        assert control is not None
        assert control.status == "pending"


def test_pending_user_is_denied(gated_db):
    Session = gated_db
    with Session() as session:
        session.add(UserAdminControl(user_id="pending-user", status="pending", role="user"))
        session.commit()

    with pytest.raises(HTTPException) as exc_info:
        get_current_active_user_id(user_id="pending-user")
    assert exc_info.value.status_code == 403
    assert "pending" in exc_info.value.detail.lower()


def test_suspended_user_is_denied(gated_db):
    Session = gated_db
    with Session() as session:
        session.add(UserAdminControl(user_id="suspended-user", status="suspended", role="user"))
        session.commit()

    with pytest.raises(HTTPException) as exc_info:
        get_current_active_user_id(user_id="suspended-user")
    assert exc_info.value.status_code == 403
    assert "suspended" in exc_info.value.detail.lower()


def test_active_user_passes(gated_db):
    Session = gated_db
    with Session() as session:
        session.add(UserAdminControl(user_id="active-user", status="active", role="user"))
        session.commit()

    assert get_current_active_user_id(user_id="active-user") == "active-user"


def test_db_role_admin_bypasses_even_when_pending(gated_db):
    """An admin promoted via the DB role (not the env allowlist) must not be
    blocked even if their control row's status is still 'pending'."""
    Session = gated_db
    with Session() as session:
        session.add(UserAdminControl(user_id="promoted-admin", status="pending", role="admin"))
        session.commit()

    assert get_current_active_user_id(user_id="promoted-admin") == "promoted-admin"


def test_env_allowlisted_admin_bypasses_with_no_control_row(gated_db):
    assert get_current_active_user_id(user_id="admin-1") == "admin-1"
    with gated_db() as session:
        control = session.query(UserAdminControl).filter(
            UserAdminControl.user_id == "admin-1"
        ).first()
        assert control is None, "admins should never be auto-enrolled into the approval gate"


def test_gate_is_inert_when_approval_not_required(monkeypatch, gated_db):
    monkeypatch.setenv("REQUIRE_USER_APPROVAL", "false")
    get_settings.cache_clear()
    try:
        assert get_current_active_user_id(user_id="anyone-at-all") == "anyone-at-all"
    finally:
        get_settings.cache_clear()
