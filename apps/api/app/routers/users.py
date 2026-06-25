"""Current-user session bootstrap endpoint."""
from __future__ import annotations

from fastapi import APIRouter

from app.auth import CurrentUserPayload, get_claim_email, get_claim_name, is_env_admin
from app.config import get_settings
from app.db.models import SessionLocal, bootstrap_user_and_control

router = APIRouter(tags=["users"])


@router.get("/me")
def me(payload: dict = CurrentUserPayload) -> dict:
    """Upserts the local user profile on every authenticated session bootstrap,
    so a User record exists from first login — even before the first chat.

    On the very first sign-in for a non-admin user, this also creates a
    UserAdminControl row with status="pending" (when REQUIRE_USER_APPROVAL is
    enabled) and notifies admins, so new accounts stay locked out of chat
    until an admin activates them. This endpoint itself must stay reachable
    by pending/suspended accounts — the frontend relies on `account_status`
    here to show the right screen — actual access control for every other
    endpoint is enforced by the `CurrentActiveUser` dependency (app/auth.py),
    which runs this same bootstrap if a client reaches it without ever
    calling /me first.
    """
    settings = get_settings()
    user_id = str(payload.get("sub") or "")
    email = get_claim_email(payload)
    name = get_claim_name(payload)
    db = SessionLocal()
    try:
        user, control, _ = bootstrap_user_and_control(
            db, user_id, email, name,
            is_admin=is_env_admin(user_id, email),
            require_approval=settings.require_user_approval,
        )
        account_status = control.status if control else "active"
        return {
            "user_id": user.clerk_id,
            "email": user.email,
            "name": user.name,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
            "account_status": account_status,
        }
    finally:
        db.close()
