"""Current-user session bootstrap endpoint."""
from __future__ import annotations

from fastapi import APIRouter

from app.auth import CurrentUserPayload, get_claim_email, get_claim_name
from app.db.models import SessionLocal, get_or_create_user

router = APIRouter(tags=["users"])


@router.get("/me")
def me(payload: dict = CurrentUserPayload) -> dict:
    """Upserts the local user profile on every authenticated session bootstrap,
    so a User record exists from first login — even before the first chat."""
    user_id = str(payload.get("sub") or "")
    email = get_claim_email(payload)
    name = get_claim_name(payload)
    db = SessionLocal()
    try:
        user = get_or_create_user(db, user_id, email=email, name=name)
        return {
            "user_id": user.clerk_id,
            "email": user.email,
            "name": user.name,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        }
    finally:
        db.close()
