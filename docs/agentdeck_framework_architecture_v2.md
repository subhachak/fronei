# AgentDeck Framework — Target Architecture (v2)

> Supersedes `agentdeck_framework_architecture.md` (v0.1), which is kept as a
> historical record. v0.1 Phases 1-3 (tasks #103-132 — design-system registry,
> component library, golden-file harness, `DocPlan`/`PptxRenderPlan`
> contracts, structured-output planner, composer, `usage_stats` loop) are
> **shipped and tested**. v2 reframes and extends that work: the core
> philosophical shift is —
>
> **The output is not a PowerPoint file. The output is a decision-ready
> artifact.**

## 0. Executive Take

The v0.1 direction is correct and materially better than the legacy system,
which is still: *LLM writes slide-ish JSON → parser normalizes → renderer
guesses visual intent → QA catches some defects.* That's why bugs like
dangling text, misdispatched section slides, wasted recommendation-slide
space, colliding chart labels, and "markdown dressed as PPT" kept recurring.

v2 moves to: *understand the user's objective → build a persuasive narrative
→ choose slide jobs → bind each slide to components/layouts → render from a
design system → visually critique → repair → deliver.*

v2 keeps everything sound from v0.1 (design-system registry, component
library, token-based rendering, `usage_stats` loop, clean break from legacy
themes) and adds: a narrative/story layer above the deck plan, mandatory slide
purpose, fit contracts on components, a two-tier QA judge (slide + deck),
structural repair, brand profiles, and fail-fast behavior for PPTX requests.

---

## 1. Pipeline Overview

```
User request / chat context / uploaded files / brand profile
        │
        ▼
Intent Classifier
        │
        ▼
Narrative Planner          → NarrativePlan
        │
        ▼
Evidence Organizer          → EvidencePack
        │
        ▼
Presentation Planner        → PresentationPlan
        │
        ▼
Component Composer          → PptxRenderPlan
        │
        ▼
Plan Validator / Fit Validator
        │
        ▼
Pptx Renderer                → .pptx
        │
        ▼
Thumbnail Renderer            → slide PNGs
        │
        ▼
Deterministic QA + LLM Judge
        │
        ├── pass → deliver
        │
        └── revise → repair PresentationPlan / RenderPlan → render again
```

Key principle (carried from v0.1): **narrative is format-agnostic, layout is
not.** `NarrativePlan` is shared across PPTX/DOCX/XLSX; `PresentationPlan`,
`DocumentPlan`, `WorkbookPlan` are format-specific siblings; `PptxRenderPlan`
(and future `DocxRenderPlan`/`XlsxRenderPlan`) are renderer-ready.

---

## 2. Plan Models — Split into Three Layers

v0.1's `DocPlan` mixed format-agnostic concepts (title, sections) with
presentation-specific ones (`slide_layout`, `zone`, `CONTENT_2COL`,
`CONTENT_HERO_STAT`, speaker notes). v2 splits this cleanly:

### 2.1 `NarrativePlan` (format-agnostic — new, top-level)

A good consultant doesn't start by choosing slide layouts — they decide the
argument first: who is this for, what decision must the audience make, what's
the thesis, what's the minimum evidence to be credible, what objections will
arise, what sequence persuades, what's excluded, what's the final ask.

```python
class StoryBeat(BaseModel):
    order: int
    purpose: Literal[
        "frame_problem", "quantify_impact", "explain_cause",
        "compare_options", "make_recommendation", "show_target_state",
        "sequence_execution", "manage_risk", "ask_for_decision",
    ]
    message: str
    required_evidence: list[str]
    suggested_visual: str | None

class EvidenceNeed(BaseModel):
    description: str
    criticality: Literal["required", "supporting", "optional"]

class NarrativePlan(BaseModel):
    audience: str
    audience_level: Literal["exec", "board", "manager", "technical", "client", "mixed"]
    objective: str
    core_thesis: str
    decision_or_action_needed: str | None
    context_summary: str
    storyline: list[StoryBeat]
    evidence_needs: list[EvidenceNeed]
    assumptions: list[str]
    risks_or_objections: list[str]
    desired_takeaway: str
```

Layout/component selection serves the story, not the reverse. Without this
layer the planner can still pick good-looking components and produce a
mediocre deck; with it, every slide exists because the narrative needs it.

### 2.2 `EvidencePack` (format-agnostic — new)

Makes evidence discipline explicit, so strategy decks aren't built on
hallucinated numbers.

```python
class EvidenceItem(BaseModel):
    id: str
    source_type: Literal["user_provided", "research", "calculation", "inference", "assumption"]
    content: str
    confidence: Literal["high", "medium", "low"]
    citation: str | None
```

Slides reference evidence via `evidence_refs: list[str]`. This supports:
avoiding hallucinated numbers, useful speaker notes, appendix/source slides,
future auditability, and sharper judge critique (e.g. "is this $4-6M figure
backed by an evidence item?").

### 2.3 `PresentationPlan` (presentation-specific — replaces most of v0.1's `DocPlan`/`SectionPlan`)

```python
SlidePurpose = Literal[
    "orient_audience", "frame_problem", "quantify_impact",
    "diagnose_root_cause", "compare_options", "recommend_decision",
    "show_target_state", "explain_architecture", "show_roadmap",
    "identify_risks", "request_approval", "summarize_takeaways",
]

class EvidenceRef(BaseModel):
    evidence_id: str
    note: str | None = None

class PresentationSlidePlan(BaseModel):
    slide_id: str
    title: str
    dek: str | None                 # subtitle / supporting line
    purpose: SlidePurpose
    audience_question: str          # e.g. "Why not build this internally?"
    message: str                    # e.g. "Managed platform delivers equivalent
                                     #        capability faster at lower 3-yr TCO."
    evidence: list[EvidenceRef]
    layout_id: str                  # presentation-specific layout (was slide_layout)
    blocks: list[ContentBlock]      # unchanged shape from v0.1
    speaker_notes: str | None

class PresentationPlan(BaseModel):
    design_system: str
    theme: Literal["dark", "light"]
    title: str
    subtitle: str | None
    slides: list[PresentationSlidePlan]
```

`purpose` + `audience_question` give the QA judge a much sharper test than
"does the slide overflow?" — namely: *does this rendered slide answer its
audience question?*

### 2.4 `PptxRenderPlan` (unchanged in shape from v0.1)

`PptxRenderPlan{design_system, theme, slides: PptxSlidePlan[]}`,
`PptxSlidePlan{slide_layout, zones: dict[str, ZoneInstance]}`,
`ZoneInstance{component_id, component_version, props, notes}` — still
Pydantic-validated, still token-based (no raw colors — see §4.1), still the
only thing `render.js` consumes.

### 2.5 Future siblings

- **`DocumentPlan`** (DOCX): sections, page structures, callouts, tables, exhibits — built from the same `NarrativePlan`/`EvidencePack`.
- **`WorkbookPlan`** (XLSX): sheets, tables, calculations, charts, dashboard areas — same.

---

## 3. Design-System Registry — Formalized Spec Sections

v0.1's `design_systems/agentdeck_v1/{spec.json, schema.py}` + `registry.py`
structure is correct. v2 formalizes `spec.json`'s top-level shape:

```json
{
  "id": "agentdeck_v1",
  "version": "1.0.0",
  "themes": {},
  "typography": {},
  "spacing": {},
  "radii": {},
  "elevation": {},
  "grid": {},
  "slide_layouts": {},
  "components": {},
  "rules": {},
  "qa_thresholds": {}
}
```

### 4.1 Token Pairs (new requirement)

All visual values must be semantic tokens, never raw hex/px:

```json
// Good
{ "fill": "bg.surface_1", "text": "text.primary", "border": "border.subtle" }

// Bad
{ "fill": "#121A24", "text": "#FFFFFF" }
```

`spec.json` should additionally declare **token pairs** so contrast-safe
combinations are pre-validated, not computed at render time:

```json
"token_pairs": {
  "card.default":   { "bg": "bg.surface_1", "text": "text.primary",
                       "muted": "text.secondary", "border": "border.subtle" },
  "accent.primary":  { "bg": "accent.primary", "text": "text.on_accent" }
}
```

This is the formal version of v0.1's "retire `heroTones()`" goal — the
renderer resolves token pairs only, never compares luminance.

---

## 4. Component Library — Stricter `ComponentDef` + Fit Contracts

v0.1's `ComponentDef` is the right starting shape but under-specifies *how
well* a component can render given data, not just *whether* it can.

### 4.1 Revised `ComponentDef`

```python
class ComponentDef(BaseModel):
    id: str
    version: str
    primitive: LayoutPrimitive
    props_model: str                 # was content_schema: type[BaseModel]
    supported_layouts: list[str]     # was applicable_slide_layouts
    supported_zones: list[str]       # new
    selection_tags: list[str]
    min_zone_width: float | None
    min_zone_height: float | None
    max_items: int | None
    density_range: tuple[float, float]
    render_function: str
    qa_rules: list[str]
```

```python
class ComponentRuntime(Protocol):
    def normalize(self, raw: dict) -> BaseModel: ...
    def validate_fit(self, props: BaseModel, zone: ZoneSpec) -> FitResult: ...
    def estimate_density(self, props: BaseModel, zone: ZoneSpec) -> float: ...
```

The question shifts from *"can this component render this data?"* to *"can
this component render this data **beautifully in this zone**?"* — e.g. a stat
card handles 4 metrics but not 9; a timeline handles 3-5 phases (6 max); a
comparison matrix handles 2-3 options, not 6; a risk table handles 4-6 risks,
not 14; a chart legend has a character budget per label.

### 4.2 `FitContract` (new, mandatory per component)

```python
class FitContract(BaseModel):
    max_title_chars: int
    max_subtitle_chars: int
    max_items: int
    max_chars_per_item: int
    min_font_size: int
    overflow_strategy: Literal[
        "truncate_at_boundary", "move_to_notes", "split_slide",
        "change_component", "fail",
    ]
```

Example:

```json
"option_score_matrix": {
  "max_options": 3,
  "max_summary_chars": 90,
  "score_dimensions": ["cost", "control", "adoption"],
  "overflow_strategy": "split_slide"
}
```

This is what prevents duplicated/truncated chart legends, dangling
punctuation, cramped timeline text, half-empty recommendation slides, and
colliding chart labels — by construction, not by post-hoc QA.

---

## 5. Planner — Four Steps, Not Two

v0.1's two-step planner (layout selection, then component selection) becomes
four:

1. **Narrative planning** → `NarrativePlan` (thesis, audience, storyline, evidence needs). Strong model.
2. **Slide planning** → story beats become `PresentationSlidePlan`s with purpose/message/evidence. Strong model or planner model.
3. **Layout selection** → choose `layout_id` per slide based on purpose + evidence shape. Deterministic + small-model fallback.
4. **Component binding** → choose `component_id` per zone based on data, `FitContract`, and design system. Deterministic + small-model fallback.

Plus a fifth, on-demand step: **repair** — a targeted model call per failing
slide (see §8). More steps but better debuggability/repairability, and spend
is concentrated where quality actually depends on it (narrative + slide
planning), not on mechanical binding.

---

## 6. Composer — Mostly Deterministic

v0.1 already proposed this; v2 makes it explicit: **the old "revision LLM
pass" should not exist long-term.** The Composer is the production designer,
not the strategist. It:

- validates `PresentationPlan`
- resolves design system + theme
- resolves layout zones
- validates component props against `props_model`
- runs `FitContract`/`validate_fit` checks
- chooses component variants
- produces `PptxRenderPlan`

It does **not** creatively rewrite the deck unless a repair loop explicitly
asks it to. Input: `PresentationPlan`. Output: `PptxRenderPlan`. No markdown,
no ad hoc title inference, no guessing.

---

## 7. Renderer — Made Dumber

`render.js` should not infer archetype, theme, "proof object," slide role, or
whether a slide is section/recommendation/timeline. It receives fully resolved
instructions and renders exactly that:

```js
for (const slide of slides) {
  const layout = registry.slideLayouts[slide.slide_layout];
  drawBackground(layout);
  for (const [zoneName, instance] of Object.entries(slide.zones)) {
    const zone = layout.zones[zoneName];
    const componentRenderer = componentRenderers[instance.component_id];
    componentRenderer(slide, zone, instance.props, tokens);
  }
}
```

All style overrides are constrained to declared `variant`s
(`"variant": "accent"`), never arbitrary style props
(`"color": "#F59E0B"`) — otherwise theme sprawl returns.

---

## 8. QA Gate — Two-Tier Judge + Structural Repair

### 8.1 Deterministic QA (always run, cheap, fast)

text overflow · text boxes outside slide bounds · shape overlaps · empty zones
· contrast below threshold · font size below minimum · excessive unused
whitespace · duplicate chart labels · dangling punctuation · too many bullets
· missing slide title · missing dek/subtitle · missing final
decision/ask-for-decision on decision decks.

### 8.2 LLM Visual Judge (slide-level)

Runs for PPTX deliverables, especially client/executive/board decks. Sees:
original user request, `NarrativePlan`, `PresentationPlan`, design-system
style guide, slide purpose, rendered slide thumbnail, extracted slide text,
component metadata.

```python
class SlideIssue(BaseModel):
    category: Literal[
        "story", "visual_hierarchy", "overflow", "whitespace", "density",
        "contrast", "brand_fit", "chart_readability", "table_readability",
        "weak_title", "missing_evidence", "unclear_decision",
        "generic_ai_slop", "layout_mismatch",
    ]
    severity: Literal["minor", "major", "critical"]
    description: str
    repair_instruction: str

class SlideJudgeResult(BaseModel):
    slide_id: str
    status: Literal["pass", "revise"]
    score: int  # 1-10
    severity: Literal["none", "minor", "major", "critical"]
    issues: list[SlideIssue]
    repair_strategy: Literal[
        "none", "tighten_copy", "change_layout", "change_component",
        "split_slide", "add_evidence", "redesign_slide",
    ]
    summary: str
```

### 8.3 Deck-Level Judge (new — slide QA alone is insufficient)

A deck can have ten individually-fine slides and still feel bad. The
deck-level judge evaluates: does the deck answer the original request? Is the
narrative coherent? Is the order persuasive? Repeated slides? Does the
recommendation follow from evidence? Does the final ask match the setup? Is
the executive summary strong? Tone appropriate? Specific enough, or generic?

```python
class DeckIssue(BaseModel):
    category: str
    severity: Literal["minor", "major", "critical"]
    description: str

class DeckRepair(BaseModel):
    description: str
    target_slide_ids: list[str]

class DeckJudgeResult(BaseModel):
    status: Literal["pass", "revise"]
    score: int
    storyline_score: int
    design_score: int
    evidence_score: int
    executive_readiness_score: int
    issues: list[DeckIssue]
    recommended_repairs: list[DeckRepair]
```

This is where "AI slop" gets caught at the deck level, not just per-slide.

### 8.4 Structural Repair Loop

A bad repair loop only edits text. A good one can change copy, component,
layout, slide split, visual hierarchy, chart type, evidence placement, or the
final ask. Example: if a recommendation slide uses 35% of the available space,
the repair instruction should not be "make the bullets longer" — it should be
"replace recommendation-banner layout with full-width decision panel: hero ask
left, three rationale cards right, risk-caveat footer."

```
Judge fails slide
  → classify failure
  → choose repair level: content | component | layout | split-slide
  → update PresentationPlan or PptxRenderPlan (never the raw .pptx)
  → re-render slide
  → re-judge
```

Iteration caps: normal deck = 2 rounds; executive/client/board deck = 3
rounds; hard stop with internal QA notes — surfaced to the user only on
severe failure (see §12 fail-fast).

---

## 9. Golden-File QA Scope — Tiered, Not Combinatorial

v0.1's "every `(slide_layout, component_id, theme)` combo" baseline can
explode. v2 uses three tiers instead:

1. **Component golden fixtures** — one normal + one stress case per component, e.g. `stat_card_4_metrics_dark`, `stat_card_long_labels_dark`, `option_matrix_3_options_light`, `timeline_6_phases_dark`, `risk_table_dense_light`.
2. **Deck golden fixtures** — a few canonical decks: enterprise AI platform consolidation, AI governance board briefing, technical architecture proposal, client strategy proposal, product roadmap review.
3. **Regression fixtures** — one per known bug class: dangling dash, duplicate chart legends, section-slide dispatch, whitespace underuse, chart-label collision, markdown-instead-of-PPTX.

---

## 10. Lighthouse Acceptance Deck

Before building a large golden-file matrix, define one "this must be good"
deck and treat it as the primary quality gate: **Enterprise AI Platform
Consolidation Steering Committee Deck.**

Acceptance criteria:

- Generated as PPTX, never markdown.
- Uses `agentdeck_v1`, dark and light variants.
- 8-12 slides, every slide has purpose, title, dek.
- No overflow, no dangling punctuation, no blank section slides.
- No chart label collisions or repeated truncated legend labels.
- Recommendation/decision slides use most of the slide area.
- Final slide has explicit approvals.
- Slide-level judge ≥ 8/10 per slide; deck-level judge ≥ 8/10.
- User can preview and download.

---

## 11. Brand Profiles (new — first-class)

Many users won't want "Fronei beautiful" — they want "my company beautiful."

```python
class BrandProfile(BaseModel):
    id: str
    user_id: str
    source_template_id: str | None
    logo_assets: list[AssetRef]
    color_tokens: dict
    font_tokens: dict
    layout_preferences: list[str]
    forbidden_patterns: list[str]
    example_slide_images: list[AssetRef]
    extracted_components: list[str]
```

When a user uploads a PPT template, extract: brand colors, fonts, logo
placements, common layouts, title treatment, header/footer conventions, slide
examples, image style. Then decide: if comprehensive, create a user-specific
design system; if partial, create a brand overlay on `agentdeck_v1`.

---

## 12. Usage Stats — Start Passive

v0.1's `usage_stats` loop (#127-130) is sound but should **not** affect
component selection immediately — early data is noisy. Phase 1 of v2's usage
logging: log only, don't weight yet.

Log: `component_id`, `component_version`, `layout_id`, `theme`,
`prompt_category`, `doc_type`, `content_density`, `qa_score`, `qa_failures`,
`repair_count`, `downloaded`, `regenerated`, `user_edited`.

Initial component selection is based on: slide purpose, data shape,
`FitContract`, design rules, deterministic scores. Once enough examples
accumulate, `usage_stats` becomes a weighting factor — same end-state as
v0.1 §3/§130, just sequenced later and gated on data volume.

---

## 13. Personalization Feeds the Planner (new)

```python
class UserDocumentProfile(BaseModel):
    preferred_tone: str
    preferred_depth: str
    preferred_slide_density: str
    brand_profiles: list[str]
    common_audiences: list[str]
    industry_context: str
    writing_style: str
    past_accepted_decks: list[str]
    past_rejected_patterns: list[str]
```

The planner uses this to decide: more analytical vs. visual, dense consulting
style vs. sparse keynote, direct recommendation vs. exploratory options,
default theme/brand. This is the differentiator vs. generic frontier-model
deck generation — "make the kind of deck Subhamoy would actually send," not
just "make a deck."

---

## 14. Chat UX — Surface Less

Internal pipeline can be sophisticated; UX stays calm.

- Before generation: at most a small number of high-leverage questions — audience, format, template/brand, desired depth.
- During generation, human-readable progress only: *Building storyline → Choosing slide structure → Designing slides → Reviewing visual quality → Finalizing PPTX.* Never expose internal component names outside debug/admin mode.
- After generation: preview + download, plus optional "improve this deck" actions (more executive, add more data, simplify, more visual, adapt to my company template).

---

## 15. Quality Modes — Cost Control

```python
quality_mode: Literal["draft", "standard", "executive"]
```

- **draft**: no LLM visual judge, deterministic QA only, 0-1 repair pass.
- **standard**: deterministic QA + LLM judge for risky slides, max 2 repair passes.
- **executive**: LLM judge every slide + deck-level judge, max 3 repair passes, stronger model for final critique.

Default chosen from intent ("quick deck" → standard; "client-ready /
board-ready / executive" → executive); advanced users can override.

---

## 16. Fail-Fast, No Silent Degradation (critical change)

If the framework cannot produce valid PPTX, **do not fall back to markdown.**
Markdown fallback is one of the main reasons the system feels unreliable.

```python
class DocumentGenerationFailure(BaseModel):
    stage: Literal["planner", "composer", "renderer", "qa"]
    user_message: str   # e.g. "I couldn't complete the deck render cleanly."
    retryable: bool
    debug_info: dict    # internal only
```

New rule: **if the user requested PPTX, the pipeline either produces a valid
PPTX or returns a structured generation failure with a retry path** — never a
silently-degraded markdown document.

---

## 17. Phased Implementation Plan (v2, supersedes v0.1 §8)

1. **AgentDeck Foundation** — `design_systems/agentdeck_v1/spec.json` (formalized §3 shape, incl. `token_pairs`/`qa_thresholds`), Pydantic schema, registry, token resolver, dark/light contrast validation + unit tests. *(Largely done in v0.1 Phase 1 — needs `token_pairs`/`qa_thresholds` additions.)*
2. **Plan Models** — `NarrativePlan`, `EvidencePack`, `PresentationPlan` (replacing `DocPlan`/`SectionPlan`), `PptxRenderPlan` (kept), schemas/validators, strict failure behavior for invalid PPTX plans.
3. **Component Registry** — implement the ~10-12 spec components with `FitContract`s and `ComponentRuntime` (`normalize`/`validate_fit`/`estimate_density`); component-level tests. *(v0.1 Phase 1 built the components — add fit contracts + runtime protocol.)*
4. **Renderer Rewrite** — `render.js` dispatch purely on `slide_layout`/zones/`component_id`/token refs; remove any remaining heuristics/raw-color logic. *(Largely done in v0.1 Phase 2 — audit for leftover heuristics.)*
5. **Deterministic QA** — overflow, empty zones, duplicate labels, dangling punctuation, excessive whitespace, contrast, tiny fonts, object bounds; regression tests per known bug class.
6. **Thumbnail + LLM Judge** — ensure `soffice`/equivalent in deployment; render slide PNGs; slide-level judge (§8.2), deck-level judge (§8.3), structural repair loop (§8.4) capped by quality mode.
7. **Lighthouse Deck** — build the AI Platform Consolidation fixture, dark + light, validate against §10 acceptance criteria; primary quality gate going forward.
8. **Chat Pipeline Cutover** — replace `DocPlan`-based draft/revision with `NarrativePlan → PresentationPlan → Composer → PptxRenderPlan`; same chat-facing contract; no markdown fallback for PPTX (§16).
9. **Brand Profiles** — template upload extraction, brand overlay / user-specific design system, default brand selection, template management UI.
10. **Usage Logging (passive)** — log QA outcomes + component usage per §12; do not weight selection yet. *(v0.1 #127-129 already logs; #130's weighting should be gated/disabled until enough data accumulates per this plan — see reconciliation note.)*

---

## 18. What Not To Do

- Don't keep patching the current renderer beyond what's needed to keep testing unblocked — every fix reveals another hidden coupling.
- Don't ask the LLM to produce `PptxRenderPlan` directly in one shot — it should produce narrative + slide intent; the Composer binds to components deterministically.
- Don't make the renderer smarter — make it dumber (§7).
- Don't treat DOCX/XLSX as first-class during the PPTX rewrite — design abstractions so they *can* come later (§2.5), but ship PPTX first.
- Don't introduce ML-based component ranking yet — log first (§12).

---

## 19. Final Recommendation Summary

- Clean break from legacy for new PPT generation (carried from v0.1, reaffirmed).
- Add `NarrativePlan` as the first-class top layer.
- Make slide purpose (`SlidePurpose` + `audience_question`) mandatory.
- Strict Pydantic contracts throughout; components own fit validation via `FitContract`.
- `agentdeck_v1` dark/light only.
- Renderer rewritten around zones/components, nothing smarter.
- Visual LLM judge mandatory for executive-quality decks (slide + deck level).
- Repair structurally (plan-level), not just textually.

---

## 20. Addendum — Named Roles: Designer / Art Director + Supporting Stages

§1's pipeline names *what* each stage produces (`NarrativePlan`,
`PresentationPlan`, `PptxRenderPlan`) but leaves one responsibility implicit:
**who decides how a slide should *look and feel*** — visual hierarchy,
density, rhythm across the deck, when to split a slide, when to use a section
divider, whether the deck reads as "premium executive" vs "tactical working
session." Right now that responsibility is smeared across `PresentationPlan`
(purpose/layout), the Composer (zone binding), and the QA judge (after the
fact, as critique). This addendum makes it an explicit, named stage —
**Designer / Art Director** — plus several other roles that a real
document-production team has and this pipeline currently lacks.

### 20.1 Revised pipeline

```
Intent Classifier
  → Evidence Builder            → EvidencePack
  → Narrative Planner            → NarrativePlan
  → Story Editor                 → NarrativePlan (sharpened)
  → Presentation Strategist      → PresentationPlan (slide purposes/order)
  → Designer / Art Director       → DesignPlan (enriched PresentationPlan)
  → Composer                     → PptxRenderPlan
  → Renderer                     → .pptx + thumbnails
  → Visual Critic / QA Judge      → SlideJudgeResult / DeckJudgeResult
  → Revision Controller          → targeted repairs, version tracking
  → Delivery Orchestrator         → quality-mode routing, user-facing status
```

This is a *relabeling + one new stage* (Designer), not a pipeline rewrite.
"Presentation Strategist" is the existing Presentation Planner (§5 steps
1-2... no — steps producing `PresentationPlan`); "Story Editor" and "Evidence
Builder" are refinements of the Narrative Planner / `EvidencePack` step that
already exist in concept (§2.1/§2.2) but should be called out as distinct
passes/prompts rather than folded silently into one call.

### 20.2 Designer / Art Director (new stage)

Sits between `PresentationPlan` and the Composer.

Input: `NarrativePlan`, `PresentationPlan`, design-system spec, `BrandProfile`
(if any), component registry (incl. `FitContract`s), audience/context,
`quality_mode`.

Output: `DesignPlan` — an enriched `PresentationPlan` where every slide also
carries a visual treatment:

```python
class DesignPlan(BaseModel):
    design_system: str
    theme: Literal["dark", "light"]
    visual_direction: str                 # e.g. "dark board deck, high-contrast, minimal text"
    density_strategy: Literal["sparse", "balanced", "consulting_dense"]
    slide_treatments: list[SlideDesignTreatment]

class SlideDesignTreatment(BaseModel):
    slide_id: str
    visual_role: Literal[
        "hero", "section_break", "evidence", "comparison",
        "diagram", "decision", "appendix",
    ]
    layout_id: str
    component_choices: list[str]          # candidate component_ids, ranked
    hierarchy_notes: str                   # what should dominate the slide
    density_target: Literal["low", "medium", "high"]
    repair_constraints: list[str]          # things the repair loop must not violate
```

The Composer becomes a pure executor of `DesignPlan` — it resolves
`component_choices` + `layout_id` into exact zones/props via `FitContract`,
but does not make hierarchy/density/rhythm decisions itself. This is the same
"renderer should be dumb" principle (§7) applied one layer up: the Composer
should also be dumb relative to the Designer.

### 20.3 Other named roles (responsibilities, not necessarily separate services)

These should exist as **named responsibilities in the architecture** even
where one LLM call or module currently covers more than one. Listed in order
of impact:

1. **Evidence Builder** (sharpens §2.2 `EvidencePack`) — extracts claims,
   numbers, risks, options, timelines from raw input; normalizes tables;
   flags missing data; computes derived metrics; *prevents invented figures*
   reaching `NarrativePlan`/`PresentationPlan`.
2. **Story Editor** (refinement pass on `NarrativePlan`, distinct from the
   Narrative Planner that drafts it) — removes repetition, makes slide
   titles assertion-based, ensures every slide advances the argument, tightens
   executive language, sharpens the final ask. Can be implemented as a second
   structured-output pass over `NarrativePlan` before `PresentationPlan` is
   produced.
3. **Brand / Template Interpreter** (the producer of `BrandProfile`, §11) —
   inspects an uploaded PPTX, extracts typography/color/logo/layout
   conventions, decides whether it becomes a full design-system variant or a
   brand overlay on `agentdeck_v1`.
4. **Visual Critic / QA Judge with structural repair** — already in scope
   (§8.2-8.4); this addendum just confirms it sits *after* the Designer stage
   and that its `repair_instruction`s should be checked against
   `SlideDesignTreatment.repair_constraints`.
5. **Revision Controller** (new) — tracks plan/render versions across repair
   iterations and future user-driven edits ("make slide 5 more executive");
   ensures a targeted repair to one slide doesn't regress another;
   preserves user-approved slides across regenerations. Becomes necessary
   once the repair loop (§8.4) and brand/personalization (§11/§13) are live.
6. **Delivery Orchestrator** (new) — owns pipeline-level workflow state:
   routes through `quality_mode`, decides when to surface a mid-generation
   status to the user vs. run autopilot, handles `DocumentGenerationFailure`
   (§16), and decides "good enough to deliver." This is largely
   `conversations.py`/`chat_pipeline.py`'s existing orchestration role, named
   explicitly so future routing logic (quality modes, repair caps, retries)
   has one home.

Two roles raised in review — **Asset Manager** (uploaded/generated
images/icons/logos) and **Chart/Visualization Designer** (chart-vs-table
choice, axis/label hygiene) and **Compliance/Fact Guard** (unsupported-claim
flagging, citation integrity) — are **deferred**: relevant mainly once
multimodal assets and research-citation decks are higher priority. Noted here
so they aren't lost, not scheduled in §17/§21.

### 20.4 Backlog impact

One net-new task inserted into the near-term sequence (see reconciliation
doc §11 for placement):

- **#157 — Define `DesignPlan`/`SlideDesignTreatment` models + Designer
  stage** (LLM step between `PresentationPlan` and Composer; Composer
  consumes `DesignPlan` instead of `PresentationPlan` directly).

Evidence Builder and Story Editor are **not** separate tasks — they're
prompt/pass-structure refinements inside #141/#142 (narrative- and
slide-planning steps) and should be addressed when those tasks are
implemented, not as standalone work items.
- One lighthouse deck as the acceptance bar before scaling the golden-file matrix.
