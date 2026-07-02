# Streaming & Progress UX Implementation Guide (Roadmap Phase 3, corrected scope)

**Status as of July 2, 2026:** implemented and superseded as an active plan.
Research is now LangGraph-only, with live node progress, answer deltas,
repair-reset handling, and quiet-step heartbeats. The old parity workflow and
pre-LangGraph research path referenced below have been retired.

**Supersedes:** Phase 3 of `docs/langgraph_implementation_roadmap.md`.
**Original trigger for this doc:** Code review of the Phase 1–4 implementation
found that Phase 3 had not been built, and — more importantly — found that the
LangGraph research path shipped zero incremental progress events to the
frontend, not just zero token streaming. That historical gap is now closed.

---

## 0. What you asked for, and the gap underneath it

You want: progress shown in user-friendly text throughout the pipeline, then the answer typed in as soon as generation starts. That's two features — node-level progress streaming, and token-level answer streaming — and only the token-level one was ever attempted (and it wasn't finished; see the previous review). The progress-event one was never wired for the LangGraph path at all. Here's the exact mechanism:

`app/services/agent/runtime.py::Runtime._run_research_subtree` is a generator
that now forwards LangGraph stream events live. Before this work landed, its
LangGraph branch returned a blocking result instead of yielding progress:

```python
# old shape, before this work landed
return run_langgraph_research(request, self.tool_registry.tools, progress)
```

A bare `return` inside a generator does not yield anything — it ends the generator and hands back the return value to whatever called it with `yield from`. `run_langgraph_research()` is a plain function, not a generator; internally its nodes call `progress(...)` (via `emit_graph_event`) which only appends `ProgressEvent` objects to an in-memory list — nothing about that call transmits anything over SSE. Before this work landed, the user saw **nothing** until the whole graph finished or paused, at which point the accumulated `events` list rode along inside the single final `TurnResult`. This was worse than the buffered-typing-animation issue — it meant there was no "what's it doing right now" signal at all during a research run.

Fix this first. Token streaming without progress streaming is a smaller win than progress streaming without token streaming — do both, but sequence progress-streaming first since token streaming depends on the same generator rewiring.

---

## 1. Current status table

| Item | Status | Implementable now? |
|---|---|---|
| **Live progress-event streaming for LangGraph runs** | Complete | Implemented by `stream_langgraph_research` and `_forward_langgraph_stream` |
| **Token-level answer streaming (synthesize/repair)** | Complete | Implemented with streamed deltas and repair reset handling |
| Frontend rendering for streamed text | Complete | `Timeline.tsx` renders live deltas with `StreamingInlineMarkdown` |
| Phase 1 bug: `tools=None` after context loss on resume | Complete | Run contexts are durable and resume reloads tool context |
| Phase 1 bug: `_RUN_CONTEXTS` in-memory, not durable | Complete | Run context storage moved to durable DB-backed state |
| Phase 1 bug: checkpointer has no thread lock | Complete | Checkpointer access is guarded for SQLite use |
| Phase 1 gap: no pause/resume integration test | Complete | Covered in LangGraph runtime/maturity tests |
| Phase 5 — legacy `research_lead.py` retirement | Complete | Done — research is LangGraph-only |
| Phase 6 — extend graph beyond research | Still a separate decision | Not implied by this work |

Historical sequencing note: the two streaming items shipped together because
they share the same call-path rewiring. The Phase 1 durability/checkpointer
fixes landed separately and are now reflected in the current LangGraph runtime.

---

## 2. Implementation — Live Progress Streaming

### 2.1 Turn `run_langgraph_research` into a real streaming generator

`app/services/agent/langgraph_runtime/runtime.py` — add a new generator
function alongside the existing `run_langgraph_research`. Don't delete
`run_langgraph_research`; evals and tests call it expecting a single blocking
dict back. Instead, make it a thin wrapper over the new generator:

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

`app/services/agent/runtime.py::_run_research_subtree` — replace the old
blocking `return run_langgraph_research(...)` branch:

```python
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
- Regression: run the LangGraph eval/maturity suite. The old
  `langgraph_parity.yml` comparator was retired with the pre-LangGraph runtime.

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

Add a sibling function rather than changing this one's signature. The
non-streaming version can remain for blocking callers/tests, while LangGraph
uses the streaming variant for live answer deltas:

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

Once section 2.2's `result["answer_streamed"] = True` is set whenever real
deltas were forwarded, this condition naturally stops firing the
buffered-replay fallback for LangGraph runs. The pre-LangGraph
`lead_research_loop` fallback no longer exists; `_emit_buffered_answer` is only
for non-research/direct-style fallback paths that still produce a complete
answer without token deltas.

No `Timeline.tsx` changes needed — `StreamingInlineMarkdown` renders incoming
text with the fade-in treatment whenever `live=true` is passed down from the
`answer_delta` event path.

### 3.4 Testing plan

- Unit: `synthesize_answer_stream` — assert `on_delta` is called more than once for a multi-sentence fixture answer (proves it's not just calling `on_delta` once with the whole string, which would technically satisfy the interface but defeat the purpose).
- Integration: SSE test asserting `answer_delta` events for the LangGraph path arrive **before** the node event for `judge`/`repair` complete, i.e., synthesis text starts reaching the client while verification/judging is still running server-side — that ordering is the actual UX win here ("start streaming as soon as possible"), so assert it directly rather than just asserting deltas exist somewhere in the stream.
- Regression: LangGraph eval/maturity tests should confirm
  `synthesize_answer_stream`'s final `ModelResponse` remains equivalent in
  content to the non-streaming synthesis path.

---

## 4. What remains correctly out of scope

**Phase 5 (legacy retirement):** complete. `research_lead.py`, the parity
workflow, and the orchestrator selector have been retired.

**Phase 6 (extend graph beyond research):** still a decision, not a task, and still gated on Phase 5. Nothing here changes that.

**Phase 1 bug fixes** (tools=None fallback, durable run-context, checkpointer locking, resume test coverage): implementable, real, but deliberately excluded from this document's scope — see the recommendation in section 1 not to bundle them with the streaming work. Track them as a separate, smaller PR referencing the earlier code review.
