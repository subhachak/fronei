# Pre-LangGraph Orchestrator Retirement Record

## Status

Implemented on July 2, 2026. Production research dispatch is LangGraph-only.
The pre-LangGraph `research_lead.py` path, runtime/config override switch,
parity comparator, parity admin surface, and parity CI workflow have been
removed from the repository.

This document is now a retirement record, not an implementation checklist.

## What Changed

- `Runtime._run_research_subtree` now dispatches directly to
  `stream_langgraph_research` and forwards the LangGraph stream.
- The `FRONEI_ORCHESTRATOR` setting, QA override setting, runtime override
  helpers, and admin promote/demote endpoints were removed.
- `apps/api/app/services/agent/research_lead.py` was deleted.
- The legacy-only re-export block was removed from
  `apps/api/app/services/agent/research_subtree.py`; shared research helpers
  remain available for LangGraph nodes and document/deck flows.
- The parity comparator module, parity runner, parity workflow, and parity UI
  controls were removed.
- LangSmith evals and the standalone golden-set runner now target the single
  LangGraph research runtime.
- The historical side-effect audit keeps the retired `research_lead.py`
  functions with `removed` classifications for traceability.

## Heartbeat Behavior

The old legacy `_with_heartbeat` wrapper is gone with the legacy runtime.
LangGraph streaming now uses `_next_langgraph_stream_item` in
`apps/api/app/services/agent/runtime.py`, which consumes the LangGraph generator
on a background thread and emits a `research_progress` heartbeat whenever the
stream is quiet for `LANGGRAPH_STREAM_HEARTBEAT_SECONDS` seconds. This covers
slow LangGraph nodes uniformly through `_forward_langgraph_stream`.

## Validation

Targeted validation completed in the sandbox:

- `apps/api/tests/test_phase_ee_lead_research.py`: 76 active tests passing.
- `apps/api/tests/test_agent_runtime.py`: 58 active tests passing.
- LangGraph maturity/slice/synthesis smoke tests passing.
- OpenAPI import check passing.
- Frontend typecheck passing.
- Frontend unit tests passing.
- `git diff --check` passing before this documentation cleanup.

A full CI run remains the final integration gate because one-shot full-suite
execution can exceed the local sandbox command budget.

## External Smoke

The only validation not performed from this sandbox is a live end-to-end smoke
against a real networked environment:

- one deep-research turn
- one explicit comparison-matrix turn

Those flows rely on outbound tools unavailable in this sandbox, so they should
be checked manually before treating the retirement as fully production-smoked.
