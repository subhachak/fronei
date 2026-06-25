from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

import app.db.models as db_models
from app.auth import (
    AdminPrincipal,
    get_current_user_is_admin,
    is_admin_user,
    is_env_admin,
    require_admin_principal,
)
from app.config import get_settings
from app.db.models import Base, User, UserAdminControl
from app.routers import admin as admin_router


@pytest.fixture
def admin_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(db_models, "SessionLocal", Session)
    monkeypatch.setattr(admin_router, "SessionLocal", Session)
    monkeypatch.setenv("ADMIN_USER_IDS", "env-admin")
    monkeypatch.setenv("ADMIN_EMAILS", "email-admin@example.com")
    get_settings.cache_clear()
    yield Session
    get_settings.cache_clear()


def _request(path: str = "/admin/overview") -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "scheme": "https",
        "server": ("testserver", 443),
        "client": ("127.0.0.1", 1234),
        "query_string": b"",
    })


def test_static_and_db_admins_share_one_policy(admin_db):
    with admin_db() as db:
        db.add(UserAdminControl(user_id="db-admin", status="pending", role="admin"))
        db.commit()

    assert is_env_admin("env-admin", None) is True
    assert is_admin_user("env-admin", None) is True
    assert is_admin_user("someone", "email-admin@example.com") is True
    assert is_admin_user("db-admin", None) is True
    assert is_admin_user("ordinary-user", None) is False


def test_boolean_and_enforcing_dependencies_use_same_policy(admin_db):
    with admin_db() as db:
        db.add(UserAdminControl(user_id="db-admin", status="active", role="admin"))
        db.commit()

    payload = {"sub": "db-admin", "email": "db-admin@example.com"}
    assert get_current_user_is_admin(payload) is True
    principal = require_admin_principal(_request(), payload)
    assert principal == AdminPrincipal(user_id="db-admin", email="db-admin@example.com")

    denied_payload = {"sub": "ordinary-user", "email": "user@example.com"}
    assert get_current_user_is_admin(denied_payload) is False
    with pytest.raises(HTTPException) as exc_info:
        require_admin_principal(_request(), denied_payload)
    assert exc_info.value.status_code == 403


def test_admin_cannot_remove_own_role(admin_db):
    with pytest.raises(HTTPException) as exc_info:
        admin_router.update_user_role(
            "db-admin",
            admin_router.UserRoleUpdate(role="user"),
            admin=AdminPrincipal(user_id="db-admin"),
        )
    assert exc_info.value.status_code == 400
    assert "own admin role" in exc_info.value.detail


def test_static_email_admin_cannot_be_demoted(admin_db):
    with admin_db() as db:
        db.add(User(clerk_id="email-admin", email="email-admin@example.com"))
        db.add(UserAdminControl(user_id="email-admin", status="active", role="admin"))
        db.commit()

    with pytest.raises(HTTPException) as exc_info:
        admin_router.update_user_role(
            "email-admin",
            admin_router.UserRoleUpdate(role="user"),
            admin=AdminPrincipal(user_id="another-admin"),
        )
    assert exc_info.value.status_code == 400
    assert "env config" in exc_info.value.detail

