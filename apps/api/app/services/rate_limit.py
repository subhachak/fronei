"""Lightweight in-memory sliding-window rate limiting.

Per-process only — fine for a single-worker deployment. If the API is scaled to
multiple processes/instances, replace `_hits` with a shared store (e.g. Redis)
behind the same `check_rate_limit` interface.
"""

import threading
import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException

from app.auth import CurrentUser, CurrentUserIsAdmin
from app.config import get_settings

_lock = threading.Lock()
_hits: dict[str, deque] = defaultdict(deque)


def check_rate_limit(key: str, limit: int, window_seconds: int) -> None:
    """Raise HTTP 429 if `key` has exceeded `limit` events in `window_seconds`."""
    now = time.monotonic()
    cutoff = now - window_seconds
    with _lock:
        hits = _hits[key]
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= limit:
            retry_after = max(1, int(hits[0] + window_seconds - now))
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded ({limit} requests per {window_seconds}s). "
                       f"Try again in {retry_after}s.",
                headers={"Retry-After": str(retry_after)},
            )
        hits.append(now)


def rate_limiter(bucket: str, limit_setting: str, window_seconds: int):
    """FastAPI dependency factory: limits each user to N requests per
    `window_seconds` for the given `bucket` name, where N is read at request time
    from `Settings.<limit_setting>` (so admins can tune it via env without a
    restart-sensitive closure). Admins (by user ID or email, same allowlist as
    `require_admin`) are exempt."""

    def _dep(user_id: str = CurrentUser, is_admin: bool = CurrentUserIsAdmin) -> None:
        if is_admin:
            return
        settings = get_settings()
        limit = getattr(settings, limit_setting)
        check_rate_limit(f"{bucket}:{user_id}", limit, window_seconds)

    return Depends(_dep)
