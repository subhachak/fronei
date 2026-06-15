# AgentDeck Framework — Target Architecture (v0.1 Proposal)

## 0. Framing

This replaces the current 5-theme, archetype-by-title-heuristic PPT renderer with a **document-generation framework**: a planner that turns raw data into a structured plan, a typed plan→render contract, a growing **component library**, and per-format **renderers** (PPTX first, DOC/XLSX later). The `AgentDeck_DS_Spec.json` (2 themes: dark/light) becomes the first entry in a **design-system registry**, not a hardcoded constant.

Everything below is organized so PPTX ships first but nothing is PPTX-specific at the contract level.

---

## 1. Layer Map

```
raw input (data, briefs, prior decks, user intent)
        │
        ▼
┌────────────────────┐
│   1. PLANNER        │  → DocPlan (format-agnostic JSON, schema-validated)
│  (LLM + retrieval)  │
└────────────────────┘
        │
        ▼
┌────────────────────┐
│ 2. COMPOSER         │  → RenderPlan (format-specific, e.g. PptxRenderPlan)
│ (binds plan to      │     resolves: layout template + component instances
│  design system +    │     + design-system token refs (NOT raw colors)
│  component library) │
└────────────────────┘
        │
        ▼
┌────────────────────┐
│ 3. RENDERER         │  PPTX: render.js (PptxGenJS)
│ (per format)        │  DOCX: docx renderer (future)
│                      │  XLSX: xlsx renderer (future)
└────────────────────┘
        │
        ▼
┌────────────────────┐
│ 4. QA / FEEDBACK    │  golden-file diff + vision critique + user edits
│                      │  → component library scoring (learning loop)
└────────────────────┘
```

Key principle: **layers 1–2 are format-agnostic in shape** (same Pydantic base classes), layer 3 is the only format-specific code, and layer 4 feeds usage/quality signals back into layers 1–2's selection weights — this is the "learn over time" mechanism.

---

## 2. Design-System Registry (replaces the 5 hardcoded themes)

`app/services/design_systems/` — one JSON per design system, validated against a shared schema derived from `AgentDeck_DS_Spec.json`:

```
design_systems/
  agentdeck_v1/
    spec.json          # = AgentDeck_DS_Spec.json (dark+light token sets, typography,
                        #   spacing, elevation, radius, grid, components, slide_layouts,
                        #   generation_rules)
    schema.py           # Pydantic models for the above (DesignSystemSpec)
  registry.py           # list/get design systems by id; default = agentdeck_v1
```

- **Themes become token sets within a design system**, not separate systems. `agentdeck_v1` ships with exactly `dark` and `light`. The old 5 themes (warm-editorial, modern-tech, etc.) are retired — or, if you want to keep them as a migration path, they become a second design system (`legacy_v1`) that old decks can still reference, but the planner never selects it for new decks.
- **Token-pair contract**: every component definition in `spec.json.components` already references *semantic* tokens (`text.on_accent`, `bg.surface_1`, `accent.primary_muted`, etc.) — render.js must resolve these via `theme = pick(dark|light)` and never compare raw fg/bg luminance again. This directly retires the `heroTones()` workaround from #100 — it becomes unnecessary because every component's fill/text token pair is pre-declared and guaranteed contrast-safe per theme.

---

## 3. Component Library (the growable part)

`app/services/components/` — each component is a **typed, versioned definition** with:

```python
class ComponentDef(BaseModel):
    id: str                      # "stat_card", "risk_table", "operating_model_lanes"
    version: str
    primitive: LayoutPrimitive    # enum: CARD_GRID, TABLE, DIAGRAM, STAT_STRIP, TEXT, TIMELINE, DIVIDER
    content_schema: type[BaseModel]   # strict pydantic schema for the data this component needs
    design_system_refs: list[str]     # which DS component keys it maps to (e.g. "stat_card", "table")
    applicable_slide_layouts: list[str]  # which slide_layouts (CONTENT_2COL, CONTENT_4COL, ...) it fits
    selection_tags: list[str]         # planner hints: ["financial","comparison","risk","kpi",...]
    usage_stats: ComponentUsageStats  # populated/updated by QA + user-edit feedback loop
```

This is the direct generalization of the current archetype zoo (`risk_register`, `operating_model`, `architecture`, `recommendation`, `stat_cards`, ...). Each existing archetype renderer in `render.js` becomes **one ComponentDef + one render function registered under that id** — no behavior is thrown away initially, it's just re-registered under the new contract. New components can be added without touching the planner's code, only the registry.

**Library growth / learning loop** (concrete, not aspirational):
1. Every generated deck logs which `(slide_layout, component_id, design_system, theme)` combos were used.
2. QA stage (golden-file + vision critique, see §6) scores each rendered slide for overflow/contrast/density issues → `usage_stats.failure_rate`.
3. If a user edits/regenerates a slide, that's logged as an implicit negative signal for the component+content combo that was used.
4. Planner's component-selection step (§4) weights candidates by `usage_stats.success_rate` for the given `selection_tags`/data shape — components that consistently fail get deprioritized, new components added to the registry start neutral and accrue their own track record.
5. This requires zero ML infra to start — it's a scored lookup table (SQLite/Postgres table `component_usage_stats`). Can graduate to embedding-based retrieval later if the library gets large.

---

## 4. Planner

Input: raw data (tables, bullet text, prior deck, KPIs, free-form brief) + target design system id + theme.

Output: **`DocPlan`** (format-agnostic):

```python
class DocPlan(BaseModel):
    doc_type: Literal["presentation","document","spreadsheet"]
    design_system: str          # e.g. "agentdeck_v1"
    theme: Literal["dark","light"]
    title: str
    sections: list[SectionPlan]

class SectionPlan(BaseModel):
    section_title: str | None
    slide_layout: str            # for PPT: one of spec.slide_layouts keys (CONTENT_2COL, CONTENT_4COL, ...)
    blocks: list[ContentBlock]   # one per zone in the slide_layout

class ContentBlock(BaseModel):
    zone: str                    # e.g. "col_left", "hero", "table"
    component_id: str            # resolved from component library
    data: dict                   # validated against component's content_schema
    notes: str | None            # speaker notes
```

Planner is two LLM-assisted steps, both **structured-output, schema-constrained**:
1. **Section/layout selection**: given the raw data, choose `slide_layout` per section from `spec.slide_layouts` (8 layouts: TITLE, SECTION_HEADER, CONTENT_1COL/2COL/3COL/4COL, CONTENT_HERO_STAT, CONTENT_TABLE_SIDEBAR, CONTENT_SPLIT_DECISIONS, CLOSING).
2. **Component selection per zone**: given a zone's shape (w/h from `slide_layouts[layout].zones[zone]`) and the data assigned to that section, pick the best-fit `component_id` from the library whose `content_schema` the data can satisfy and whose `applicable_slide_layouts` includes this layout — ranked by `selection_tags` match + `usage_stats`.

This is where `parse_deck_plan`/`compose_deck_plan_parallel`'s current title-text heuristics get replaced: layout and component choice become **explicit structured-output fields**, not inferred from string matching on titles.

---

## 5. Composer → RenderPlan (the typed compose→render contract)

`RenderPlan` is the JSON actually sent to `render.js` (or future docx/xlsx renderers). For PPTX:

```python
class PptxRenderPlan(BaseModel):
    design_system: DesignSystemSpec   # full resolved spec.json (already theme-aware)
    theme: Literal["dark","light"]
    slides: list[PptxSlidePlan]

class PptxSlidePlan(BaseModel):
    slide_layout: str                 # key into design_system.slide_layouts
    zones: dict[str, ZoneInstance]    # zone name -> rendered component instance

class ZoneInstance(BaseModel):
    component_id: str
    component_version: str
    props: dict                       # validated, component-specific
    notes: str | None
```

This is validated with Pydantic **before** being handed to `render.js`. `render.js`'s job shrinks to: for each `(slide_layout, zone, component_id)`, call the matching render function, resolving all colors via `design_system.color_tokens[theme]`. No more archetype-inference-from-title in JS. This is a from-scratch rewrite of `render.js`'s dispatch layer but the actual drawing primitives (rounded rects, tables, icon badges, stat cards, timeline nodes) are largely portable from the current ~2000 lines — they get re-registered against `spec.json.components` definitions rather than the old ad hoc archetypes.

---

## 6. QA Gate (now mandatory, not optional)

Per `generation_rules.mandatory`: "QA: always generate slide thumbnails and inspect for overflow before delivering." Implement as a pipeline stage, not a manual step:

1. Render → PDF → PNG per slide (existing `soffice`/`pdftoppm` pipeline, already proven).
2. **Golden-file regression**: for every `(slide_layout, component_id, theme)` combo, maintain an approved baseline PNG. New renders diff against baseline; drift beyond threshold flags for review. This is what makes "presentable to CTO/CEO" enforceable rather than hoped-for.
3. **Vision-critique pass** (cheap model, e.g. Haiku): check each rendered slide PNG for text overflow, contrast violations, empty zones — feeds `usage_stats.failure_rate` (§3).
4. CI runs (1)+(2) for the full layout×component×theme matrix whenever `spec.json` or a component render function changes — this is the safety net that #100/#101-style regressions should have been caught by.

---

## 7. Multi-format Extension Path (DOC/Excel)

Because layers 1–2 (`DocPlan`, component library, design-system registry) are format-agnostic:

- **DOCX**: new `slide_layouts`-equivalent ("page_layouts": cover, section, body, table_page) + a `docx` renderer consuming the same `ContentBlock`/component-id contract, mapping `component_id` → python-docx drawing calls. Token resolution (colors/typography/spacing) reuses `design_system.spec.json` directly — Word supports hex colors and point sizes the same way.
- **XLSX**: components become "sheet regions" (KPI summary block, data table, chart) instead of slide zones; `slide_layouts` → `sheet_layouts`. Planner step 1 (layout selection) and step 2 (component selection) are unchanged in shape — only the layout/component catalogs and the renderer differ.
- Net effect: adding a format means writing one new renderer + one new layout/component catalog. The planner, design-system registry, component-library schema, usage-stats learning loop, and QA gate are shared.

---

## 8. Migration Plan (phased, minimal-risk)

**Phase 1 — Foundation (no behavior change yet)**
- Add `design_systems/agentdeck_v1/spec.json` (the uploaded spec) + Pydantic schema.
- Add `ComponentDef` registry seeded by wrapping each *existing* render.js archetype function (risk_register, operating_model, architecture, recommendation, stat_cards, comparison, timeline, etc.) as a component under the new ids, mapped to `agentdeck_v1` tokens for both dark/light.
- Add the golden-file QA harness (render every component × slide_layout × theme combo, capture baselines).

**Phase 2 — Contract**
- Introduce `DocPlan`/`PptxRenderPlan`/`ComponentDef` Pydantic models.
- Rewrite `_js_slide_from_deck_spec` + `render.js` dispatcher to consume `PptxRenderPlan` directly (typed, no title-heuristic archetype inference).
- Retire the 5 legacy themes for new decks; `agentdeck_v1` (dark/light) becomes default.

**Phase 3 — Planner**
- Replace `parse_deck_plan`'s heuristics with the two-step structured-output planner (layout selection, component selection) producing `DocPlan`.
- Wire `usage_stats` table + feedback loop.

**Phase 4 — Multi-format**
- DOCX renderer using the same `DocPlan`/component contract.
- XLSX renderer.

---

## 8.5 Integration with the Current Chat Pipeline

Today's flow (per codebase trace):

```
conversations.py (chat endpoint)
  → planner.py: Plan{wants_document_output, document_brief}  (high-level only:
     doc_type/title/audience/tone/length — no slide content)
  → chat_pipeline.py: generate_document_output()
     - draft LLM pass writes DeckPlan JSON directly as free text
     - revision LLM pass tightens it
     - parse_deck_plan(body) validates
  → documents.py: build_document_artifact()
     - parse_deck_plan → compose_deck_plan_parallel → generate_pptx_bytes
     - QA/repair loop (run_pptx_render_qa / repair_deck_plan_for_qa)
  → base64 pptx returned as document_preview
```

The new framework slots in as a **drop-in replacement for the two LLM passes inside `generate_document_output`**, without changing the chat endpoint's contract (`conversations.py` still gets back `(result, doc_body, chat_summary, doc_type)` and calls `build_document_artifact` the same way):

| Current | Replacement |
|---|---|
| `planner.py` `Plan.document_brief` (doc_type/title/audience/tone/length) | Unchanged — still the first-pass intent classifier. Add `design_system` + `theme` fields (default `agentdeck_v1`/`light`), inferable from brief or user preference. |
| `generate_document_output()` draft pass — LLM freeform DeckPlan JSON | Replaced by **Planner step 1+2** (§4): structured-output calls producing `DocPlan` (section/slide_layout selection, then component selection per zone), constrained by Pydantic schema + the component-library registry (§3). Same LLM, same call site, different system prompt + structured output target. |
| `generate_document_output()` revision pass | Becomes the **Composer** (§5): `DocPlan` → `PptxRenderPlan`, resolving design-system tokens and component versions. This is largely deterministic/code, not an LLM pass — cuts one LLM round trip. |
| `parse_deck_plan(body)` in `document_generator.py` | Becomes `PptxRenderPlan.model_validate(...)` — same validation role, new schema. |
| `compose_deck_plan_parallel` / `_js_slide_from_deck_spec` | Replaced by the Composer's zone-resolution logic (§5) — same file (`document_generator.py`), new internals. |
| `generate_pptx_bytes` → `render.js` | `render.js` dispatch rewritten per §5 (component_id-driven, not archetype-inferred-from-title). |
| `run_pptx_render_qa` / `repair_deck_plan_for_qa` in `build_document_artifact` (`documents.py`) | Extended into the QA gate (§6): same hook point, now also logs `usage_stats` for the learning loop and runs golden-file diff in CI (not per-request). |

**Net effect on the chat experience**: unchanged surface (user asks for a deck in chat, gets a pptx preview back); what changes is everything between "user request" and "pptx bytes" — one fewer LLM pass, schema-validated at every hop, theme fixed to `agentdeck_v1` dark/light.

**Backward compatibility**: existing decks generated under the 5 old themes remain renderable (old `render.js` archetype functions kept as `legacy_v1` design system per §9.1) — only *new* generations route through the new pipeline. `generate_document_output` can feature-flag between old/new path during Phase 2/3 rollout.

---

## 9. Decisions

1. **Legacy themes: retired, clean break.** Per your steer ("if legacy is too much drag, start from scratch"), the old 5-theme system (warm-editorial, modern-tech, executive-navy, data-product-os, clean-light) and its ~15 archetype renderers in `render.js` are **not carried forward as a `legacy_v1` design system**. New pipeline = `agentdeck_v1` (dark/light) only, from day one. This removes §8.5's "backward compatibility" row and the feature-flag — `generate_document_output`/`build_document_artifact` cut over directly. Any decks already generated under the old themes remain as static files (already-rendered .pptx); they're just not regenerable through the new code path. This significantly de-scopes Phase 1: no need to wrap/port 15 old archetypes — only build the ~9-10 components actually defined in `AgentDeck_DS_Spec.json.components` (header_bar, card + color variants, stat_card, badge, divider, bullet_list, table, callout_bar, progress_bar, icon_circle, timeline_node) against the 8 `slide_layouts`.

2. **Phase 1 done before new feature work**, ad hoc fixes on the current system frozen once Phase 1 starts.

3. **Component scope for Phase 1**: build directly from `AgentDeck_DS_Spec.json.components` (the ~10 components listed above) rather than porting old archetypes — these already have explicit token bindings, variants, and slot definitions, so they're closer to "build new" than "migrate."

**Next**: start Phase 1 — `design_systems/agentdeck_v1/` (spec + Pydantic schema), `ComponentDef` registry + render functions for the spec's ~10 components, golden-file harness across component × slide_layout × theme.
