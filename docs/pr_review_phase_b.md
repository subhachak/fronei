# Tech Lead Review — PR #1: `codex/phase-b-guardrail-control-plane`

**Commit:** `057fd43`  
**Reviewer:** Subh (Tech Lead)  
**Verdict: ✅ APPROVE** — with 4 flags below. None are blockers for merge; 2 need follow-up before Phase E enforcement goes live.

---

## Summary

Phase B delivers a clean, well-isolated guardrail foundation. The `GuardrailService` is deterministic and testable. The shadow hook is correctly fire-and-forget. The migration is idempotent. All four review focus areas check out with minor notes.

---

## 1. GuardrailService — Action Precedence ✅

`_more_restrictive_action` ordering is correct:

```
allow(0) < allow_with_constraints(1) < transform=redact(2) < require_research=require_judge(3) < ask_user(4) < stop_with_caveat(5) < escalate_to_admin(6) < block(7)
```

All 10 `GuardrailAction` values are covered. The two tied pairs (`transform`/`redact` at 2, `require_research`/`require_judge` at 3) correctly model equivalent severity between variants. The function is pure and deterministic — no hidden state, no registry dependency.

**Flag 1 (minor): `evaluate_boundary` returns a flat list with no aggregate action.**

When Phase E+ enforcement goes live, the caller needs to compute the effective action across all decisions for a given boundary. Right now there's no `max_boundary_action(decisions: list[GuardrailDecision]) -> GuardrailAction` helper. The shadow hook logs each decision individually, which is fine for telemetry — but enforcement will need that aggregation. Add it before the Phase E cutover.

```python
# Suggested addition to guardrails.py (before Phase E)
def max_boundary_action(decisions: list[GuardrailDecision]) -> GuardrailAction:
    result: GuardrailAction = "allow"
    for d in decisions:
        result = _more_restrictive_action(result, d.action)
    return result
```

---

## 2. Shadow Hook Placement and Failure Swallowing ✅

The hook:
- Fires **after** the existing pipeline — correct, zero risk to live turns.
- Swallows all exceptions via `except Exception: logger.exception(...)` — correct.
- `turn_graph_enabled` guard prevents shadow evaluation when the shell is disabled.
- Builds separate `GuardrailContext` instances for `tool_pre`, `tool_post`, and `output` — correct isolation.

**Flag 2 (needs fix before Phase E): `read_url` is not in the shadow hook's tool set.**

The hook covers `{"web_context", "web_search", "generate_document"}`. `read_url` is the only tool with a real URL in its input and is covered by `tool.ssrf_prevention` in the registry, but it is absent from the hook. This is fine now because `read_url` isn't an active turn_graph tool yet. When Phase E wires it in, add `"read_url"` to the hook's tool set and pass `context.tool_input["url"]` correctly.

**Flag 3 (minor, low urgency): SSRF check is a no-op for `web_search`/`web_context` in shadow mode.**

The synthesized tool_pre input for these tools is `{"query": ..., "max_results": 5}`. `_check_public_url` reads `context.tool_input.get("url")`, which returns `None`, so the check silently passes. The shadow mode won't collect meaningful SSRF telemetry for query-based web tools. This doesn't matter today but document it so no one mistakes the all-`allow` SSRF events in the DB as confirmation that SSRF checking is working for web_search — it isn't, and won't be until `read_url` is wired in.

**Flag 4 (tech debt): Bare `SessionLocal()` in `_write_guardrail_events` and `_template_belongs_to_user_db`.**

Both open their own session and close in `finally`. Acceptable for shadow mode, but when guardrails go live these must switch to the app's DI session (FastAPI `Depends(get_db)`) for proper connection pool management and request-scoped transaction control. Add a `# TODO Phase E: replace with DI session` comment now.

---

## 3. Alembic Migration Portability ✅

`f8a9b0c1d2e3_phase_b_guardrail_tables.py` is clean:

- `table_exists()` / `index_exists()` guards make it idempotent. Safe to run on any environment.
- `triggered_checks` stored as `TEXT` (JSON-serialized list) — correct; no need for a JSON column type.
- UUID primary keys on all three tables — consistent with the rest of the schema.
- `down_revision = "d7e8f9a0b1c2"` correctly chains to the Phase A migration.
- `goals` and `agent_runs` pre-created here in Phase B even though they're Phase C/D — this is the right call; prevents a migration ordering problem when those phases land.

One suggestion: add a `server_default=func.now()` to `created_at` on `guardrail_events` if it doesn't already have one, so DB-level insertions without an explicit timestamp don't produce nulls.

---

## 4. Default Registry Shape ✅

The `applies_to` schema in `guardrails.json` uses a **dual-index design** — boundary strings and tool strings coexist in the same array:

```json
"applies_to": ["tool_pre", "tool:web_search", "tool:read_url"]
```

`evaluate_boundary("tool_pre", ...)` matches on the bare boundary string. The `"tool:*"` entries are reserved for future per-tool policy filtering (Phase E+ agent-layer enforcement). This is forward-compatible and smart.

**The only gap:** this dual-purpose convention is undocumented. Add a comment block at the top of `guardrails.json` explaining the two selector types, or add it to the `GuardrailPolicy` schema docstring. Without this, the next engineer to add a policy will likely not know to include both and will wonder why `evaluate_boundary` doesn't match.

The file-seeded agent and prompt registries pre-built in Phase A (`agents.json`, `prompts.json`) already cover the four core agents that Phase C depends on. No gaps blocking Phase C.

---

## Test Coverage ✅

11 tests, all passing in CI (393 passed, 4 skipped).

High-value tests present:
- SSRF block on private IP, allow on public IP
- `strip_tool_instructions` strips all five injection patterns
- `require_source_manifest` blocks missing manifest
- Template ownership block/allow with injectable lookup (no DB)
- `evaluate_boundary` returns all matching policies
- Unknown check type allows rather than crashes
- Shadow hook swallows DB failure without raising
- Shadow hook writes guardrail event rows with fake session

One suggestion: add a test for `_more_restrictive_action` directly (the pure function) — it's the most load-bearing function in the file and currently only covered implicitly through policy evaluation.

---

## Merge Checklist

| Item | Status |
|---|---|
| Action precedence correct | ✅ |
| Shadow hook post-pipeline, non-raising | ✅ |
| Migration idempotent and chained | ✅ |
| Registry shape forward-compatible | ✅ |
| `read_url` gap documented | Flag 2 — track in Phase E |
| `max_boundary_action` helper | Flag 1 — add before Phase E |
| Bare `SessionLocal()` marked as tech debt | Flag 4 — comment, fix in Phase E |
| `applies_to` dual-index documented | Flag 3 — doc comment |

**All flags are non-blocking. Approve to merge. Phase C can start.**
