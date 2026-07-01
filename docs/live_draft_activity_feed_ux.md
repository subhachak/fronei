# Live turns: scrolling activity feed instead of an in-place answer bubble — Implementation Guide

**Decision (option 2 of two discussed):** stop rendering the in-progress draft as the actual answer at any point during the run — that's what produced "two rounds of animated streaming" when repair fires (draft types in fully, pauses, then a visibly different revision types in again in the same spot). Instead, for the entire duration a turn is running, show one consistent view: a small scrolling activity feed — node commentary and the draft's growing text interleaved in arrival order, auto-scrolling like a live log. The moment the turn is truly done (verified, judged, repaired if needed), the feed disappears and the real formatted answer appears once, the same way every past turn already renders. There is exactly one reveal, and it only ever shows settled content.

**Prerequisite:** `docs/repair_answer_streaming_fix.md` — its backend changes (tag every delta with `source_node`, stop suppressing repair's tokens, emit the "repair" commentary at first token rather than at node completion, clear `liveAnswer` via `data.reset` at the synthesize→repair transition) are all still required and unchanged. Only that doc's section 3 (how `liveAnswer` gets *rendered*) is superseded by this one.

## Why this fits the existing code with minimal new surface

`apps/web/app/components/Timeline.tsx` already has exactly the scrolling-feed pattern needed: `TelemetryWindow` (~line 458), currently only used in `LiveTurn`'s "no answer yet" branch. It's a fixed-height, auto-scrolling box listing the last 16 progress events with a commentary line each, already using `plainCommentaryForEvent` — which already has correct entries for `verify`, `judge`, `repair`, and everything else. The only two things missing are: (1) it's not shown once `answer` is non-empty (the "answer" branch takes over and shows the full bubble instead), and (2) it has no concept of the currently-streaming draft text — only discrete node events.

## 1. `LiveTurn`: remove the two-branch split (`Timeline.tsx` ~line 382-456)

Delete the `answer ? (...) : (...)` conditional entirely. Render the "no answer yet" layout unconditionally for the whole run — header with pulsing Sparkles icon, live commentary subtitle, and the (now extended) `TelemetryWindow`:

```tsx
function LiveTurn({
  message,
  answer,
  answerLive,
  events,
  copiedKey,
  onCopyText,
}: {
  message: string
  answer: string
  answerLive: boolean
  events: ProgressEvent[]
  copiedKey: string | null
  onCopyText: (value: string, key: string) => void | Promise<void>
}) {
  const commentary = plainCommentary(events)
  const latestMessage = commentary.at(-1) || 'I’m getting oriented and deciding the best way to handle this.'
  const telemetryEvents = events.filter(event => !['tool_selection', 'tool_result'].includes(event.stage))
  const userCopied = copiedKey === 'live:user'
  const assistantCopied = copiedKey === 'live:assistant'
  const copyValue = answer || latestMessage
  const copyLabel = answer ? 'Copy current draft' : 'Copy current status'

  return (
    <div className="flex flex-col gap-2.5">
      <div className="self-end max-w-[min(88%,860px)] rounded-2xl rounded-br-md bg-neutral-900 px-4 py-3 text-white dark:bg-white dark:text-neutral-900">
        <div className="mb-1.5">
          <p className="text-[11px] font-bold uppercase tracking-wide text-white/55 dark:text-neutral-500">You</p>
        </div>
        <p className="whitespace-pre-wrap text-[15px] leading-relaxed [overflow-wrap:anywhere]">{message}</p>
        <div className="mt-2 flex justify-end">
          <CopyButton tone="on-inverted-bubble" copied={userCopied} label="Copy your message" onClick={() => onCopyText(message, 'live:user')} />
        </div>
      </div>

      <div className="w-full max-w-[860px] rounded-2xl rounded-bl-md border border-neutral-200 bg-white p-4 shadow-sm dark:border-neutral-800 dark:bg-neutral-900">
        <div className="mb-3.5 flex items-start gap-3">
          <span className="av3-pulse-ring grid h-9 w-9 flex-shrink-0 place-items-center rounded-full bg-neutral-900 text-white dark:bg-white dark:text-neutral-900">
            <Sparkles size={16} />
          </span>
          <div className="min-w-0 flex-1">
            <p className="text-sm font-bold text-neutral-900 dark:text-neutral-50">Fronei</p>
            <p className="mt-0.5 text-sm leading-relaxed text-neutral-500 dark:text-neutral-400">{latestMessage}</p>
          </div>
        </div>

        <div aria-label="Fronei is actively working" className="av3-pulse-bars relative mb-4 ml-12 grid max-w-[180px] grid-cols-3 gap-1.5">
          <span className="h-1 rounded-full bg-emerald-500/70" />
          <span className="h-1 rounded-full bg-emerald-500/70" />
          <span className="h-1 rounded-full bg-emerald-500/70" />
        </div>

        <TelemetryWindow events={telemetryEvents} fallback={latestMessage} draftText={answer} draftLive={answerLive} />
        <div className="mt-3.5 flex justify-end">
          <CopyButton copied={assistantCopied} label={copyLabel} onClick={() => onCopyText(copyValue, 'live:assistant')} />
        </div>
      </div>
    </div>
  )
}
```

`answer` is kept as a prop (still needed to decide the copy button's value/label) but is no longer used to branch the whole layout. The pulsing ring on the Sparkles icon now runs for the entire turn, not just the pre-answer phase — consistent with "still working" being true the whole time now, which it genuinely is.

`StreamingMarkdown` (~line 158) and `StreamingInlineMarkdown`/`LiveParagraph` become unused inside `LiveTurn` once this lands — leave them defined (they're reasonable general-purpose pieces and removing them is a separate cleanup, not required for this fix) unless a lint rule flags unused exports.

## 2. `TelemetryWindow`: add the growing draft as one more feed entry (~line 458)

```tsx
function TelemetryWindow({
  events,
  fallback,
  draftText,
  draftLive,
}: {
  events: ProgressEvent[]
  fallback: string
  draftText: string
  draftLive: boolean
}) {
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const visibleEvents = events.slice(-16)

  useEffect(() => {
    if (!scrollRef.current) return
    scrollRef.current.scrollTop = scrollRef.current.scrollHeight
  }, [visibleEvents.length, draftText])

  if (visibleEvents.length === 0 && !draftText) {
    return (
      <div className="ml-12 rounded-xl border border-neutral-200 bg-neutral-50 px-3 py-2 text-xs leading-relaxed text-neutral-500 dark:border-neutral-800 dark:bg-neutral-950/40 dark:text-neutral-400">
        {fallback}
      </div>
    )
  }

  return (
    <div className="ml-12 overflow-hidden rounded-xl border border-neutral-200 bg-neutral-50 dark:border-neutral-800 dark:bg-neutral-950/40">
      <div className="flex items-center justify-between border-b border-neutral-200 px-3 py-2 dark:border-neutral-800">
        <span className="text-[10px] font-bold uppercase tracking-wider text-neutral-400">Live telemetry</span>
        <span className="text-[10px] font-semibold text-neutral-400">{visibleEvents.length} step{visibleEvents.length === 1 ? '' : 's'}</span>
      </div>
      <div ref={scrollRef} className="max-h-64 overflow-y-auto px-3 py-2">
        <div className="grid gap-2">
          {visibleEvents.map((event, index) => {
            const summary = plainCommentaryForEvent(event) || event.message || 'Working through the task.'
            const chips = eventChips(event)
            return (
              <div key={event.event_id || `${event.stage}-${index}-${event.created_at || ''}`} className="grid grid-cols-[8px_minmax(0,1fr)] gap-2">
                <span className={`mt-1.5 h-2 w-2 rounded-full ${index === visibleEvents.length - 1 && !draftText ? 'bg-emerald-500' : 'bg-neutral-300 dark:bg-neutral-700'}`} />
                <div className="min-w-0">
                  <div className="flex min-w-0 items-center gap-2">
                    <span className="truncate text-[11px] font-bold uppercase tracking-wide text-neutral-500 dark:text-neutral-400">{humanizeStage(event.stage)}</span>
                    {event.created_at && <span className="flex-shrink-0 text-[10px] text-neutral-400">{formatRelativeTime(event.created_at)}</span>}
                  </div>
                  <p className="mt-0.5 text-xs leading-relaxed text-neutral-600 dark:text-neutral-300">{summary}</p>
                  {chips.length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {chips.slice(0, 3).map(chip => (
                        <span key={chip} className="rounded-full bg-white px-1.5 py-0.5 text-[10px] font-semibold text-neutral-400 ring-1 ring-neutral-200 dark:bg-neutral-900 dark:ring-neutral-800">
                          {chip}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            )
          })}
          {draftText && (
            <div className="grid grid-cols-[8px_minmax(0,1fr)] gap-2">
              <span className="av3-pulse-dot mt-1.5 h-2 w-2 rounded-full bg-emerald-500" />
              <div className="min-w-0">
                <span className="truncate text-[11px] font-bold uppercase tracking-wide text-neutral-500 dark:text-neutral-400">Drafting</span>
                <p className="mt-0.5 whitespace-pre-wrap text-xs leading-relaxed text-neutral-600 dark:text-neutral-300 [overflow-wrap:anywhere]">
                  <StreamingText text={draftText} />
                  {draftLive && <StreamCursor />}
                </p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
```

Changes from today's version: `max-h-44` → `max-h-64` (the box now regularly carries a paragraph of draft text in addition to the event log, so it needs more room — adjust to taste, but it should stay a bounded, scrolling box, not grow to fit the whole draft, or this reintroduces the exact "shows the draft as a whole" problem being fixed here). The empty-state guard now checks `draftText` too, since the feed can have live text before any discrete event has fired. The last real event's dot only lights up green when there's no draft text growing after it (so the "currently active" indicator tracks whichever is actually the newest thing happening).

`StreamingText` and `StreamCursor` (defined earlier in `Timeline.tsx`, ~lines 16 and 52) are reused as-is — no changes needed to either.

## 3. `Timeline`'s call site (~line 240)

Pass the one new prop:

```tsx
{running && (
  <LiveTurn
    message={draftMessage}
    answer={liveAnswer}
    answerLive={running}
    events={events}
    copiedKey={copiedKey}
    onCopyText={onCopyText}
  />
)}
```

`answerLive` is really just `running` restated with an explicit name at the call site — `running` is already a prop `Timeline` receives, so no new state is needed anywhere.

## 4. `useTurnRunner.ts`: no changes beyond `repair_answer_streaming_fix.md`

`liveAnswer` continues to accumulate exactly as that doc describes (token-smoothing queue, `data.reset` clearing the buffer at the synthesize→repair transition). Nothing about how it's produced changes — only how `Timeline.tsx` displays it.

## What this produces, end to end

1. Turn starts: feed shows "I've chosen a path..." → "I'm breaking the question into focused research angles." → search/read/rank entries, scrolling as they arrive — exactly as today.
2. `synthesize` starts: a "Drafting" entry appears at the bottom of the same feed, growing character by character with the same fade-in animation, auto-scrolling to stay in view. The header subtitle also reads "Writing the answer..." — no full-size answer bubble appears anywhere yet.
3. `verify` runs: a new feed entry appears below the (now-paused, fully grown) draft text — "I'm double-checking the citations and source support." The draft text stays visible above it, static, still inside the small box.
4. `judge` decides repair is needed: another feed entry, "I'm doing a quality pass before handing it back," then `budget_gate_pre_repair` clears.
5. `repair` starts: a feed entry "I found something to improve, so I'm tightening it up" appears, and — per the backend fix — the "Drafting" block clears and starts growing again from empty, now with repair's revised text. Nothing about this reads as "the answer changed," because no answer has been shown yet at all — it reads as "still working," which is accurate.
6. Turn completes: the entire card (feed and all) is replaced by the real `WorkItem` rendering via `MarkdownResult` — the same code path every already-completed turn uses, unanimated, exactly once, with the final, correct, fully-repaired content.

## Testing plan

- Manual: the repair-triggering scenario from before — confirm the answer bubble never appears mid-run, confirm the feed box auto-scrolls as draft text grows, confirm the "Drafting" entry visibly clears and restarts when repair fires, confirm the final answer appears once, unanimated relative to the feed (a quick fade-in on the whole card is fine; a second retype is not).
- Manual: a turn with no repair — confirm behavior is unchanged from the repair case minus the reset, i.e., draft grows once, feed shows verify/judge entries after it stops, turn completes normally.
- Regression: existing `Timeline`/`LiveTurn` component tests (if any assert on `StreamingMarkdown` or the old two-branch structure) will need updating to match the single-branch structure — grep for `LiveTurn` and `TelemetryWindow` in the test suite before landing this.
