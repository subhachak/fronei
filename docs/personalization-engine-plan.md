# Fronei Personalization / Memory Engine — Implementation Plan for Codex

This document is a hand-off spec for implementing the personal context engine
described in design discussion. Each phase is scoped to be a self-contained
PR. Follow existing patterns in the codebase (idempotent Alembic migrations
via `app/db/migration_helpers.py`, `SessionLocal()` try/finally, silent
background-task failure for extraction).

Verification after every phase:
- `cd apps/api && python3 -m py_compile $(git diff --name-only -- '*.py')`
- `cd apps/api && uv run alembic upgrade head` against a scratch/local DB
- `cd apps/web && npx tsc --noEmit -p tsconfig.json`

---

## Phase 1 — Memory schema upgrade + classification-based extraction + ranked retrieval

### 1.1 Migration

New Alembic revision (`apps/api/alembic/versions/<rev>_enrich_user_memories.py`),
following the idempotent pattern (`column_exists` guards) used in
`ad98597a4589_add_auth_users_table_and_user_id_columns.py`.

Add columns to `user_memories`:

| column | type | default | notes |
|---|---|---|---|
| `scope` | String(32) | `'global'` | one of: global, work, project, style, personal |
| `confidence` | Float | `0.6` | 0–1 |
| `source` | String(16) | `'stated'` | stated \| inferred \| confirmed |
| `seen_count` | Integer | `1` | incremented on reinforcement |
| `last_seen_at` | DateTime | `now()` | distinct from `updated_at` (content edits) |
| `importance` | Float | `0.5` | 0–1, drives ranking |
| `decay_rate` | Float | `0.05` | per-day decay applied to recency_weight |
| `pinned` | Boolean | `false` | pinned memories never auto-superseded/decayed |
| `status` | String(16) | `'active'` | active \| superseded \| archived |
| `superseded_by_id` | Integer, nullable | `null` | FK-ish ref to another `user_memories.id` |

Index: `(user_id, status)` for fast active-memory lookups.

### 1.2 `app/db/models.py`

- Add the new columns to `UserMemory`.
- Add `DEFAULT_DECAY_RATES: dict[str, float]` mapping `category` → decay rate
  (per design: communication style ~0.01, bio/work identity ~0.005, active
  projects ~0.08, preferences ~0.03, temporary plans ~0.15). Extractor and
  ranker both import this.
- Replace `get_all_memories(db, user_id) -> str` — keep it for backwards
  compat but mark deprecated; new code uses `personal_context.py` (Phase 1.5).

### 1.3 `app/services/memory_extractor.py` — classification upgrade

Current behavior: every turn, ask Gemini Flash for a list of new facts and
blind-insert them.

New behavior:
1. Fetch the user's current **active** memories (status='active'), grouped
   by category — pass a compact summary (content + id only) to the LLM as
   context.
2. Update `_PROMPT` to ask the model to return, per candidate fact, one of:
   ```json
   [
     {"action": "new", "content": "...", "category": "...", "scope": "...", "source": "stated|inferred", "confidence": 0.0-1.0, "importance": 0.0-1.0},
     {"action": "reinforce", "memory_id": 123},
     {"action": "update", "memory_id": 123, "content": "new text", "confidence": 0.0-1.0},
     {"action": "contradict", "memory_id": 123, "content": "new text", "confidence": 0.0-1.0},
     {"action": "ignore"}
   ]
   ```
3. Apply actions:
   - `new` → insert row with `source`, `confidence`, `importance`,
     `decay_rate` (from `DEFAULT_DECAY_RATES[category]`), `last_seen_at=now`.
   - `reinforce` → `seen_count += 1`, `last_seen_at = now`,
     `importance = min(1.0, importance + 0.05)`, `confidence = min(1.0, confidence + 0.05)`.
   - `update` → update `content`, bump `last_seen_at`, `seen_count += 1`.
     If the memory is `pinned`, skip silently (pinned = user-confirmed,
     don't auto-overwrite — log for the nightly consolidator instead, see
     Phase 3).
   - `contradict` → if target memory `pinned`, skip. Otherwise: insert the
     new fact as `status='active', source='inferred'`, and set the old
     memory's `status='superseded'`, `superseded_by_id=<new.id>`.
   - `ignore` → no-op.
4. Keep the whole thing inside the existing `try/except: pass` silent-failure
   wrapper and `ThreadPoolExecutor`.

### 1.4 `app/services/memory_ranker.py` (new)

```python
def score(memory: UserMemory, now: datetime, turn_category_hint: str | None = None) -> float:
    age_days = (now - memory.last_seen_at).total_seconds() / 86400
    recency_weight = math.exp(-memory.decay_rate * age_days)
    repetition = min(1.0, memory.seen_count / 10)
    pin_bonus = 0.5 if memory.pinned else 0.0
    relevance_bonus = 0.2 if turn_category_hint and memory.category == turn_category_hint else 0.0
    return (
        memory.importance
        + memory.confidence
        + repetition
        + recency_weight
        + pin_bonus
        + relevance_bonus
    )

def rank_memories(memories: list[UserMemory], now: datetime, turn_category_hint: str | None, limit: int) -> list[UserMemory]:
    active = [m for m in memories if m.status == "active"]
    return sorted(active, key=lambda m: score(m, now, turn_category_hint), reverse=True)[:limit]
```

`turn_category_hint` — derive from the planner's existing `turn_type`/`intent`
classification (e.g. map turn_type → memory category: "coding" → "work",
"writing" → "style", etc.) — pass-through, no new LLM call.

### 1.5 `app/services/personal_context.py` (new)

```python
def build_context(db, user_id: str, turn_category_hint: str | None = None, limit: int = 12) -> str:
    """Return a compact, ranked block of user memories for prompt injection.
    Replaces get_all_memories(). Token-budgeted (~limit items, short lines)."""
    memories = db.query(UserMemory).filter(
        UserMemory.user_id == user_id,
        UserMemory.status == "active",
    ).all()
    ranked = rank_memories(memories, datetime.now(timezone.utc), turn_category_hint, limit)
    if not ranked:
        return ""
    lines = []
    for m in ranked:
        tag = f"[{m.category}/{m.scope}]"
        conf = "" if m.confidence >= 0.8 else " (uncertain)"
        lines.append(f"- {tag} {m.content}{conf}")
    return "\n".join(lines)
```

Phase 2 will extend this to prepend the `UserProfile` brief.

### 1.6 Wire into the pipeline

- `app/routers/conversations.py`: replace the 4 call sites of
  `get_all_memories(db, user_id)` with
  `personal_context.build_context(db, user_id, turn_category_hint=<derived from plan if available>)`.
  Note: at the call sites before planning, `turn_category_hint` may not be
  known yet — pass `None` for the first retrieval; OK to keep it simple in
  Phase 1 (hint is a nice-to-have, not required for correctness).
- `app/services/planner.py` line ~173-174: no change needed — it already
  just receives `user_memory: str` and inserts it into the prompt.

### 1.7 `app/routers/memory.py` + `app/schemas.py`

- `MemoryItem`: add `scope`, `confidence`, `source`, `seen_count`,
  `last_seen_at`, `importance`, `pinned`, `status`.
- New endpoint: `PATCH /memory/{id}` — body `{pinned?: bool, content?: str,
  category?: str}` — for user edits/pin from the UI (Phase 4).
- `list_memories`: only return `status != 'archived'` by default; add
  `?include_superseded=true` for the dev/admin "why this context" view.

---

## Phase 2 — `UserProfile` table + nightly consolidation

### 2.1 Migration

New table `user_profiles`:

| column | type |
|---|---|
| `id` | Integer PK |
| `user_id` | String(128), unique, indexed |
| `profile_json` | Text (JSON: bio, role, company, location, active_projects, key_preferences, constraints, communication_style) |
| `last_consolidated_at` | DateTime, nullable |
| `created_at` / `updated_at` | DateTime |

### 2.2 `app/services/memory_consolidator.py` (new)

`consolidate_user(user_id: str)`:
1. Load active `UserMemory` rows + existing `UserProfile.profile_json` (or `{}`).
2. LLM pass (same model tier as extractor): given current profile JSON +
   all active memories (grouped by category, with `confidence`/`seen_count`/
   `last_seen_at`), produce an updated profile JSON. Prompt rules:
   - Recency + confirmation wins on conflicts.
   - `pinned` memories are authoritative — never contradicted by the LLM's
     output.
   - Output must be valid JSON matching the `UserProfile` shape.
3. Decay pass (pure Python, no LLM): for each active memory where
   `status='active' and not pinned`, compute `recency_weight` (Phase 1.4
   formula). If `recency_weight < 0.05` and `confidence < 0.5`, set
   `status='archived'`.
4. Write `UserProfile.profile_json`, `last_consolidated_at = now`.

`consolidate_all_active_users(since: timedelta = 24h)`:
- Query distinct `user_id` from `UserMemory` / `Conversation` with activity
  in the window, call `consolidate_user` for each. Wrap each user in
  try/except so one failure doesn't abort the batch.

### 2.3 Trigger mechanism

Add an internal endpoint, not a long-running in-process scheduler (matches
existing Railway Pre-Deploy-Command-style externalized scheduling):

`app/routers/internal.py` (new):
```python
@router.post("/internal/consolidate-profiles")
def consolidate_profiles(x_internal_secret: str = Header(...)) -> dict:
    if x_internal_secret != settings.internal_task_secret:
        raise HTTPException(403)
    consolidate_all_active_users()
    return {"status": "ok"}
```

> Superseded implementation note: this endpoint now idempotently enqueues a
> leased `maintenance_jobs` row and returns HTTP 202. The maintenance worker
> executes profile consolidation with renewable leases and retries; the
> scheduler polls `/internal/maintenance-jobs/{job_id}` rather than holding the
> consolidation request open.
- New `Settings.internal_task_secret: str = ""` in `config.py`.
- Document in `railway.toml`: set up a Railway Cron Job (or external
  cron — e.g. GitHub Actions scheduled workflow, or cron-job.org) hitting
  this endpoint nightly with the secret header.

### 2.4 `personal_context.build_context` extension

Prepend a compact `UserProfile` brief (bio/role/communication_style —
~100-150 tokens) before the ranked memory list, read from `UserProfile.profile_json`.

---

## Phase 3 — Merge `TwinProfile` into `UserProfile.communication_style`

- `memory_consolidator.py`'s LLM pass also takes `TwinProfile.fingerprint_json`
  / `prefs_json` / recent `WritingSample` rows as input, folding the result
  into `UserProfile.profile_json["communication_style"]`.
- `app/routers/twin_profile.py`: `get_profile` reads `communication_style`
  from `UserProfile` if present, falling back to the legacy `TwinProfile`
  row (for users not yet consolidated). Keep `TwinProfile` table for writing
  samples storage (`WritingSample` FK) — only the *derived* fingerprint/prefs
  move to `UserProfile`.
- `add_sample` / `delete_sample`'s `_trigger_extraction` background task can
  now also call `consolidate_user(user_id)` directly (on-demand path,
  independent of the nightly batch) since it's already a rate-limited,
  user-triggered action.

---

## Phase 4 — Personalization settings UI

In `apps/web/app/page.tsx`, extend the existing `'memory'` settings tab
(currently ~line 1748) into a richer "Personalization" view with sub-sections:

1. **Profile summary** — read-only cards for bio/role/company,
   communication style, active projects, key preferences — sourced from
   `GET /personal-context/profile` (new endpoint returning `UserProfile.profile_json`).
2. **What Fronei remembers** — existing memory list, enhanced:
   - Show `category`/`scope` badges, `confidence` indicator, `pinned` star.
   - Actions: pin/unpin (`PATCH /memory/{id}`), edit content, delete
     (existing `DELETE /memory/{id}`), "mark as wrong" (sets `confidence=0,
     status='archived'`).
   - Filter by category/scope; toggle to show archived/superseded
     ("why this context" / dev view).
3. **Writing style** — existing voice/sample UI, now also displays the
   derived `communication_style` JSON from the profile.

Backend additions for this phase:
- `GET /personal-context/profile` (new router `personal_context.py` or add
  to `memory.py`) → `UserProfile.profile_json` for `CurrentUser`.
- `PATCH /personal-context/profile` → allow direct user edits to profile
  fields (these edits should themselves be `pinned`-equivalent — store an
  `overrides` sub-object in `profile_json` that the consolidator never
  overwrites).

---

## Open implementation notes for Codex

- All new LLM calls should reuse `_MODEL = "gemini/gemini-2.5-flash"` (cost)
  unless profile consolidation proves to need a stronger model for JSON
  reliability — if so, gate behind a config flag rather than hardcoding.
- Every new background/LLM path must follow the existing silent-failure
  convention (`try/except: pass` or logged warning) — personalization must
  never break or slow down the chat response path.
- Privacy: extend `privacy-delete` (admin.py / users router) to also delete
  `UserProfile` rows when a user requests data deletion.
- Add unit tests for `memory_ranker.score` (pure function, easy to test) and
  for the extractor's action-application logic (new/reinforce/update/contradict)
  using a scratch SQLite DB.
