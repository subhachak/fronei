# Stop/Cancel In-Flight Turn — Implementation Guide

**Scope:** frontend only. Confirmed by reading the code: `POST /turns/{turn_id}/cancel` (`apps/api/app/routers/agent.py:304`) already exists, is user-facing (not admin-gated), already checks ownership (`persistence.py::request_turn_cancellation` returns `False` if `turn.user_id != user_id`), and already flows correctly through `job_worker.py`'s cancellation check and `fail_or_requeue_turn`'s `cancel_requested` handling to land on `status="cancelled"`. Nothing on the backend needs to change for the core feature. `useTurnRunner.ts::applyTerminalStatus` already has a branch for `status === 'cancelled'`.

What's missing is purely: (1) the hook needs to track the active turn's ID so it has something to cancel, and expose a `cancel()` function, and (2) the Composer needs a Stop button that appears while a turn is running.

---

## 1. `useTurnRunner.ts`: track the active turn ID and expose `cancel()`

The hook currently discards `started.turn_id` as a local variable inside `run()` — nothing outside the function can see it. Add a ref alongside the existing `activeRunMessageRef` (~line 85):

```typescript
const activeTurnIdRef = useRef<string | null>(null)
const [cancelling, setCancelling] = useState(false)
```

Set it in `run()`, right after the turn is created (~line 375, where `started` is parsed):

```typescript
const started = await response.json() as { turn_id: string; conversation_id: string; status: string }
activeTurnIdRef.current = started.turn_id
const activeConversation = started.conversation_id || conversationId
const streamed = await streamTurnStatus(started.turn_id, activeConversation, runMessage, option)
if (!streamed) await pollTurnStatus(started.turn_id, activeConversation, runMessage, option)
```

Clear it in `run()`'s `finally` block, alongside the existing `activeRunMessageRef.current = null`:

```typescript
} finally {
  setRunning(false)
  activeRunMessageRef.current = null
  activeTurnIdRef.current = null
}
```

Do the same in `resumeTurn()` — set `activeTurnIdRef.current = turnId` at the top, clear it in `finally`, since a resumed background turn (reopening a conversation with a still-running job) is just as cancellable as a freshly started one.

Add the `cancel()` function itself, near `resumeTurn`:

```typescript
async function cancel() {
  const turnId = activeTurnIdRef.current
  if (!turnId || cancelling) return
  setCancelling(true)
  try {
    const response = await authorizedFetch(`/turns/${turnId}/cancel`, { method: 'POST' })
    if (!response.ok && response.status !== 409) {
      throw new Error(await readErrorBody(response, 'Could not stop this turn'))
    }
    // Don't set any terminal state here — request_turn_cancellation only sets
    // a flag (or, for a still-queued turn, resolves it immediately). The
    // in-flight streamTurnStatus/pollTurnStatus loop already running inside
    // run()/resumeTurn() will pick up the eventual status:'cancelled' event
    // through the existing applyTerminalStatus branch. A 409 here just means
    // the turn already finished on its own between the click and this
    // request landing — not a real error, nothing to surface.
  } catch (err) {
    setError(streamErrorMessage(err))
  } finally {
    setCancelling(false)
  }
}
```

Add `cancel` and `cancelling` to the hook's return object (~line 402):

```typescript
return {
  events,
  activeEvents,
  result,
  liveAnswer,
  error,
  setError,
  running,
  canRun,
  run,
  resumeTurn,
  cancel,
  cancelling,
  activeRunMessage: activeRunMessageRef.current,
  resetTurnState,
  setTurnState,
}
```

**On responsiveness — set expectations correctly, don't over-promise:** cancellation is cooperative, not preemptive. `job_worker.py` checks `turn_cancel_requested()` between each `StreamEnvelope` the runtime yields — for the LangGraph research path, that's now after every graph node completes and after every streamed answer token (a side benefit of the Phase 3 streaming work: cancellation checkpoints got much finer-grained for free). But if cancellation lands while a single node is mid-flight on a blocking call — an LLM request inside `synthesize` before its first token arrives, or a web search inside `search_worker` — that individual call still runs to completion (and is still billed) before the pipeline notices the cancellation and stops advancing to the next step. This is the same limitation any synchronous HTTP call has; don't build UI copy that implies an instant, guaranteed-mid-request kill.

---

## 2. `Composer.tsx`: swap Send for Stop while running

The button at ~line 225 currently shows a static spinner while `running` and is `disabled={!canRun}` — since `canRun` is false whenever `running` is true, the button is already inert during a run. Change it to become an active Stop button instead:

```tsx
export function Composer({
  message,
  setMessage,
  ...
  running,
  canRun,
  run,
  cancel,
  cancelling,
  ...
}: {
  ...
  running: boolean
  canRun: boolean
  run: () => void
  cancel: () => void
  cancelling: boolean
  ...
}) {
```

And the button itself:

```tsx
<button
  type="button"
  onClick={running ? cancel : run}
  disabled={running ? cancelling : !canRun}
  aria-label={running ? 'Stop' : 'Start'}
  title={running ? 'Stop this turn' : 'Start'}
  className={`grid h-9 w-9 place-items-center rounded-lg text-white disabled:opacity-50 ${
    running
      ? 'bg-red-600 hover:bg-red-700 dark:bg-red-600 dark:hover:bg-red-500'
      : 'bg-neutral-900 disabled:bg-neutral-300 dark:bg-white dark:text-neutral-900 dark:disabled:bg-neutral-700'
  }`}
>
  {running ? (
    cancelling ? <Loader2 size={15} className="animate-spin" /> : <Square size={14} fill="currentColor" />
  ) : (
    <Send size={15} />
  )}
</button>
```

Add `Square` to the existing `lucide-react` import at the top of the file (~line 3).

---

## 3. Wire it through `AgentShell.tsx`

Wherever `AgentShell.tsx` currently renders `<Composer running={agent.running} canRun={agent.canRun} run={agent.run} ... />`, add the two new props straight from the hook:

```tsx
<Composer
  ...
  running={agent.running}
  canRun={agent.canRun}
  run={agent.run}
  cancel={agent.cancel}
  cancelling={agent.cancelling}
  ...
/>
```

No other plumbing needed — `agent` here is already the object returned by `useTurnRunner(...)`.

---

## 4. Testing plan

- `useTurnRunner.test.tsx` already exists — add a test that calls `run()`, waits for `activeTurnIdRef` to populate (or just asserts `cancel()` issues a `POST /turns/{id}/cancel` with the ID returned by the mocked `POST /turns` response), then asserts a subsequent `turn` SSE event with `status: 'cancelled'` results in `error` being set to the cancellation message via the existing `applyTerminalStatus` branch — this exercises the whole loop, not just the new function in isolation.
- Add a test for the 409 case: mock `/turns/{id}/cancel` returning 409 (turn already finished), assert `cancel()` does not set `error` — a race between the click and natural completion isn't a failure.
- Manual: start a `deep` research turn (longest-running, easiest to click Stop on in time), click Stop mid-flight, confirm the chat shows a clear "cancelled" state rather than looking like a silent failure, and confirm in the admin Jobs tab that the turn shows `status: cancelled`, not `failed`.

## Explicit non-goal for this pass

`request_turn_cancellation` only accepts `turn.status in {"queued", "running"}` — a turn currently sitting in the new `"paused"` state (from the HITL approval work) is **not** cancellable through this endpoint; canceling something waiting on budget approval is a different, smaller follow-up (allow `"paused"` as a third cancellable status, and decide what happens to the now-abandoned LangGraph checkpoint) rather than something to fold into this change. Flagging it so it doesn't get assumed as covered.
