# Fronei Latency Audit — June 2026

**Scope:** End-to-end latency review across (1) simple chat turns, (2) deep research, (3) AgentDeck v2 document/PPTX generation, (4) frontend perceived latency. Goal: identify the highest-leverage fixes for both real and perceived response time.

**Method:** Static review of `chat_pipeline.py`, `planner.py`, `research_orchestrator.py`, `compose_docplan.py`, `documents.py`, `apps/web/app/page.tsx`. Numbers are estimates from code structure (timeouts, fallback chains, loop bounds), not live profiling — treat as directional, validate with tracing (see §6).

---

## 1. Current-State Latency Profile

| Workload | Today (typical) | Today (worst case) | Dominant cost |
|---|---|---|---|
| Simple chat reply | 2.1 – 6.2 s | up to ~12 s (model fallback storms) | 2 sequential LLM calls (planner + worker) |
| Deep research (deep mode) | ~200 – 280 s | ~350 s+ | 3 search/eval iterations + synthesis + verification, mostly sequential |
| Presentation (8–12 slides, standard) | ~46 s | ~100 s (executive, full repairs) | 4 sequential planning LLM calls + repair/QA loop |
| Perceived (research/doc) | +2–5 s human delay for plan-confirmation modal, then 60–200 s blank spinner | — | No progressive feedback |

**The core architectural pattern across all three pipelines is the same problem**: every stage is a sequential LLM call that blocks the next stage, with no parallelism across independent sub-tasks (slides, sources, beats) and no intermediate signal to the user. This is the single biggest lever — bigger than any individual model/timeout tweak.

---

## 2. Findings by Pipeline

### 2.1 Simple Chat Turn (2.1–6.2 s)

```
request → auth/DB (~20ms) → build_context (~20-100ms)
        → PLANNER LLM CALL  (1-3s, sequential fallback chain)
        → plan_gate.evaluate (negligible)
        → choose_route (negligible)
        → WORKER LLM CALL   (1-3s, sequential fallback chain)
        → persist + return
```

| # | Issue | Impact | Fix complexity |
|---|---|---|---|
| 1 | **Planner LLM runs for every message**, including trivial follow-ups ("yes", "thanks", "ok continue") | +1–3 s on ~30%+ of turns | Low |
| 2 | **Sequential model fallback chains** for both planner and worker — each failed model is a full 1–3 s round trip before the next is tried | +3–9 s on degraded-model days | Medium |
| 3 | Memory ranking (`rank_memories`) recomputed every turn | +20–100 ms | Low |
| 4 | No caching/memoization of planner output across retried/duplicate `client_request_id`s | wasted 1–3 s on client retries | Low |

### 2.2 Deep Research (~200–280 s)

```
query plan (1 LLM, ~5s)
  → direct URL fetch (sequential, ~12s × up to 4)
  → up to 3 iterations:
       question workers (parallel, 5-6 threads)
         → search providers (Tavily→Brave→DDG, SEQUENTIAL fallback)
         → crawl candidates (SEQUENTIAL within worker, 12s timeout × 8)
       claim extraction (parallel, Haiku)
       gap/contradiction eval (1 LLM, ~8s)
  → synthesis (1 LLM, ~20-30s)
  → verification (1 LLM, ~15-20s, conditional)
```

| # | Issue | Impact | Fix complexity |
|---|---|---|---|
| 5 | **URL crawling is sequential inside each question worker** (up to 8 URLs × 12 s timeout) | ~40–60 s per pass — single biggest research lever | Medium |
| 6 | **Search provider fallback (Tavily→Brave→DDG) is sequential per query variant** | ~10–20 s per worker pass | Medium |
| 7 | Cache key is query-signature-specific — stable sources (gov/docs sites) aren't reused across differently-worded queries | 20–40 s lost on repeat/related research | Medium |
| 8 | No partial synthesis after iteration 1 even when confidence is already high | 30–40 s of avoidable "silence" | Medium |
| 9 | "Deep" mode budgets (16 sources, 5 questions, 3 passes) may be oversized relative to marginal value of sources 13–16 | 15–25 s | Low (config) |

### 2.3 Document / PPTX Generation (~46 s typical, ~100 s worst case)

```
generate_narrative_plan   (LLM, 5-8s)
  → generate_presentation_plan (LLM, 6-10s)
    → generate_design_plan      (LLM, 4-7s)
      → generate_doc_plan (2 LLM sub-calls, 8-12s)
        → compose (mechanical, ~1s)
          → render via Node subprocess (1-3s, cold-spawned per request)
            → LibreOffice QA (3-8s, on critical path)
              → repair loop (0-5 iterations × ~4.6-11s, standard cap=2, exec cap=5)
                → vision judge (executive only, serial per-slide, 0.8-1.2s × N)
```

| # | Issue | Impact | Fix complexity |
|---|---|---|---|
| 10 | **4 fully sequential planning LLM calls** (narrative → presentation → design → doc plan), each ~5–12 s, each depending on the prior's full output | ~23–37 s of the ~46 s typical case | High (requires re-architecting planner contract) |
| 11 | **Node renderer cold-spawned per request** (no persistent process/daemon) | 0.3–0.5 s × every render + every repair iteration | Medium |
| 12 | **LibreOffice QA is synchronous and on the critical path** even though it only feeds the optional vision judge | 3–8 s × (1 + repair iterations) | Medium |
| 13 | Repair loop caps (standard=2, executive=5) re-run compose+render+QA in full each time | up to 40 s in executive worst case | Low (config) + Medium (true fix) |
| 14 | Vision judge calls slides one at a time | a few seconds | Low |

### 2.4 Perceived Latency (Frontend)

| # | Issue | Impact | Fix complexity |
|---|---|---|---|
| 15 | **Plan-confirmation modal blocks all research/document turns**, even high-confidence/obvious plans — adds a full round trip + 2–5 s human reaction time | 100% of research/doc turns | Low–Medium |
| 16 | **Zero progressive feedback during 60–200 s generation** — just a spinner | Dominates *felt* slowness | Medium |
| 17 | No "thinking" placeholder for the 100–300 ms gap before first token on simple chat | Minor, but free to fix | Low |
| 18 | Sequential file-extraction before send (for attachments) | Adds latency proportional to attachment count | Low |

---

## 3. Why This Feels Worse Than the Numbers Suggest

Two compounding effects explain why "even simple queries feel painfully slow":

1. **Every turn pays a planner tax.** The planner LLM call (1–3 s) runs unconditionally before the user sees anything, even for "thanks" or "go on." This is the single most-felt tax because it affects 100% of interactions.
2. **Long workflows are silent.** A 60–200 s document generation with no intermediate signal reads as "broken" to most users well before it reads as "slow." Perceived latency here is arguably a bigger problem than actual latency.

---

## 4. Prioritized Roadmap

Scored on a simple Impact (latency seconds saved or perception improvement) × Effort matrix. "Effort" is rough engineering complexity, not calendar time.

### Tier 1 — Quick Wins (days, low risk, do first)

| Fix | Addresses | Est. impact |
|---|---|---|
| **T1.1** Skip planner LLM for trivial/continuation messages (heuristic: short message, no new topic signal, recent active_task present) | #1 | −1–3 s on ~30% of chat turns |
| **T1.2** Auto-bypass plan-confirmation modal for high-confidence plans; reserve modal for ambiguous/low-confidence or destructive actions | #15 | Removes 2–5 s + a full round trip from majority of research/doc turns |
| **T1.3** Add "thinking" placeholder immediately on send | #17 | Closes the 100–300 ms dead-air gap |
| **T1.4** Trim "deep" research budgets (16→12 sources, re-tune pass count) and standard-mode repair cap (2→1) | #9, #13 | −15–30 s, low risk |
| **T1.5** Parallelize vision-judge calls (batch instead of serial per slide) | #14 | a few seconds |

### Tier 2 — Structural Parallelism (1–3 weeks each, highest ROI)

| Fix | Addresses | Est. impact |
|---|---|---|
| **T2.1** Parallelize URL crawling within each research question worker (thread pool instead of sequential loop) | #5 | −40–60 s per research run — **single largest research win** |
| **T2.2** Race search providers (Tavily/Brave/DDG) instead of sequential fallback | #6 | −10–20 s per pass |
| **T2.3** Move LibreOffice QA off the synchronous critical path — render returns immediately; QA/vision-repair run async and patch the artifact in place, with the UI updating when ready | #12, #16 | −3–8 s per iteration on critical path; also unlocks progressive UI |
| **T2.4** Keep Node renderer warm (persistent daemon/HTTP service instead of per-request spawn) | #11 | −0.3–0.5 s × every render/repair (compounds with T2.3) |
| **T2.5** Stream progress events for research and document generation (stage-level: "Searching…", "Drafting slide 3 of 10…", "Quality-checking…") | #16 | Biggest perceived-latency win in the whole audit |
| **T2.6** Parallelize model fallback chains (race primary + first fallback with short stagger instead of full sequential retries) | #2 | −1–9 s on degraded-model events |

### Tier 3 — Architectural Rework (larger bets, plan deliberately)

| Fix | Addresses | Est. impact |
|---|---|---|
| **T3.1** Collapse the 4-stage sequential planner (narrative → presentation → design → doc plan) into fewer calls, or fan out per-slide/per-beat generation in parallel once the outline exists | #10 | −10–20 s of the ~25–37 s planning chain |
| **T3.2** Loosen research cache keys for stable, slow-changing sources (gov/docs/pricing pages) independent of exact query phrasing | #7 | −20–40 s on related/repeat research |
| **T3.3** Incremental/streaming synthesis — begin drafting once iteration-1 confidence is high, refine if iteration 2/3 surfaces contradictions | #8 | −30–40 s perceived, possibly real |

---

## 5. Expected Outcome

If Tier 1 + Tier 2 are implemented:

- **Simple chat**: ~2.1–6.2 s → ~1.5–4 s real, and *feels* instant due to T1.3.
- **Deep research**: ~200–280 s → ~120–180 s real, with progress streaming making the wait feel cooperative rather than broken.
- **Document generation (standard)**: ~46 s → ~30–35 s real; executive mode worst case ~100 s → ~60–70 s.
- **Plan-confirmation friction**: removed for the majority of turns.

Tier 3 is where you'd go after validating Tier 1/2 in production — it's the path to getting document generation closer to ~20–25 s and research closer to ~90–120 s, but requires re-contracting the planner output schema and is higher-risk.

---

## 6. Before Building: Instrument First

This audit is based on code-structure analysis, not live traces. Before committing engineering time to Tier 2/3, add lightweight per-stage timing (already partially present via `usage_stats` and `exec_log_json`) to a dashboard segmented by: planner vs. worker LLM latency, per-model fallback frequency, research stage durations, and document pipeline stage durations (planning / compose / render / QA / repair iterations). This will confirm which of the above estimates are actually the dominant cost in production traffic and let you sequence Tier 2 by *measured* impact rather than estimated impact.

---

## 7. Suggested Next Steps (superseded by §8)

1. Add stage-level timing/telemetry (§6) — 1–2 days, unblocks everything else.
2. Ship Tier 1 (T1.1–T1.5) — low risk, immediately felt by users.
3. Pick T2.1 (research crawl parallelism) and T2.5 (progress streaming) as the first Tier-2 pair — they address the two workloads users complain about most (research, document generation) with the best impact/effort ratio.
4. Re-measure, then sequence remaining Tier 2 items and decide on Tier 3 scope.

---

## 8. Agreed Execution Plan (refined, owner: Subh)

Reviewed against the audit above. Diagnosis confirmed; three refinements adopted before build:

1. **Fast-path is narrow, not broad.** Don't bypass the planner for "simple-looking" messages generally — only for a deterministic allow-list of obvious, low-risk continuations (`thanks`, `ok`, `continue`, `explain that`, `make it shorter`, and similarly short follow-ups with no new tool/document/research signal). Everything else still goes through `run_planner()`. The planner is the product's brain; this is a guardrail, not a general bypass.
2. **Plan-confirmation degrades gracefully, not binary on/off.** High-confidence + low-risk plans skip the modal but still surface a quiet inline status chip ("Using web + creating PPTX") with cancel/adjust affordances — so the user retains control without the blocking round trip.
3. **PPTX QA/judge depth is mode-gated, not just async.**
   - Draft: no render QA at all.
   - Standard: deterministic plan/compose checks only; LibreOffice off the critical path.
   - Executive: full render QA + vision judge loop, explicitly framed in UI as a "polishing" pass.
4. **Tier 3 AgentDeck collapse is deferred and re-scoped.** Don't collapse narrative→presentation→design→doc-plan into fewer calls yet (quality risk). Instead, once storyline + design are locked, fan out per-slide content generation in parallel — preserves the planning quality bar while parallelizing the expensive part.

### Execution order

| Step | What | Maps to audit | Notes |
|---|---|---|---|
| 1 | **Instrument** — per-stage timing spans into `ConversationTurn`/`exec_log_json` (planner, web context, worker, research sub-stages, AgentDeck sub-stages incl. compose/render/QA/repair/vision judge), exposed as p50/p95 in admin dashboard | §6 | Blocks nothing else from starting, but should land first/in parallel |
| 2 | **Fast-path trivial chat** — deterministic pre-router allow-list ahead of `run_planner()` | T1.1 (narrowed) | First user-visible win |
| 3 | **Flip production defaults**: `pptx_render_qa_enabled=false`; vision judge restricted to `quality_mode="executive"`; standard repair cap 2→1; deep research max sources 16→10–12; no refinement pass on short answers | T1.4, T2.3 (partial) | Config-only, ship immediately |
| 4 | **Documents/research as durable progressive jobs** — return a job/status card immediately; stream milestone updates (`Found 8 sources` → `Extracted 23 claims` → `Drafting synthesis` → `Building 10-slide deck` → `Rendering` → `Quality checking` → `Ready`) | T2.5, T2.3 | Biggest perceived-latency win; supersedes the generic spinner |
| 5 | **Parallelize research crawling** — bounded-concurrency crawl within each question worker | T2.1 | Biggest real-latency research win |
| 6 | **Improve long-job UI** — render the milestone stream from Step 4 as real progress, not generic "working" | T2.5 | Pairs with Step 4 |
| 7 | **Warm renderer / async QA** (persistent Node daemon, LibreOffice off critical path) | T2.3, T2.4 | Deliberately last — do this *after* the job model from Step 4 exists, so we're optimizing the right boundary |
| 8 | **AgentDeck per-slide parallel fan-out** (post storyline/design lock) | T3.1 (rescoped) | Only after 1–7 land and are measured |

### Product principle (north star for all of the above)
> Chat feels instant. Research feels actively worked on. Document generation feels like a professional background production workflow, not a frozen chat response.

---

## 9. Implementation Log

### 2026-06-15 — Slice 1: instrumentation + default flips

**Shipped**

- Added normalized `ExecutionLog.stage_timings` for planner, web context, route selection, worker, document generation, and artifact build.
- Added normalized research-stage timings (`research_pipeline`, `research_followup`) to saved research execution logs.
- Added matching frontend `StageTiming` typing so execution logs can be surfaced/aggregated later.
- Added admin Dashboard latency-hotspots table from recent `stage_timings`, showing p50/p95/count by stage.
- Flipped `pptx_render_qa_enabled` default to `false`; render QA is no longer on the default critical path.
- Reduced AgentDeck standard repair cap from `2` to `1`; draft remains `0`, executive remains `5`.
- Reduced deep-research budget from `16` sources / `5` planned questions to `12` sources / `4` planned questions.
- Raised the chat refinement threshold from `50` words to `120` words so short/simple answers do not pay a second LLM pass.

**Verified**

- Backend `py_compile` clean for touched files.
- Web `npx tsc --noEmit -p tsconfig.json` clean.
- Targeted backend tests: `113 passed`.
- OpenAPI import check: `openapi ok`.

### 2026-06-15 — Slice 1b: turn profiler moved into Admin turns

**Shipped**

- Moved the turn-profiler UI out of Settings → Dashboard and into Admin → Turn profiler, next to the live turn controls.
- Added a turn-level / conversation-roll-up toggle so admins can inspect individual slow turns or aggregate latency/cost by unique conversation.
- Extended `/admin/turn-profiler` with `conversation_rollups`, including total latency, p95 latency, cost, token count, slowest turn, status counts, turn-kind counts, and aggregate bottleneck stage per conversation.
- Kept the profiler turn-level data intact: stage summaries, model summaries, slow turns, recommendations, unattributed latency, and per-turn bottlenecks.

**Verified**

- Web `npx tsc --noEmit -p tsconfig.json` clean.
- Targeted backend tests: `29 passed`.

### 2026-06-15 — Slice 2: quick wins + research crawl parallelism

**Shipped**

- Added a narrow deterministic trivial-continuation fast path before `run_planner()` for messages like `thanks`, `ok`, `continue`, `make it shorter`, etc., only when no tools/attachments/document/research flags are active.
- Relaxed plan confirmation for high-confidence, non-sensitive web/research recommendations. Sensitive research risk factors such as `legal_regulatory`, `medical`, and `financial` still confirm.
- Added immediate frontend “Thinking…” placeholder before the SSE `start` event arrives.
- Added explicit document milestones in the stream: document plan ready / preparing preview, and artifact rendering start.
- Parallelized direct URL crawling and per-question candidate crawling with bounded concurrency inside the research orchestrator.
- Added shared search-provider racing (`Tavily` / `Brave` / `DuckDuckGo`) for both normal web context and deep research, returning the first non-empty provider result instead of serial fallback.

**Still pending after this slice**

- Full durable background job split for research/document generation.
- Warm Node renderer.
- AgentDeck per-slide parallel fan-out after storyline/design lock.

**Verified**

- Full backend suite: `334 passed, 4 skipped`.
- Web `npx tsc --noEmit -p tsconfig.json` clean.
- Backend `py_compile` clean for touched Python modules.

### 2026-06-15 — Slice 3: executive vision-judge parallelism

**Shipped**

- Parallelized AgentDeck executive-mode vision judge calls across rendered slide thumbnails with bounded concurrency.
- Preserved deterministic slide-result ordering in the returned QA payload even though the model calls now complete out of order.

**Still pending after this slice**

- Full durable background job split for research/document generation.
- Warm Node renderer.
- AgentDeck per-slide parallel fan-out after storyline/design lock.

**Verified**

- Targeted judge/document tests: `17 passed`.
- Full backend suite: `335 passed, 4 skipped`.
- Web `npx tsc --noEmit -p tsconfig.json` clean.
- Backend `py_compile` clean for touched Python modules.

### 2026-06-15 — Slice 4: durable progressive jobs for explicit long turns

**Shipped**

- Added a `job_started` SSE event for explicit deep/expert research turns and confirmed document-generation turns.
- The SSE response now intentionally detaches after `job_started` for those long jobs, while the existing durable turn worker continues in the background and persists progress, final messages, research metadata, and generated document previews.
- The frontend now treats `job_started` as the handoff to the durable status card: it keeps the assistant placeholder, starts active-turn polling, and updates the card from persisted turn progress instead of relying on a long-lived SSE connection.
- Initial document requests still surface the final metadata/template/format popup before detaching; generation detaches only after the user confirms the document plan.
- Extended active-turn polling from ~4 minutes to ~12 minutes so research/deck jobs do not prematurely leave the UI in a stale state.

**Still pending after this slice**

- Warm Node renderer.
- Async/off-critical-path LibreOffice QA polishing pass.
- AgentDeck per-slide parallel fan-out after storyline/design lock.

**Verified**

- Stream regression suite: `23 passed`.
- Full backend suite: `335 passed, 4 skipped`.
- Web `npx tsc --noEmit -p tsconfig.json` clean.
- Backend `py_compile` clean for touched Python modules.

### 2026-06-15 — Slice 5: warm AgentDeck renderer

**Shipped**

- Refactored `render_agentdeck.js` so the CLI still works while exposing a reusable `renderPayload()` function.
- Added `render_agentdeck_server.js`, a persistent JSONL stdio renderer that keeps Node, PptxGenJS, and layout modules warm across deck renders.
- Added a Python warm-renderer process manager used by `generate_agentdeck_pptx_bytes()`, with automatic fallback to the previous one-shot subprocess renderer on warm-process failure.
- Added `agentdeck_warm_renderer_enabled` config, defaulting to `true`.

**Still pending after this slice**

- Async/off-critical-path LibreOffice QA polishing pass for executive mode beyond the durable-job boundary.
- AgentDeck per-slide parallel fan-out after storyline/design lock.

**Verified**

- AgentDeck renderer tests: `33 passed`.
- Full backend suite after warm-renderer integration: `337 passed, 4 skipped`.
- Web `npx tsc --noEmit -p tsconfig.json` clean.
- Backend `py_compile` clean for touched Python modules.

### 2026-06-15 — Slice 6: AgentDeck slide-content fan-out

**Shipped**

- Changed AgentDeck block/content selection so, after the outline/storyline is locked, multi-slide decks bind content blocks with one LLM call per content slide using bounded parallelism.
- Kept the existing single-call behavior for one-content-slide decks to preserve the old cheap path and existing test assumptions.
- Added regression coverage proving block prompts are scoped to one slide each and recomposed into the final `DocPlan`.

**Still pending after this slice**

- Async/off-critical-path LibreOffice QA polishing pass for executive mode beyond the durable-job boundary.

**Verified**

- AgentDeck planner/renderer focused tests: `84 passed`.
- Full backend suite: `337 passed, 4 skipped`.
- Web `npx tsc --noEmit -p tsconfig.json` clean.
- Backend `py_compile` clean for touched Python modules.

### 2026-06-15 — Slice 7: async executive PPTX polish

**Shipped**

- Added a deferred render-QA path for executive PPTX artifacts: the first downloadable deck returns without waiting for LibreOffice thumbnail rendering, deterministic render QA, vision judging, or repair loops.
- The initial preview carries `render_qa.status="queued"` so the UI/backend can distinguish a fast artifact from a fully polished artifact.
- Added a background polish worker that rebuilds the same preview with full executive QA enabled and patches the saved assistant message in place when it succeeds.
- Render-QA failure logging now happens after the background polish pass for deferred executive decks; failed polish attempts leave the original usable deck attached.

**Still pending after this slice**

- Measure production p50/p95 for the new durable-job and background-polish boundaries; the code path is now split, but live traces should decide whether to further tune executive defaults.

**Verified**

- Deferred QA + durable stream focused tests: `36 passed`.
- Full backend suite: `338 passed, 4 skipped`.
- Web `npx tsc --noEmit -p tsconfig.json` clean.
- Backend `py_compile` clean for touched Python modules.
