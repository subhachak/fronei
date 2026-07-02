# Retire the pre-LangGraph orchestrator — Implementation Guide

**Where things stand today, confirmed by reading the code, not the roadmap doc alone:** `config.py:159` already defaults `fronei_orchestrator` to `"langgraph"` — legacy hasn't served real traffic in a while. It's only reachable via an admin "demote" action (`set_orchestrator_override`), the CI parity workflow's forced env var, or an eval admin explicitly forcing `pipeline="legacy"` for a comparison run. This is a real go-ahead, not a leap — you already confirmed deep research has felt solid in actual use, which is the one thing the project's own documented Phase 5 gate (`docs/langgraph_implementation_roadmap.md:466-478`) existed to de-risk.

**One correction to how I initially scoped this:** my first pass (via a research subagent) characterized `research_subtree.py` as a "legacy-only compat shim" safe to delete alongside `research_lead.py`. That's wrong, and I caught it by reading the file directly before writing this doc. `research_subtree.py` is a backward-compat re-export facade covering **eight** underlying modules (`research_models`, `research_profiles`, `research_contracts`, `research_utils`, `research_planner`, `research_evidence`, `research_synthesis`, and `research_lead`) — only the last of those eight blocks (lines 218-240) is legacy-only. The other seven are genuinely shared and are imported from `research_subtree` by `deck_subtree.py`, `document_subtree.py`, `runtime.py`'s top-level imports, and a 2,800-line test file (`test_phase_ee_lead_research.py`). Deleting the whole file would have broken deck/document generation. This doc reflects the corrected, verified scope.

## Phase 1 — Remove the orchestrator switch and legacy runtime branches (mechanical, low risk)

**`apps/api/app/services/agent/runtime.py`**, `_run_research_subtree` (~line 762-1298). The LangGraph branch (762-782) is already a complete, self-contained, hard-return path — everything from line 784 to the end of the function (legacy deep-research thread bridging, and the inline legacy plan→search→rank→bind→synthesize→judge→repair pipeline) is dead code once `configured_orchestrator()` can only ever return `"langgraph"`. Collapse the function to:

```python
def _run_research_subtree(self, request: TurnRequest, progress):
    from app.config import get_settings
    from app.services.agent.langgraph_runtime import stream_langgraph_research
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
    return (yield from self._forward_langgraph_stream(gen, progress))
```

This also removes the only call site of `configured_orchestrator()` inside `_run_research_subtree` — check for other call sites before the next step (there's at least one more, in `evals.py`, handled in Phase 3).

**`apps/api/app/services/agent/langgraph_runtime/runtime.py`** (~line 31-81): delete `VALID_ORCHESTRATORS`, `_RUNTIME_ORCHESTRATOR_OVERRIDE`, `set_orchestrator_override`, `clear_orchestrator_override`, and `configured_orchestrator()` itself, once nothing calls them (confirm via grep after Phase 1 + Phase 3 land together — `evals.py` still references all of these, see below).

**`apps/api/app/config.py`** (~line 157-171): remove `fronei_orchestrator` and `fronei_orchestrator_qa_override_enabled` settings fields once nothing reads them.

## Phase 2 — Delete the legacy module, fix the one shared re-export file

**Delete `apps/api/app/services/agent/research_lead.py`** (1,806 lines) — confirmed via grep, nothing outside `research_lead.py` itself and `research_subtree.py`'s re-export block imports from it directly, **except** two real call sites that need separate handling (below).

**`apps/api/app/services/agent/research_subtree.py`**: remove only the `research_lead` re-export block (lines 218-240 — `LeadResearchAgent`, `lead_research_loop`, `verify_claims`, and the underscore-prefixed worker helpers). Leave the other seven import blocks untouched.

**Two real call sites outside the module itself** that also need fixing, both confirmed by direct grep (not caught by the "who imports research_subtree" search alone, since these import from `research_lead` directly):

- `apps/api/app/services/langsmith_evals.py:192-208`, `_make_pipeline_target(pipeline_name, tools)` — has a live `if pipeline_name == "legacy": from app.services.agent.research_lead import lead_research_loop` branch. Once `research_lead.py` is gone, this branch needs to be removed (or the whole `pipeline_name` parameter simplified away, since there's only one pipeline left). Find and check the caller that passes `pipeline_name="legacy"` before deciding which — it may be reachable from an eval-comparison entry point that also needs updating.
- `apps/api/app/services/agent/langgraph_runtime/comparators.py` — this entire module's stated purpose (line 3: "compares the output of `lead_research_loop` (legacy) against...") stops making sense with only one pipeline. Delete it.

## Phase 3 — Retire the parity harness and its admin surface

**`.github/workflows/langgraph_parity.yml`** — retire (delete or disable). This is the workflow that sets `FRONEI_ORCHESTRATOR: legacy` specifically to run the comparator; nothing else in the repo sets that env var to `legacy`.

**`apps/api/app/routers/evals.py`** — three things to remove, all confirmed by direct read:
- `POST /admin/evals/parity/promote` (~line 340-364) and its `DELETE` counterpart (~line 366-388) — the admin cutover buttons.
- `GET /admin/evals/parity/orchestrator` (~line 405-416) — status readout.
- `_forced_pipeline` context manager (~line 1054-1078) and its lock (`_ORCHESTRATOR_OVERRIDE_LOCK`) — used by the eval-case runner to force a specific pipeline for a batch (call site confirmed at line 2240, inside the eval-batch-run function, parameterized by a `pipeline` argument). **Trace where that `pipeline` parameter actually comes from before deleting** — confirm it's solely a legacy-vs-langgraph selector for eval comparison runs and not doing double duty for something else (e.g. forcing research_level or a model variant) that would still be needed after this cleanup. If it's purely the legacy/langgraph selector, the whole forcing mechanism has no purpose left and the eval-run endpoint should just always run against the one live pipeline.

**`apps/web/app/admin/components/EvalsTab.tsx:394-395`** and surrounding code — remove the corresponding UI for the promote/demote/orchestrator-status controls.

## Phase 4 — Test suite triage

**`apps/api/tests/test_agent_runtime.py`**: 11 call sites use the `_use_legacy_orchestrator` helper (defined ~line 50-58). Triaged individually, not as a block:

- `test_agent_research_document_creates_artifact`, `test_agent_markdown_output_renders_in_chat_without_artifact`, `test_agent_vague_followup_does_not_import_other_workspace_conversation` — confirmed the `document`/`research_document` routes are handled entirely outside the orchestrator switch (`runtime.py`'s outer dispatch, not `_run_research_subtree`). These three almost certainly just need the now-meaningless `_use_legacy_orchestrator(monkeypatch)` call deleted; the tests should pass unchanged against the LangGraph default. Verify, don't assume.
- `test_agent_research_streams_milestones`, `test_agent_research_emits_agentic_goal_guardrail_and_judge_events`, `test_agent_research_repair_loop_runs_when_judge_requests_repair`, `test_agent_confirmed_deep_research_runs_deep_budget` — these test behaviors (progress streaming, judge/guardrail events, repair loop, deep budget enforcement) that `test_langgraph_maturity.py` already covers extensively from this session's work. Check for overlap first — if LangGraph-path coverage already exists for the same assertion, delete the legacy-path duplicate rather than convert it; if not, convert (drop the monkeypatch, adjust assertions to LangGraph's event shapes).
- `test_agent_deep_research_replays_final_answer_stream`, `test_agent_deep_research_replays_repaired_final_answer` — these test a **legacy-specific mechanism** (buffered replay of the final answer after the thread-bridged deep-research loop completes). LangGraph doesn't replay — it streams natively, including for repair (this session's `repair_answer_streaming_fix.md` work). These tests don't have a like-for-like LangGraph equivalent because the thing they're testing (a replay step) doesn't exist on that path. Don't try to force a conversion; write new tests asserting LangGraph deep research streams its answer natively instead, or confirm `test_langgraph_maturity.py` already does.
- `test_agent_deep_research_emits_heartbeat_during_quiet_lead_loop` — **the one genuine open question in this whole plan.** This test verifies the legacy path's `_with_heartbeat` wrapper keeps the connection alive during a long silent LLM call. Grepped for `_with_heartbeat` usage: it's called only from legacy code paths in `runtime.py`, never from the LangGraph branch or from `nodes.py`. That means a single long-running LLM call *inside* one LangGraph node (e.g. `verify`'s citation check, or `synthesize` before its first streamed token) currently has no equivalent heartbeat protection — the node-level "updates" progress event only fires when the node completes, not during it. For short calls this is invisible; for a genuinely slow individual call it's a real gap that could risk a proxy/gateway timeout on the *connection*, independent of anything this retirement touches. **Don't silently drop this test's coverage** — either confirm LangGraph nodes have adequate protection some other way (they may not need `_with_heartbeat`'s exact mechanism if the SSE connection itself has sufficiently long timeouts configured, which is worth checking directly) or treat closing this gap as a small separate follow-up before or shortly after this retirement, not something to wave away.
- `test_agent_background_turn_persists_and_polls_status` — already flagged as flaky under full-suite load in the last review pass (passes in isolation, times out intermittently under contention). It currently tests the *legacy* background-turn path specifically. Convert it to test the LangGraph path — this may or may not also resolve the flakiness (unclear, don't assume it will), but it needs to test something that still exists either way.

Once all 11 are resolved, delete the `_use_legacy_orchestrator` helper itself.

**`apps/api/tests/test_phase_ee_lead_research.py`** (~2,800 lines) — this needs its own dedicated pass, not a blanket deletion. It imports heavily from `research_subtree`, but as established above, most of those imports (`bind_evidence`, `rank_sources`, `plan_from_contract`, `CoverageContract`, `_extract_named_comparison_subjects`, citation verification, etc.) are testing **shared** functions that LangGraph's `nodes.py` calls directly — that coverage should stay. Only the subset of this file that specifically imports and tests `LeadResearchAgent`, `lead_research_loop`, `verify_claims`, or the `_worker_*`/`_source_*` helpers (research_lead.py's actual contents) tests code that's being deleted. Recommended approach for whoever implements this: grep the file for those specific names first, isolate which test functions reference them, remove only those, and leave the rest — given the file's size, don't attempt this as a single mechanical pass; budget real review time for it.

## Suggested sequencing

Phases 1-3 are well-scoped and independently verifiable (each is a "delete this, grep confirms nothing else references it" step). Phase 4 is the long pole — recommend landing Phases 1-3 first (they're the part that actually removes the dual-pipeline risk and dormant admin surface), then treating the test triage as its own follow-up pass rather than blocking the whole cleanup on fully resolving a 2,800-line test file in one sitting.

## Testing plan

- After Phase 1: run `test_langgraph_maturity.py` and `test_agent_runtime.py` in full — confirm the collapsed `_run_research_subtree` doesn't change any LangGraph-path behavior (it shouldn't; the code is identical, just unconditional now).
- After Phase 2: `grep -r "research_lead\|LeadResearchAgent\|lead_research_loop" apps/api/app` should return zero results outside test files pending Phase 4 triage.
- After Phase 3: confirm the app boots and `/admin/evals` still loads without the removed endpoints being referenced anywhere in the frontend (a stale fetch to a deleted endpoint would surface immediately in the Evals admin tab).
- Phase 4: track before/after test counts per file — the goal is preserved or increased coverage of shared behavior, not just a smaller test suite. If total assertions meaningfully drop, that's a signal something got deleted that should have been converted instead.
- Manual: run one deep-research turn and one comparison-mode turn end-to-end post-cleanup, confirm nothing regressed — these are the two features this session did the most work on and the ones most worth a real smoke test before calling this done.
