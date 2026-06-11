"""Minimal Clerk Backend API client.

Used to look up a user's email/display name by clerk_id when the JWT
session token doesn't carry those claims (Clerk's default session token
only includes `sub`). Requires CLERK_SECRET_KEY (sk_...) from the Clerk
dashboard (API Keys). If unset, lookups are skipped and callers should
fall back to whatever is already stored locally.
"""
from __future__ import annotations

import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

CLERK_API_BASE = "https://api.clerk.com/v1"


def fetch_clerk_user(clerk_id: str) -> dict | None:
    """Fetch {email, name} for a clerk user id via the Clerk Backend API.

    Returns None if CLERK_SECRET_KEY is unset or the request fails."""
    settings = get_settings()
    if not settings.clerk_secret_key:
        return None
    try:
        resp = httpx.get(
            f"{CLERK_API_BASE}/users/{clerk_id}",
            headers={"Authorization": f"Bearer {settings.clerk_secret_key}"},
            timeout=5.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.warning("Clerk user lookup failed for %s", clerk_id, exc_info=True)
        return None

    email = None
    for addr in data.get("email_addresses") or []:
        if addr.get("id") == data.get("primary_email_address_id"):
            email = addr.get("email_address")
            break
    if not email and data.get("email_addresses"):
        email = data["email_addresses"][0].get("email_address")

    name = " ".join(filter(None, [data.get("first_name"), data.get("last_name")])).strip()
    if not name:
        name = data.get("username")

    return {"email": email, "name": name or None}
