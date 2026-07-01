# Fix: repaired answers snap in instead of streaming — Implementation Guide

**Update:** after the first version of this doc, testing showed the "I found something to improve, so I'm tightening it up" commentary never appeared on screen either — not a rendering miss, a sequencing bug, confirmed by reading `graph.py`: `repair` is the last node before `END`. Its friendly progress message currently fires from LangGraph's `updates` stream mode, which reports a node only *after* its function returns — so for `repair`, that event and the terminal "turn complete" event land within milliseconds of each other. The message was never visible; it fired and was immediately overwritten. This is independent of the streaming/snap-in issue and would have stayed broken even with the earlier subtitle fix alone. The design below fixes both by emitting the "repair" commentary manually at the moment repair's *first* token arrives, rather than waiting for LangGraph to report the node as finished — reusing the existing commentary/eventsRef pipeline outright, so no new frontend event type is needed for the message, only for the buffer reset.

**What the screenshots show, confirmed against the code:** `judge` sent this answer to `repair`, and `repair` produced a meaningfully rewritten version — different bullet wording, a new "Transparent Statement of Evidence Gaps" section header that wasn't in the paused screenshot at all. That's real content, not a rendering glitch. It appeared as a hard swap because of a deliberate suppression rule in `stream_langgraph_research` (`apps/api/app/services/agent/langgraph_runtime/runtime.py`):

```python
if source_node == "repair" and synthesis_streamed:
    continue
```

This drops every token `repair` produces whenever `synthesize` already streamed — which is nearly always, since `synthesize` runs first in every case. It was there to stop two unrelated text streams from visually colliding, but the cost is that any time repair fires, its output has no path to the client except appearing all at once in the final result.

There's a second, smaller bug feeding this: even if you removed the suppression, the client would append repair's tokens onto the *end* of synthesize's already-fully-displayed draft, because `source_node` is currently discarded before it reaches the frontend — `stream_langgraph_research` yields a bare string (`("delta", str(payload["answer_delta"]))`), and `apps/api/app/services/agent/runtime.py`'s forwarding loop just does `delta = str(payload)`. Repair's output is a full rewritten answer, not a continuation — appending it would produce garbled, duplicated text. Both pieces need fixing together: stop dropping repair's tokens, *and* give the client enough information to reset the display before the new stream starts rather than concatenate.

## Design

When the delta stream's source transitions from `synthesize` to `repair`, the client should clear the displayed answer and restart the typing animation — the existing `LiveTurn` component in `Timeline.tsx` already has a "no answer yet, show live commentary + pulsing bars" branch (`answer ? (...) : (...)`) that activates whenever `liveAnswer` is empty. If clearing the answer text on transition is done correctly, that branch naturally reactivates for a moment, showing the already-fixed "I found something to improve, so I'm tightening it up" message with the pulse animation — then flips back to the streaming-text view as repair's tokens arrive. **No `Timeline.tsx` changes are needed** — this reuses a mechanism you already have working correctly rather than building a new one.

## 1. Backend: stop discarding `source_node`, remove the suppression

`apps/api/app/services/agent/langgraph_runtime/runtime.py`, `stream_langgraph_research()`:

```python
if mode == "custom":
    if isinstance(payload, dict) and payload.get("answer_delta"):
        source_node = str(payload.get("source_node") or "")
        yield ("delta", {"text": str(payload["answer_delta"]), "source_node": source_node})
    continue
```

Remove `synthesis_streamed` entirely (the flag and both `if` checks that reference it) — it's no longer needed once repair is allowed to stream. Double-check `run_langgraph_research()` (the blocking wrapper just below) still works unchanged — it already ignores `kind == "delta"` payloads entirely (only branches on `kind == "node"`), so the shape change from a bare string to a `{"text", "source_node"}` dict doesn't affect it.

## 2. Backend: detect the transition, emit the commentary early, forward `source_node`

`apps/api/app/services/agent/runtime.py`, inside the `configured_orchestrator() == "langgraph"` branch (~line 663). Confirmed via `streamTurnStatus` in `useTurnRunner.ts` (~line 260-280) that *any* progress event whose `stage` isn't `answer_delta`/`answer_complete` already gets pushed into `eventsRef` and passed to `applyAnswerProgress` automatically — so the fix is to emit a normal node-shaped event (reusing the existing `stage: "repair"` → `_LANGGRAPH_NODE_MESSAGES["repair"]` → `commentary.ts`'s existing `case 'repair':` chain verbatim) at the moment repair's first token arrives, instead of relying on LangGraph's late `updates` event. No new event type needs plumbing through — only a `reset: true` flag the frontend checks for:

```python
gen = stream_langgraph_research(request, self.tool_registry.tools, progress)
buffered_answer = ""
last_source_node: str | None = None
try:
    while True:
        kind, payload = next(gen)
        if kind == "delta":
            text = payload.get("text", "") if isinstance(payload, dict) else str(payload)
            source_node = payload.get("source_node", "") if isinstance(payload, dict) else ""
            if last_source_node is not None and source_node and source_node != last_source_node and text:
                # The stream switched producers (synthesize -> repair): the new
                # text is a full rewrite, not a continuation, so reset the
                # client's buffer before appending rather than concatenating.
                # Emit the commentary *now* -- at first token -- rather than
                # waiting for LangGraph's "updates" event, which for a
                # repair -> END pipeline fires within milliseconds of the
                # turn's terminal event and is never actually visible.
                buffered_answer = ""
                message = _LANGGRAPH_NODE_MESSAGES.get(
                    source_node, f"{source_node.replace('_', ' ').capitalize()}..."
                )
                reset_event = progress(source_node, message, reset=True, ephemeral_ui=True)
                yield StreamEnvelope(type="progress", data=reset_event.model_dump(mode="json"))
            last_source_node = source_node
            buffered_answer += text
            event = progress(
                "answer_delta",
                "Streaming answer.",
                delta=text,
                char_count=len(buffered_answer),
                source_node=source_node,
                ephemeral_ui=True,
            )
            yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
        elif kind == "node":
            ...  # unchanged — repair's own late "updates" event still fires
                 # here too; plainCommentary's consecutive-dedup means the
                 # repeated identical message doesn't produce a second line.
except StopIteration as stop:
    result = stop.value
```

The rest of that method (the `answer_complete` emission, `answer_streamed`/`replay_final_answer` handling below it) stays as-is — `buffered_answer` being reset mid-stream just means `char_count` on the final `answer_complete` event correctly reflects the length of what's actually on screen, not a phantom sum of both drafts.

## 3. Frontend: reset the buffer on `data.reset`, and reuse `applyAnswerProgress`

`apps/web/app/hooks/useTurnRunner.ts` already has `clearStreamState()` — it wipes the token queue and streaming flags but not `liveAnswer` itself (by design, since it's currently only called at the very start of a turn or when a terminal result arrives and takes over). Add a check at the top of `applyAnswerProgress` (~line 449), *before* its existing `if (event.stage !== 'answer_delta') return` guard — today that guard means calling the function on a non-delta event is a silent no-op, which is why this needs to run first:

```typescript
function applyAnswerProgress(event: ProgressEvent) {
  if (event.data?.reset) {
    clearStreamState()
    setLiveAnswer('')
    return
  }
  if (event.stage === 'answer_complete') {
    flushTokenQueue()
    return
  }
  if (event.stage !== 'answer_delta') return
  ...  // unchanged
}
```

No change is needed to `streamTurnStatus`'s dispatch (~line 274) — the reset event's `stage` is `"repair"`, not `answer_delta`/`answer_complete`, so it already takes the branch that both appends it to `eventsRef` (making it visible to `plainCommentary`/`latestMessage`) *and* calls `applyAnswerProgress` (line 278-280). That's exactly the behavior needed: the commentary becomes visible and the buffer clears in the same event, using plumbing that already exists.

Once `liveAnswer` goes back to `''`, `LiveTurn`'s existing `answer ? (...) : (...)` branching in `Timeline.tsx` does the rest automatically — no changes needed there. `latestMessage` will already read "I found something to improve, so I'm tightening it up" from the event just pushed into `eventsRef`, so the pulsing-bars view shows the right text immediately, not a stale "Checking the research budget…" left over from `budget_gate_pre_repair`.

## What this produces, end to end

1. `synthesize` streams the draft answer in, as it does today.
2. `verify` runs — subtitle already correctly shows "I'm double-checking the citations and source support" (from the earlier fix).
3. `judge` decides repair is needed; `budget_gate_pre_repair` clears (or, if HITL approval is configured and triggers, genuinely pauses for it) — subtitle shows "Checking the research budget…", accurately, for however long that actually takes.
4. The moment `repair`'s first token arrives, the client gets a `stage: "repair", data.reset: true` event: the draft text clears, `LiveTurn` drops back into its pulsing-bars view showing "I found something to improve, so I'm tightening it up" — visible for as long as repair takes to produce its first token, not a flash.
5. `repair`'s tokens then stream in as genuinely new text, typing in exactly like the original draft did.
6. Turn completes; `result.answer` (already correct today) matches what's now been visibly typed, so there's no final swap at all — what streamed in *is* the answer.

## Testing plan

- There's already a test for the no-repair case (`test_langgraph_deep_repair_does_not_buffer_replay_after_stream` in `test_langgraph_maturity.py`) using a monkeypatched `judge` that forces `status="repair"`. Extend it (or add a sibling test) to assert: a `stage: "repair"` progress event with `data.reset == True` appears between the last synthesize-sourced `answer_delta` and the first repair-sourced one, and that `"".join(post_reset_deltas)` equals the final `result["answer"]` exactly (proving the streamed text is the real repaired answer, not the stale draft).
- Regression: re-run the golden-set parity harness — this changes delivery mechanics only, not what gets synthesized/repaired/judged, so final answers must be byte-identical to before.
- Manual: the exact scenario in your screenshots — a query likely to trigger repair (citation-heavy, multi-subject) — confirm you now see the draft type in, a brief "tightening it up" pulse, then the *revised* answer type in fresh, rather than a silent jump.
