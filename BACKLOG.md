# Fronei – Tech Debt Backlog

Generated from codebase audit · June 2026  
Severity: 🔴 High · 🟡 Medium · 🟢 Low

---

## 🔴 High Priority

### TD-01 · research_subtree.py is a 4,915-line god file
**File:** `apps/api/app/services/agent/research_subtree.py`  
**Effort:** Medium (incremental, safe to split across PRs)  
**Status:** ✅ Complete — 8 focused modules extracted; `research_subtree.py` is now a 226-line re-export hub (down from 4915, −95%)

Single module handles: research planning, parallel URL extraction, claim verification,
gap analysis, evidence binding, synthesis, repair loops, judge scoring, deep link
ranking, and a budget ledger. Any bug is hard to isolate; tests cover only the top-level
entry points.

**Target decomposition — COMPLETE:**
- `research_models.py` — Pydantic models ✅ done (714 lines)
- `research_utils.py` — pure utility functions ✅ done (171 lines)
- `research_profiles.py` — profile policies + brief generation ✅ done (638 lines)
- `research_contracts.py` — coverage contracts ✅ done (319 lines)
- `research_planner.py` — planning, reflection, judge helpers ✅ done (1026 lines)
- `research_evidence.py` — `bind_evidence`, claims, architecture cards ✅ done (677 lines)
- `research_synthesis.py` — `synthesize_answer`, ranking, deep links ✅ done (572 lines)
- `research_lead.py` — `LeadResearchAgent` + `lead_research_loop` ✅ done (1112 lines)
- `research_subtree.py` — thin re-export hub ✅ done (226 lines, down from 4915)

---

### TD-02 · Background thread-per-turn with no durable queue
**File:** `apps/api/app/routers/agent.py` (lines 137–145)  
**Effort:** Medium-High  
**Status:** Open

Each turn spawns `Thread(daemon=True)`. Daemon threads are killed on process exit —
in-flight research or document generation is silently lost on every Railway deploy or
crash. No thread pool cap means unbounded concurrency under load.

**Fix:** Replace with a durable task queue. Options in order of effort:
1. DB-backed polling worker (reuse existing `Turn` table with status column) — lowest infra lift
2. ARQ (asyncio + Redis) — production-grade, Railway supports Redis
3. Celery + Redis — standard but heavier

Until fixed, set `RESTART_POLICY=never` on Railway to avoid mid-turn restarts.

---

### TD-03 · Artifact binary data stored as base64 in PostgreSQL TEXT
**File:** `apps/api/app/db/models.py` (`Artifact.base64_data`), `apps/api/app/services/agent/persistence.py`  
**Effort:** Medium  
**Status:** Open

PPTX/DOCX files stored as base64 text in the DB bloats the database, inflates query
results, and forces all artifact bytes through the API process on download. The
`storage_path` column already exists — it just needs to point at an object store
instead of local disk (which doesn't survive Railway redeploys without a volume).

**Fix:**
1. Add S3/R2/GCS client behind a `blob_store.py` abstraction
2. On artifact write: upload to blob, store presigned URL or object key in `storage_path`, leave `base64_data` empty
3. On artifact read: generate a short-lived presigned URL; redirect instead of streaming bytes through the API
4. Migration: backfill existing rows

---

### TD-04 · Triple-track schema management (Alembic + create_all + _ensure_sqlite_schema)
**File:** `apps/api/app/db/models.py` (`init_db`, `_ensure_sqlite_schema`)  
**Effort:** Low  
**Status:** Partially fixed — `init_db` now skips `create_all` in production; `_ensure_sqlite_schema` documented as dev-only

Three mechanisms run on startup:
- `Base.metadata.create_all()` — additive, ignores existing columns
- `_ensure_sqlite_schema()` — 100+ lines of manual `ALTER TABLE` statements
- Alembic migrations (41 and counting) — the canonical tool

**Remaining work:** Once all local dev DBs have been migrated via `alembic upgrade head`,
delete `_ensure_sqlite_schema` entirely. Commit that deletion as a named PR so it's
clearly intentional.

---

## 🟡 Medium Priority

### TD-05 · In-memory rate limiter and circuit breaker — not distributed
**File:** `apps/api/app/services/rate_limit.py`, `apps/api/app/services/llm_gateway.py`  
**Effort:** Low (interface is clean, swap is drop-in)  
**Status:** Open; acceptable until second Railway instance is added

`rate_limit.py` is explicitly commented "per-process only." Circuit breaker
(`_circuit_state` dict) is also in-memory. On a second instance, users can bypass rate
limits by hitting different workers; a provider outage detected by one worker won't
protect requests to another.

**Fix:** Add Redis (Railway has a Redis plugin). Replace `_hits` deque with a Redis
sliding-window counter using `ZADD`/`ZREMRANGEBYSCORE`. Replace circuit breaker state
with a Redis hash.

---

### TD-06 · useAgent.ts is a ~700-line god hook with 40+ state variables
**File:** `apps/web/app/hooks/useAgent.ts`  
**Effort:** Medium  
**Status:** In progress — `useTemplates`, `useAttachment`, `useProfileSettings` extracted

Handles: auth, workspaces CRUD, conversations CRUD, turn polling, file attachments,
templates, profile settings, admin flag, model override, clipboard, artifact download.
No unit tests (frontend is Playwright e2e only).

**Remaining work:**
- Extract `useWorkspaces.ts` (workspace + conversation CRUD, ~200 lines)
- Extract `useTurnRunner.ts` (turn lifecycle, polling, ~150 lines)
- Add Vitest + React Testing Library for unit tests

---

### TD-07 · Turn status delivered by polling at 1.2s — not push-based
**File:** `apps/web/app/hooks/useAgent.ts` (`pollTurnStatus`), `apps/api/app/routers/agent.py`  
**Effort:** Medium  
**Status:** Open; SSE producer pattern already exists in agent router

A 5-minute deep research run generates ~250 poll requests per client. The SSE streaming
endpoint and queue/producer pattern already exist in `agent.py`; the primary flow just
hasn't been migrated to use them.

**Fix:** Wire `GET /turns/{turn_id}/stream` (SSE) as the primary delivery path for the
background-job turn mode. Fall back to polling when SSE is blocked (corporate proxies).

---

### TD-08 · All JSON stored as TEXT columns — not JSONB on Postgres
**File:** `apps/api/app/db/models.py` (~15 `_json` columns)  
**Effort:** Medium (Alembic migration per table)  
**Status:** Blocked on TD-09 (SQLite must be dropped as prod DB first)

`context_json`, `profile_json`, `sources_json`, `data_json`, `input_json`, `output_json`
etc. are stored as raw text with manual `json.loads/dumps`. On PostgreSQL, `JSONB` gives
native indexing, `@>` containment queries, and schema validation at the DB layer.

**Fix:** Drop SQLite as a dev target, migrate all `*_json` TEXT columns to JSONB via
Alembic. SQLAlchemy's `JSONB` type handles serialization automatically.

---

### TD-09 · Feature flag sprawl — 8 env-var booleans gating an in-flight migration
**File:** `apps/api/app/config.py` (lines 116–139)  
**Effort:** Low (clean up after migration decision)  
**Status:** Open; turn-graph migration is stalled at `turn_graph_enabled=False`

`turn_graph_enabled`, `turn_graph_authoritative`, `orchestrator_enabled`,
`turn_graph_debug_enabled`, `agentdeck_usage_stats_weighting_enabled`,
`agentdeck_vision_judge_enabled`, `agentdeck_warm_renderer_enabled`,
`seed_registry_on_startup` — all off by default in production.

**Fix:** Make a decision: complete the turn-graph migration or revert it. If completing:
- Enable flags one at a time in staging, validate, then cut to production
- Delete each flag + its guarded code path once the cutover is clean
If reverting: delete all turn-graph code and flags in a single PR.

---

### TD-10 · package.json pins core deps to `latest`
**File:** `apps/web/package.json`  
**Effort:** Trivial  
**Status:** Fixed — pinned to locked versions

---

## 🟢 Low Priority

### TD-11 · No observability layer (no APM, no structured logging)
**Effort:** Low (Sentry SDK is a one-liner; structlog is a near-drop-in)  
**Status:** Open

No Sentry, OpenTelemetry, Prometheus, or Datadog. For a multi-LLM agentic system,
provider failures silently degrade quality without alerting. DB-level cost tracking
exists but no real-time dashboard or alerting.

**Fix:**
1. `pip install sentry-sdk[fastapi]` → add `sentry_sdk.init()` in `main.py`
2. Replace `logging.getLogger` with `structlog` for structured JSON output
3. Add a Prometheus `/metrics` endpoint (optional, useful on Railway)

---

### TD-12 · All LLM calls are synchronous (no async I/O)
**File:** `apps/api/app/services/agent/model_client.py`, `apps/api/app/services/agent/tools.py`  
**Effort:** High (requires FastAPI async migration)  
**Status:** Open; not urgent while single-worker

All LiteLLM calls use synchronous `completion()`, not `acompletion()`. Research subtree
parallelizes via `ThreadPoolExecutor`. On a single worker this works; under load, blocked
threads are the bottleneck.

**Fix:** Migrate route handlers to `async def`, swap to `acompletion()` and `httpx.AsyncClient`.
This is a larger refactor — do it after TD-02 (durable queue) is resolved.

---

### TD-13 · No CDN or presigned URL pattern for artifact delivery
**Effort:** Low once TD-03 (blob storage) is done  
**Status:** Blocked on TD-03

Artifacts stream through the API process. With blob storage in place, replace the
download endpoint with a presigned URL redirect — zero bytes through the API.

---

### TD-14 · Admin role checked via two separate code paths
**File:** `apps/api/app/auth.py`, `apps/api/app/routers/admin.py`  
**Effort:** Low  
**Status:** Partially fixed — `RequireAdmin` dependency added to `auth.py`

`is_admin_user()` (env-only) and `is_admin_user_db()` (env + DB role) exist as separate
functions. Call sites must pick the right one; using the env-only path silently misses
DB-assigned admin roles.

**Remaining work:** Audit every `is_admin_user()` call site and replace with
`is_admin_user_db()` or the new `RequireAdmin` dependency.

---

## Completed

| ID | Summary | Files changed |
|----|---------|---------------|
| TD-10 | Pin package.json deps away from `latest` | `apps/web/package.json` |
| TD-04 (partial) | Guard `init_db` create_all behind `not is_production`; document Alembic as authoritative | `apps/api/app/db/models.py` |
| TD-14 (partial) | Add `RequireAdmin` FastAPI dependency; annotate intentional env-only call sites | `apps/api/app/auth.py`, `apps/api/app/routers/admin.py` |
| TD-01 (partial) | Extract all Pydantic models → `research_models.py` (−623 lines); backward-compat re-exports preserved | `research_models.py` (new), `research_subtree.py` |
| TD-01 (partial) | Extract utilities → `research_utils.py`, profile/brief → `research_profiles.py`, contracts → `research_contracts.py`; planner → `research_planner.py` | `research_utils.py`, `research_profiles.py`, `research_contracts.py`, `research_planner.py` (all new) |
| TD-01 ✅ | Extract `research_evidence.py`, `research_synthesis.py`, `research_lead.py`; `research_subtree.py` is now a 226-line re-export hub (−95% from 4915 lines); all 69 .py files compile clean | `research_evidence.py`, `research_synthesis.py`, `research_lead.py` (new); `research_subtree.py` |
| TD-06 (partial) | Extract `useTemplates`, `useAttachment`, `useProfileSettings` hooks from `useAgent.ts`; `useAgent.ts` composes them via spread; TypeScript + py_compile both pass clean | `useTemplates.ts` (new), `useAttachment.ts` (new), `useProfileSettings.ts` (new), `useAgent.ts` |
