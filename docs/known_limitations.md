# Known Limitations

Status as of 2026-07-09. Documented deliberately rather than left implicit,
so a future scale-up decision doesn't silently reintroduce a bug that was
already known and accepted.

## Rate limiting is in-memory and per-process

`app/services/rate_limit.py` implements a sliding-window rate limiter
(`check_rate_limit`) backed by a plain Python `dict`/`deque` guarded by a
`threading.Lock` — state lives in the process's memory, not in Redis or the
database.

**This is fine today because every documented deployment path runs a single
process/instance:**

- `render.yaml` / `infra/render.yaml` declare a single Render `type: web`
  service with no `numInstances` (Render defaults to 1 when omitted, and
  neither file overrides it).
- `railway.toml`'s deploy comments describe Railway's rolling-deploy overlap
  (the old instance keeps serving while a new one starts) — a transient
  two-instance window during deploys, not sustained horizontal scaling.
- No deployment config in this repo declares `replicas`, `scale`, or any
  other multi-instance setting.

**What breaks if this changes:** if Fronei is ever scaled to run more than
one API process/instance concurrently (multiple Render instances, a
multi-worker Gunicorn/Uvicorn setup, or a Kubernetes deployment with >1
replica), each process gets its **own independent counter**. A user hitting
different instances behind a load balancer could exceed the configured
limit by roughly a factor of (number of instances) before any single
instance's counter trips — the limiter would still function, just silently
weaker than configured, with no error or warning to indicate it.

**Fix when that becomes real:** replace the in-memory `_hits` dict in
`rate_limit.py` with a shared store (Redis is the natural fit — sliding-window
counters via `INCR`/`EXPIRE` or a sorted set) behind the same
`check_rate_limit(key, limit, window_seconds)` interface, so
`rate_limiter()`'s FastAPI dependency call sites don't need to change.

## Provider circuit breakers are also in-memory and per-process

`app/services/provider_health.py`'s `_circuit_state` (which providers are
currently in cooldown after repeated failures) has the identical
single-process caveat, for the identical reason (no multi-instance
deployment exists yet). Worth fixing at the same time as rate limiting if
Fronei ever moves to multiple instances, since both would otherwise give a
false sense of protection under load once split across processes.
