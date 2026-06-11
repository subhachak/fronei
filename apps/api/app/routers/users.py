"""Current-user session bootstrap endpoint."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

from app.auth import CurrentUserPayload, get_claim_email, get_claim_name, is_admin_user
from app.config import get_settings
from app.db.models import SessionLocal, UserAdminControl, get_or_create_user, get_user_control
from app.services.notifications import notify_new_signup

router = APIRouter(tags=["users"])


@router.get("/me")
def me(payload: dict = CurrentUserPayload) -> dict:
    """Upserts the local user profile on every authenticated session bootstrap,
    so a User record exists from first login — even before the first chat.

    On the very first sign-in for a non-admin user, this also creates a
    UserAdminControl row with status="pending" (when REQUIRE_USER_APPROVAL is
    enabled) and notifies admins, so new accounts stay locked out of chat
    until an admin activates them.
    """
    settings = get_settings()
    user_id = str(payload.get("sub") or "")
    email = get_claim_email(payload)
    name = get_claim_name(payload)
    db = SessionLocal()
    try:
        user, created = get_or_create_user(db, user_id, email=email, name=name)
        if created and settings.require_user_approval and not is_admin_user(user_id, email):
            now = datetime.now(timezone.utc)
            db.add(UserAdminControl(
                user_id=user_id,
                status="pending",
                role="user",
                created_at=now,
                updated_at=now,
            ))
            db.commit()
            notify_new_signup(user_id, email, name)

        control = get_user_control(db, user_id)
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
