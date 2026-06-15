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
| **B** | #152 — Fail-fast: remove markdown + legacy-renderer fallback, add `DocumentGenerationFailure` | `documents.py` (`build_document_artifact`), `chat_pipeline.py`, `conversations.py` (surface structured failure as retryable chat message) | Per reconciliation §8 "compatibility freeze": no markdown fallback for requested pptx/docx/xlsx, no legacy-renderer fallback on AgentDeck compose/render failure. |
| **B** | #144 — Feature-flag usage-stats-weighted ranking off by default | `app/services/components/selection.py` or `planner.py` (wherever `load_usage_stats_map`/`rank_components` is invoked with `db`) | Add `AGENTDECK_USAGE_STATS_WEIGHTING_ENABLED` env flag, default `false`. Logging (#127-129) stays active — only the ranking *weight* is gated. |
| **B** | #146 — Audit `render.js` for residual heuristics/raw-color logic | `render.js` | Find/remove any leftover title-string-based archetype inference or raw fg/bg comparisons predating #107-118. Likely small; if nothing found, document that and close. |

**File overlap**: none. `selection.py`/`planner.py` (#144) and `registry.py`
(#139) are adjacent but #144's diff should be ~5 lines (an `if` guard around
the `usage_stats_map` lookup) — low collision risk even if both tracks touch
`planner.py`-adjacent files; confirm via `git diff` before merge.

**Exit criteria**: `pytest` green on `v2/integration`; a forced-failure test
confirms #152's `DocumentGenerationFailure` path (no markdown/legacy
fallback); spec validates with new `token_pairs`/`qa_thresholds` sections;
`FitContract`/`ComponentRuntime` importable with passing unit tests for the
default-derived implementations.

---

## 3. Phase 2 (parallel) — Plan Models + Composer/QA Wiring

**Goal**: split `DocPlan` into the v2 layered models (Track A) while Track B
wires fit-validation and deterministic QA against the *existing* model shape
via an alias, so the two don't block each other.

| Track | Task | Files touched | Notes |
|---|---|---|---|
| **A** | #137/#138 — `NarrativePlan`, `EvidencePack`, `PresentationPlan`/`PresentationSlidePlan` (rename+extend `DocPlan`/`SectionPlan`) | `app/services/components/render_plan.py` (or split into `narrative_plan.py` + `presentation_plan.py`), `compose_docplan.py` (update imports only) | Per reconciliation §3 correction: `DocPlan`/`SectionPlan` *are* `PresentationPlan`/`PresentationSlidePlan` today — rename+extend mechanically (add `slide_id`, `dek`, `purpose: SlidePurpose`, `audience_question`, `message`, `evidence: list[EvidenceRef]`, mostly optional). Keep `DocPlan = PresentationPlan` and `SectionPlan = PresentationSlidePlan` as module-level aliases through Phase 2 so Track B's imports don't break mid-phase. `NarrativePlan`/`EvidencePack`/`StoryBeat`/`SlidePurpose` are net-new, placed above. |
| **B** | #145 — Wire `FitContract` validation into composer | `compose_docplan.py` (`_section_to_slide` / `compose_docplan_to_pptx_render_plan`) | Uses `FitContract`/`ComponentRuntime` from Phase 1 (Track A, already merged). Add `_validate_fit(zone_instance, zone_spec, fit_contract) -> FitResult` as a **separate function** (`fit_validation.py`) called from the composer — keeps it a distinct stage per reconciliation §5, easing #150's reuse later. Code against `SectionPlan`/`DocPlan` aliases — survives Track A's rename since aliases hold through the phase. |
| **B** | #147 — Deterministic QA checks | new `app/services/qa/deterministic_checks.py`, wire into existing QA hook in `documents.py` | Shape-overlap, contrast-below-`qa_thresholds` (from #136), missing title/dek/ask, duplicate chart labels, dangling punctuation, excessive whitespace. Define `QAIssue`/`QAIssueType` here — #148/#149 will reuse this taxonomy. |

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
| **A** | #141 — Narrative-planning LLM step → `NarrativePlan` | `planner.py` (new function `generate_narrative_plan`), prompt templates | First of the 4 steps; structured-output call producing `NarrativePlan` (incl. `storyline: list[StoryBeat]`). Per v2 §20.3, fold "Story Editor" sharpening into this step's prompt/second pass rather than a separate task. |
| **A** | #142 — Slide-planning LLM step → `PresentationSlidePlan` list | `planner.py` (new function `generate_presentation_plan`) | Consumes `NarrativePlan.storyline` + `EvidencePack`; produces slide list with `purpose`/`audience_question`/`message`/`evidence` populated. Fold "Evidence Builder" extraction into this step or a thin pre-pass per v2 §20.3. |
| **A** | #157 — `DesignPlan`/`SlideDesignTreatment` + Designer stage | new `app/services/components/design_plan.py`, `planner.py` (new function `generate_design_plan`) | New LLM step between #142's output and the existing layout/component-binding logic. Input: `NarrativePlan` + `PresentationSlidePlan`s + design-system spec (incl. `token_pairs` from #136) + component registry (incl. `FitContract`s from Phase 1) + `BrandProfile` if present (stub `None` until #155 lands) + `quality_mode` (stub default until #151). |
| **A** | #143 — Wire narrative+slide+design planning into existing layout/component steps | `planner.py` (rename existing `generate_doc_plan` → becomes steps 4-5 of the new pipeline, now consuming `DesignPlan`) | Existing component-selection ranking (#121/#130) now reads `SlideDesignTreatment.component_choices` as the candidate set instead of the full registry — narrows ranking input, doesn't change `rank_components` signature. |
| **B** | #148 — `SlideJudgeResult` + slide-level LLM visual judge | new `app/services/qa/slide_judge.py` | Reuses `QAIssue`/`QAIssueType` taxonomy from #147 (Phase 2). Thumbnail-to-judge prompt wiring using existing `soffice`-based thumbnail pipeline (#57). `SlideJudgeResult{slide_id, status, score, severity, issues, repair_strategy, summary}`. |
| **B** | #149 — `DeckJudgeResult` + deck-level LLM judge | new `app/services/qa/deck_judge.py` | `DeckJudgeResult{status, score, storyline_score, design_score, evidence_score, executive_readiness_score, issues, recommended_repairs}`. Storyline/evidence scoring references `NarrativePlan`/`EvidencePack` — **interface dependency on Track A's Phase 2 models (already merged) and Phase 3 #141/#142 outputs**; if #149 lands before #141/#142 merge, code against the Phase-2 model stubs and a synthetic `NarrativePlan` fixture. |
| **B** | #154 — Reorganize golden-file harness into component/deck/regression tiers | `tests/golden/` (or wherever harness lives), harness runner script | Mostly file moves + a tiering config; light touch. Independent of #148/#149 but sequenced here since both touch test infra. |

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
| **B** | #150 — Structural repair loop on `PresentationPlan`/`DesignPlan`/`PptxRenderPlan` | new `app/services/qa/repair_loop.py`, wired into `documents.py` after #148/#149 | Targeted repairs per `SlideJudgeResult.repair_instruction`/`DeckJudgeResult.recommended_repairs`, checked against `SlideDesignTreatment.repair_constraints` (now real, not stubbed — Track A's #157 from Phase 3 is merged). Re-render → re-judge loop, capped by iteration count (hardcoded cap now; `quality_mode`-driven cap in #151). |
| **B** | #153 — Lighthouse deck fixture + acceptance test | `tests/golden/lighthouse/` (per #154's new tiering) | "Enterprise AI Platform Consolidation Steering Committee Deck" — full pipeline run (4-step planner + Designer + Composer + Renderer + judges + repair), dark + light themes, asserted against v2 §10 acceptance criteria. This is the primary quality gate going forward — run in CI after this phase. |

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
| #141 | A | 3 |
| #142 | A | 3 |
| #157 | A | 3 |
| #143 | A | 3 |
| #148 | B | 3 |
| #149 | B | 3 |
| #154 | B | 3 |
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
