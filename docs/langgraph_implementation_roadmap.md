# LangGraph Maturity Roadmap — Fronei Research Runtime

**Status as of July 2, 2026:** implemented for the research runtime. Research
now runs on LangGraph only; the pre-LangGraph `research_lead.py` path, parity
cutover workflow, orchestrator override settings, and admin parity controls have
been retired. This document is kept as historical design context. Sections that
refer to `FRONEI_ORCHESTRATOR`, parity cutover gates, or legacy rollback are
superseded.

**Scope:** `apps/api/app/services/agent/langgraph_runtime/` and its call sites (`runtime.py`, `routers/agent.py`, `routers/evals.py`, `config.py`).
**Objective:** Close the six gaps identified in the code review — real human-in-the-loop (HITL), compile-once graph lifecycle, native streaming, production tracing, legacy retirement, and a deliberate scope decision on further graph adoption.
**Non-goal:** This roadmap does not migrate `runtime.py`'s routing/document/deck orchestration to LangGraph. That is Phase 6, gated on an explicit go/no-go decision, not assumed.

LangGraph version in use: `langgraph==1.2.6` (`langgraph-checkpoint 4.1.1`, `langgraph-prebuilt 1.1.0`, `langgraph-sdk 0.4.2` — confirmed in `uv.lock`). All syntax below targets this version's stable API (`interrupt()`, `Command(resume=...)`, `graph.stream(stream_mode=...)`).

---

## 0. Sequencing and Risk

| Phase | Change | Effort | Risk if skipped | Depends on |
|---|---|---|---|---|
| 1 | Checkpointer + `interrupt()` for real HITL pause/resume | High | Budget-approval flow silently discards work; state schema lies about capability | None |
| 2 | Compile graph once; inject per-request data via `config` | Medium | Ongoing latency tax on every research turn; blocks Phase 1's `thread_id` model | Phase 1 (checkpointer needs a stable compiled graph) |
| 3 | Native streaming (`.stream()` / stream writer) | Medium | UX keeps relying on the buffered-replay hack; no per-node observability for free | Phase 2 |
| 4 | Production LangSmith tracing | Low | No trace visibility into real user runs, only synthetic evals | None (parallelizable) |
| 5 | Legacy `research_lead.py` retirement | Complete | Retired; no dual-maintenance path remains | Superseded direct decision |
| 6 | Extend graph modeling beyond research (decision only) | — | Scope creep by default instead of by decision | Post-retirement product/engineering decision |

Do Phase 1 first. It's the only gap with a live correctness bug (a schema field, `resume_checkpoint_id`, that is always empty). Phases 2–4 can run in parallel once Phase 1's checkpointer is in place, since Phase 2 needs a `thread_id` concept that Phase 1 introduces anyway.

---

## Phase 1 — Real Human-in-the-Loop via `interrupt()` + Checkpointer

### 1.1 Add the checkpointer dependency

Your `DATABASE_URL` defaults to `sqlite:///./fronei.db` and `infra/docker-compose.yml` mounts a single persistent volume (`db_data:/data`) for a single API container — there's no Postgres in the stack today. Match that architecture rather than introducing a new datastore.

```toml
# apps/api/pyproject.toml — add to [project.dependencies]
"langgraph-checkpoint-sqlite>=1.0.0",
```

Run `uv lock && uv sync` after editing.

**Decision — SqliteSaver vs Postgres:** `SqliteSaver` (sync) is explicitly documented as not safe across multiple threads without serialization, and your SSE endpoint (`routers/agent.py::stream_turn`) already runs each turn on a background `Thread` feeding a `Queue`. Two options:

- **Option A (recommended for current scale):** One process-wide `SqliteSaver` instance behind a `threading.Lock`, backed by the same mounted volume as `fronei.db` (e.g. `checkpoints.db` in the same `/data` directory). Matches your existing single-instance deployment model exactly.
- **Option B (if you scale to multiple API instances/workers):** `langgraph-checkpoint-postgres`'s `PostgresSaver`, which is safe for concurrent access across processes. Defer this until you actually run >1 API replica — don't add Postgres infra speculatively.

Implement Option A now; the swap to Option B later is a one-line change (`get_checkpointer()` factory, below) because both implement the same `BaseCheckpointSaver` interface.

### 1.2 Create a checkpointer factory

New file: `app/services/agent/langgraph_runtime/checkpointer.py`

```python
from __future__ import annotations

import sqlite3
import threading
from functools import lru_cache

from langgraph.checkpoint.sqlite import SqliteSaver

from app.config import get_settings

_lock = threading.Lock()


class _LockedSqliteSaver(SqliteSaver):
    """SqliteSaver is documented as unsafe across threads without external
    serialization. Fronei's SSE bridge (routers/agent.py) runs each turn on
    its own Thread, so every checkpoint read/write must be serialized here.
    """

    def put(self, *args, **kwargs):
        with _lock:
            return super().put(*args, **kwargs)

    def put_writes(self, *args, **kwargs):
        with _lock:
            return super().put_writes(*args, **kwargs)

    def get_tuple(self, *args, **kwargs):
        with _lock:
            return super().get_tuple(*args, **kwargs)

    def list(self, *args, **kwargs):
        with _lock:
            return list(super().list(*args, **kwargs))


@lru_cache(maxsize=1)
def get_checkpointer() -> _LockedSqliteSaver:
    settings = get_settings()
    path = settings.langgraph_checkpoint_db_path  # new setting, see 1.3
    conn = sqlite3.connect(path, check_same_thread=False)
    saver = _LockedSqliteSaver(conn)
    saver.setup()  # creates tables on first run; idempotent
    return saver
```

Add the setting in `app/config.py` near the other LangGraph settings:

```python
    # LangGraph checkpoint store — required for interrupt()-based human-in-the-loop
    # (budget-approval pause/resume). Same volume as DATABASE_URL in docker-compose.
    langgraph_checkpoint_db_path: str = "./langgraph_checkpoints.db"
```

Update `infra/docker-compose.yml` to point this at the mounted volume, mirroring `DATABASE_URL`:

```yaml
    environment:
      DATABASE_URL: sqlite:////data/fronei.db
      LANGGRAPH_CHECKPOINT_DB_PATH: /data/langgraph_checkpoints.db
```

### 1.3 Wire the checkpointer into `graph.compile()`

`app/services/agent/langgraph_runtime/graph.py`, in `build_research_graph`:

```python
from app.services.agent.langgraph_runtime.checkpointer import get_checkpointer

def build_research_graph(...) -> Any:
    ...
    return graph.compile(checkpointer=get_checkpointer())
```

(This line also gets replaced again in Phase 2 when the graph moves to compile-once-at-import — leave it here for now so Phase 1 is independently shippable and testable.)

### 1.4 Give every run a stable `thread_id`

LangGraph resumes by matching `config["configurable"]["thread_id"]` against the checkpoint store — not by the `run_id` you already generate for telemetry. Reuse `run_id` as the `thread_id` so you don't introduce a second identifier:

`app/services/agent/langgraph_runtime/runtime.py`, `run_langgraph_research`:

```python
def run_langgraph_research(request: Any, tools: Any, progress: Any = None) -> dict[str, Any]:
    run_id = new_id("lgrun")
    config = {"configurable": {"thread_id": run_id}}
    compiled = build_research_graph(run_id=run_id, request=request, progress=progress, tools=tools)
    final_state = compiled.invoke(
        {"request_message": getattr(request, "message", ""), "visited_nodes": [], "artifacts": {}},
        config=config,
    )
    ...
```

`run_stub_graph` in `graph.py` needs the same `config=` threaded through — update its signature to accept and forward `config`.

### 1.5 Replace the "END on approval" shortcut with `interrupt()`

Current behavior (`nodes.py::budget_gate`, `graph.py`'s `_budget_gate_router`): `REQUIRE_HUMAN_APPROVAL` routes straight to `END`. Nothing is preserved to resume from. Replace this with a genuine pause.

`nodes.py::budget_gate` — after computing `decision`, instead of only returning a dict, call `interrupt()` when approval is required:

```python
from langgraph.types import interrupt

def budget_gate(state, *, run_id, request, tools=None, progress=None) -> dict:
    ...
    updates: dict = {"budget_decision": decision}
    if decision == BudgetDecision.REQUIRE_HUMAN_APPROVAL:
        continuation_budget = budget.max_cost_usd
        pause_contract = {
            "pause_reason": f"Cost ceiling reached: ${cost:.4f} spent against ${budget.max_cost_usd:.4f} limit.",
            "required_additional_budget_usd": continuation_budget,
            "resume_checkpoint_id": run_id,   # now a REAL, resumable identifier
            "audit_event_id": new_id("lgpause"),
            "paused_at": datetime.datetime.utcnow().isoformat() + "Z",
        }
        updates["pause_contract"] = pause_contract
        emit_graph_event(
            progress, run_id=run_id, node_name="budget_gate",
            message="Paused for human budget approval.", **pause_contract,
        )
        # Suspends here. LangGraph persists `updates` merged into state via the
        # checkpointer BEFORE raising, so pause_contract survives the pause.
        approval = interrupt(pause_contract)
        # Execution only reaches this line after Command(resume=...) is sent
        # to the SAME thread_id. `approval` is exactly what resume=... passed.
        updates["approval_contract"] = approval
        updates["budget_decision"] = BudgetDecision.CONTINUE
    ...
    return updates
```

Important LangGraph semantics to respect here (get these wrong and resume silently reruns the wrong thing):
- `interrupt()` re-executes the **entire node** from the top on resume, not just the line after the call. Keep everything before `interrupt()` in `budget_gate` idempotent (it already is — pure reads of `state`) and put nothing with a side effect (LLM calls, tool calls) before the `interrupt()` line in this node. `budget_gate` already satisfies this; don't let scope creep add logic above the `interrupt()` call later.
- Because of that re-execution rule, do not move `interrupt()` into `synthesize`/`repair`/any node that calls `synthesize_answer`/`repair_research_answer` without first isolating the LLM call behind a check — those nodes are not currently interrupt-safe.

### 1.6 Remove the `_budget_gate_router` "requires_approval → END" branches

`graph.py`, both `add_conditional_edges` calls that reference `budget_gate_pre_synthesis` / `budget_gate_pre_repair` currently map `"requires_approval": END`. With `interrupt()` handling the pause inline inside the node, the graph never actually reaches the router in the approval case — execution stops at `interrupt()` before the node even returns. Simplify the router and edge maps:

```python
def _budget_gate_router(state: ResearchGraphState) -> str:
    decision = state.get("budget_decision")
    if decision == BudgetDecision.STOP_WITH_GAPS:
        return "stop_with_gaps"
    return "continue"   # REQUIRE_HUMAN_APPROVAL never reaches here anymore —
                         # interrupt() suspended the node before this point.

# and the edge maps drop "requires_approval": END entirely:
graph.add_conditional_edges(
    "budget_gate_pre_synthesis", _budget_gate_router,
    {"continue": "synthesize", "stop_with_gaps": END},
)
```

Keep `BudgetDecision.REQUIRE_HUMAN_APPROVAL` in the enum (it's still the value read by API responses and tests) — just recognize it's now resolved inside the node, not by the router.

### 1.7 New API surface: query pause state, submit approval

New router, or extend `app/routers/agent.py`. Two endpoints:

```python
@router.get("/research/{run_id}/pause")
def get_pause_state(run_id: str, user=CurrentActiveUser) -> dict:
    """Returns the PauseContract if the run is currently interrupted, else 404."""
    from app.services.agent.langgraph_runtime.graph import build_research_graph
    from app.services.agent.langgraph_runtime.checkpointer import get_checkpointer
    checkpointer = get_checkpointer()
    config = {"configurable": {"thread_id": run_id}}
    state = checkpointer.get_tuple(config)
    if state is None or not state.checkpoint.get("pending_sends"):
        raise HTTPException(404, "No paused run found for this run_id.")
    # LangGraph surfaces the interrupt payload via graph.get_state(config).tasks[i].interrupts
    ...


@router.post("/research/{run_id}/approve")
def approve_research(run_id: str, body: ApprovalRequest, admin=RequireAdmin) -> dict:
    """Resumes a paused run with an ApprovalContract, then runs it to completion
    (or the next pause)."""
    from langgraph.types import Command
    from app.services.agent.langgraph_runtime.runtime import _resume_langgraph_research

    approval_contract = {
        "approved_by": admin.user_id,
        "approved_at": datetime.datetime.utcnow().isoformat() + "Z",
        "updated_budget_ceiling_usd": body.updated_budget_ceiling_usd,
        "approval_audit_event_id": new_id("lgapprove"),
    }
    result = _resume_langgraph_research(run_id, Command(resume=approval_contract))
    return result
```

`_resume_langgraph_research` in `runtime.py` mirrors `run_langgraph_research` but calls `compiled.invoke(command, config=config)` where `command` is the `Command(resume=...)` object instead of the initial state dict, and it must rebuild the graph for the same `run_id`/`request`/`tools` — which means the original `request`/`tools` need to be persisted alongside the checkpoint (they currently live only in the `functools.partial` closures baked into the compiled graph — see Phase 2, which fixes this properly by moving `request`/`tools` into `config["configurable"]` so they're naturally available on resume without a second lookup).

**Sequencing note:** step 1.7's resume path is genuinely awkward until Phase 2 lands, because `request`/`tools` are currently captured in per-request `functools.partial` closures, not in graph state or config — so a resume request in a fresh process (or after a restart) has no way to reconstruct them. For Phase 1 alone, persist `request` (serialized) and enough of `tools` config to reconstruct a `Tools` instance in your existing turn-persistence table (`app/services/agent/persistence.py`), keyed by `run_id`, and load it in `_resume_langgraph_research`. This becomes unnecessary once Phase 2 moves that data into `config["configurable"]`, at which point you can delete the extra persistence table.

### 1.8 Update `ResearchGraphState` / result plumbing

`state.py` already has `PauseContract`/`ApprovalContract` — no schema change needed. Just note in the docstring that `resume_checkpoint_id` is now populated with the real `thread_id`, not a placeholder.

`runtime.py::run_langgraph_research`'s existing dead-answer handling (the block checking `if judge_result is None`) stays as the fallback for `STOP_WITH_GAPS` and any other early-`END` path, but the `REQUIRE_HUMAN_APPROVAL` branch of that block becomes unreachable in normal operation (interrupt() never lets the graph reach `END` with that decision anymore) — keep it as a defensive fallback in case a future edge accidentally routes there, but add a comment explaining why it should no longer fire.

### 1.9 Testing plan

Add `tests/test_langgraph_runtime_slice_6_hitl.py`:

1. Unit: force `budget.max_cost_usd` to `0` so `budget_gate` always decides `REQUIRE_HUMAN_APPROVAL`; assert `compiled.invoke(...)` returns a state whose `__interrupt__` key is populated (LangGraph's convention) rather than reaching `END`.
2. Integration: invoke → assert interrupted → call `compiled.invoke(Command(resume={...}), config=same_config)` → assert the graph continues from `synthesize`, not from `brief` (verify via `visited_nodes` not containing duplicate early-stage entries, and via a call-count assertion that `generate_research_brief` was called exactly once across both invocations).
3. Regression: assert a **non**-approval-required run (cheap budget) never touches the checkpointer's `put_writes` for an interrupt (i.e., normal runs pay no new overhead beyond checkpoint writes already required for durability).
4. Failure mode: process restart between pause and resume — kill and re-instantiate `get_checkpointer()` pointing at the same file, then resume; assert it still works (this is the actual scenario `resume_checkpoint_id` needs to support and the one the current code silently doesn't).

### 1.10 Rollback

Superseded: `FRONEI_ORCHESTRATOR=legacy` no longer exists. Rollback now means
reverting the LangGraph runtime change or deploying a previous application
revision, not flipping an in-process orchestrator selector.

---

## Phase 2 — Compile Once, Inject Per-Request Data via `config`

### 2.1 The problem precisely

`build_research_graph()` is called inside `run_stub_graph()` on every invocation (`graph.py:175`), which is called once per research turn from `runtime.py:72`. Every call re-registers 15 nodes, rebuilds every edge, and re-validates the graph shape — pure overhead, since the graph topology never changes per request. The only things that vary per request are `run_id`, `request`, `tools`, `progress` — currently baked in via `functools.partial` at node-registration time (`graph.py:73-79`).

### 2.2 Move per-request data into `config["configurable"]`

This is the standard LangGraph mechanism for exactly this problem, and it composes correctly with `interrupt()`/`Command(resume=...)` from Phase 1 (config is preserved across the pause/resume boundary automatically, whereas closure-captured data is not — this is *why* Phase 1's resume path needed the workaround in 1.7).

Node signature change — every node in `nodes.py` currently takes `run_id`, `request`, `tools`, `progress` as bound kwargs. Change them to read from LangGraph's injected `config` parameter instead:

```python
from langgraph.runtime import get_config  # or accept `config: RunnableConfig` as a node arg

def brief(state: ResearchGraphState, config: RunnableConfig) -> dict:
    cfg = config["configurable"]
    run_id, request, tools, progress = cfg["run_id"], cfg["request"], cfg["tools"], cfg["progress"]
    ...
```

This is a mechanical find-and-replace across all 15 node functions in `nodes.py`, plus `dispatch_search_router`, `budget_gate` (both instances), and the two router functions in `graph.py`. Do it in one PR — partial migration means half the nodes read from closures and half from config, which is worse than either pure approach.

### 2.3 Compile once, at import time

`graph.py`:

```python
_COMPILED_GRAPH = None

def get_compiled_research_graph():
    global _COMPILED_GRAPH
    if _COMPILED_GRAPH is None:
        graph: StateGraph = StateGraph(ResearchGraphState)
        for node_name in nodes.NODE_ORDER:
            graph.add_node(node_name, getattr(nodes, node_name))   # no functools.partial anymore
        graph.add_node("budget_gate_pre_synthesis", nodes.budget_gate)
        graph.add_node("budget_gate_pre_repair", nodes.budget_gate)
        # ... same edges as before, but router functions also take (state, config)
        # instead of bound run_id/request/tools/progress kwargs.
        _COMPILED_GRAPH = graph.compile(checkpointer=get_checkpointer())
    return _COMPILED_GRAPH
```

`runtime.py::run_langgraph_research`:

```python
def run_langgraph_research(request: Any, tools: Any, progress: Any = None) -> dict[str, Any]:
    run_id = new_id("lgrun")
    config = {
        "configurable": {
            "thread_id": run_id,
            "run_id": run_id,
            "request": request,
            "tools": tools,
            "progress": progress,
        }
    }
    compiled = get_compiled_research_graph()
    final_state = compiled.invoke(
        {"request_message": getattr(request, "message", ""), "visited_nodes": [], "artifacts": {}},
        config=config,
    )
    ...
```

This also directly resolves Phase 1's resume gap (1.7): on resume, `config["configurable"]` is restored from the checkpoint automatically by LangGraph, so `request`/`tools` are available without the extra persistence table — **delete that table's usage** once this phase lands, if you built it as a stopgap in Phase 1.

**Caveat — non-serializable objects in config:** `request` (a Pydantic `TurnRequest`) and `tools` (a `Tools` instance, likely holding HTTP clients) go into `config["configurable"]`, which LangGraph does *not* checkpoint by default (only graph *state* is persisted, not config) — so this is safe for the compile-once/no-interrupt path. But if a node interrupts (Phase 1's `budget_gate`), resume must re-supply the *same* `config` including `tools`/`request` on the `Command(resume=...)` call — LangGraph does not reconstruct config from the checkpoint. This means your resume endpoint (1.7) must still be able to reconstruct `request`/`tools`, just via config-passing at resume time rather than via closures. Net effect: this phase doesn't eliminate the need to persist `request`/`tools` metadata somewhere retrievable by `run_id` for the resume endpoint — it just moves *where* the data flows from (config, not closures) during normal non-interrupted execution. Keep a lightweight `run_id → {request_json, tool_config}` row in `persistence.py` specifically to support the resume endpoint; that's a legitimate small persistence addition, not a workaround.

### 2.4 Testing plan

- Re-run the full `test_langgraph_runtime_slice_*` suite unmodified — behavior must be identical, only the plumbing changed. This is the regression gate for this phase.
- Add a test asserting `get_compiled_research_graph()` returns the same object identity across two calls (proves compile-once).
- Add a concurrency test: run two `run_langgraph_research()` calls with different `request`s on separate threads against the same compiled graph object; assert no cross-request field bleed (this is the scenario compile-once introduces risk for for the first time — shared compiled graph, concurrent invokes — and it's exactly what `config["configurable"]` is designed to isolate correctly, but verify it).

---

## Phase 3 — Native Streaming

### 3.1 The problem precisely

`run_langgraph_research()` uses `compiled.invoke()` (blocks until the full graph finishes) and hardcodes `"answer_streamed": False` in its return dict. `runtime.py::_run_research_subtree` then checks that flag and, for `research_level == "deep"`, calls `_emit_buffered_answer()` — which chunks the already-complete answer text and sleeps between chunks to fake a typing animation. Meanwhile progress events flow through a hand-built `ProgressCallback` threaded via `functools.partial`/`config["configurable"]["progress"]` into `emit_graph_event()` calls scattered through every node.

LangGraph's `.stream()` gives you both of these for free: `stream_mode="updates"` yields each node's state delta as it completes (replaces `emit_graph_event`'s node-progress role), and a custom stream writer (`get_stream_writer()`) inside `synthesize`/`repair` can emit token-level deltas as they arrive from `model_client.stream_complete()` (replaces the buffered-replay hack with genuine streaming).

### 3.2 Switch node-level progress to `stream_mode="updates"`

Once Phase 2's config-based node signatures are in place, `emit_graph_event()` calls can largely be deleted — the caller iterating `compiled.stream(..., stream_mode="updates")` already receives `{node_name: {field: delta, ...}}` after every node. You only need `emit_graph_event` for messages/semantics not derivable from the state delta (e.g., the human-readable `message` string). Keep a slimmed-down version of it, but have it write to LangGraph's custom stream channel instead of calling a bound `progress` function directly:

```python
from langgraph.config import get_stream_writer

def emit_graph_event(*, node_name: str, message: str, **data) -> None:
    writer = get_stream_writer()   # no-op if not inside a .stream() call
    if writer is None:
        return
    writer({"node_name": node_name, "message": message, **data})
```

This removes `progress` as a value that needs to be threaded through every node signature and every `functools.partial`/config entry — one more simplification on top of Phase 2.

### 3.3 Token-level streaming inside `synthesize`/`repair`

`nodes.py::synthesize` currently calls `synthesize_answer(request, plan, evidence)`, which internally calls a non-streaming `complete()`-style function. To get real token deltas, `synthesize_answer` (in `research_synthesis.py`) needs a streaming variant using `model_client.stream_complete()` (already used elsewhere, e.g. `runtime.py::_stream_model_response`) — and the node should push each `ModelDelta` to the stream writer as it arrives:

```python
def synthesize(state, config: RunnableConfig) -> dict:
    writer = get_stream_writer()
    buffered = ""
    response = None
    for item in model_client.stream_complete(messages, role="synthesis", ...):
        if isinstance(item, model_client.ModelDelta):
            buffered += item.text
            if writer:
                writer({"node_name": "synthesize", "answer_delta": item.text})
        else:
            response = item
    ...
```

This is the piece that actually deletes `_emit_buffered_answer` in `runtime.py` — the graph itself now produces real deltas, so the "replay the final answer with sleeps" workaround has nothing left to compensate for.

### 3.4 Wire graph streaming into the existing SSE bridge

`routers/agent.py::stream_turn` already has a queue/thread bridge pattern from
`runtime.run_stream()`. `runtime.py::_run_research_subtree` now iterates the
graph stream and re-yields `StreamEnvelope`s directly:

```python
from app.services.agent.langgraph_runtime import stream_langgraph_research

gen = stream_langgraph_research(request, self.tool_registry.tools, progress)
return (yield from self._forward_langgraph_stream(gen, progress))
```

`_run_research_subtree` is a generator (`yield from` elsewhere in `runtime.py`), so this fits the existing control flow without changing `stream_turn`'s consumption code in `routers/agent.py` at all — that's the payoff of matching the existing `StreamEnvelope` contract instead of inventing a new one.

### 3.5 Testing plan

- Superseded: the old golden-set parity harness (`langgraph_parity.yml`) has
  been retired with the legacy runtime. Use the LangGraph eval harness,
  LangSmith runs, and focused streaming/maturity tests as the regression gate.
- New test: assert `answer_delta` events are emitted incrementally (not all at once) by asserting wall-clock spacing between the first and last `answer_delta` SSE event in an integration test — mirrors the intent of the docstring comment currently on `_emit_buffered_answer` about giving the client's typing animation "genuine wall-clock spacing," except now it's real instead of simulated.
- Load test: confirm `.stream()` doesn't hold the checkpointer lock (from Phase 1's `_LockedSqliteSaver`) for the duration of the whole run — only for the brief writes between node completions. This matters because `.stream()` naturally checkpoints more frequently than a single `.invoke()` did.

---

## Phase 4 — Production LangSmith Tracing

Currently `LANGCHAIN_TRACING_V2` is only set inside `langsmith_evals.py`'s eval runner (`app/config.py:153` — `langchain_tracing_v2: bool = False` by default, and the comment correctly notes this needs deliberate opt-in for privacy/data-retention reasons). Extend it to cover normal production graph runs, gated the same deliberate way.

### 4.1 Config

No new setting needed — `langchain_tracing_v2` already exists. The gap is that nothing sets the env var from it outside the eval path. Add to `app/main.py` (or wherever settings are loaded at startup):

```python
settings = get_settings()
if settings.langchain_tracing_v2 and settings.langsmith_api_key:
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGCHAIN_PROJECT"] = settings.langchain_project
```

### 4.2 Run metadata

Tag each run so traces are filterable by the dimensions you already care about (research_level, orchestrator, environment):

```python
config["metadata"] = {
    "research_level": getattr(request, "research_level", None),
    "orchestrator": "langgraph",
    "env": get_settings().app_env,
}
config["tags"] = [f"research_level:{getattr(request, 'research_level', 'unknown')}"]
```

### 4.3 Privacy

Before enabling in production, review what's in `ResearchGraphState` that would get traced verbatim: `request_message` (raw user query) and full `evidence`/`sources` content. If that's not acceptable for production tracing, use LangSmith's redaction hooks (`hide_inputs`/`hide_outputs` on the client, or a `process_inputs` callback) rather than leaving tracing off — partial visibility beats none, and the existing `langsmith_evals.py` code already treats this as a real constraint (see its docstring), so don't relax that discipline just because this is now the production path rather than the eval path.

### 4.4 Rollout

Enable for staging first. In production, consider sampling (LangSmith supports trace sampling via `LANGCHAIN_TRACING_SAMPLING_RATE`) rather than 100% if volume/cost is a concern — start at 100% given your current traffic is presumably modest, and dial down only if it becomes a cost line item.

---

## Phase 5 — Legacy `research_lead.py` Retirement

**Status:** retired directly by explicit decision on July 2, 2026; the parity-gate criteria below are superseded historical context.

The original parity-gate criteria below were superseded by an explicit direct
retirement decision after LangGraph research proved stable in production.

**Completed mechanical steps:**
1. Removed the orchestrator override settings and escape-hatch flag.
2. Deleted `app/services/agent/research_lead.py` and its direct-only callers.
3. Collapsed `runtime.py::_run_research_subtree` to a single LangGraph stream path.
4. Retired `langgraph_parity.yml`.
5. Removed `VALID_ORCHESTRATORS`, override helpers, and the admin parity endpoints.
6. Updated `domain_function_side_effect_audit.md` with `removed` rows.

---

## Phase 6 — Strategic Decision: Extend Graph Modeling Beyond Research

This is a decision, not a task. `runtime.py` (~1,600 lines) hand-rolls routing, direct-answer, document, and deck orchestration with the same concerns LangGraph already solves (progress events, tool sequencing, thread-based heartbeat bridging for blocking calls). Before doing this work, resolve it explicitly:

| Question | If yes → | If no → |
|---|---|---|
| Does the document/deck path need parallelism LangGraph's `Send` would simplify (e.g., parallel slide generation)? | Strong case for a graph — this is exactly the `search_worker` fan-out pattern already proven in research. | Manual sequential code is fine; a graph adds ceremony without payoff. |
| Is the previous `_with_heartbeat` bridge still needed? | No. LangGraph stream advancement now emits `research_progress` heartbeats during quiet graph steps. | N/A — retired. |
| Do you need LangSmith trace visibility into routing/document decisions the way you now have for research? | Graph adoption gets you that for free (Phase 4). | Direct instrumentation is cheaper than a migration. |
| Is the team's LangGraph fluency (post Phases 1–5) high enough to extend it without repeating the six gaps this review found? | Proceed. | Wait — a second rushed migration recreates this same audit. |

Recommendation: revisit this after the research-runtime retirement with real
data from current LangGraph traces and turn latencies, not from the removed
legacy bridge.

---

## Appendix A — File-by-File Change Manifest

| File | Phase 1 | Phase 2 | Phase 3 | Phase 4 | Phase 5 |
|---|---|---|---|---|---|
| `apps/api/pyproject.toml` | add `langgraph-checkpoint-sqlite` | — | — | — | — |
| `app/config.py` | add `langgraph_checkpoint_db_path` | — | — | wire `langchain_tracing_v2` at startup | remove orchestrator-override settings |
| `langgraph_runtime/checkpointer.py` | new file | — | — | — | — |
| `langgraph_runtime/graph.py` | wire checkpointer; simplify router | compile-once singleton; router sig change | `.stream()` call site | — | — |
| `langgraph_runtime/nodes.py` | `interrupt()` in `budget_gate` | all node signatures → `config` | stream writer in `synthesize`/`repair` | metadata/tags | — |
| `langgraph_runtime/events.py` | — | — | rewrite `emit_graph_event` to use stream writer | — | — |
| `langgraph_runtime/runtime.py` | `thread_id`, resume path | config-based invoke | generator rewrite of research subtree call | — | delete branching |
| `routers/agent.py` | new pause/approve endpoints | — | — (already compatible) | — | — |
| `routers/evals.py` | — | — | — | — | delete promote endpoints |
| `research_synthesis.py` | — | — | streaming `synthesize_answer` variant | — | — |
| `research_lead.py` | — | — | — | — | deleted |
| `infra/docker-compose.yml` | mount checkpoint DB path | — | — | — | — |
| `.github/workflows/langgraph_parity.yml` | — | — | superseded | — | retired |
| `domain_function_side_effect_audit.md` | — | — | — | — | mark legacy rows removed |

## Appendix B — Testing/Validation Matrix

| Layer | Phase 1 | Phase 2 | Phase 3 |
|---|---|---|---|
| Unit | interrupt fires on `REQUIRE_HUMAN_APPROVAL` | node signature migration is mechanical — full existing suite is the gate | stream writer emits correct payload shape |
| Integration | pause → resume across process restart | compiled-graph identity + concurrent-invoke isolation | SSE deltas arrive incrementally, not batched |
| Regression | non-approval runs unaffected | full `test_langgraph_runtime_slice_*` suite unchanged | LangGraph eval/maturity suite |
| Load | checkpointer lock contention under concurrent turns | — | checkpointer write frequency under `.stream()` |

## Appendix C — Rollout Order

Superseded rollout note: Phases 1–5 have landed for the research runtime.
There is no `FRONEI_ORCHESTRATOR` flag, parity promotion gate, or promote/revert
admin endpoint left. Future rollout work should focus on live smoke tests,
LangGraph eval quality, LangSmith trace review, and the separate Phase 6
decision about whether non-research orchestration should also move to graphs.
