# Inline "still working" status during verify/judge/repair — Implementation Guide

**Root cause (confirmed by reading the code, not guessed):** `apps/web/app/components/Timeline.tsx`'s `LiveTurn` component has two branches. Before the first answer token arrives, it shows a live-updating subtitle driven by `plainCommentary(events)` (already computed at the top of the component as `latestMessage`) — this is why the "getting oriented" style messages work correctly during search/read/rank. But once `answer` is non-empty (first synthesize token has arrived), it switches to a **hardcoded, frozen string**:

```tsx
{answer ? (
  <div ...>
    ...
    <p className="mt-0.5 text-sm leading-relaxed text-neutral-500 dark:text-neutral-400">Writing the response…</p>
    ...
```

That string never changes again for the rest of the turn — through `verify`, `judge`, and `repair` — even though `latestMessage` right above it is already updating correctly in real time as those stages' progress events arrive (they already reach the client; nothing needs to change on the backend). The fix is to use `latestMessage` in both branches instead of a static string in one of them.

## 1. `Timeline.tsx`: use `latestMessage` in the answer branch

`apps/web/app/components/Timeline.tsx`, inside `LiveTurn` (~line 419-422):

```tsx
<div className="min-w-0 flex-1">
  <p className="text-sm font-bold text-neutral-900 dark:text-neutral-50">Fronei</p>
  <p className="mt-0.5 text-sm leading-relaxed text-neutral-500 dark:text-neutral-400">
    {answerInProgressMessage}
  </p>
</div>
```

Just above the `return` in `LiveTurn` (near where `latestMessage` is already computed at ~line 396), add a small derivation so the subtitle reads naturally once text exists rather than repeating the same "getting oriented" framing used before any text has appeared:

```tsx
const commentary = plainCommentary(events)
const latestMessage = commentary.at(-1) || 'I'm getting oriented and deciding the best way to handle this.'
// Once the answer has started, "I'm getting oriented..." no longer makes sense
// as a fallback — fall back to a writing-in-progress phrasing instead, but
// still prefer the live commentary (verify/judge/repair messages) whenever
// a newer one is available.
const answerInProgressMessage = commentary.at(-1) || 'Writing the response…'
```

That's the entire behavioral fix. `latestMessage` (used in the no-answer branch) and `answerInProgressMessage` (used in the answer branch) now both prefer the latest real commentary line and only differ in their fallback text for the brief window before any progress event has arrived yet.

## 2. `commentary.ts`: give `verify` its own message instead of the generic fallback

`apps/web/app/lib/commentary.ts`, `plainCommentaryForEvent` (~line 40). `judge` and `repair` already have dedicated, well-written cases; `verify` currently falls through to the generic regex fallback (`/judge|quality|verify/i` → "I'm checking the quality before finishing."), which works but doesn't say what's actually happening. Add a dedicated case matching the existing style:

```typescript
case 'document_judge_result':
case 'judge':
  return 'I'm doing a quality pass before handing it back.'
case 'verify':
  return 'I'm double-checking the citations and source support.'
case 'repair':
case 'repair_loop':
  return 'I found something to improve, so I'm tightening it up.'
```

This is a small polish, not required for the fix to work (the regex fallback already produces something reasonable), but it's a one-line addition matching the effort already put into every other stage in this file, and it directly matches the phrasing you used when describing the symptom ("Checking citations...").

## 3. Nothing else needs to change

- No backend changes. `verify`/`judge`/`repair` progress events already carry through `_LANGGRAPH_NODE_MESSAGES` (`app/services/agent/runtime.py`) and the SSE forwarding built in the streaming work — they were always reaching the client, just not being displayed in the right spot.
- `events`/`eventsRef` in `useTurnRunner.ts` already include these events unconditionally (only `answer_delta`/`answer_complete` get special-cased out of the regular event list) — no change needed there either.
- The blinking cursor (`StreamCursor`) itself doesn't need to change — it correctly represents "the turn is still live." The fix is what sits *above* it: the subtitle now explains why nothing new is appearing, instead of silently repeating "Writing the response…" through a phase where nothing is being written.

## What this does and doesn't fix

This closes the "looks like a silent stall" perception — the label will now visibly update to "I'm double-checking the citations and source support," then "I'm doing a quality pass before handing it back," and (if repair fires) "I found something to improve, so I'm tightening it up," right next to the still-blinking cursor.

It does **not** fix the separate issue flagged earlier: if `repair` fires, its corrected answer still doesn't stream — it snaps in silently the moment the turn ends, because of the `synthesis_streamed` suppression rule in `stream_langgraph_research` (`langgraph_runtime/runtime.py`). With this fix, the user will at least see "I found something to improve, so I'm tightening it up" during that window instead of a blank pause — but the corrected text itself still appears all at once rather than typing in. That's the other option I mentioned earlier (letting repair's tokens actually stream); still open if you want it.

## Testing plan

- Manual: trigger a research turn where `verify` genuinely runs (any answer with `[S#]` citations) and watch the subtitle change from "Writing the response…" to "I'm double-checking the citations and source support" right as the text stops advancing.
- Manual: force a repair loop (same technique used in `test_langgraph_deep_repair_does_not_buffer_replay_after_stream` — monkeypatch `judge` to return `status="repair"`) and confirm the subtitle updates to the repair message during that phase.
- No new automated test strictly required — this is a pure presentation change reading data that's already flowing correctly and already covered by the existing SSE-ordering test (`test_langgraph_research_sse_streams_progress_and_answer_before_result`). If you want one anyway: a `Timeline`/`LiveTurn` component test asserting the subtitle text matches the latest non-`answer_delta` event's commentary once `answer` is non-empty, not a hardcoded string.
