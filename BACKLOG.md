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
**Status:** ✅ Complete — DB-backed leased turn workers

Turns now persist their full request and are claimed by a bounded worker pool using
renewable database leases. Expired leases are reclaimed after deploys/crashes, retries
are capped, cancellation is persisted, and lease-owner fencing prevents a stale worker
from overwriting a newer attempt.

Current recovery restarts the turn from its beginning. Stage-level checkpoint/resume can
be added later if real workloads show that replay cost is material.

---

### TD-03 · Artifact binary data stored as base64 in PostgreSQL TEXT
**File:** `apps/api/app/db/models.py` (`Artifact.base64_data`), `apps/api/app/services/agent/persistence.py`  
**Effort:** Medium  
**Status:** ✅ Complete — local/S3 blob-store abstraction and presigned downloads

New artifacts are stored through a backend-qualified blob location, with local storage
for development and private S3-compatible storage for production. Database rows retain
metadata and object keys only; historical turn payloads no longer re-embed stored files.
Authenticated downloads redirect to short-lived presigned URLs for S3 objects. Legacy
absolute-path and base64 rows remain readable, and
`python -m app.services.artifact_migration` migrates them to the configured backend.

---

### TD-04 · Triple-track schema management (Alembic + create_all + _ensure_sqlite_schema)
**File:** `apps/api/app/db/models.py` (`init_db`, `_ensure_sqlite_schema`)  
**Effort:** Low  
**Status:** ✅ Complete — Alembic is the only application schema-management path

Application startup no longer calls `create_all`, runs manual SQLite DDL, or repairs
missing columns. Every database, including local SQLite, must be Alembic-stamped at the
code migration head. Startup and the internal smoke endpoint fail with an explicit
`alembic upgrade head` instruction when the database is blank or stale.

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
**Status:** ✅ Complete — focused hooks and frontend unit test foundation

`useAgent.ts` is now a 222-line composition layer. Workspace/conversation CRUD and
selection live in `useWorkspaces.ts`; SSE, reconnection, polling fallback, progress,
terminal state, and run execution live in `useTurnRunner.ts`. Templates, attachments,
and profile settings remain in their previously extracted hooks.

Vitest + React Testing Library now cover fragmented SSE parsing, multiline frames,
stream completion, and optimistic workspace creation. Playwright remains the browser
workflow layer.

---

### TD-07 · Turn status delivered by polling at 1.2s — not push-based
**File:** `apps/web/app/hooks/useAgent.ts` (`pollTurnStatus`), `apps/api/app/routers/agent.py`  
**Effort:** Medium  
**Status:** ✅ Complete — authenticated replayable SSE with polling fallback

The background-job flow now streams persisted progress through
`GET /turns/{turn_id}/stream`. Event IDs support resume via `Last-Event-ID`, heartbeats
keep proxy connections alive, terminal snapshots carry the completed/failed/cancelled
turn, and the browser deduplicates replayed events. After repeated stream failures the
client automatically returns to the existing polling recovery path.

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
**Status:** ✅ Complete — structured worker telemetry, optional Sentry, admin job monitor

Production can emit JSON logs with correlated `turn_id`, `user_id`, worker, attempt,
outcome, and exception fields. Sentry is enabled when `SENTRY_DSN` is configured, with
PII disabled and adjustable trace sampling. The admin Jobs tab exposes queue depth,
worker liveness, retries, stale leases, failures, recent turn details, and audited
cancellation. A dedicated Prometheus exporter remains optional if an external metrics
collector is introduced later.

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
**Status:** ✅ Complete — authenticated presigned URL redirects for S3-compatible storage

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
| TD-04 ✅ | Remove startup `create_all` and SQLite schema repair; enforce Alembic head in every environment | `apps/api/app/db/models.py`, `apps/api/app/db/schema_check.py` |
| TD-14 (partial) | Add `RequireAdmin` FastAPI dependency; annotate intentional env-only call sites | `apps/api/app/auth.py`, `apps/api/app/routers/admin.py` |
| TD-01 (partial) | Extract all Pydantic models → `research_models.py` (−623 lines); backward-compat re-exports preserved | `research_models.py` (new), `research_subtree.py` |
| TD-01 (partial) | Extract utilities → `research_utils.py`, profile/brief → `research_profiles.py`, contracts → `research_contracts.py`; planner → `research_planner.py` | `research_utils.py`, `research_profiles.py`, `research_contracts.py`, `research_planner.py` (all new) |
| TD-01 ✅ | Extract `research_evidence.py`, `research_synthesis.py`, `research_lead.py`; `research_subtree.py` is now a 226-line re-export hub (−95% from 4915 lines); all 69 .py files compile clean | `research_evidence.py`, `research_synthesis.py`, `research_lead.py` (new); `research_subtree.py` |
| TD-06 (partial) | Extract `useTemplates`, `useAttachment`, `useProfileSettings` hooks from `useAgent.ts`; `useAgent.ts` composes them via spread; TypeScript + py_compile both pass clean | `useTemplates.ts` (new), `useAttachment.ts` (new), `useProfileSettings.ts` (new), `useAgent.ts` |
