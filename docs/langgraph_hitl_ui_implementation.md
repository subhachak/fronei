# LangGraph Human-in-the-Loop UI — Implementation Guide

**Scope:** wire the existing backend pause/resume capability (`interrupt()`/`Command(resume=...)`, `/admin/langgraph/runs/{run_id}/pause`, `/admin/langgraph/runs/{run_id}/approve`) into both the chat UI (the user who triggered the run) and the admin panel (oversight across all runs).

**Confirmed current state (read directly from the code, not assumed):** the backend pause/approve capability is real and tested, but **nothing downstream of it is wired**. Specifically:

- `TurnResult.turn_status` (`apps/api/app/services/agent/models.py`) already exists as a free-form string field, but `persistence.py::complete_turn` **hardcodes `Turn.status: "completed"`** (line ~1234) regardless of what `turn_status` says — so today a paused LangGraph run is persisted and reported to the frontend as an ordinary completed turn with an empty answer and a failed judge result. There is no distinction anywhere in the stack.
- `Turn` (the DB model backing chat turns) has no column linking it to a LangGraph `run_id` — there's no way today to go from a chat turn the user sees to the LangGraph run that needs approval, or back.
- `resume_langgraph_research()` (called by the existing `/approve` endpoint) returns a result dict directly from the HTTP handler — it never touches the `Turn` row or the turn-events pipeline. Even if you approved a run today via curl, the original chat turn would stay stuck exactly as it was.
- `useTurnRunner.ts::applyTerminalStatus` only branches on `'completed' | 'failed' | 'cancelled'`; anything else returns `false` ("not done yet, keep polling"), which means a paused turn would currently just poll forever with no visible change.
- No frontend code anywhere references pause/approval/budget/langgraph admin endpoints. No admin tab for it either — the existing tabs are Overview, Jobs, Users, Model policy, Usage, System, Evals.

So this isn't "add a button" — it's closing a real gap in the turn lifecycle first, then building UI on top of a foundation that doesn't fully exist yet. The work is bigger than it looks from the outside; treat it as three layered pieces, in this order.

**Design default assumed below (flag if you want it different):** approval stays admin-gated, matching the existing `/approve` endpoint's `is_admin` check. The chat-UI card shows the paused state to whoever triggered the run; the "Approve and continue" action only renders if the viewer is an admin. Non-admin users see a read-only "waiting for approval" state. If you want regular users to self-approve (e.g. up to some budget ceiling), that's a deliberate policy change to the `/approve` endpoint's auth check, not a UI-only decision — say so and I'll fold it in.

---

## Part 1 — Backend: make "paused" a real turn status

### 1.1 Add `langgraph_run_id` and `pause_reason` columns to `Turn`

`apps/api/app/db/models.py`, in the `Turn` class (~line 248), add two columns:

```python
    langgraph_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    pause_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
```

New Alembic migration (follow the style of `d926d728ff35_add_langgraph_run_context_resume_guard.py` — `table_exists`/`column_exists` guards, `batch_alter_table`):

```python
"""add langgraph_run_id/pause_reason to turns

Revision ID: <new>
Revises: d926d728ff35
Create Date: 2026-07-XX
"""
from alembic import op
import sqlalchemy as sa
from app.db.migration_helpers import column_exists, table_exists

revision = "<new>"
down_revision = "d926d728ff35"

def upgrade() -> None:
    if not table_exists("turns"):
        return
    with op.batch_alter_table("turns", schema=None) as batch_op:
        if not column_exists("turns", "langgraph_run_id"):
            batch_op.add_column(sa.Column("langgraph_run_id", sa.String(length=64), nullable=True))
        if not column_exists("turns", "pause_reason"):
            batch_op.add_column(sa.Column("pause_reason", sa.Text(), nullable=True))
    op.create_index("ix_turns_langgraph_run_id", "turns", ["langgraph_run_id"], unique=False)

def downgrade() -> None:
    op.drop_index("ix_turns_langgraph_run_id", table_name="turns")
    with op.batch_alter_table("turns", schema=None) as batch_op:
        if column_exists("turns", "pause_reason"):
            batch_op.drop_column("pause_reason")
        if column_exists("turns", "langgraph_run_id"):
            batch_op.drop_column("langgraph_run_id")
```

### 1.2 Add the same fields to `TurnResult`

`apps/api/app/services/agent/models.py`, in `TurnResult` (~line 112):

```python
    langgraph_run_id: str | None = None
    pause_reason: str | None = None
    required_additional_budget_usd: float | None = None
```

### 1.3 Set these fields when a research turn actually paused

`apps/api/app/services/agent/runtime.py`, in the `elif route == "research":` block of `run_stream()` — right where `result = TurnResult(...)` gets built after `research = yield from self._run_research_subtree(request, progress)`. The interrupted state is already available as `research["langgraph_state"]["interrupted"]` (set in `langgraph_runtime/runtime.py::stream_langgraph_research`). Add:

```python
elif route == "research":
    research = yield from self._run_research_subtree(request, progress)
    response = research["response"]
    langgraph_state = research.get("langgraph_state") or {}
    is_paused = bool(langgraph_state.get("interrupted"))
    ...
    if request.research_level == "deep" and (
        not research.get("answer_streamed") or (research.get("replay_final_answer") and not is_langgraph_streamed)
    ) and not is_paused:  # don't fake-replay an answer that doesn't exist yet
        yield from self._emit_buffered_answer(response, progress)
    pause_contract = langgraph_state.get("pause_contract") or {}
    result = TurnResult(
        turn_id=turn_id,
        goal=goal,
        answer=response.text,
        route=goal.route,
        turn_status="paused" if is_paused else "completed",
        langgraph_run_id=research.get("langgraph_run_id") if is_paused else None,
        pause_reason=pause_contract.get("pause_reason") if is_paused else None,
        required_additional_budget_usd=pause_contract.get("required_additional_budget_usd") if is_paused else None,
        model_used=response.model_used,
        sources=research["sources"],
        tool_calls=research["tool_calls"],
        events=events,
        latency_ms=response.latency_ms + sum(call.latency_ms for call in research["tool_calls"]),
        cost_usd=response.cost_usd,
    )
```

### 1.4 Make `persistence.py::complete_turn` respect `turn_status`

`apps/api/app/services/agent/persistence.py`, `complete_turn()` (~line 1224). Currently hardcodes `Turn.status: "completed"`. Change to:

```python
def complete_turn(result: TurnResult, *, lease_owner: str | None = None) -> bool:
    context_snapshot = _context_snapshot_from_result(result)
    should_update_context = bool(result.goal.conversation_id)
    completed_at = datetime.now(timezone.utc)
    is_paused = result.turn_status == "paused"
    completion_values = {
        Turn.user_id: result.goal.user_id,
        Turn.conversation_id: result.goal.conversation_id,
        Turn.objective: result.goal.objective,
        Turn.route: result.route,
        Turn.quality_mode: result.goal.quality_mode,
        Turn.status: result.turn_status,  # "paused" or "completed" — was hardcoded "completed"
        Turn.answer: result.answer,
        Turn.model_used: result.model_used,
        Turn.sources_json: _dumps([source.model_dump(mode="json") for source in result.sources]),
        Turn.latency_ms: result.latency_ms,
        Turn.cost_usd: result.cost_usd,
        # Only set completed_at for genuinely terminal outcomes — a paused turn
        # may still be approved and finished later; don't mark it "done" yet.
        Turn.completed_at: None if is_paused else completed_at,
        Turn.updated_at: completed_at,
        Turn.lease_owner: None,
        Turn.lease_expires_at: None,
        Turn.heartbeat_at: None,
        Turn.error_message: None,
        Turn.langgraph_run_id: result.langgraph_run_id,
        Turn.pause_reason: result.pause_reason,
    }
    ...  # rest of the function unchanged — the WHERE clause / lease check logic stays as-is
```

Don't touch the `Turn.status == "running"` precondition in the lease-owner `UPDATE ... WHERE` clause — a paused turn is still transitioning *from* "running", same as a completed one; only the target value changes.

### 1.5 Wire `/approve` to update the original `Turn` row

This is the piece most likely to get missed: today, calling `/approve` resumes the LangGraph run but leaves the chat turn exactly as it was. Add a helper to `persistence.py`:

```python
def complete_turn_after_langgraph_resume(langgraph_run_id: str, result: dict[str, Any]) -> bool:
    """After a paused LangGraph run is approved and resumed to completion (or
    paused again), find the Turn row by langgraph_run_id and update it —
    resume_langgraph_research() never touches the turns table itself, since
    it's called from an admin action, not from job_worker's turn pipeline.
    """
    from app.services.agent.langgraph_runtime.state import BudgetDecision

    db = SessionLocal()
    try:
        turn = db.query(Turn).filter(Turn.langgraph_run_id == langgraph_run_id).first()
        if turn is None:
            return False
        langgraph_state = result.get("langgraph_state") or {}
        still_paused = bool(langgraph_state.get("interrupted"))
        response = result.get("response")
        now = datetime.now(timezone.utc)
        turn.status = "paused" if still_paused else "completed"
        turn.answer = getattr(response, "text", "") or turn.answer
        turn.cost_usd = (turn.cost_usd or 0.0) + getattr(response, "cost_usd", 0.0)
        turn.updated_at = now
        turn.completed_at = None if still_paused else now
        if still_paused:
            pause_contract = langgraph_state.get("pause_contract") or {}
            turn.pause_reason = pause_contract.get("pause_reason")
        else:
            turn.pause_reason = None
            turn.langgraph_run_id = None  # resolved — no longer needs to be looked up
        db.commit()
        return True
    finally:
        db.close()
```

`apps/api/app/routers/agent.py::approve_langgraph_pause` — call this after a successful resume:

```python
@router.post("/admin/langgraph/runs/{run_id}/approve")
def approve_langgraph_pause(...) -> dict:
    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    from app.services.agent.langgraph_runtime import LangGraphResumeConflict, resume_langgraph_research
    from app.services.agent import persistence

    try:
        result = resume_langgraph_research(
            run_id,
            approved_by=user_id,
            updated_budget_ceiling_usd=(body.updated_budget_ceiling_usd if body else None),
        )
    except LangGraphResumeConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    persistence.complete_turn_after_langgraph_resume(run_id, result)
    return jsonable_encoder(result)
```

### 1.6 Surface the new fields on the status endpoints

`persistence.py::load_turn_status` (~line 1391) currently returns a hand-built dict — add `langgraph_run_id` and `pause_reason` from the `Turn` row into that dict, and make sure `load_turn`/whatever builds the `AgentResult`-shaped `turn` sub-object also includes `turn_status`, `langgraph_run_id`, `pause_reason`, `required_additional_budget_usd` (check how `Turn` rows get serialized into `AgentResult` elsewhere in `persistence.py` — likely a `_turn_to_result`-style function — and add the three fields there).

---

## Part 2 — New admin endpoint: list paused runs

`apps/api/app/routers/admin.py`, alongside the existing `/jobs` endpoint (~line 236), using the same `RequireAdmin`/`AdminPrincipal` pattern the rest of that file uses:

```python
@router.get("/langgraph/runs")
def list_langgraph_runs(
    status: str | None = None,
    admin: AdminPrincipal = RequireAdmin,
) -> dict:
    """List langgraph_run_contexts rows, optionally filtered by status.
    Joins to Turn (via langgraph_run_id) for objective/user context so the
    admin panel doesn't need a second round-trip per row.
    """
    from app.db.models import LangGraphRunContext, Turn, SessionLocal

    db = SessionLocal()
    try:
        query = db.query(LangGraphRunContext)
        if status:
            query = query.filter(LangGraphRunContext.status == status)
        rows = query.order_by(LangGraphRunContext.updated_at.desc()).limit(200).all()
        items = []
        for row in rows:
            turn = db.query(Turn).filter(Turn.langgraph_run_id == row.run_id).first()
            items.append({
                "run_id": row.run_id,
                "status": row.status,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                "resumed_at": row.resumed_at.isoformat() if row.resumed_at else None,
                "resumed_by": row.resumed_by,
                "turn_id": turn.id if turn else None,
                "objective": turn.objective if turn else None,
                "user_id": turn.user_id if turn else None,
                "pause_reason": turn.pause_reason if turn else None,
            })
        return {"items": items}
    finally:
        db.close()
```

This is a plain synchronous list, matching `/admin/jobs`'s shape closely enough to reuse the same frontend patterns. Add the corresponding response type to `apps/web/app/admin/types.ts` (mirror `AdminJobsResponse`/`AdminJobStatus`):

```typescript
export type LangGraphRunStatus = 'running' | 'paused' | 'resuming' | 'completed' | 'failed' | 'orphaned'

export type LangGraphRunItem = {
  run_id: string
  status: LangGraphRunStatus
  created_at: string | null
  updated_at: string | null
  resumed_at: string | null
  resumed_by: string | null
  turn_id: string | null
  objective: string | null
  user_id: string | null
  pause_reason: string | null
}

export type LangGraphRunsResponse = { items: LangGraphRunItem[] }
```

---

## Part 3 — Frontend types

`apps/web/app/types.ts` — extend `AgentResult` and `AgentTurnStatus` (~lines 79–106):

```typescript
export type AgentResult = {
  turn_id: string
  goal?: { objective?: string; quality_mode?: string }
  answer: string
  route: string
  turn_status?: string
  langgraph_run_id?: string | null
  pause_reason?: string | null
  required_additional_budget_usd?: number | null
  model_used?: string
  latency_ms?: number
  sources?: Source[]
  artifacts?: Artifact[]
  events?: ProgressEvent[]
  follow_up_options?: FollowUpOption[]
  research_plan_preview?: ResearchPlanPreview | null
  created_at?: string
}

export type AgentTurnStatus = {
  turn_id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | 'paused' | string
  error_message?: string | null
  attempt_count?: number
  max_attempts?: number
  heartbeat_at?: string | null
  turn: AgentResult
}
```

---

## Part 4 — `useTurnRunner.ts`: handle the paused terminal status

`apps/web/app/hooks/useTurnRunner.ts`, `applyTerminalStatus()` (~line 216):

```typescript
function applyTerminalStatus(
  payload: AgentTurnStatus,
  conversationId: string,
  turnMessage: string,
  option?: FollowUpOption,
): boolean {
  if (payload.status === 'completed') {
    completeTurn(payload.turn, conversationId, turnMessage, option)
    return true
  }
  if (payload.status === 'paused') {
    // Treat as terminal for polling purposes (stop the stream/poll loop) but
    // keep it distinct from completeTurn so the paused-state UI renders
    // instead of a normal finished answer.
    clearStreamState()
    setTurnState(payload.turn, payload.turn.events || eventsRef.current)
    return true
  }
  if (payload.status === 'failed') {
    setError(payload.error_message || "I couldn't complete this request. Please try again.")
    return true
  }
  if (payload.status === 'cancelled') {
    setError('This turn was cancelled.')
    return true
  }
  return false
}
```

`result.turn_status === 'paused'` is now reachable from whatever component consumes `agent.result` (see Part 5). No other change needed here — `setTurnState` already stores the full `AgentResult`, which now carries `langgraph_run_id`/`pause_reason`.

---

## Part 5 — User-facing chat UI: the paused card

New file `apps/web/app/components/PausedApprovalCard.tsx`:

```tsx
'use client'

import { useState } from 'react'
import { AlertTriangle, CheckCircle2 } from 'lucide-react'
import { readErrorBody } from '../lib/api'
import type { AgentResult, AuthorizedFetch } from '../types'

export function PausedApprovalCard({
  result,
  isAdmin,
  authorizedFetch,
  onResolved,
}: {
  result: AgentResult
  isAdmin: boolean
  authorizedFetch: AuthorizedFetch
  onResolved: (updated: AgentResult) => void
}) {
  const [approving, setApproving] = useState(false)
  const [error, setError] = useState('')

  async function approve() {
    if (!result.langgraph_run_id) return
    setApproving(true)
    setError('')
    try {
      const response = await authorizedFetch(
        `/admin/langgraph/runs/${encodeURIComponent(result.langgraph_run_id)}/approve`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) },
      )
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not approve this run'))
      // Resume is synchronous on the backend — a single follow-up fetch gets
      // the finalized turn (or a second pause_contract if it paused again).
      const statusResponse = await authorizedFetch(`/turns/${result.turn_id}/status`)
      if (statusResponse.ok) {
        const payload = await statusResponse.json()
        onResolved(payload.turn as AgentResult)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not approve this run')
    } finally {
      setApproving(false)
    }
  }

  return (
    <div className="my-3 rounded-lg border border-amber-300 bg-amber-50 p-4 dark:border-amber-800 dark:bg-amber-950/40">
      <div className="flex items-start gap-3">
        <AlertTriangle size={18} className="mt-0.5 flex-shrink-0 text-amber-600 dark:text-amber-400" />
        <div className="flex-1 space-y-1">
          <p className="text-sm font-semibold text-amber-900 dark:text-amber-200">
            Research paused — budget approval needed
          </p>
          <p className="text-sm text-amber-800 dark:text-amber-300">
            {result.pause_reason || 'This research run reached its cost limit before finishing.'}
          </p>
          {typeof result.required_additional_budget_usd === 'number' && (
            <p className="text-xs text-amber-700 dark:text-amber-400">
              Continuing requires authorizing up to ${result.required_additional_budget_usd.toFixed(2)} more.
            </p>
          )}
          {error && <p className="text-xs text-red-600 dark:text-red-400">{error}</p>}
        </div>
      </div>
      {isAdmin ? (
        <button
          type="button"
          onClick={() => void approve()}
          disabled={approving}
          className="mt-3 inline-flex h-8 items-center gap-1.5 rounded-md bg-amber-600 px-3 text-xs font-semibold text-white hover:bg-amber-700 disabled:opacity-50"
        >
          <CheckCircle2 size={14} />
          {approving ? 'Approving…' : 'Approve and continue'}
        </button>
      ) : (
        <p className="mt-3 text-xs text-amber-700 dark:text-amber-400">
          Waiting on an admin to approve additional budget.
        </p>
      )}
    </div>
  )
}
```

Wire it into `apps/web/app/components/AgentShell.tsx`, near where `agent.error` is currently rendered (~line 440) and where `<Timeline ... />` is rendered (~line 423) — render the card instead of (or alongside) the normal answer timeline when paused:

```tsx
{agent.result?.turn_status === 'paused' ? (
  <PausedApprovalCard
    result={agent.result}
    isAdmin={isAdmin}
    authorizedFetch={authorizedFetch}
    onResolved={(updated) => agent.setTurnState(updated, updated.events || [])}
  />
) : (
  <Timeline
    ...
    liveAnswer={agent.liveAnswer}
    ...
  />
)}
```

`agent.setTurnState` already exists in `useTurnRunner`'s return value (line 415 of the current file) — reuse it rather than adding new state plumbing.

---

## Part 6 — Admin-facing tab: all paused runs

New file `apps/web/app/admin/components/ApprovalsTab.tsx`, modeled directly on `JobsTab.tsx` (poll every 5s, status filter chips, table, per-row action):

```tsx
'use client'

import { CheckCircle2, RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { readErrorBody } from '../../lib/api'
import { formatAppDateTime } from '../../lib/format'
import type { AuthorizedFetch, LangGraphRunItem, LangGraphRunsResponse, LangGraphRunStatus } from '../types'

const FILTERS: Array<{ label: string; value: '' | LangGraphRunStatus }> = [
  { label: 'Paused', value: 'paused' },
  { label: 'All', value: '' },
  { label: 'Running', value: 'running' },
  { label: 'Completed', value: 'completed' },
  { label: 'Failed', value: 'failed' },
  { label: 'Orphaned', value: 'orphaned' },
]

const STATUS_STYLES: Record<string, string> = {
  paused: 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
  running: 'bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300',
  resuming: 'bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300',
  completed: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300',
  failed: 'bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300',
  orphaned: 'bg-neutral-200 text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300',
}

export function ApprovalsTab({ authorizedFetch }: { authorizedFetch: AuthorizedFetch }) {
  const [data, setData] = useState<LangGraphRunsResponse | null>(null)
  const [status, setStatus] = useState<'' | LangGraphRunStatus>('paused')
  const [error, setError] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [approving, setApproving] = useState<string | null>(null)

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setRefreshing(true)
    try {
      const query = status ? `?status=${status}` : ''
      const response = await authorizedFetch(`/admin/langgraph/runs${query}`)
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not load LangGraph runs'))
      setData(await response.json() as LangGraphRunsResponse)
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load LangGraph runs')
    } finally {
      if (!quiet) setRefreshing(false)
    }
  }, [authorizedFetch, status])

  useEffect(() => {
    void load()
    const timer = window.setInterval(() => void load(true), 5000)
    return () => window.clearInterval(timer)
  }, [load])

  async function approve(runId: string) {
    setApproving(runId)
    try {
      const response = await authorizedFetch(`/admin/langgraph/runs/${encodeURIComponent(runId)}/approve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      })
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not approve this run'))
      await load(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not approve this run')
    } finally {
      setApproving(null)
    }
  }

  function formatTime(value: string | null) {
    return formatAppDateTime(value, { second: '2-digit' })
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap gap-1">
          {FILTERS.map(filter => (
            <button
              key={filter.label}
              type="button"
              onClick={() => setStatus(filter.value)}
              className={`h-8 rounded-md px-3 text-xs font-semibold ${
                status === filter.value
                  ? 'bg-neutral-900 text-white dark:bg-white dark:text-neutral-900'
                  : 'border border-neutral-200 text-neutral-600 hover:bg-neutral-100 dark:border-neutral-800 dark:text-neutral-300 dark:hover:bg-neutral-900'
              }`}
            >
              {filter.label}
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={() => void load()}
          disabled={refreshing}
          className="grid h-8 w-8 place-items-center rounded-md border border-neutral-200 text-neutral-500 disabled:opacity-50 dark:border-neutral-800 dark:text-neutral-400"
          aria-label="Refresh"
        >
          <RefreshCw size={14} className={refreshing ? 'animate-spin' : ''} />
        </button>
      </div>

      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}

      {!data ? (
        <div className="h-40 animate-pulse bg-neutral-100 dark:bg-neutral-900" />
      ) : data.items.length === 0 ? (
        <p className="py-16 text-center text-sm text-neutral-400">No runs match this status.</p>
      ) : (
        <div className="overflow-x-auto border border-neutral-200 dark:border-neutral-800">
          <table className="w-full min-w-[900px] border-collapse text-left text-xs">
            <thead className="bg-neutral-50 text-[10px] uppercase text-neutral-400 dark:bg-neutral-900">
              <tr>
                <th className="px-3 py-2.5">Status</th>
                <th className="px-3 py-2.5">Run</th>
                <th className="px-3 py-2.5">Reason</th>
                <th className="px-3 py-2.5">Updated</th>
                <th className="w-32 px-3 py-2.5" />
              </tr>
            </thead>
            <tbody>
              {data.items.map((run: LangGraphRunItem) => (
                <tr key={run.run_id} className="border-t border-neutral-200 align-top dark:border-neutral-800">
                  <td className="px-3 py-3">
                    <span className={`inline-flex rounded px-2 py-1 font-bold ${STATUS_STYLES[run.status] || ''}`}>
                      {run.status}
                    </span>
                  </td>
                  <td className="max-w-[360px] px-3 py-3">
                    <p className="truncate font-mono text-[10px] text-neutral-400" title={run.run_id}>{run.run_id}</p>
                    <p className="mt-1 line-clamp-2 font-medium text-neutral-800 dark:text-neutral-200">
                      {run.objective || '—'}
                    </p>
                    <p className="mt-1 truncate text-[10px] text-neutral-400">{run.user_id || '—'}</p>
                  </td>
                  <td className="px-3 py-3 text-neutral-600 dark:text-neutral-300">{run.pause_reason || '—'}</td>
                  <td className="px-3 py-3 text-neutral-500">{formatTime(run.updated_at)}</td>
                  <td className="px-3 py-3">
                    {run.status === 'paused' && (
                      <button
                        type="button"
                        onClick={() => void approve(run.run_id)}
                        disabled={approving === run.run_id}
                        className="inline-flex h-7 items-center gap-1 rounded-md bg-amber-600 px-2 text-[11px] font-semibold text-white hover:bg-amber-700 disabled:opacity-50"
                      >
                        <CheckCircle2 size={12} />
                        Approve
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
```

Register it in `apps/web/app/admin/components/AdminShell.tsx`:

```tsx
import { ApprovalsTab } from './ApprovalsTab'
// ...
import { ShieldCheck } from 'lucide-react'  // add to the existing lucide-react import

type AdminTab = 'overview' | 'jobs' | 'approvals' | 'users' | 'modelpolicy' | 'usage' | 'system' | 'evals'

const TABS: { id: AdminTab; label: string; icon: typeof LayoutDashboard }[] = [
  { id: 'overview', label: 'Overview', icon: LayoutDashboard },
  { id: 'jobs', label: 'Jobs', icon: ListTodo },
  { id: 'approvals', label: 'Approvals', icon: ShieldCheck },
  { id: 'users', label: 'Users', icon: Users },
  ...
]
```

Then wherever `AdminShell` switches on `tab` to render the active component (find the `{tab === 'jobs' && <JobsTab ... />}`-style block further down in the same file), add:

```tsx
{tab === 'approvals' && <ApprovalsTab authorizedFetch={authorizedFetch} />}
```

---

## Testing plan

- Backend: extend `test_langgraph_maturity.py` with a test that runs a request through `Runtime().run_stream()` (not just `run_langgraph_research` directly) with a forced budget pause, and asserts the final `StreamEnvelope(type="result")` payload has `turn_status == "paused"`, `langgraph_run_id` set, and `pause_reason` set — this is the piece that was completely unverified before (everything tested so far exercised the LangGraph layer directly, never the full turn pipeline).
- Backend: test `complete_turn` persists `status="paused"` and leaves `completed_at` null; a second test approving that run via `complete_turn_after_langgraph_resume` and asserting the `Turn` row flips to `status="completed"` with `langgraph_run_id` cleared.
- Backend: test `GET /admin/langgraph/runs?status=paused` returns the joined objective/user fields correctly.
- Frontend: a `useTurnRunner` test (there's already `useTurnRunner.test.tsx`) asserting that a `turn` SSE event with `status: 'paused'` stops the polling loop and populates `result` with `turn_status: 'paused'` rather than calling `completeTurn`'s side effects (which append to conversation history as a finished turn).
- Manual: run the full loop end-to-end once — trigger a real pause via a low budget ceiling, see the amber card in chat, approve from the admin Approvals tab (not just curl), confirm the chat turn updates in place.

## Non-goals for this pass

- No changes to who's allowed to approve (stays `is_admin`-gated, per the design default stated above).
- No changes to the LangGraph graph/node logic — this is entirely turn-lifecycle plumbing and UI.
- Not building a "list of all my paused runs" view for regular users — the chat-UI card covers the run they're actively looking at; broader self-service history is a separate feature if you want it later.
