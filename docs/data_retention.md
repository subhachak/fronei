# Data Retention and Deletion Audit

Status as of 2026-07-09. This traces the full lifecycle of an uploaded
document, extracted facts, and stored research artifacts, and answers: can a
user's data be fully deleted on request? Findings below come from reading the
actual storage/deletion code paths and running the test suite against them,
not from documentation claims.

## Short answer

**Yes, with two gaps that this task fixes and one gap that remains scoped but
unfixed.** A "delete my data" endpoint already existed
(`POST /profile/privacy-delete`, self-service; `POST
/admin/users/{user_id}/privacy-delete`, admin-triggered) before this audit.
It correctly deleted turns, workspaces/conversations, tool calls, events,
artifacts (DB row *and* the underlying blob/file), and document templates.
It was **missing** two stores that are keyed by `user_id` but not
FK-linked to turns: extracted facts (`known_facts`) and cross-session memory
summaries (`session_summaries`). Both are now included (see "Fixed in this
task" below). One further store (`langgraph_run_contexts`) is bounded by an
existing age-based cleanup job but not purged immediately on request — see
"Known gap, not fixed" below.

## 1. Uploaded document lifecycle

`POST /documents/extract` (`app/routers/documents.py` →
`app/services/document_extractor.py`) reads the uploaded file's bytes
in-memory, extracts text (via a vision model for PDF pages, native parsers
for everything else), and returns the extracted text in the HTTP response.
**The raw uploaded file is never written to disk or any persistent store** —
it exists only for the duration of that one request.

The client then includes the extracted text as `attachment_context` in the
next `POST /turns` request. Server-side (`app/routers/agent.py`), this gets
folded into `conversation_context` and ultimately persisted as part of that
turn's `Turn.objective`/context fields (`app/services/agent/models.py`
`TurnRequest.attachment_context`, capped at `ATTACHMENT_CONTEXT_MAX_CHARS`).

**Conclusion:** an uploaded document's content only persists as part of the
`Turn` row it was attached to. Deleting that turn (which privacy-delete
does) removes it. There is no separate raw-file store to worry about.

Document *templates* (user-uploaded pptx/docx branding templates, distinct
from one-off document uploads) are a different, persistent store
(`DocumentTemplate` table + a file on disk/S3 via
`template_path_for_row`) — both privacy-delete endpoints already delete the
DB row and unlink/remove the underlying file.

## 2. Extracted facts lifecycle (`fact_extractor.py` → `known_facts`)

`extract_and_store_facts()` (`app/services/agent/fact_extractor.py`) runs
after a research synthesis, asks a model to pull durable facts out of the
answer, and calls `upsert_fact()` (`app/services/agent/known_facts.py`) for
each one. These land in the `known_facts` table, keyed by `user_id` +
`entity_id` + `fact_key` (see `alembic/versions/f0b1c2d3e4f6_*`).

This table is **not** an ORM-mapped model (`app/db/models.py` has no
`KnownFact` class — it's managed entirely via raw SQL in `known_facts.py`)
and has **no foreign key to `turns`**. This meant it was invisible to both
privacy-delete endpoints, which only ever walked `Turn`/`Workspace` and their
FK-cascaded children.

**Fixed in this task:** added `delete_facts_for_user()` to `known_facts.py`
and wired it into both `/profile/privacy-delete` and
`/admin/users/{user_id}/privacy-delete` (gated behind the same `agent_data`
flag as turns/workspaces on the admin path). The admin dry-run preview
(`_privacy_counts`) now also reports a `known_facts` count.

## 3. Stored research artifacts lifecycle (`Artifact` + blob storage)

Generated docx/pptx/markdown artifacts are recorded in the `Artifact` table
and their content lives in the configured blob backend
(`app/services/blob_store.py` — local filesystem or S3, per
`ARTIFACT_STORAGE_BACKEND`). `persistence.delete_artifacts_for_turn_ids()`
already deletes both the DB row *and* calls `delete_blob_location()` on the
actual stored file/object — this was correctly implemented before this audit
and required no changes.

## 4. Cross-session memory (`session_memory.py` → `session_summaries`) — the second gap

Separately from `known_facts`, Fronei has an L2 "cross-session memory" layer:
`save_session_summary()` (`app/services/agent/session_memory.py`) embeds a
short summary of a completed conversation and inserts it into
`session_summaries` (Postgres only — this table stays empty on SQLite/local
dev, but the table itself is created regardless of dialect). Like
`known_facts`, this is a raw-SQL table with **no ORM model and no FK to
turns**, keyed by `user_id` directly, and was **not purged by either
privacy-delete endpoint**.

**Fixed in this task:** added `delete_session_summaries_for_user()` to
`session_memory.py`, wired into both endpoints the same way as
`known_facts`, with a matching dry-run count on the admin path.

## 5. Known gap, not fixed in this task: `langgraph_run_contexts`

`LangGraphRunContext` (`app/db/models.py`) stores the full serialized request
and tool config for every paused/resumed deep-research run
(`request_json`, `tool_config_json` — this can contain the user's message
and conversation context). It has **no `user_id` column** — correlating a
row back to a user requires joining through `Turn.langgraph_run_id`, which
neither privacy-delete endpoint currently does.

This is a real gap, but it is **bounded**, not unbounded like the other two
were: `cleanup_langgraph_checkpoints()` (`app/services/maintenance_jobs.py`,
covered by `tests/test_langgraph_maturity.py`) already deletes old completed
run contexts on a schedule. A user's data doesn't live here forever — it's
just not removed *immediately* on a privacy-delete request, and a paused run
close to the retention boundary could theoretically survive a few extra
hours/days past the deletion request.

**Recommended follow-up (not implemented here — deliberately scoped, not
executed, per this task's brief):** in both privacy-delete endpoints, before
deleting `Turn` rows, collect `langgraph_run_id` values from
`Turn.langgraph_run_id`-equivalent state (the run IDs referenced in
turn-level pause metadata) and delete the matching `LangGraphRunContext`
rows in the same transaction. This needs a bit of care because a live/active
run (status `running` or `resuming`) shouldn't be deleted out from under an
in-flight request — the safest version of this fix only touches rows already
`completed`/`failed`/`orphaned` for that user, which is exactly the state
`cleanup_langgraph_checkpoints` already targets on its own schedule.

## 6. Explicitly out of scope for individual deletion (by design, not oversight)

- **`AdminAuditLog`** — records that an admin action (including a
  privacy-delete request) happened. Retained deliberately: an audit trail of
  *"user X's data was deleted on date Y"* is the compliance evidence that
  deletion occurred, and erasing it alongside the deletion it documents would
  defeat that purpose. Standard practice for privacy-regulation compliance
  (e.g., GDPR's erasure right has a recognized carve-out for records needed
  to demonstrate compliance).
- **`RoutingSignalCandidate`** — a system-wide learned phrase→route mapping
  built from aggregated patterns across many users' message phrasings. It's
  not attributable to, or meaningfully "about," any single user in a way a
  deletion request is aimed at (comparable to a spam filter's trained
  vocabulary, not a user's personal data).
- **`RoutingDecisionFeedback`** — has `turn_id` as a real foreign key with
  `ondelete="CASCADE"` to `turns.id`, and the app enables
  `PRAGMA foreign_keys=ON` for SQLite connections (`app/db/models.py`) while
  Postgres enforces FKs by default. Deleting a user's `Turn` rows already
  cascades this table correctly — verified, not just assumed.
- **`EvalCase`/`EvalRun`** — Fronei's own internal quality-testing fixtures
  and results, not user conversational data.

## Summary table

| Store | Keyed by | FK to turns? | Covered before this task? | Covered now? |
|---|---|---|---|---|
| `turns`, `events`, `tool_calls` | `user_id` / `turn_id` | n/a (source) | Yes | Yes |
| `workspaces`, `conversations` | `user_id` | n/a (source) | Yes | Yes |
| `artifacts` (DB row + blob) | `turn_id` | Yes | Yes | Yes |
| `document_templates` (DB row + file) | `user_id` | No (direct) | Yes | Yes |
| `routing_decision_feedback` | `turn_id` | Yes, CASCADE | Yes (via DB cascade) | Yes |
| `known_facts` | `user_id` | No | **No** | **Yes (fixed)** |
| `session_summaries` | `user_id` | No | **No** | **Yes (fixed)** |
| `langgraph_run_contexts` | *(none — needs join)* | No | No | **No — documented gap, age-bounded by maintenance job** |
| `admin_audit_logs` | `user_id` (subject) | No | N/A | Intentionally retained |
| `routing_signal_candidates` | none (aggregate) | No | N/A | Not user-specific data |
