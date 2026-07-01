# Streaming & Progress UX Implementation Guide (Roadmap Phase 3, corrected scope)

**Supersedes:** Phase 3 of `docs/langgraph_implementation_roadmap.md`.
**Trigger for this doc:** Code review of the Phase 1–4 implementation found that Phase 3 was not built, and — more importantly — found that the LangGraph research path currently ships **zero** incremental progress events to the frontend, not just zero token streaming. This changes what "implementable now" means: there are two layered gaps, not one.

---

## 0. What you asked for, and the gap underneath it

You want: progress shown in user-friendly text throughout the pipeline, then the answer typed in as soon as generation starts. That's two features — node-level progress streaming, and token-level answer streaming — and only the token-level one was ever attempted (and it wasn't finished; see the previous review). The progress-event one was never wired for the LangGraph path at all. Here's the exact mechanism:

`app/services/agent/runtime.py::Runtime._run_research_subtree` is a generator (it has `yield StreamEnvelope(...)` statements throughout, which is what makes a Python function a generator). Its legacy branches call `progress(...)` and then explicitly `yield StreamEnvelope(...)` right after, so the SSE consumer in `routers/agent.py::stream_turn` gets each event the moment it happens. Its LangGraph branch, however, is:

```python
if configured_orchestrator() == "langgraph":
    ...
    return run_langgraph_research(request, self.tool_registry.tools, progress)
```

A bare `return` inside a generator does not yield anything — it ends the generator and hands back the return value to whatever called it with `yield from`. `run_langgraph_research()` is a plain function, not a generator; internally its nodes call `progress(...)` (via `emit_graph_event`) which only appends `ProgressEvent` objects to an in-memory list — nothing about that call transmits anything over SSE. So for the entire duration of a LangGraph research run (which is the default orchestrator today), the user sees **nothing** until the whole graph finishes or pauses, at which point the accumulated `events` list rides along inside the single final `TurnResult`. This is worse than the buffered-typing-animation issue — it means there's currently no "what's it doing right now" signal at all for the majority of your traffic.

Fix this first. Token streaming without progress streaming is a smaller win than progress streaming without token streaming — do both, but sequence progress-streaming first since token streaming depends on the same generator rewiring.

---

## 1. Pending & implementable — status table

| Item | Status | Implementable now? |
|---|---|---|
| **Live progress-event streaming for LangGraph runs** | Not implemented (confirmed above) | **Yes — do this first** |
| **Token-level answer streaming (synthesize/repair)** | Not implemented | **Yes — do this second, same PR is fine** |
| Frontend rendering for streamed text | Already built (`Timeline.tsx`'s `StreamingInlineMarkdown`) | N/A — just needs real deltas, not simulated ones |
| Phase 1 bug: `tools=None` after context loss on resume | Confirmed bug (previous review) | Yes, but separate concern from streaming — don't conflate in the same PR |
| Phase 1 bug: `_RUN_CONTEXTS` in-memory, not durable | Confirmed bug | Yes, separate PR |
| Phase 1 bug: checkpointer has no thread lock | Confirmed risk | Yes, separate PR |
| Phase 1 gap: no pause/resume integration test | Confirmed gap | Yes, separate PR |
| Phase 5 — legacy `research_lead.py` retirement | Correctly not started | **No** — gate requires 6 clean parity cycles with Phases 1–3 stable in production; Phase 3 doesn't exist yet, so the clock hasn't started |
| Phase 6 — extend graph beyond research | Correctly not started | **No** — explicitly decision-gated on Phase 5 completing first |

Recommendation: ship the two streaming items together (they share the same call-path rewiring), as their own PR, separate from the Phase 1 bug fixes. Streaming touches `runtime.py` (both the langgraph_runtime one and the orchestration one) and `nodes.py`; the Phase 1 bugs touch `checkpointer.py` and `graph.py::_runtime_context`. Different blast radius, different reviewers' attention — don't bundle them.

---

## 2. Implementation — Live Progress Streaming

### 2.1 Turn `run_langgraph_research` into a real streaming generator

`app/services/agent/langgraph_runtime/runtime.py` — add a new generator function alongside the existing `run_langgraph_research`. Don't delete `run_langgraph_research`; the eval harness and existing tests call it expecting a single blocking dict back, and changing its contract would break `langgraph_parity.yml`. Instead, make it a thin wrapper over the new generator:

```python
def stream_langgraph_research(request: Any, tools: Any, progress: Any = None):
    """Generator form of run_langgraph_research.

    Yields ('node', {...}) once per completed graph node (mirrors what
    emit_graph_event already captures — this just gets it to the caller in
    real time instead of buffering it) and ('delta', str) for token-level
    answer text pushed via get_stream_writer() inside synthesize/repair
    (see nodes.py changes in section 3). Returns the final result dict
    exactly as run_langgraph_research() does today, via StopIteration.value.
    """
    run_id = new_id("lgrun")
    _RUN_CONTEXTS[run_id] = {"request": request, "tools": tools, "progress": progress}
    config = _langgraph_config(run_id, request, tools, progress)
    graph = get_compiled_research_graph()

    pause_contract: dict[str, Any] | None = None
    for mode, payload in graph.stream(
        _initial_state(run_id, request), config=config, stream_mode=["updates", "custom"]
    ):
        if mode == "custom":
            # Pushed by get_stream_writer() inside synthesize/repair (section 3).
            # Anything other than a plain answer-delta string is ignored here —
            # keeps this loop from having to know every custom payload shape
            # a future node might introduce.
            if isinstance(payload, dict) and "answer_delta" in payload:
                yield ("delta", payload["answer_delta"])
            continue

        # mode == "updates": {node_name: state_delta_dict}
        interrupt_payload = _interrupt_payload(payload)
        if interrupt_payload is not None:
            pause_contract = interrupt_payload
            break
        node_name, delta = next(iter(payload.items()))
        yield ("node", {"node_name": node_name, **delta})

    final_state = _snapshot_values(run_id, request, tools)
    if pause_contract is not None:
        final_state["pause_contract"] = pause_contract
        final_state["budget_decision"] = BudgetDecision.REQUIRE_HUMAN_APPROVAL
        final_state["interrupted"] = True
    else:
        _RUN_CONTEXTS.pop(run_id, None)
    return _result_from_state(run_id, final_state)


def run_langgraph_research(request: Any, tools: Any, progress: Any = None) -> dict[str, Any]:
    """Blocking wrapper over stream_langgraph_research for callers that only
    want the final result (eval harness, tests, admin tooling)."""
    gen = stream_langgraph_research(request, tools, progress)
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value
```

Export `stream_langgraph_research` from `langgraph_runtime/__init__.py` alongside the existing exports.

**Why `stream_mode=["updates", "custom"]` and not just `"updates"`:** `"updates"` gives you the per-node state delta (what you need for progress text). `"custom"` is the channel `get_stream_writer()` writes to from inside a node — that's what section 3 uses for token deltas. Requesting both means a single `.stream()` call serves both features; you don't need two separate traversals of the graph.

### 2.2 Rewire the orchestration layer to forward events live

`app/services/agent/runtime.py::_run_research_subtree` — replace the `return run_langgraph_research(...)` branch:

```python
if configured_orchestrator() == "langgraph":
    from app.config import get_settings
    from app.services.agent.langgraph_runtime.runtime import stream_langgraph_research
    from app.services.agent.models import new_id

    audit_id = new_id("lgaudit")
    logger.info(
        "langgraph_orchestrator_dispatch",
        extra={
            "audit_id": audit_id,
            "orchestrator": "langgraph",
            "env": get_settings().app_env,
            "research_level": getattr(request, "research_level", None),
            "message_preview": (getattr(request, "message", "") or "")[:60],
        },
    )
    gen = stream_langgraph_research(request, self.tool_registry.tools, progress)
    buffered_answer = ""
    result: dict | None = None
    try:
        while True:
            kind, payload = next(gen)
            if kind == "delta":
                buffered_answer += payload
                event = progress(
                    "answer_delta", "Streaming answer.",
                    delta=payload, char_count=len(buffered_answer), ephemeral_ui=True,
                )
                yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
            elif kind == "node":
                node_name = payload.pop("node_name", "")
                message = _LANGGRAPH_NODE_MESSAGES.get(node_name, payload.get("message") or f"{node_name.replace('_', ' ').capitalize()}…")
                event = progress(node_name, message, **{k: v for k, v in payload.items() if k != "message"})
                yield StreamEnvelope(type="progress", data=event.model_dump(mode="json"))
    except StopIteration as stop:
        result = stop.value
    if buffered_answer and result is not None:
        result["answer_streamed"] = True
    return result
```

This is the exact `try/except StopIteration` pattern needed to both re-yield a generator's items *and* capture its return value — `yield from` alone can't transform each item into a `StreamEnvelope` the way this loop does, so manual iteration is correct here, not a workaround.

### 2.3 User-friendly node message table

Add near the top of `runtime.py` (or a new small module if you prefer, `app/services/agent/langgraph_runtime/ui_messages.py`):

```python
_LANGGRAPH_NODE_MESSAGES: dict[str, str] = {
    "brief": "Understanding what you're asking…",
    "subject_derivation": "Identifying what to compare…",
    "contract": "Mapping out what needs to be covered…",
    "plan": "Planning the research approach…",
    "dispatch_search": "Starting the searches…",
    "search_worker": "Searching the web…",
    "rank": "Ranking the best sources…",
    "read": "Reading the source pages…",
    "classify_claims": "Reviewing claims for accuracy…",
    "expand_source_graph": "Following up on related links…",
    "bind": "Pulling the evidence together…",
    "budget_gate_pre_synthesis": "Checking the research budget…",
    "budget_gate_pre_repair": "Checking the research budget…",
    "synthesize": "Writing the answer…",
    "verify": "Double-checking citations…",
    "judge": "Reviewing answer quality…",
    "repair": "Improving the answer…",
}
```

The existing `message` strings already produced by `emit_graph_event()` inside `nodes.py` (e.g. `"Ranked 14 source(s); selected top 6 for reading."`) are more specific and still useful — they're not being thrown away, they're passed through as the `**data` payload on the `ProgressEvent` (see `payload.get("message")` in 2.2). Whether the frontend shows the friendly label, the detailed message, or both is a `Timeline.tsx` decision, not a backend one — this table just guarantees every node has *a* human-readable label even before you decide on final copy.

### 2.4 Testing plan

- Unit: iterate `stream_langgraph_research(...)` against a fixture request with `FakeTools`; assert you get at least one `("node", ...)` tuple per entry in `nodes.NODE_ORDER` that the plan actually visits (search-fanout nodes will emit one `("node", ...)` per parallel worker, not one per NODE_ORDER entry — assert on the node *names* seen, not a 1:1 count).
- Integration: hit `/turns/stream` (or whatever `stream_turn`'s route path is) for a `research` route request, collect SSE events, assert `progress` events with `stage` in `_LANGGRAPH_NODE_MESSAGES.keys()` arrive **before** the terminal `result` event, with a measurable time gap between the first and last (proves they're not all flushed at once — this is the actual regression the current code has).
- Regression: re-run `langgraph_parity.yml` — this change doesn't touch node logic or state shape, only how already-computed data reaches the client, so the parity comparator's outputs should be byte-for-byte identical to before. If they're not, something in this rewiring leaked into the graph's actual computation, which would be a bug in this change, not an acceptable side effect.

---

## 3. Implementation — Token-Level Answer Streaming

### 3.1 Add a streaming variant of `synthesize_answer`

`app/services/agent/research_synthesis.py` currently has (line 148):

```python
def synthesize_answer(request: TurnRequest, plan: ResearchPlan, evidence: EvidencePack):
    system_prompt, user_prompt = build_synthesis_prompt(request, plan, evidence)
    return model_client.simple_completion(
        system_prompt, user_prompt,
        max_tokens=_synthesis_token_budget(request, plan),
        role="synthesis", quality_mode=request.quality_mode,
        overrides=request.model_overrides, timeout_s=_longform_timeout_s(),
    )
```

Add a sibling function rather than changing this one's signature (keep the non-streaming version for any caller that doesn't need deltas — e.g. the legacy `lead_research_loop` path, which should stay untouched):

```python
def synthesize_answer_stream(request: TurnRequest, plan: ResearchPlan, evidence: EvidencePack, *, on_delta=None):
    """Streaming variant of synthesize_answer. Calls on_delta(text_chunk) for
    each token delta as it arrives; returns the same ModelResponse shape as
    synthesize_answer() once the stream completes."""
    system_prompt, user_prompt = build_synthesis_prompt(request, plan, evidence)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    response = None
    for item in model_client.stream_complete(
        messages,
        role="synthesis",
        quality_mode=request.quality_mode,
        overrides=request.model_overrides,
        max_tokens=_synthesis_token_budget(request, plan),
        timeout_s=_longform_timeout_s(),
    ):
        if isinstance(item, model_client.ModelDelta):
            if item.text and on_delta:
                on_delta(item.text)
        else:
            response = item
    return response
```

This mirrors the exact pattern already proven in `app/services/agent/runtime.py::_stream_model_response` — same `model_client.stream_complete` call, same `ModelDelta`/`ModelResponse` split — so you're not introducing a new streaming mechanism, just reusing the one that already works for the `direct`/`document` routes.

### 3.2 Wire `get_stream_writer()` into the `synthesize` node

`app/services/agent/langgraph_runtime/nodes.py::synthesize`:

```python
def synthesize(state, *, run_id, request, tools=None, progress=None) -> dict:
    from langgraph.config import get_stream_writer
    from app.services.agent.research_synthesis import synthesize_answer_stream

    node_name: GraphNodeName = "synthesize"
    visited = [*state.get("visited_nodes", []), node_name]
    artifacts = {**state.get("artifacts", {}), node_name: {"status": "real"}}

    plan = state.get("plan")
    evidence = state.get("evidence")

    emit_graph_event(progress, run_id=run_id, node_name=node_name, message="Writing one coherent answer from the evidence.")

    if plan is None or evidence is None:
        logger.warning("synthesize: plan or evidence missing — returning empty answer")
        return {
            "visited_nodes": visited, "artifacts": artifacts, "answer": "",
            "model_used": "synthesize-no-plan", "latency_ms": 0,
            "cost_usd_spent": 0.0, "model_calls_made": 0,
        }

    writer = get_stream_writer()

    def _on_delta(text: str) -> None:
        if writer is not None:
            writer({"answer_delta": text})

    response = synthesize_answer_stream(request, plan, evidence, on_delta=_on_delta)

    emit_graph_event(
        progress, run_id=run_id, node_name=node_name,
        message=f"Synthesis used {response.model_used or 'the configured synthesis model'}.",
        model_used=response.model_used, latency_ms=response.latency_ms, cost_usd=response.cost_usd,
    )
    return {
        "visited_nodes": visited, "artifacts": artifacts,
        "answer": response.text or "", "model_used": response.model_used or "",
        "latency_ms": response.latency_ms or 0, "cost_usd_spent": response.cost_usd or 0.0,
        "model_calls_made": 1,
    }
```

`get_stream_writer()` returns `None` when the node runs outside a `.stream()` call (e.g. `run_stub_graph`'s `.invoke()` path, or the direct-call unit tests) — the `writer is not None` guard means this is safe in every calling context without a try/except, unlike `interrupt()` in `budget_gate`, which actually raises. That asymmetry is a LangGraph API detail, not an inconsistency in your code — `get_stream_writer()` is designed to be a no-op outside streaming; `interrupt()` is designed to require it.

Apply the identical change to `repair` in the same file — same `get_stream_writer()` pattern, wrapping a new `repair_research_answer_stream` you add next to `repair_research_answer` in `research_synthesis.py` the same way as 3.1.

### 3.3 Frontend: stop treating LangGraph answers as non-streamed

`app/services/agent/runtime.py` — the existing check:

```python
if request.research_level == "deep" and (
    not research.get("answer_streamed") or research.get("replay_final_answer")
):
    yield from self._emit_buffered_answer(response, progress)
```

Once section 2.2's `result["answer_streamed"] = True` is set whenever real deltas were forwarded, this condition naturally stops firing the buffered-replay fallback for LangGraph runs — you don't need to touch this block at all, it already does the right thing once the upstream flag is honest. Leave `_emit_buffered_answer` in place; it's still the correct fallback for the legacy `lead_research_loop` path (which has no token stream) and for the edge case where a LangGraph run produced an answer via the repair path without deltas ever firing (e.g. `synthesize` was skipped and only `repair` ran — make sure 3.2's change to `repair` covers that, per the note above).

No `Timeline.tsx` changes needed — `StreamingInlineMarkdown` (added in the earlier diff) already renders incoming text with the fade-in treatment whenever `live=true` is passed down from whatever prop currently gates that on `answer_delta` events; verify that prop is driven by the presence of `answer_delta` progress events rather than a hardcoded assumption about which route produced them, since it now needs to handle deltas arriving from both the legacy direct/document paths and the LangGraph research path identically.

### 3.4 Testing plan

- Unit: `synthesize_answer_stream` — assert `on_delta` is called more than once for a multi-sentence fixture answer (proves it's not just calling `on_delta` once with the whole string, which would technically satisfy the interface but defeat the purpose).
- Integration: SSE test asserting `answer_delta` events for the LangGraph path arrive **before** the node event for `judge`/`repair` complete, i.e., synthesis text starts reaching the client while verification/judging is still running server-side — that ordering is the actual UX win here ("start streaming as soon as possible"), so assert it directly rather than just asserting deltas exist somewhere in the stream.
- Regression: parity harness again — `synthesize_answer_stream`'s final `ModelResponse` must be identical in content to what `synthesize_answer` would have produced (same prompt, same model, same params — only delivery mechanism changed). If token streaming changes truncation/timeout behavior versus the non-streaming call, that's a real regression the parity gate should catch.

---

## 4. What remains correctly out of scope

**Phase 5 (legacy retirement):** still not implementable. The retirement gate — 6 clean weekly parity runs with Phases 1–3 stable in production — can't start counting until Phase 3 (this document) actually ships and has been live for a cycle. Don't touch `research_lead.py` yet.

**Phase 6 (extend graph beyond research):** still a decision, not a task, and still gated on Phase 5. Nothing here changes that.

**Phase 1 bug fixes** (tools=None fallback, durable run-context, checkpointer locking, resume test coverage): implementable, real, but deliberately excluded from this document's scope — see the recommendation in section 1 not to bundle them with the streaming work. Track them as a separate, smaller PR referencing the earlier code review.
