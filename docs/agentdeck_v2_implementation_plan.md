# AgentDeck v2 — Implementation Plan (Parallel: Claude / Codex)

Source backlog: tasks #152, #136-151, #157 (reordered per
`agentdeck_v2_reconciliation.md` §11, extended for the Designer stage per
`agentdeck_framework_architecture_v2.md` §20). This plan splits that backlog
into two tracks that can run **concurrently** with minimal merge conflict,
organized into four phases with explicit sync gates.

- **Track A (Claude)** — Plan/Design layer: contracts, schemas, narrative +
  design-stage planner extensions.
- **Track B (Codex)** — Pipeline/Runtime layer: fail-fast, composer fit
  wiring, renderer audit, QA/judges, fixtures.

Split rationale: Track A owns *new Pydantic models and LLM-facing schema*
(low runtime risk, high churn in `render_plan.py`/`content_schemas.py`/spec
files). Track B owns *execution-path code* (`documents.py`,
`chat_pipeline.py`, `compose_docplan.py`, `render.js`, QA modules) where bugs
are user-visible immediately. Within each phase, the two tracks touch
largely disjoint files; cross-track dependencies are called out explicitly
with an interface contract so the dependent track can start against a stub.

**Revision note**: this version incorporates Codex's review of the prior
draft — 8 adjustments folded in: `DesignPlan`/`SlideDesignTreatment` model
stubs moved to Phase 2 (full implementation stays Phase 3); fail-fast (#152)
explicitly scoped to renderer/artifact stage only, not the planner's
minimal-valid-plan fallback; usage-stats gate (#144) moved to `planner.py`
via a settings flag, `selection.py` stays pure; #147 split into
`plan_checks.py` (pre-render) and `render_checks.py` (post-render); Phase 3
Track A is now additive (`generate_agentdeck_v2_plan()` alongside, not
replacing, `generate_doc_plan()`); `block_id` added to `ContentBlock` in
Phase 2 for repair addressing; `BrandProfile`/`UserDocumentProfile` interface
fields stubbed as optional in Phase 2; lighthouse fixture starts as a Phase 3
smoke test, promoted to a strict gate in Phase 4; `QualityMode` type defined
in Phase 1.

---

## 0. Ground Rules

1. **Branch per phase, not per task**: `v2/phaseN-track-a`, `v2/phaseN-track-b`. Merge both into `v2/integration` at the end of each phase before starting the next.
2. **Interface-first for cross-track dependencies**: when Track B needs a Track A model before A has merged, A publishes the Pydantic model + docstring in a short-lived stub PR (just the class definitions, no logic) so B can import and code against it without waiting for the full implementation.
3. **No silent renames mid-phase**: if a track needs to rename a field (e.g. `slide_layout` → `layout_id`), land it as an additive alias first (`layout_id = Field(alias="slide_layout")` or a `@property`), remove the old name only in the integration step at the end of the phase.
4. **Every phase ends with**: full `pytest` run, `tsc`/build if frontend touched, and a sample-deck render (existing golden-file harness) — owned jointly, run on `v2/integration` before declaring the phase done.
5. **#151 (`quality_mode` plumbing) is intentionally last and joint** — it's a thin parameter threaded through every stage built in Phases 1-4, so it's cheapest once those stages exist.

---

## 1. Dependency Graph (informal)

```
#152 (fail-fast)            — independent, do first, either track
#136 (token_pairs/qa_thresholds) — independent
#144 (usage-stats gate)     — independent, tiny
#139 (FitContract)          — independent (new file)
#140 (ComponentRuntime)      — depends on #139
#146 (render.js audit)       — independent

#137/#138 (NarrativePlan/EvidencePack/PresentationPlan) — independent of #139/#140
#145 (fit validation in composer) — depends on #139
#147 (deterministic QA)      — depends on #136 (qa_thresholds)

#157 (DesignPlan/Designer)   — depends on #137/#138 (PresentationPlan) + #139 (FitContract, for component_choices)
#141-143 (4-step planner)    — depends on #137/#138, and on #157 for the design step

#148/#149 (judges)           — depends on #147 (shares issue taxonomy) + #157 (repair_constraints)
#150 (structural repair loop) — depends on #148/#149 + #145
#153 (lighthouse fixture)     — depends on #141-143, #157, #150 (needs the full pipeline to exercise)
#154 (golden-file reorg)      — mostly independent, light touch after #147 lands

#155/#156 (brand/personalization) — depends on #157 (DesignPlan consumes BrandProfile) but model definitions can start early

#151 (quality_mode)          — joint, last
```

---

## 2. Phase 1 (parallel) — Foundations

**Goal**: land all independent, low-risk groundwork. No shared files between tracks.

| Track | Task | Files touched | Notes |
|---|---|---|---|
| **A** | #136 — `token_pairs` + `qa_thresholds` in spec + schema | `design_systems/agentdeck_v1/spec.json`, `design_systems/agentdeck_v1/schema.py` | `token_pairs` must be theme-aware (resolvable per `color_tokens.dark`/`.light`, not a flat map — per reconciliation §1 correction). |
| **A** | #139 — `FitContract` model + author per-component contracts | new `app/services/components/fit_contract.py`, `registry.py` (add `fit_contract: FitContract` field to `ComponentDef`, default-populate for ~10-12 components) | Publish `FitContract` class definition early (stub PR) — Track B's #145 in Phase 2 needs it. |
| **A** | #140 — `ComponentRuntime` protocol | new `app/services/components/runtime.py` | Thin: `normalize`/`validate_fit`/`estimate_density` as `Protocol`; start with default implementations derived from existing char-count heuristics in `compose.py`/`render.js`. |
| **B** | #152 — Fail-fast: remove markdown + legacy-renderer fallback, add `DocumentGenerationFailure` | `documents.py` (`build_document_artifact`), `chat_pipeline.py`, `conversations.py` (surface structured failure as retryable chat message) | Per reconciliation §8 "compatibility freeze", **strict for PPTX**: no markdown fallback, no legacy-renderer fallback, no "best effort" downgrade — return `DocumentGenerationFailure` with a retry/regenerate path in the UI. DOCX/XLSX get the same principle but are not urgent this phase. **Important distinction**: `generate_doc_plan()`/`generate_agentdeck_v2_plan()` (planner layer) may still fall back to a *minimal valid plan* if the LLM call fails — that's a planner-level retry, not a format downgrade. The fail-fast rule applies to the **renderer/artifact stage**: once a valid plan exists, a pptx-requested output must become pptx or a structured failure, never markdown/legacy pptx. |
| **B** | #144 — Feature-flag usage-stats-weighted ranking off by default | `planner.py` (call site of `load_usage_stats_map`), `app/core/config.py` (settings) | Add `AGENTDECK_USAGE_STATS_WEIGHTING_ENABLED` setting, default `false`. **Gate lives in `planner.py`, not `selection.py`**: `usage_stats_map = load_usage_stats_map(db) if settings.agentdeck_usage_stats_weighting_enabled else {}`. `selection.rank_components()` stays pure/unchanged — it already accepts `usage_stats_map={}` correctly. Logging (#127-129) stays active — only the ranking *weight* is gated. |
| **B** | #146 — Audit `render.js` for residual heuristics/raw-color logic | `render.js` | Find/remove any leftover title-string-based archetype inference or raw fg/bg comparisons predating #107-118. Likely small; if nothing found, document that and close. |
| **A** | *(new, small)* Define `QualityMode` enum early | `app/services/components/quality_mode.py` (or alongside `render_plan.py`) | `QualityMode = Literal["draft", "standard", "executive"]`. Not threaded anywhere yet (#151 does that), but Phase 2/3 models (`DesignPlan`, prompts) can reference the type now so #151 is pure wiring later. |

**File overlap**: none. `selection.py`/`planner.py` (#144) and `registry.py`
(#139) are adjacent but #144's diff should be ~5 lines (an `if` guard around
the `usage_stats_map` lookup) — low collision risk even if both tracks touch
`planner.py`-adjacent files; confirm via `git diff` before merge.

**Exit criteria**: `pytest` green on `v2/integration`; spec validates with new
`token_pairs`/`qa_thresholds` sections; `FitContract`/`ComponentRuntime`
importable with passing unit tests for the default-derived implementations.
Specifically for #152/#144, add:

- Unit test: requested-pptx generation that fails at render stage returns
  `DocumentGenerationFailure`, **never** a markdown body.
- Unit test: AgentDeck composer/render failure does **not** fall back to the
  legacy renderer — same `DocumentGenerationFailure` path.
- Settings test: `AGENTDECK_USAGE_STATS_WEIGHTING_ENABLED` defaults to
  `false`, and with it `false`, `generate_*_plan()` passes `{}` to
  `rank_components` regardless of `db` being present.

---

## 3. Phase 2 (parallel) — Plan Models + Composer/QA Wiring

**Goal**: split `DocPlan` into the v2 layered models (Track A) while Track B
wires fit-validation and deterministic QA against the *existing* model shape
via an alias, so the two don't block each other.

| Track | Task | Files touched | Notes |
|---|---|---|---|
| **A** | #137/#138 — `NarrativePlan`, `EvidencePack`, `PresentationPlan`/`PresentationSlidePlan` (rename+extend `DocPlan`/`SectionPlan`) | `app/services/components/render_plan.py` (or split into `narrative_plan.py` + `presentation_plan.py`), `compose_docplan.py` (update imports only) | Per reconciliation §3 correction: `DocPlan`/`SectionPlan` *are* `PresentationPlan`/`PresentationSlidePlan` today — rename+extend mechanically (add `slide_id`, `dek`, `purpose: SlidePurpose`, `audience_question`, `message`, `evidence: list[EvidenceRef]`, mostly optional). **Also add `block_id: str \| None` to `ContentBlock`** (optional now) — Phase 4's repair loop needs durable addressing for "fix slide 7's right column card" style instructions even if slides are split/reordered. Keep `DocPlan = PresentationPlan` and `SectionPlan = PresentationSlidePlan` as module-level aliases through Phase 2 so Track B's imports don't break mid-phase. `NarrativePlan`/`EvidencePack`/`StoryBeat`/`SlidePurpose` are net-new, placed above. |
| **A** | *(new)* `DesignPlan`/`SlideDesignTreatment` **model stubs** (pulled forward from Phase 3's #157) | new `app/services/components/design_plan.py` | Definitions only — `DesignPlan{design_system, theme, visual_direction, density_strategy, slide_treatments}`, `SlideDesignTreatment{slide_id, visual_role: VisualRole, layout_id, component_choices, hierarchy_notes, density_target: DensityTarget, repair_constraints: list[RepairConstraint]}`, plus the `VisualRole`/`DensityTarget`/`RepairConstraint` literal/type defs. No `generate_design_plan()` implementation yet (that's Phase 3) — this gives Track B's #147/#148/#149 a stable object to validate/judge against from the start, per Codex Adjustment 1. |
| **A** | *(new, small)* Optional `BrandProfile`/`UserDocumentProfile` interface fields | `design_plan.py` (or wherever `DesignPlan`/`PresentationPlan` live) | Add `brand_profile_id: str \| None`, `brand_profile: "BrandProfile" \| None`, `user_document_profile: "UserDocumentProfile" \| None` as optional fields (forward-referenced/`Any`-typed placeholders if the real models (#155/#156) don't exist yet). Always `None` until Phase 4, but lets Phase 3's Designer prompt and model shape be built around their presence — avoids a prompt rewrite in Phase 4. |
| **B** | #145 — Wire `FitContract` validation into composer | `compose_docplan.py` (`_section_to_slide` / `compose_docplan_to_pptx_render_plan`) | Uses `FitContract`/`ComponentRuntime` from Phase 1 (Track A, already merged). Add `_validate_fit(zone_instance, zone_spec, fit_contract) -> FitResult` as a **separate function** (`fit_validation.py`) called from the composer — keeps it a distinct stage per reconciliation §5, easing #150's reuse later. Code against `SectionPlan`/`DocPlan` aliases — survives Track A's rename since aliases hold through the phase. |
| **B** | #147 — Deterministic QA checks, **split into plan-level and render-level** | new `app/services/qa/plan_checks.py` + new `app/services/qa/render_checks.py`, wire both into existing QA hook in `documents.py` | Per Codex Adjustment 4: **`plan_checks.py`** runs on `PresentationPlan`/`PptxRenderPlan` *before* rendering — dangling punctuation, duplicate chart labels, missing title/dek/missing final "ask" on decision slides, unsupported component/zone combos, too-many-items vs. `FitContract.max_items`. **`render_checks.py`** runs on rendered output (post-`soffice` thumbnails) — shape-overlap, contrast-below-`qa_thresholds` (from #136), tiny-text risk, empty zones, excessive whitespace. Define shared `QAIssue`/`QAIssueType` taxonomy in a small `qa/types.py` imported by both — #148/#149 reuse it. This split makes Phase 4's repair loop able to target the right layer (plan vs. render). |

**File overlap**: `compose_docplan.py` is touched by both Track A (import
rename, mechanical) and Track B (#145, new validation call). **Sequencing
within the phase**: Track A lands its rename+alias first (small, low-risk
diff) → Track B rebases and adds `_validate_fit` call on top. Track A should
notify Track B once the rename PR is on `v2/integration` for Phase 2.

**Exit criteria**: `PresentationPlan`/`PresentationSlidePlan` validate with
all new optional fields; existing planner/composer tests pass unchanged
(aliases hold); `_validate_fit` runs for all 10-12 components against their
`FitContract`s with at least one overflow-triggering fixture per component;
deterministic QA checks have unit tests for each check type using synthetic
`PptxRenderPlan`s.

---

## 4. Phase 3 (parallel) — Designer Stage + 4-Step Planner ‖ Judges + Golden-File Reorg

**Goal**: Track A builds the new planner steps (narrative, slide, design)
that produce the models from Phase 2; Track B builds the judge layer against
the QA taxonomy from Phase 2 and reorganizes fixtures — both depend on Phase
2 merged, but not on each other within this phase.

| Track | Task | Files touched | Notes |
|---|---|---|---|
| **A** | #141 — Narrative-planning LLM step → `NarrativePlan` | `planner.py` (**new** function `generate_narrative_plan`), prompt templates | First of the 4 steps; structured-output call producing `NarrativePlan` (incl. `storyline: list[StoryBeat]`). Per v2 §20.3, fold "Story Editor" sharpening into this step's prompt/second pass rather than a separate task. |
| **A** | #142 — Slide-planning LLM step → `PresentationSlidePlan` list | `planner.py` (**new** function `generate_presentation_plan`) | Consumes `NarrativePlan.storyline` + `EvidencePack`; produces slide list with `purpose`/`audience_question`/`message`/`evidence` populated. Fold "Evidence Builder" extraction into this step or a thin pre-pass per v2 §20.3. |
| **A** | #157 — `DesignPlan`/`SlideDesignTreatment` + Designer stage (implementation; model stubs already merged in Phase 2) | `app/services/components/design_plan.py` (fill in), `planner.py` (**new** function `generate_design_plan`) | New LLM step between #142's output and the existing layout/component-binding logic. Input: `NarrativePlan` + `PresentationSlidePlan`s + design-system spec (incl. `token_pairs` from #136) + component registry (incl. `FitContract`s from Phase 1) + `BrandProfile`/`UserDocumentProfile` (Phase 2 stub fields, still `None` until #155/#156) + `QualityMode` (type defined Phase 1, default `"standard"` until #151 wires real values). |
| **A** | #143 — Orchestration wrapper, additive | `planner.py` (**new** function `generate_agentdeck_v2_plan()`) | Per Codex Adjustment 5: **do not rewrite `generate_doc_plan()` in place.** `generate_agentdeck_v2_plan()` chains #141→#142→#157→(existing layout/component-binding logic from `generate_doc_plan`, called as a sub-step/extracted helper, reading `SlideDesignTreatment.component_choices` as its candidate set instead of the full registry). `generate_doc_plan()` stays as-is and remains the active path until Phase-3 exit criteria are proven on `v2/integration`; cutover (feature-flagged) happens at integration, not mid-phase. This keeps `planner.py` changes additive — new functions alongside the old, not a rewrite of it. |
| **B** | #148 — `SlideJudgeResult` + slide-level LLM visual judge | new `app/services/qa/slide_judge.py` | Reuses `QAIssue`/`QAIssueType` taxonomy from #147 (Phase 2). Thumbnail-to-judge prompt wiring using existing `soffice`-based thumbnail pipeline (#57). `SlideJudgeResult{slide_id, status, score, severity, issues, repair_strategy, summary}`. References `SlideDesignTreatment`/`repair_constraints` — real models available from Phase 2 stub, no waiting on #157's implementation. |
| **B** | #149 — `DeckJudgeResult` + deck-level LLM judge | new `app/services/qa/deck_judge.py` | `DeckJudgeResult{status, score, storyline_score, design_score, evidence_score, executive_readiness_score, issues, recommended_repairs}`. Storyline/evidence scoring references `NarrativePlan`/`EvidencePack` (Phase 2, already merged — no blocker). If #149 lands before #141/#142's real outputs exist, code against a hand-written `NarrativePlan`/`EvidencePack` fixture. |
| **B** | #154 — Reorganize golden-file harness into component/deck/regression tiers | `tests/golden/` (or wherever harness lives), harness runner script | Mostly file moves + a tiering config; light touch. Independent of #148/#149 but sequenced here since both touch test infra. |
| **B** | *(new)* Lighthouse fixture — **smoke only**, not gating | `tests/golden/lighthouse/` (per #154's new tiering) | Per Codex Adjustment 8: start the "Enterprise AI Platform Consolidation Steering Committee Deck" fixture *now*, run through whatever of #141-143/#157 exists at the time, generated-but-not-strictly-judged — used for visual inspection and to give Track A a real deck to tune the Designer prompt against while building it. Promoted to a strict acceptance gate with judge/repair assertions in Phase 4 (#153). |

**File overlap**: none direct (`planner.py` is Track A only this phase;
Track B works in new `qa/` modules + test infra). **Cross-track interface
dependency**: #149 references `NarrativePlan`/`EvidencePack` (Phase 2,
already merged — no blocker) and conceptually `SlideDesignTreatment.repair_constraints`
(#157, same phase) — #149 can stub `repair_constraints` as `list[str] = []`
and tighten in Phase 4 when #150 wires them together.

**Exit criteria**: full 4-step planner (#141-143 + #157) produces a valid
`PptxRenderPlan` end-to-end for at least 2 sample briefs (one narrative-heavy,
one data-heavy); `SlideJudgeResult`/`DeckJudgeResult` produce scores on the
same sample decks; golden-file harness runs green under the new tiering.

---

## 5. Phase 4 (parallel) — Repair Loop + Lighthouse ‖ Brand/Personalization

| Track | Task | Files touched | Notes |
|---|---|---|---|
| **A** | #155 — `BrandProfile` model + template-upload extraction | new `app/services/brand/brand_profile.py`, upload endpoint wiring | `BrandProfile{id, user_id, source_template_id, logo_assets, color_tokens, font_tokens, layout_preferences, forbidden_patterns, example_slide_images, extracted_components}`. Wire as optional input to #157's `generate_design_plan` (already has the parameter stubbed from Phase 3). |
| **A** | #156 — `UserDocumentProfile` personalization wiring | new `app/services/brand/user_document_profile.py`, wiring into #141/#142 prompts | `UserDocumentProfile{preferred_tone, preferred_depth, preferred_slide_density, brand_profiles, common_audiences, industry_context, writing_style, past_accepted_decks, past_rejected_patterns}`. Check for overlap with existing `user_memory` injection (#38) before adding a parallel mechanism — reconciliation §10 flags this as possible overlap. |
| **B** | #150 — Structural repair loop, **repair at the highest layer that fixes the cause** | new `app/services/qa/repair_loop.py`, wired into `documents.py` after #148/#149 | Per Codex Adjustment 7, repair target depends on issue category, not always `PptxRenderPlan`: weak storyline/argument → repair `NarrativePlan`/`PresentationPlan`; wrong layout/visual role → repair `DesignPlan`; content overflow → `PresentationPlan` (split slide or swap component) via `plan_checks`; token/contrast issue → `PptxRenderPlan`/design-system token resolution via `render_checks`. Targeted repairs per `SlideJudgeResult.repair_instruction`/`DeckJudgeResult.recommended_repairs`, checked against `SlideDesignTreatment.repair_constraints` (real since Phase 2). `block_id`/`slide_id` (Phase 2) give the loop stable addressing across re-renders. Re-render → re-judge loop, capped by iteration count (hardcoded cap now; `quality_mode`-driven cap in #151). |
| **B** | #153 — Promote lighthouse fixture to strict acceptance gate | `tests/golden/lighthouse/` (built as smoke fixture in Phase 3) | "Enterprise AI Platform Consolidation Steering Committee Deck" — full pipeline run (4-step planner + Designer + Composer + Renderer + judges + repair), dark + light themes, now asserted against v2 §10 acceptance criteria with judge/repair assertions added. This is the primary quality gate going forward — run in CI after this phase. |

**File overlap**: none direct. **Sequencing note**: #150 depends on #157
(merged Phase 3) and #148/#149 (merged Phase 3) — both available at Phase 4
start, no stubbing needed. #153 depends on #150 being at least partially
functional to exercise the repair loop — if #150 slips, #153 can initially
run without repair (judge-only) and add repair assertions once #150 lands.

**Exit criteria**: lighthouse deck passes acceptance criteria in both themes;
repair loop demonstrably fixes at least 2 injected defect classes (e.g.
forced overflow, forced contrast violation) without breaking other slides;
`BrandProfile`/`UserDocumentProfile` models validate and `generate_design_plan`
accepts them (even if UI for uploading templates is out of scope here).

---

## 6. Final Joint Step — #151 `quality_mode` Plumbing

After Phase 4 integration, thread `quality_mode: Literal["draft","standard","executive"]`
through:

- `generate_design_plan` (#157) — affects `density_strategy` defaults and
  `BrandProfile` strictness.
- Repair loop (#150) — iteration cap (`draft`=0, `standard`=2, `executive`=5
  as starting defaults).
- Lighthouse/acceptance thresholds (#153) — `executive` mode required to pass
  full acceptance criteria; `draft`/`standard` have relaxed thresholds.
- Chat-facing API (`conversations.py`) — user-selectable or inferred from
  `document_brief`.

Small diffs across many files already built in Phases 1-4 — assign to
whichever track has bandwidth first; low conflict risk since it's additive
parameter-threading.

---

## 7. Summary Table — Task → Track → Phase

| Task | Track | Phase |
|---|---|---|
| #152 | B | 1 |
| #136 | A | 1 |
| #144 | B | 1 |
| #139 | A | 1 |
| #140 | A | 1 |
| #146 | B | 1 |
| #137/#138 | A | 2 |
| #145 | B | 2 |
| #147 | B | 2 |
| #157 (model stubs) | A | 2 |
| #141 | A | 3 |
| #142 | A | 3 |
| #157 (implementation) | A | 3 |
| #143 | A | 3 (additive, `generate_agentdeck_v2_plan()`) |
| #148 | B | 3 |
| #149 | B | 3 |
| #154 | B | 3 |
| lighthouse smoke fixture | B | 3 (promoted in Phase 4) |
| #155 | A | 4 |
| #156 | A | 4 |
| #150 | B | 4 |
| #153 | B | 4 |
| #151 | joint | final |

---

## 8. Risks / Watch Items

- **Phase 2 `compose_docplan.py` overlap**: mitigated by sequencing (Track A's
  rename lands first), but if Track A's rename slips past Track B's start,
  Track B should begin against current `DocPlan`/`SectionPlan` names directly
  — the alias makes either order safe.
- **`planner.py` becomes a hot file in Phase 3** (Track A owns #141-143/#157
  entirely) — no Track B work scheduled there that phase, but if Phase 2
  spills into Phase 3, confirm `selection.py`/`planner.py` diffs from #144
  (Phase 1, Track B) are fully merged before Track A starts #143.
- **#149's dependency on #141/#142 outputs**: if Track A is delayed, Track B
  builds #149 against a hand-written `NarrativePlan`/`EvidencePack` fixture
  (schema already merged in Phase 2) — no hard blocker, just re-validate once
  real outputs exist.
- **#156 vs. existing `user_memory` injection (#38)**: resolve via a short
  spike at the start of Phase 4 before writing `UserDocumentProfile` — may
  reduce #156 to an extension of #38 rather than a new model.
