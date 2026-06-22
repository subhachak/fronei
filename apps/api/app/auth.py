from functools import lru_cache

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt
from jwt import PyJWKClient

from app.config import get_settings

security = HTTPBearer()


@lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient:
    settings = get_settings()
    return PyJWKClient(f"{settings.clerk_issuer}/.well-known/jwks.json")


def _decode_token(token: str) -> dict:
    try:
        signing_key = _jwks_client().get_signing_key_from_jwt(token)
        settings = get_settings()
        # verify_aud is disabled when clerk_audience is not configured; set
        # CLERK_AUDIENCE in env once the Clerk production app audience is known.
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            options={"verify_aud": bool(settings.clerk_audience)},
            audience=settings.clerk_audience or None,
        )
        if not payload.get("sub"):
            raise HTTPException(status_code=401, detail="Invalid token")
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def get_current_user_payload(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    return _decode_token(credentials.credentials)


def get_current_user_id(payload: dict = Depends(get_current_user_payload)) -> str:
    """Proves the bearer token is valid and returns the Clerk user id.

    This does NOT check admin-approval status. It exists only for the
    handful of endpoints that must remain reachable by pending/suspended
    accounts (GET /me, so the frontend can detect and display that status).
    Every other authenticated, resource-consuming endpoint must use
    `CurrentActiveUser` / `get_current_user_active_id` instead — see below.
    """
    return str(payload["sub"])


def get_current_active_user_id(user_id: str = Depends(get_current_user_id)) -> str:
    """The dependency every protected, resource-consuming endpoint should use.

    Like `get_current_user_id`, but additionally enforces the admin-approval
    gate server-side: suspended or pending accounts are rejected with 403
    before any handler logic runs, and a brand-new account that has never
    been seen before (no UserAdminControl row yet) is bootstrapped as
    "pending" and denied right here — not just on first login through GET
    /me. That closes the gap where a client could reach the API directly
    (skipping the web frontend's /me bootstrap call entirely) and get
    unrestricted access because no control row existed yet to deny it.

    Admins (env allowlist or DB-assigned admin role) are always exempt.

    Deliberately depends on `get_current_user_id` (not the raw payload)
    rather than re-decoding the token itself, for two reasons: it avoids a
    second JWKS round trip (FastAPI caches the shared payload dependency
    once per request regardless), and it means
    `app.dependency_overrides[get_current_user_id] = ...` in tests fully
    short-circuits this dependency too, exactly like every other endpoint —
    no test needs to know this gate exists unless it's specifically testing
    approval/suspension behavior.
    """
    if is_admin_user(user_id, None):
        return user_id
    settings = get_settings()
    if not settings.require_user_approval:
        return user_id
    from app.db.models import SessionLocal, User, bootstrap_user_and_control, get_user_control  # local import: avoid import cycle
    db = SessionLocal()
    try:
        control = get_user_control(db, user_id)
        if control is not None and control.role == "admin":
            return user_id
        if control is None:
            # No control row yet — this account has never been seen by the
            # gate before. Look up any email/name already on file (e.g. from
            # a prior /me call) purely so the admin notification is useful;
            # the bootstrap and the deny below happen either way.
            row = db.query(User).filter(User.clerk_id == user_id).first()
            email = row.email if row else None
            name = row.name if row else None
            _, control, _ = bootstrap_user_and_control(
                db, user_id, email, name, is_admin=False, require_approval=True,
            )
        if control is not None and control.status == "suspended":
            raise HTTPException(status_code=403, detail="This account is suspended.")
        if control is not None and control.status == "pending":
            raise HTTPException(status_code=403, detail="Your account is pending admin approval.")
        return user_id
    finally:
        db.close()


def get_claim_email(payload: dict) -> str | None:
    """Extract a lowercased email claim from a Clerk JWT payload, if present."""
    for key in ["email", "email_address", "primary_email_address"]:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value.lower()
    claims = payload.get("claims")
    if isinstance(claims, dict):
        value = claims.get("email")
        if isinstance(value, str) and value:
            return value.lower()
    return None


def get_claim_name(payload: dict) -> str | None:
    """Extract a display name claim from a Clerk JWT payload, if present."""
    for key in ["name", "full_name", "username"]:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    first = payload.get("first_name") or payload.get("given_name")
    last = payload.get("last_name") or payload.get("family_name")
    combined = " ".join(p for p in [first, last] if isinstance(p, str) and p.strip())
    if combined:
        return combined
    claims = payload.get("claims")
    if isinstance(claims, dict):
        for key in ["name", "full_name", "username"]:
            value = claims.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def is_admin_user(user_id: str, email: str | None) -> bool:
    """Env-allowlist admin check (ID or email). This is the static, deploy-time
    allowlist; see `is_admin_user_db` for the combined env + DB-role check."""
    settings = get_settings()
    return (
        user_id in settings.admin_id_set
        or (email is not None and email in settings.admin_email_set)
    )


def is_admin_user_db(user_id: str, email: str | None) -> bool:
    """Single source of truth for admin access: env allowlist OR a DB-assigned
    'admin' role on the user's UserAdminControl row. The DB role lets admins
    grant/revoke admin access for other users at runtime via the admin UI."""
    if is_admin_user(user_id, email):
        return True
    from app.db.models import SessionLocal, UserAdminControl  # local import: avoid import cycle
    db = SessionLocal()
    try:
        control = db.query(UserAdminControl).filter(UserAdminControl.user_id == user_id).first()
        return bool(control and control.role == "admin")
    finally:
        db.close()


def get_current_user_is_admin(payload: dict = Depends(get_current_user_payload)) -> bool:
    return is_admin_user_db(str(payload.get("sub") or ""), get_claim_email(payload))


CurrentUser = Depends(get_current_user_id)
CurrentActiveUser = Depends(get_current_active_user_id)
CurrentUserPayload = Depends(get_current_user_payload)
CurrentUserIsAdmin = Depends(get_current_user_is_admin)
