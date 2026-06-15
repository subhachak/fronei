# AgentDeck v2 Reconciliation — v0.1 Shipped Code vs. v2 Plan

Audits the code shipped under v0.1 Phases 1-3 (tasks #103-132,
`app/services/components/`, `app/services/design_systems/agentdeck_v1/`)
against the v2 architecture (`agentdeck_framework_architecture_v2.md`).
Verdict per area: **keep**, **extend**, **rename/split**, or **net-new**.

---

## 1. Design-System Registry — `design_systems/agentdeck_v1/{spec.json, schema.py}`, `registry.py`

**Verdict: keep, extend.**

- `spec.json` already has `themes`/`typography`/`spacing`/`radii`/`elevation`/`grid`/`slide_layouts`/`components`/`rules` per v2 §3's formalized shape.
- **Missing**: `token_pairs` (v2 §3, contrast-safe bg/text/muted/border groupings per component) and `qa_thresholds` (used by deterministic QA, v2 §8.1). Both are additive — no breaking change to `DesignSystemSpec` (schema.py), just new optional sections + Pydantic models.
- The "no raw colors, semantic tokens only" principle is already in place; `token_pairs` formalizes existing per-component token references rather than introducing new ones.

**Action**: add `token_pairs` + `qa_thresholds` sections to `spec.json` + `schema.py`. Small, additive.

**Correction (validated against repo)**: the current schema's color model is `color_tokens.dark`/`color_tokens.light`, not a flat top-level `themes` object. `token_pairs` must be defined as theme-aware (resolvable per `color_tokens.dark`/`color_tokens.light`), not as a single global mapping — otherwise it reintroduces a contrast bug class under the other theme.

---

## 2. Component Registry — `registry.py`, `content_schemas.py`

**Verdict: extend (rename optional).**

- `ComponentDef{id, version, primitive, content_schema, design_system_refs, applicable_slide_layouts, selection_tags, usage_stats}` covers most of v2's revised `ComponentDef` already. Field-name deltas vs. v2 §4.1:
  - `content_schema` ↔ v2's `props_model` — same role (Pydantic schema for `data`/`props`), v2 just renames the *string reference* form. Can keep `content_schema: Type[BaseModel]` as-is; `props_model` in v2 is descriptive, not a hard requirement to rename.
  - `applicable_slide_layouts` ↔ v2's `supported_layouts` — same thing, naming only.
  - `design_system_refs` — kept as-is, not in v2's list but harmless/useful.
- **Missing entirely**: `supported_zones`, `min_zone_width`/`min_zone_height`, `max_items`, `density_range`, `render_function` (string ref — currently implicit via render.js dispatch by `component_id`), and the whole `FitContract`/`ComponentRuntime` protocol (`normalize`/`validate_fit`/`estimate_density`).
- `ComponentUsageStats{uses, qa_failures, user_rejections, success_rate}` matches v2 §12's passive-logging fields conceptually but v2 wants a richer set (`component_version`, `layout_id`, `theme`, `prompt_category`, `doc_type`, `content_density`, `qa_score`, `repair_count`, `downloaded`, `regenerated`, `user_edited`) — current `component_usage_stats` DB table (#127) only has `success_count`/`failure_count`/`last_used_at` per `(component_id, slide_layout, design_system, theme)`. v2 §12's extra fields are additive columns/dimensions, not a redesign.

**Action**: add `FitContract` Pydantic model + per-component fit data (11-12 components — moderate effort, this is the bulk of new work); add `ComponentRuntime` protocol with `validate_fit`/`estimate_density` (can start as thin wrappers — `estimate_density` from existing char-count heuristics in `render.js`/`compose.py`). `supported_zones` can be derived from `spec.json.slide_layouts[*].zones` cross-referenced with `applicable_slide_layouts` — likely don't need to hand-author it.

---

## 3. Plan Models — `render_plan.py` (`DocPlan`/`SectionPlan`/`ContentBlock`/`PptxRenderPlan`/`ZoneInstance`)

**Verdict: split.**

- `PptxRenderPlan`/`PptxSlidePlan`/`ZoneInstance` (v0.1 #112) map **directly** to v2 §2.4 — no changes needed. This is the renderer-facing contract and v2 keeps it as-is.
- `DocPlan`/`SectionPlan`/`ContentBlock` (v0.1 #120) become v2's `PresentationPlan`/`PresentationSlidePlan` (§2.3) **plus** the new `NarrativePlan` (§2.2) and `EvidencePack` above them:
  - `ContentBlock{zone, component_id, component_version, data, notes}` — **unchanged**, reused verbatim as `PresentationSlidePlan.blocks`.
  - `SectionPlan{slide_layout, section_title, blocks, hero_title, subtitle, ...}` → becomes `PresentationSlidePlan`, **adding**: `slide_id`, `dek`, `purpose: SlidePurpose`, `audience_question`, `message`, `evidence: list[EvidenceRef]`. Rename `slide_layout` → `layout_id` is cosmetic (v2 prose uses `layout_id`; keeping `slide_layout` is fine and avoids churn in `compose_docplan.py`).
  - `DocPlan{doc_type, design_system, theme, title, sections}` → becomes `PresentationPlan{design_system, theme, title, subtitle, slides}` — nearly identical, `doc_type` drops (implicit: this *is* the presentation plan) and `sections`→`slides`.
- **Net-new, no current equivalent**: `NarrativePlan`, `StoryBeat`, `EvidenceNeed`, `EvidencePack`, `EvidenceItem`, `EvidenceRef`, `SlidePurpose`.

**Correction (validated against repo)**: don't read this as "DocPlan is format-agnostic, just rename fields." `DocPlan`/`SectionPlan` are *already presentation-shaped* — `slide_layout`, zone-keyed `blocks`, title-slide fields (`hero_title`/`subtitle`/`presenter`/...), and closing-slide fields are baked in. The honest framing is: `DocPlan` *is* what v2 calls `PresentationPlan` (today, without a narrative layer above it). The split is therefore: `DocPlan`/`SectionPlan` → rename+extend → `PresentationPlan`/`PresentationSlidePlan` (mechanical, low risk), and `NarrativePlan`/`EvidencePack` are a genuinely new layer placed *above* it (net-new, higher risk) — not a peer extracted from existing fields.

**Action**: this is the biggest structural change but mechanically additive — `compose_docplan.py`'s `_section_to_slide` mapping logic carries over almost unchanged once `SectionPlan`→`PresentationSlidePlan` rename/extension lands. Recommend: (a) add `NarrativePlan`/`EvidencePack` as new models + new planner step producing them; (b) extend `SectionPlan`→`PresentationSlidePlan` with the 5 new fields (mostly optional initially to avoid breaking existing tests); (c) keep `compose_docplan_to_pptx_render_plan` signature stable, just reading from the renamed/extended model.

---

## 4. Planner — `planner.py` (`generate_doc_plan`, two-step structured output)

**Verdict: extend (add steps before/around existing two).**

- Current `generate_doc_plan`'s step 1 (outline/section+layout selection) and step 2 (component selection per zone, `_candidates_payload`/`_build_blocks_user_message`, `rank_components` with `usage_stats_map`) map to v2 §5 steps 3 ("layout selection") and 4 ("component binding") respectively — **these are reusable as the back half of the v2 four-step planner.**
- **Missing**: v2 steps 1 ("narrative planning" → `NarrativePlan`) and 2 ("slide planning" → story beats → `PresentationSlidePlan`s with purpose/message/evidence). These are new LLM calls that run *before* the current step 1, and their output (`NarrativePlan.storyline`) becomes the input "outline" that today's step 1 consumes — i.e. today's step 1 input shape needs to be derivable from `NarrativePlan.storyline` + `EvidencePack` rather than directly from `route`/`prompt_text`.
- `rank_components`/`load_usage_stats_map`/`score_component` (#121/#130, `selection.py`) are unaffected by the narrative layer — they operate at the component-binding step regardless of what feeds the slide list.

**Action**: add two new structured-output calls upstream of `generate_doc_plan`'s existing logic; existing logic becomes steps 3-4 of a renamed `generate_presentation_plan`. `usage_stats`-weighted ranking (#130) should be **gated off by default** per v2 §12 (start passive) until volume justifies it — currently it's always-on when `db` is passed. Recommend adding a feature flag (`AGENTDECK_USAGE_STATS_WEIGHTING_ENABLED`, default off) rather than reverting #130's code.

---

## 5. Composer — `compose_docplan.py`

**Verdict: keep, extend.**

- `compose_docplan_to_pptx_render_plan` is already "mostly deterministic" per v2 §6 — no LLM call, just `DocPlan`→`PptxRenderPlan` reshape + synthesized title slide. This *is* the v2 Composer, modulo the renamed input model (§3 above).
- **Missing**: explicit `FitContract`/`validate_fit` invocation per zone before emitting `ZoneInstance` (v2 §6 "run fit checks, choose component variants"). Currently fit/overflow is only caught downstream by render QA (#57-58), i.e. post-render, not pre-render.
- **Missing**: "Plan Validator / Fit Validator" as a distinct pipeline stage (v2 §1 diagram) between Composer and Renderer — could be folded into the composer function itself rather than a separate module.

**Action**: once `FitContract` exists (§2 above), add a `_validate_fit(zone_instance, zone_spec, fit_contract) -> FitResult` call inside `_section_to_slide`/`compose_docplan_to_pptx_render_plan`, surfacing `FitResult.overflow_strategy` actions (e.g. trigger `change_component` by re-querying `rank_components` with a narrower candidate set, or `split_slide` by emitting an extra `PptxSlidePlan`).

---

## 6. Renderer — `render.js` (PptxGenJS dispatch)

**Verdict: keep, audit.**

- v0.1 Phase 2 already rewrote dispatch around `(slide_layout, zone, component_id)` + token resolution (#107-109) — this is the v2 §7 "dumb renderer" model. No fundamental rework expected.
- **Action**: targeted audit pass for any remaining heuristic/title-based branches or raw-color comparisons left over from the pre-agentdeck renderer (v2 §18 "what not to do") — likely small, since #107-118 already did most of this. Lower priority than §1-5.

---

## 7. QA — `run_pptx_render_qa`/`repair_deck_plan_for_qa` (documents.py), `usage_stats.py`

**Verdict: extend significantly — biggest gap.**

- Deterministic checks (v2 §8.1) partially exist (#57-58's LibreOffice-based render QA: dense_text/dense_ink/tiny_text_risk flags feed `usage_stats.log_render_qa_failures`, #129). **Missing**: explicit checks for shape-overlap, contrast-below-threshold, missing title/dek, missing decision/ask on decision slides, duplicate chart labels as a *general* rule (some chart-specific fixes existed pre-agentdeck but weren't ported as generic checks).
- **Missing entirely**: `SlideJudgeResult`/LLM visual judge (§8.2), `DeckJudgeResult`/deck-level judge (§8.3), and the structural repair loop operating on `PresentationPlan`/`PptxRenderPlan` (§8.4) — current repair (`repair_deck_plan_for_qa`) is a single LLM pass over the whole deck JSON, not a per-slide judge→targeted-repair→re-render→re-judge loop.
- Golden-file harness (#110-111) exists; v2 §9's tiering (component/deck/regression fixtures) is a reorganization of what's there, not new infra.

**Action**: this is the largest net-new workstream — `SlideJudgeResult`/`DeckJudgeResult` Pydantic models, thumbnail-to-judge prompt wiring (thumbnails already produced by #57's pipeline), structural repair targeting `PresentationPlan`/`PptxRenderPlan` fields, and iteration caps wired to `quality_mode` (§15, also net-new — no quality-mode concept exists today).

---

## 8. Fail-Fast / Markdown Fallback (v2 §16)

**Verdict: confirmed gap, not just an audit — fix directly.**

- `build_document_artifact()` still contains both: (a) a fallback to Markdown when a requested binary format (pptx/docx/xlsx) fails to render, and (b) a fallback to the *legacy* (non-agentdeck) PPTX renderer when AgentDeck compose/render fails. Both directly violate the v2 principle: if PPTX was requested, do not silently degrade to markdown or to the legacy renderer.
- Replace both fallback branches with `DocumentGenerationFailure{stage, user_message, retryable, debug_info}` returned to `conversations.py` and surfaced as a retry-able chat message — no silent format/quality downgrade.

**Action**: targeted fix (not just grep/audit) in `documents.py`'s `build_document_artifact()` and any `chat_pipeline.py` callers. Sequence **first** — directly addresses a named trust problem and is low-effort relative to impact.

### Compatibility freeze (new)

To make the cutover behavior unambiguous going forward:

- Legacy `DeckPlan` JSON parser (`parse_deck_plan`): retained only for explicit legacy/dev test paths, never invoked from the live generation path.
- New generation path is strictly: `NarrativePlan` → `PresentationPlan` → `PptxRenderPlan`.
- No markdown fallback when a binary format (pptx/docx/xlsx) was requested.
- No legacy-renderer fallback when AgentDeck compose/render fails.
- All such failures return a structured `DocumentGenerationFailure` to the chat layer.

---

## 9. Net-New, No Current Equivalent

- `NarrativePlan`, `StoryBeat`, `EvidenceNeed`, `EvidencePack`, `EvidenceItem`, `EvidenceRef`, `SlidePurpose` (§2.2/2.3 above)
- `FitContract`, `ComponentRuntime` protocol (§2 above)
- `SlideJudgeResult`, `DeckJudgeResult`, structural repair loop (§7 above)
- `BrandProfile` + template-upload extraction (v2 §11)
- `UserDocumentProfile` personalization (v2 §13)
- `quality_mode` (v2 §15)
- `DocumentGenerationFailure` (v2 §16, §8 above)
- Lighthouse deck fixture + acceptance test (v2 §10)

---

## 10. Summary Table

| Area | v0.1 status | v2 verdict | Relative effort |
|---|---|---|---|
| Design-system registry | shipped | extend (+token_pairs, +qa_thresholds) | small |
| Component registry | shipped | extend (+FitContract, +ComponentRuntime) | medium |
| Plan models | shipped (`DocPlan`) | split → `NarrativePlan`+`EvidencePack`+`PresentationPlan` | large (structural, mechanical) |
| Planner | shipped (2-step) | extend to 4-step (prepend narrative+slide planning) | large |
| Composer | shipped | extend (+fit validation) | medium |
| Renderer | shipped | audit only | small |
| QA / judges / repair | partially shipped | major extension (slide+deck judges, structural repair, quality modes) | largest |
| Fail-fast | gap | audit + fix | small |
| Brand profiles | none | net-new | large |
| Personalization profile | none | net-new (may overlap existing `user_memory` injection, #38) | medium |
| Usage stats | shipped, active | keep, gate weighting off by default | small |
| Lighthouse deck | none | net-new fixture + test | small-medium |

**Nothing shipped in Phases 1-3 needs to be thrown away.** v2 is additive and
restructuring, not a rewrite — the riskiest/largest items are the new
narrative/evidence/judge layers, which are genuinely new product surface
rather than rework of existing code.

---

## 11. Revised Priority Order (post-validation)

1. Fail-fast / no silent markdown or legacy-renderer fallback (§8) — **was #152, now first**.
2. `token_pairs` + `qa_thresholds`, theme-aware (§1) — #136.
3. Gate usage-stats weighting off by default (§4) — part of #144, pulled forward.
4. `FitContract` + fit validation in composer (§2, §5) — #139, #145.
5. `NarrativePlan`, `EvidencePack`, `PresentationPlan`/`PresentationSlidePlan` (§3) — #137, #138.
6. Extend planner to 4 steps (§4) — #141-143.
6a. **Designer / Art Director stage — `DesignPlan`/`SlideDesignTreatment` (v2 §20)** — #157, inserted between #143 and #147. Composer (#145) and the deterministic/LLM QA steps below should target `DesignPlan` rather than `PresentationPlan` directly once #157 lands.
7. Deterministic QA gaps: duplicate labels, dangling punctuation, missing dek, missing decision/ask, whitespace (§7) — #147.
8. Slide-level LLM judge — #148.
9. Structural repair loop — #150 (repairs checked against `SlideDesignTreatment.repair_constraints` per v2 §20.2).
10. Lighthouse deck fixture — #153.
11. Brand profiles / personalization — #155, #156.

This reorders the original #136-156 backlog: **#152 moves to the front**, the usage-stats-weighting portion of #144 is pulled forward to run alongside #136, and **#157 (Designer/DesignPlan, net-new per v2 §20) is inserted after the 4-step planner extension and before deterministic QA**. Evidence Builder and Story Editor (v2 §20.3) are not separate tasks — they're refinements folded into #141/#142. Asset Manager, Chart/Viz Designer, and Compliance/Fact Guard (v2 §20.3) are deferred, not scheduled.
