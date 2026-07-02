# Fix: approving a paused LangGraph run blocks with no feedback — Implementation Guide

**The gap, confirmed by reading the code:** `resume_langgraph_research` (`apps/api/app/services/agent/langgraph_runtime/runtime.py:497-542`) calls `get_compiled_research_graph().invoke(Command(resume=approval), config=config)` — `.invoke()`, a blocking call that runs the rest of the graph (verify → judge → possibly repair, each a real LLM call) to completion before returning. The `/admin/langgraph/runs/{run_id}/approve` route (`apps/api/app/routers/agent.py:340-361`) calls this directly inside a synchronous request handler. Both places that trigger it — `PausedApprovalCard.tsx:24-43` (user-facing) and `ApprovalsTab.tsx:59-75` (admin table) — just `await` the fetch and show a static "Approving…" label for however long that takes. None of the token-by-token/commentary streaming built for the main path applies here.

There's a second effect of the same root cause: `complete_turn_after_langgraph_resume` (`persistence.py:1421-1458`) updates the `Turn` row directly and never calls `_submit_context_update` — the resumed answer never reaches `conversation.context_json`'s `running_summary`/`key_facts`. `_merge_live_recent_turns` covers the very next reply, but the summary permanently misses it once that turn ages out of the 8-turn window.

**Fix strategy:** make resume go through the exact same streaming + completion plumbing a fresh turn already uses, instead of a bespoke blocking path. Concretely: add a streaming twin of `resume_langgraph_research`, dispatch it off the request thread, and feed its output through `persistence.persist_turn_envelope` — the same function `job_worker.py` already calls for every ordinary turn. That function's `result` branch already calls `complete_turn`, which already calls `_submit_context_update` — so the context-update gap disappears as a side effect, with no new completion code needed.

**Scope note before you commit to this:** this touches four files across two layers and adds a new background executor. Given HITL pauses only fire when a run hits its budget ceiling — presumably rare for 2-3 users — this is a real but low-frequency papercut, not an outage. If the full version below feels like more than it's worth, the cheap partial fix is just making `PausedApprovalCard`/`ApprovalsTab` show an honest "this can take a minute, please wait" message instead of a bare spinner, without changing any backend plumbing. That doesn't fix the missing context-update, but it's a 10-minute change instead of this one. Your call — the rest of this doc assumes you want the real fix.

## 1. Backend: a streaming twin of `resume_langgraph_research`

`apps/api/app/services/agent/langgraph_runtime/runtime.py`, add after `resume_langgraph_research` (~line 543):

```python
def stream_resume_langgraph_research(
    run_id: str,
    *,
    approved_by: str,
    updated_budget_ceiling_usd: float | None = None,
    progress: Any = None,
):
    """Streaming twin of resume_langgraph_research.

    Same contract as stream_langgraph_research: yields ("node", payload) and
    ("delta", {"text": ..., "source_node": ...}) tuples, returns the same
    result-dict shape via StopIteration.value. Raises LangGraphResumeConflict
    if run_id is not currently paused (see resume_langgraph_research's
    docstring — callers must translate that into a 409, not retry silently).
    """
    _claim_run_for_resume(run_id, resumed_by=approved_by)

    ctx = _RUN_CONTEXTS.get(run_id) or _load_run_context(run_id) or {}
    if ctx.get("request") is None:
        _mark_run_context(run_id, "paused")
        raise RuntimeError(f"LangGraph run context is missing for run_id={run_id!r}")

    approval: dict[str, Any] = {
        "approved_by": approved_by,
        "approved_at": datetime.utcnow().isoformat() + "Z",
        "approval_audit_event_id": new_id("lgapprove"),
    }
    if updated_budget_ceiling_usd is not None:
        approval["updated_budget_ceiling_usd"] = updated_budget_ceiling_usd

    config = _langgraph_config(run_id, ctx.get("request"), ctx.get("tools"), None)
    graph = get_compiled_research_graph()

    pause_contract: dict[str, Any] | None = None
    try:
        for mode, payload in graph.stream(
            Command(resume=approval),
            config=config,
            stream_mode=["updates", "custom"],
        ):
            if mode == "custom":
                if isinstance(payload, dict) and payload.get("answer_delta"):
                    source_node = str(payload.get("source_node") or "")
                    yield ("delta", {"text": str(payload["answer_delta"]), "source_node": source_node})
                continue
            pause_contract = _interrupt_payload(payload)
            if pause_contract is not None:
                break
            if not isinstance(payload, dict) or not payload:
                continue
            node_name, delta = next(iter(payload.items()))
            if isinstance(delta, dict):
                yield ("node", _summarize_node_delta(str(node_name), delta))
    except BaseException:
        _mark_run_context(run_id, "paused")
        raise

    final_state = _snapshot_values(run_id, ctx.get("request"), ctx.get("tools"))
    if pause_contract is not None:
        final_state["pause_contract"] = pause_contract
        final_state["budget_decision"] = BudgetDecision.REQUIRE_HUMAN_APPROVAL
        final_state["interrupted"] = True
        _mark_run_context(run_id, "paused")
        return _result_from_state(run_id, final_state)

    _RUN_CONTEXTS.pop(run_id, None)
    _complete_run(run_id)
    return _result_from_state(run_id, final_state)
```

This is `resume_langgraph_research` with `.invoke()` swapped for the identical `graph.stream(..., stream_mode=["updates","custom"])` loop already used in `stream_langgraph_research` — same yield shapes, same `_result_from_state` finalization both existing functions already share. A second budget gate pausing again mid-resume is handled the same way the first one was (rare, but the graph structure allows it).

## 2. Backend: share the delta/node forwarding loop between fresh and resume turns

`apps/api/app/services/agent/runtime.py`'s `_run_research_subtree` (~line 644-723) currently builds `gen = stream_langgraph_research(request, self.tool_registry.tools, progress)` itself, then runs the delta/node forwarding loop inline. Split the loop out so a resume path can reuse it without duplicating ~60 lines:

```python
def _run_research_subtree(self, request: TurnRequest, progress):
    from app.services.agent.langgraph_runtime.runtime import configured_orchestrator

    if configured_orchestrator() == "langgraph":
        from app.services.agent.langgraph_runtime import stream_langgraph_research
        gen = stream_langgraph_research(request, self.tool_registry.tools, progress)
        yield from self._forward_langgraph_stream(gen, progress)
        return
    ...  # unchanged: deep/lead research and other branches below

def _forward_langgraph_stream(self, gen, progress):
    """Shared delta/node → StreamEnvelope forwarding for both a fresh
    LangGraph research run and a resumed one — same event shapes either way,
    so the frontend doesn't need to know which case produced them."""
    buffered_answer = ""
    last_source_node: str | None = None
    result = None
    try:
        while True:
            kind, payload = next(gen)
            if kind == "delta":
                delta = payload.get("text", "") if isinstance(payload, dict) else str(payload)
                source_node = payload.get("source_node", "") if isinstance(payload, dict) else ""
                if last_source_node is not None and source_node and source_node != last_source_node and delta:
                    buffered_answer = ""
                    message = _LANGGRAPH_NODE_MESSAGES.get(
                        source_node, f"{source_node.replace('_', ' ').capitalize()}..."
                    )
                    reset_event = progress(source_node, message, reset=True, ephemeral_ui=True)
                    yield StreamEnvelope(type="progress", data=reset_event.model_dump(mode="json"))
                last_source_node = source_node
                buffered_answer += delta
                event = progress(
                    "answer_delta", "Streaming answer.",
                    delta=delta, char_count=len(buffered_answer), source_node=source_node, ephemeral_ui=True,
                )
                yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
            elif kind == "node":
                node_payload = dict(payload)
                node_name = str(node_payload.pop("node_name", "") or "")
                message = (
                    _LANGGRAPH_NODE_MESSAGES.get(node_name)
                    or node_payload.pop("message", None)
                    or f"{node_name.replace('_', ' ').capitalize()}..."
                )
                event = progress(node_name, message, **node_payload)
                yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
    except StopIteration as stop:
        result = stop.value
    if buffered_answer and result is not None:
        result["answer_streamed"] = True
        if not result.get("replay_final_answer"):
            result["replay_final_answer"] = False
        response = result.get("response")
        event = progress(
            "answer_complete", "Answer stream complete.",
            char_count=len(buffered_answer),
            model_used=getattr(response, "model_used", ""),
            latency_ms=getattr(response, "latency_ms", 0),
            cost_usd=getattr(response, "cost_usd", 0.0),
            ephemeral_ui=True,
        )
        yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
    return result
```

This is a pure extraction — behavior for the existing fresh-turn path is unchanged; verify with the existing `test_langgraph_maturity.py` suite before relying on it.

Now add a resume entry point that reuses this helper. Find where `_run_research_subtree`'s `result` gets turned into a `StreamEnvelope(type="result", ...)` for the fresh-turn case (immediately after where it's called) and mirror it here — the exact `TurnResult` construction from a raw LangGraph result dict should already be encapsulated somewhere reachable from that call site; reuse it rather than re-deriving it. Add:

```python
def resume_langgraph_turn_stream(self, turn_id: str, langgraph_run_id: str, *, approved_by: str, updated_budget_ceiling_usd: float | None, user_id: str):
    """Streaming resume, called from a background thread by the /approve
    endpoint (see persistence.py's resume dispatch). Yields StreamEnvelopes
    exactly like run_stream does for a fresh LangGraph research turn."""
    from app.services.agent.langgraph_runtime import stream_resume_langgraph_research

    def progress(stage: str, message: str, **data) -> ProgressEvent:
        return ProgressEvent(turn_id=turn_id, stage=stage, message=message, data=data)

    gen = stream_resume_langgraph_research(
        langgraph_run_id, approved_by=approved_by, updated_budget_ceiling_usd=updated_budget_ceiling_usd, progress=progress,
    )
    result = yield from self._forward_langgraph_stream(gen, progress)
    # Build the same TurnResult shape run_stream's fresh-turn path builds from
    # `result` — reuse that exact conversion (whatever helper/inline code
    # constructs TurnResult(...) from the langgraph result dict today) rather
    # than duplicating field mapping here. Locate it by searching runtime.py
    # for where _run_research_subtree's return value feeds into TurnResult(...).
    turn_result = ...  # <-- fill in using the existing conversion
    yield StreamEnvelope(type="result", data=turn_result.model_dump(mode="json"))
```

The `progress()` closure here is intentionally simpler than the one `run_stream` builds for fresh turns (no `events.append(...)` accumulation) because nothing downstream of `persist_turn_envelope` needs an in-memory `events` list for this path — `persist_turn_envelope` writes each progress event straight to the `Event` table itself.

## 3. Backend: dispatch resume off the request thread, reuse `persist_turn_envelope`

`apps/api/app/services/agent/persistence.py`, mirror the existing `_CONTEXT_EXECUTOR` pattern (~line 49) with a sibling:

```python
_RESUME_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="langgraph-resume")
_PENDING_RESUME_FUTURES: set[Future] = set()


def submit_langgraph_resume(turn_id: str, langgraph_run_id: str, *, approved_by: str, updated_budget_ceiling_usd: float | None, user_id: str) -> None:
    future = _RESUME_EXECUTOR.submit(
        _run_langgraph_resume, turn_id, langgraph_run_id,
        approved_by=approved_by, updated_budget_ceiling_usd=updated_budget_ceiling_usd, user_id=user_id,
    )
    _PENDING_RESUME_FUTURES.add(future)

    def _cleanup(done: Future) -> None:
        _PENDING_RESUME_FUTURES.discard(done)
        try:
            done.result()
        except Exception:
            logger.exception("LangGraph resume worker failed for turn %s", turn_id)

    future.add_done_callback(_cleanup)


def _run_langgraph_resume(turn_id: str, langgraph_run_id: str, *, approved_by: str, updated_budget_ceiling_usd: float | None, user_id: str) -> None:
    from app.services.agent.runtime import Runtime

    runtime = Runtime()
    for envelope in runtime.resume_langgraph_turn_stream(
        turn_id, langgraph_run_id, approved_by=approved_by, updated_budget_ceiling_usd=updated_budget_ceiling_usd, user_id=user_id,
    ):
        if envelope.type == "error":
            fail_turn(turn_id, str(envelope.data.get("detail") or envelope.data.get("message") or "Resume failed"))
            return
        persist_turn_envelope(envelope, turn_id)
```

`persist_turn_envelope` (persistence.py:1196-1214) is called with no `lease_owner` — its lease-ownership check only applies when one is passed, so this is safe for a single admin-triggered action with no worker-pool contention. Its `elif envelope.type == "result"` branch already calls `complete_turn(...)`, which already calls `_submit_context_update(...)` (persistence.py:1417) — this is what closes the context-update gap, with no new completion logic required. `complete_turn_after_langgraph_resume` becomes dead code once this lands; delete it rather than leave a second, now-unused completion path (the same "don't leave two copies of this logic to drift" note as the earlier context-truncation fix).

Add a small status-flip helper alongside the executor:

```python
def mark_turn_running_for_resume(turn_id: str) -> None:
    db = SessionLocal()
    try:
        turn = db.get(Turn, turn_id)
        if turn and turn.status == "paused":
            turn.status = "running"
            turn.updated_at = _now()
            db.commit()
    finally:
        db.close()
```

## 4. `/approve` endpoint: claim fast, dispatch, return immediately

`apps/api/app/routers/agent.py:340-361`:

```python
@router.post("/admin/langgraph/runs/{run_id}/approve")
def approve_langgraph_pause(
    run_id: str,
    body: LangGraphApprovalBody | None = None,
    user_id: str = CurrentActiveUser,
    is_admin: bool = CurrentUserIsAdmin,
) -> dict:
    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    from app.services.agent.langgraph_runtime import LangGraphResumeConflict, _claim_run_for_resume  # or an equivalent fast-claim-only export
    from app.services.agent import persistence

    turn = persistence.find_turn_by_langgraph_run_id(run_id)  # add if not already present
    if turn is None:
        raise HTTPException(status_code=404, detail="No turn found for this run.")
    try:
        persistence.claim_langgraph_run_for_resume(run_id, resumed_by=user_id)  # thin wrapper around _claim_run_for_resume
    except LangGraphResumeConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    persistence.mark_turn_running_for_resume(turn.id)
    persistence.submit_langgraph_resume(
        turn.id, run_id,
        approved_by=user_id,
        updated_budget_ceiling_usd=(body.updated_budget_ceiling_usd if body else None),
        user_id=turn.user_id,
    )
    return {"turn_id": turn.id, "status": "running"}
```

This returns in milliseconds instead of however long the rest of the graph takes. `_claim_run_for_resume` is already idempotency-safe (409 on conflict) — that part of the contract is unchanged, just called earlier and separately from the actual graph execution.

## 5. Frontend: attach to the SSE stream instead of waiting on the blocking response

`apps/web/app/components/PausedApprovalCard.tsx`, change `approve()` to hand off to the parent once the (now-fast) POST resolves, rather than fetching `/turns/{turn_id}/status` once and calling `onResolved` with a single snapshot:

```tsx
export function PausedApprovalCard({
  result,
  isAdmin,
  authorizedFetch,
  onApproved,
}: {
  result: AgentResult
  isAdmin: boolean
  authorizedFetch: AuthorizedFetch
  onApproved: (turnId: string, conversationId: string | null) => void
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
      onApproved(result.turn_id, result.conversation_id ?? null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not approve this run')
      setApproving(false)
    }
  }
  // ...unchanged JSX below; `approving` now only covers the fast claim step,
  // not the actual research — once onApproved fires, the parent's live-turn
  // view (the same one every in-flight turn uses) takes over entirely.
```

Wire `onApproved` from wherever `PausedApprovalCard` is rendered (in `AgentShell.tsx` or its chat view) to `agent.resumeTurn(turnId, conversationId, ...)` — the exact function `useTurnRunner.ts` already uses to reattach to a still-running background turn on page reload (`useTurnRunner.ts:397-413`). This means a resumed run now shows the same activity feed / drafting text / commentary as any other in-flight turn, including the `verify`/`judge`/`repair` messages already fixed this session.

`ApprovalsTab.tsx` (the admin table) doesn't have a live-turn view to hand off to — it's a monitoring table, not a chat surface. Its fix is smaller: after the fast `/approve` POST resolves, just start polling `/admin/langgraph/runs?status=running` (which it already does every 5s) — the row will show `status: running` immediately and flip to `completed` on its own within the existing poll cycle, which is now an honest reflection of reality instead of the button silently blocking.

## Testing plan

- Extend `test_langgraph_maturity.py`'s existing pause/resume coverage (it already builds a paused run) to assert `stream_resume_langgraph_research` yields `answer_delta` events with `source_node` present, and that its final `StopIteration.value` matches what `resume_langgraph_research` (the old blocking version, if kept temporarily for comparison) would have produced for the same inputs.
- New test: assert `POST /admin/langgraph/runs/{run_id}/approve` returns within a tight time bound (e.g. under 500ms with the LLM calls mocked to be slow) — proving the endpoint no longer blocks on the graph.
- New test mirroring `test_agent_conversation_context_uses_committed_turns_before_async_context_update` (added for the earlier context-truncation fix): pause a run, approve it, assert the resumed answer's `running_summary`/`key_facts` get updated — closing the gap this doc opened with.
- Manual: force a real budget-gate pause, approve from `PausedApprovalCard`, confirm you see live commentary/streaming instead of a frozen button, and that the answer appears via the same activity-feed-then-reveal flow as a fresh turn.
