# Unified Plan Architecture

## 1. Problem with the current design

Today the planner (`services/planner.py`) already classifies every turn and
recommends web search / deep research. But **document generation is a parallel,
self-contained flow**:

- `/documents/generate/from-prompt/docx` re-runs its own planner call, has its
  own 409 "recommendation" contract (`document_plan_recommended`), and its own
  modal (`DocumentPlanModal`).
- The conversation pipeline (`/conversations/.../stream`) has a *different*
  recommendation contract (`research_recommendation` SSE event) for web
  search / deep research, surfaced as an inline card.
- The frontend has two manual "mode" toggles (`researchOn`, `documentIntentOn`)
  that the user must flip *before* sending, which pre-commit to a flow before
  the planner has seen the request.

Result: three different "ask the user" mechanisms (409+modal, SSE+inline card,
manual pre-toggle), and document output is architecturally a sibling of chat
rather than an outcome of it.

## 2. Core idea

**There is one plan per turn.** The planner produces it, a deterministic *gate*
decides how much of it needs human confirmation, and execution is always
autopilot from that point on — the only question is whether the user saw a
confirmation screen first.

```text
message ──▶ planner (single call) ──▶ Plan ──▶ gate ──▶ {auto | confirm}
                                                   │
                                                   ▼
                                            execute(plan) ──▶ chat reply
                                                              + web context (optional)
                                                              + research synthesis (optional)
                                                              + document artifact (optional)
```

A document is no longer a different *mode*; it's just one field the planner
can set on the plan (`wants_document_output: true`), same as
`needs_web_search` or `recommend_deep_research`. Execution always produces
"the outcome" — a chat message, optionally with a document attached — through
the existing `done` SSE event, which already supports `document_preview`.

**When `wants_document_output` is true, the chat message is not the document.**
The chat reply is a short bullet outline of the document's sections — e.g.:

```
Created a technical spec covering:
- Architecture overview
- Data model changes
- Migration plan
- Rollout risks

Preview or download below.
```

— while the full content lives only in `document_preview`. Today the
document body *is* the chat answer (`build_document_preview` renders
`final_answer` itself); this changes to two distinct outputs from one
generation — see §7.

## 3. Extended Plan schema

Add to `Plan` (`services/planner.py`) and the planner JSON contract
(`services/prompts.py`):

```json
{
  "...": "existing fields unchanged",

  "web_search_criticality": "trivial|material",

  "wants_document_output": false,
  "document_brief": {
    "doc_type": "executive_report|proposal|memo|technical_spec|meeting_notes|one_pager|letter|resume|null",
    "title": "string|null",
    "audience": "string|null",
    "tone": "string|null",
    "length": "string|null",
    "output_format": "docx|markdown|null"
  },

  "plan_confidence": "high|medium|low",
  "open_questions": ["short strings — things the planner is unsure about"],

  "document_format_options": ["markdown", "docx", "pptx", "pdf", "xlsx"],
  "document_format_recommendation": "docx|pptx|pdf|xlsx|null"
}
```

- `wants_document_output` replaces the frontend's `documentIntentOn` heuristic
  (`defaultDocumentBrief()`); the planner makes this call using the same signal
  it already uses for `task_type: "writing"` vs everything else, plus explicit
  cues ("write me a memo", "create a proposal doc").
- `document_brief` fields are `null` when the planner can't infer them — that
  incompleteness is itself a confidence signal.
- **Default output is markdown.** `document_format_options` lists every format
  the planner believes is plausible for this content (e.g. a board update could
  reasonably be `markdown`, `docx`, or `pptx`). If that list has more than one
  entry, format choice becomes part of `open_questions` and is surfaced in the
  confirmation popup (§5) as a single-select, defaulting to
  `document_format_recommendation` (or `markdown` if the planner has no
  preference). If only `markdown` is plausible, no format question is raised.
- **Phase 1 format availability.** The planner can still propose `pptx`,
  `pdf`, `xlsx` when relevant — they remain visible in the format picker as
  *disabled* options (e.g. "PowerPoint — coming soon") rather than being
  filtered out, so the UI doesn't need to change again once those generators
  ship. `document_format_options` always carries the planner's full judgment;
  a frontend-side `SUPPORTED_DOCUMENT_FORMATS` constant (initially
  `["markdown", "docx"]`) determines what's selectable vs. shown-disabled.
- `plan_confidence` is the planner's own self-assessment of *the whole plan*
  (not just the research question). High = "I'm confident about task type,
  whether to search/research, and (if applicable) the document brief."
- `open_questions` gives the gate/UI human-readable text for anything the
  planner flagged as uncertain (e.g. "Should this be a Word doc or just chat?",
  "Audience isn't specified — defaulting to internal team").

`recommend_deep_research` / `needs_web_search` keep their current meaning and
become two of several "capabilities" the plan can request.

## 4. The Plan Gate (new: `services/plan_gate.py`)

A pure, deterministic function — no LLM call — that turns a `Plan` into an
execution decision. This **replaces** `research_advisor.advise_research()` and
the 409 logic in `documents.py`, both of which become special cases of the gate.

```python
@dataclass
class PlanGateResult:
    mode: Literal["auto", "confirm"]
    capabilities: PlanCapabilities      # resolved web/research/document toggles
    reasons: dict[str, str]             # capability -> human-readable reason
    risk_factors: list[str]
    confidence: str
```

Gate rules (tunable, but this is the starting policy):

| Condition | Result |
|---|---|
| `needs_web_search` is true and `web_search_criticality != "trivial"` | `confirm` — going outside the user-supplied context is a deliberate choice when it materially shapes the answer. |
| `recommend_deep_research` is true, regardless of confidence | `confirm` — deep research is slow and costs money; always a deliberate choice. |
| `wants_document_output` and (`len(document_format_options) > 1` or any `document_brief` field `null`) | `confirm` — format choice or ambiguous brief is an open decision. |
| `plan_confidence == "low"` | `confirm` — planner itself is unsure; surface `open_questions`. |
| everything else (`plan_confidence` medium/high, no/trivial web search, no deep research, document brief complete with a single plausible format — or no document at all) | `auto` |

**Web search criticality.** The planner adds `web_search_criticality:
"trivial" | "material"`:

- `trivial` — single, low-stakes, easily-verifiable fact that doesn't change
  the substance of the answer if wrong or omitted (today's date, current
  version number of a library, a unit conversion constant). Runs silently
  even though `needs_web_search` is true.
- `material` — anything that shapes the content, recommendation, or framing
  of the response (current pricing, vendor comparisons, regulatory status,
  "what's happening with X"), or whenever the user's intent suggests they want
  the answer scoped to what *they* supplied (e.g. "based on the attached doc,
  ..."). Gates via `confirm`.

This keeps the policy intent-driven rather than a blanket toggle: trivial
lookups stay invisible, anything that could change *what Fronei tells the
user* — or that risks going beyond the user's intended scope — gets one
visible decision in the same bundled popup.

This whole gate (including the `web_search_criticality` split and the
document-ambiguity threshold) should live in `services/plan_gate.py` as
**config-driven**, not hardcoded — see open question on admin tuning (§9, now
resolved to "yes, config-driven, similar to `routing_rules.yaml`").

This gives the adaptive behavior you described:

- **Low end** (clear chat question, clear "write me a one-pager about X for
  the exec team" with all brief fields inferable, confidence high) → `auto`,
  zero interruptions, straight to the outcome.
- **High end** (ambiguous ask, deep research warranted, or a document request
  with missing audience/tone/format) → exactly **one** `confirm` screen that
  bundles *every* open decision — not three separate prompts.

## 5. One confirmation surface, not three

### Backend: single SSE event

Replace `research_recommendation` and the `document_plan_recommended` 409
with one event, emitted by the conversation pipeline right after planning,
before any execution:

```text
event: plan_proposed
data: {
  "conversation_id": ...,
  "message_id": ...,
  "plan_confidence": "medium",
  "open_questions": ["..."],
  "capabilities": {
    "web_search":    { "enabled": false, "recommended": true,  "reason": "..." },
    "deep_research": { "enabled": false, "recommended": true,  "reason": "...", "risk_factors": [...] },
    "document":      { "enabled": true,  "recommended": true,  "reason": "...",
                        "brief": { "doc_type": "memo", "title": null, "audience": null,
                                   "tone": "Concise", "length": "Short", "output_format": "docx" } }
  }
}
```

`enabled` = the gate/planner's *proposed* state (pre-checked in the UI).
`recommended` = whether this capability is the reason confirmation triggered
(drives the "Recommended" badge). The frontend never has to special-case
"is this a document turn or a research turn" — it's the same payload shape
either way.

When `gate.mode == "auto"`, no `plan_proposed` event is emitted; the pipeline
goes straight into `routing` / `token` / `done` as it does today, with
`document_preview` populated on `done` if `capabilities.document.enabled`.

### Frontend: one `PlanModal`

`DocumentPlanModal` (already mid-rename in the latest commit) becomes the
**only** plan-confirmation surface, rendered whenever a message carries a
`plan_proposed` payload (replacing the inline `research_recommendation` card
entirely):

- "Source plan" section (existing): Web search / Deep research toggles,
  pre-checked per `capabilities.*.enabled`, badge "Recommended" per
  `capabilities.*.recommended`, reason text from `capabilities.*.reason`.
- New "Document" section: a toggle for "Produce a document", and — only when
  on — the brief fields (doc type, audience, tone, length), pre-filled from
  `capabilities.document.brief`, with empty fields highlighted using
  `open_questions`. If `document_format_options` has more than one entry, a
  format picker (Markdown / Word / PowerPoint / PDF / Excel as applicable)
  defaults to `document_format_recommendation` (or Markdown); single-option
  cases render no picker.
- Footer: **Start** (re-submits referencing `message_id` with
  `confirmed_plan: {...}` = the user-edited capabilities/brief, so the backend
  skips planning+gate and runs `auto` on this exact plan) and **Send as chat**
  (forces `wants_document_output: false`, all capabilities off — today's
  escape hatch, unchanged).

One popup, at most once per turn, covering every open decision.

### Plan persistence and `confirmed_plan` re-submission

The planner's output is persisted on the user's `ConversationMessage` row
(new `plan_json` column) the moment it's produced — whether the gate resolves
to `auto` or `confirm`. When the gate is `confirm`:

1. `plan_proposed` is emitted with `message_id` pointing at that row.
2. The user edits capabilities/brief/format in `PlanModal` and hits **Start**.
3. Frontend calls a small follow-up endpoint, e.g.
   `POST /conversations/{id}/messages/{message_id}/execute-plan` with body
   `{ confirmed_plan: {...edited capabilities/brief...} }`.
4. Backend loads the persisted `Plan` for `message_id`, overlays the edited
   capabilities/brief, and runs execution directly — **no re-planning, no
   re-sending the original message text**. This avoids drift (the planner
   won't re-interpret the prompt differently the second time) and is cheaper
   (zero extra planner calls).

This makes `plan_proposed` ↔ `execute-plan` a clean two-step protocol instead
of "abort and resend the whole message with extra flags," which is how
`research_recommendation` and the doc 409 work today.

## 6. Execution state machine

```text
            ┌─────────────┐
 user msg ─▶│   planning   │  (single planner call; persisted on the message)
            └──────┬───────┘
                   ▼
            ┌─────────────┐
            │  plan gate   │
            └──┬───────┬───┘
         auto  │       │ confirm
               ▼       ▼
        ┌───────────┐ ┌────────────────┐
        │ executing │ │ awaiting_plan   │──user edits + Start──┐
        │ (autopilot)│ │ (PlanModal open)│                      │
        └─────┬─────┘ └────────────────┘                       │
              │                                    confirmed_plan│
              │◀─────────────────────────────────────────────────┘
              ▼
   web_search? ──▶ deep_research? ──▶ worker/decompose ──▶ document_output? ──▶ done
   (skip if off)    (skip if off)      (existing logic)     (skip if off)
```

Once `executing` starts (whether entered directly via `auto` or via
`confirmed_plan` after `awaiting_plan`), it is **fully autopilot** — no further
prompts, matching "once it starts the execution, it just goes in autopilot".

## 7. File-level change list

**Backend**

- `services/prompts.py` — extend `PLANNER_SYSTEM_PROMPT` with
  `wants_document_output`, `document_brief`, `plan_confidence`, `open_questions`.
- `services/planner.py` — extend `Plan`/`_build_plan`/`passthrough` with the new fields.
- `services/plan_gate.py` *(new)* — `evaluate(plan) -> PlanGateResult`, unit-testable, no I/O.
- `services/research_advisor.py` — delete; logic absorbed into `plan_gate.py`.
- `routers/conversations.py` (`stream` endpoint) —
  - run gate after planning; emit `plan_proposed` and return early on `confirm`.
  - on `confirmed_plan` in the request, skip planning+gate, use the supplied capabilities directly.
  - after existing web/research/worker logic, if `capabilities.document.enabled`, call the document-generation step (moved from `documents.py`) and attach `document_preview` to `done`.
- `routers/documents.py` —
  - **Good news:** `build_document_preview()` already exists and is already
    called from `conversations.py`'s `done` handler — the wiring for
    "chat turn produces a document artifact" is in place. It currently gates
    on its own `detect_document_intent()` keyword check; switch that gate to
    `capabilities.document.enabled` from the plan, and pass `capabilities.document.brief`
    (doc_type, title, output_format) into `generate_docx_bytes` instead of
    re-deriving them.
  - extract the rest of the brief-driven generation logic (`_document_system_prompt`,
    `DOC_TYPE_PROMPTS`, research/web-context folding into `doc_context`) into a
    shared helper so `conversations.py` can call it.
  - **two-output generation:** when `capabilities.document.enabled`, the worker
    call must produce (a) the document body and (b) a short chat description.
    Cheapest approach: one LLM call whose system prompt asks for the document
    markdown plus a final `---SUMMARY---` block (1–3 sentences); split the
    response — body → `document_preview.markdown`, summary → chat `answer`.
    Avoids a second model round-trip while keeping the chat reply minimal.
  - format expansion: `generate_docx_bytes` covers docx; pptx/pdf/xlsx outputs
    need equivalents (`generate_pptx_bytes`, etc.) — see open question #4.
  - remove the 409 `document_plan_recommended` branch and the planner re-run (planning now happens once, upstream).
  - the standalone `/documents/generate/from-prompt/docx` endpoint can stay as a thin wrapper for non-chat callers (e.g. "regenerate as docx" on an existing message) but no longer drives the UX.
- `schemas.py` — add `ConfirmedPlan` request model (capabilities + brief overrides); extend `ChatRequest`/stream request with `confirmed_plan: ConfirmedPlan | None`.

**Frontend** (`apps/web/app/page.tsx`)

- Remove `researchOn` / `documentIntentOn` as pre-send gates and
  `defaultDocumentBrief()` heuristic — the planner decides `wants_document_output`.
  (Optional: keep small UI affordances that set *hints* sent alongside the
  message, e.g. "user explicitly asked for deep research" → pass as a hint the
  planner/gate weighs, not a hard pre-commit.)
- Replace `ResearchRecommendation` + `DocumentPlanRecommendations` types with
  one `PlanProposal` type matching the `plan_proposed` payload.
- `DocumentPlanModal` → `PlanModal`: add the "Document" section described in §5;
  drive all three sections from `PlanProposal.capabilities`.
- Remove `DocumentPlanRecommendationError` / 409 handling in
  `generateDocumentFromPrompt` — document output now arrives via `done.document_preview`
  in the normal stream, same as today's non-confirm path already does.
- `actOnResearchRecommendation` → `actOnPlanProposal(proposal, edited)`: re-submits
  with `confirmed_plan: edited`.

## 8. Net effect vs. today

| | Today | After |
|---|---|---|
| Decision points surfaced to user | up to 2 (research card + doc modal), different shapes | 0 or 1, one shape |
| Manual pre-send mode toggles | 2 (`researchOn`, `documentIntentOn`), hard pre-commit | 2 retained as soft *hints* to the planner |
| Planner calls per turn | 2 (chat planner + doc planner) | 1 |
| "Document" as architecture | parallel flow, own endpoint contract | a capability flag on the plan, same as web search |

## 9. Decisions

1. **Hints vs. toggles** — retained. The sidebar "Document" / "Research"
   buttons remain as *hints* passed alongside the message (e.g.
   `hint_document_output: true`, `hint_deep_research: true`); the planner
   weighs them but they don't pre-commit a flow or bypass the gate.
2. **`confirmed_plan` re-submission** — via persisted plan + `message_id`,
   per §5 "Plan persistence" above. No re-planning, no resending message text.
3. **Gate thresholds** — config-driven, in a new `plan_gate_rules.yaml`
   (sibling to `routing_rules.yaml`), so the §4 table's thresholds
   (criticality mapping, brief-completeness rules, confidence cutoffs) are
   admin-tunable without code changes.
4. **Format scope for phase 1** — unavailable formats (`pptx`, `pdf`, `xlsx`)
   stay visible but disabled in the picker ("coming soon"), per the
   `SUPPORTED_DOCUMENT_FORMATS` note in §3. Generators for those formats are
   out of scope for this phase.
5. **Chat description depth** — short bullet outline of the document's
   sections (not just 1–2 sentences), per the example in §3.
6. **Web-search confirm fatigue** — resolved via `web_search_criticality`
   (§4): `trivial` lookups stay silent, `material` ones (anything that shapes
   the answer's substance, or risks exceeding the scope of user-supplied
   context) gate via the bundled popup. Thresholds for trivial vs. material
   also live in `plan_gate_rules.yaml` per decision 3.

## 10. Next step

With the architecture and gate policy settled, implementation can proceed
file-by-file per §7. Suggested sequencing:

1. `plan_gate_rules.yaml` + `services/plan_gate.py` (pure logic, unit-testable
   in isolation).
2. Planner schema/prompt extension (`prompts.py`, `planner.py`) — additive,
   non-breaking (`passthrough()` covers fallback).
3. `plan_json` persistence column + migration.
4. `conversations.py` wiring: `plan_proposed` emission, `execute-plan`
   endpoint, two-output document generation, `document_preview` gating switch.
5. Frontend: `PlanProposal` type, `PlanModal` Document section + format
   picker, hints wiring, removal of `documentIntentOn`/`researchOn` pre-send
   gates and the 409 path.
6. Remove `services/research_advisor.py` and the `documents.py` 409 branch
   once (4) and (5) are live.
