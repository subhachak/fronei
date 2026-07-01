# Fix: follow-up replies lose track of the offer they're answering — Implementation Guide

**Symptom:** an assistant turn ends with an offer/question ("...Would you like that?"). The user replies "yes." The next turn (correctly routed `direct_fast`) responds "Could you clarify what you'd like help with?" — as if the offer never existed.

**Root cause, confirmed by reading the code:** `apps/api/app/services/agent/persistence.py:292`, inside `_update_context_with_snapshot` — the function that runs after every completed turn (`_update_context_for_completed_turn`, called from the job worker) and writes what becomes the next turn's conversation history:

```python
"assistant": _compact_text(str(snapshot.get("answer") or ""), 520),
```

`_compact_text` (line 77-79) is `cleaned[:limit].rstrip()` — a plain prefix slice. Any answer longer than 520 characters has everything past that point silently discarded before it's ever stored. A structured multi-paragraph medical answer easily exceeds 520 characters in its opening section alone, so the closing offer sentence never survives storage. Traced the full path from there:

1. `_update_context_with_snapshot` truncates and appends this into `ctx["recent_turns"]`, then writes it to `conversation.context_json`.
2. `_render_context` (~line 173-215) turns `recent_turns[-6:]` into the text block that becomes `request.conversation_context`.
3. `decide_fast_path` (`fast_path.py:104-113`) and `answer_direct_fast` (`fast_path.py:160-172`) both consume `request.conversation_context` directly — by the time either runs, the offer sentence is already gone. Router misclassification isn't the issue; `direct_fast`'s existing rule ("use direct_fast only if context clearly contains the target") is reasonable, it just has nothing to check against.

There's a narrow existing safety net — `_merge_live_recent_turns` (~line 334-366) appends *untruncated* `Turn.answer` text for turns not yet reflected in the persisted context, to cover a race window. But once `_update_context_for_completed_turn` has run (the normal case, not a race), that turn is already in `recent_turns` and gets skipped by `_merge_live_recent_turns`'s dedup check — so the safety net only helps in the rare case where the next message arrives before the async context-update job finishes.

## The fix: preserve the tail, not just the head

Increasing the 520-char cap doesn't really solve this — it just moves the cutoff point; any sufficiently long answer (which is common for research-route or repair-lengthened answers) still loses its ending. The actual fix is to stop assuming the important part of an answer is always at the start. Offers, follow-up questions, and next-step prompts are conventionally at the *end* — so truncation needs to keep both ends and drop the middle instead.

`apps/api/app/services/agent/persistence.py`, add near `_compact_text` (~line 77):

```python
def _compact_text(value: str, limit: int) -> str:
    cleaned = " ".join((value or "").split())
    return cleaned[:limit].rstrip()


def _compact_text_preserve_tail(value: str, limit: int, tail_chars: int = 180) -> str:
    """Like _compact_text, but keeps the end of the text too.

    Conversational answers routinely end with an offer or follow-up
    question ("Would you like that?") that later turns need to resolve
    pronouns/short replies against. A plain head-truncation silently
    drops exactly that sentence for any answer longer than `limit`.
    """
    cleaned = " ".join((value or "").split())
    if len(cleaned) <= limit:
        return cleaned
    tail_budget = min(tail_chars, limit // 3)
    head_budget = max(limit - tail_budget - 1, 0)
    head = cleaned[:head_budget].rstrip()
    tail = cleaned[-tail_budget:].lstrip()
    return f"{head} … {tail}"
```

Then use it for the `"assistant"` field specifically — this is the one that ends in offers/questions; `"user"` messages (the other truncated field, 360 chars) don't have the same pattern and can stay as-is:

`_update_context_with_snapshot` (~line 292):

```python
"assistant": _compact_text_preserve_tail(str(snapshot.get("answer") or ""), 520),
```

Also apply the same swap in `_update_context_with_result` (~line 259) for consistency, even though it currently has no call sites (`grep` found none — it appears to be dead code, possibly a foreground/non-job-worker path that's no longer wired up). Worth a quick check with the team on whether it's intentionally unused before touching it; if it truly has zero callers, this is a good moment to either delete it or confirm it's meant to be reinstated somewhere, rather than silently leaving two copies of this logic to drift.

While in this function, consider bumping the raw limit modestly too (e.g. 520 → 700) — the tail-preservation fix is the real correctness fix, but a slightly larger budget reduces how much of the *middle* reasoning gets dropped for long structured answers, which is a secondary quality-of-context concern, not the bug itself.

## Optional hardening (not required to fix the reported bug)

`FAST_ROUTER_PROMPT` (`fast_path.py:38-61`) already instructs the router to prefer `agentic` when context doesn't clearly cover a vague follow-up. Once the tail fix lands, that rule has the text it needs to actually work — no prompt change should be necessary. If you want a belt-and-suspenders guard against this whole class of bug (context truncated for some *other* reason later, a different field, etc.), the more robust fix is structural: detect when the previous turn's answer ends in a question mark and the current message is a short affirmative/negative ("yes", "sure", "no", "ok" — a handful of literal strings), and force `route=agentic` for that one turn via the same mechanism `last_turn_route` already uses in `orchestrator.py` — except that logic needs to run *before* `decide_fast_path` short-circuits in `runtime.py`, not after, since fast-path currently bypasses the orchestrator entirely. This is a larger structural change (moving a decision point earlier in the pipeline) — flagging it as a separate, optional follow-up rather than folding it into this fix, since the tail-preservation change directly resolves the reported symptom on its own.

## Testing plan

- Unit test for `_compact_text_preserve_tail`: an answer longer than `limit` retains both a recognizable prefix and its exact final sentence; an answer shorter than `limit` is returned unchanged (parity with `_compact_text`).
- Integration test on `_update_context_with_snapshot`: build a long `snapshot["answer"]` ending in a question, assert the stored `recent_turns[-1]["assistant"]` contains that trailing question verbatim.
- Regression/manual: reproduce the exact reported scenario — ask a question that produces a long structured answer ending in an offer, reply "yes," confirm the next turn now proceeds with the offered content instead of asking for clarification.
- Check `_render_context`'s `max_chars` trimming (`_trim_context`, ~line 233) still behaves sensibly with the `…`-joined tail format — it slices `rendered[-max_chars:]` from the *rendered* text, so a very tight overall context budget could still cut into an individual turn's preserved tail; this is a pre-existing behavior of the outer trim and not something this fix needs to solve, but worth being aware of if `max_chars` is ever tightened significantly.
