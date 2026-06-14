# PPTX Renderer Follow-Up Spec — Wire the Design System Into Rendering

## Context

The recent change (`presentation_design_system.py` + `compose_deck_plan_parallel`,
HEAD~2..HEAD, 3022 insertions) added a registry that matches
`docs/pptx-design-system-catalog.md` (theme tokens, fit contracts, slide
templates, component trees, archetype inference, deck-level polish). 92/92
relevant tests pass and the composition layer itself is sound.

However, **the registry is computed and attached to the render payload but is
not yet load-bearing**: a re-render of the catalog deck through the new code
produced a byte-identical PPTX to the pre-change output (md5 match). A fresh
real-world deck (`enterprise-ai-platform-consolidation.pptx`, 11 slides)
confirms the same defects identified before this change are still present.
This spec lists the specific wiring gaps, in priority order, with file/line
references from the current `HEAD`.

Scope: `apps/api/app/services/document_generator.py` and
`apps/api/pptx_render/render.js`. No changes to
`presentation_design_system.py`'s data model should be needed — the tokens
and fit contracts it already exposes are sufficient for items 1–3 below.

---

## Priority 1 — Generalize truncation to a word/clause-boundary helper

**Symptom**: slide 4 of the latest deck — a stat card label renders as
*"Operating independently with no shared platform or govern…"*, cut mid-word.

**Root cause**: `_shorten()` (line 1174) and `_shorten_to_notes()` (line 1216)
in `document_generator.py` are unchanged by this slice:

```python
return cleaned[: max(0, limit - 3)].rstrip() + "..."
```

Both are still called at every primitive call site in `parse_deck_plan`
(bullets at `MAX_BULLET_CHARS=90`, comparison-card bullets/headings, stat
value/label/source, chart series names/categories, table cells, callout
text, timeline phase fields, risk register fields).

Meanwhile `_shorten_title_to_notes()` (line 1184, added in #86) already does
the right thing for titles — clause-boundary first, then word-boundary, full
text preserved to speaker notes, no literal `"..."`.

**Fix**:
1. Generalize `_shorten_title_to_notes` into a single helper, e.g.
   `_shorten_text_to_notes(text, limit, *, allow_clause_break=True)`, usable
   for both titles and body content.
2. Replace all `_shorten`/`_shorten_to_notes` call sites in `parse_deck_plan`
   (and `_infer_chart_from_stats`, `_risk_heatmap_from_slide`) with the new
   helper.
3. Source the `limit` values from `FIT_CONTRACTS` in
   `presentation_design_system.py` (e.g. `BodyBulletList.chars_per_item`,
   `StatCard.label_chars`, `Chart.legend_chars`, `Table.cell_chars`,
   `RiskRegisterTable.cell_chars`) instead of the hardcoded constants
   (`MAX_BULLET_CHARS`, the literal `40`/`60`/`80`/`90`/`200` scattered through
   `parse_deck_plan`). This makes the fit contracts the actual source of
   truth, closing the gap the catalog doc claims is already closed.

**Acceptance**: no slide in a re-rendered catalog or sample deck ends a
text run with `…` preceded by a partial word. Full original text appears in
speaker notes whenever truncation occurs.

---

## Priority 2 — Fix whitespace underutilization (5 of 11 slides in latest deck)

**Symptom**: `two_content`, `comparison`, `timeline`, and `recommendation`
slides render content only in the top ~30–40% of the content area, leaving
50–65% empty below. This is the single most visible quality problem in the
latest deck (slides 5, 6, 7, 8, 11 of 11).

**Root cause**: card/column/lane shapes in render.js use fixed heights
(e.g. `renderComparisonMatrixSlide` cards at `h: 4.25`,
`renderOperatingModelSlide` lanes at fixed `laneH`) sized for the
`max_items`/`max_lines` ceiling in `FIT_CONTRACTS`, but bullet text is
rendered at a fixed font size with no vertical redistribution when a slide
has fewer items than the ceiling (3 bullets vs. the 5-item budget for
`ComparisonCard`, 2 bullets vs. `BodyBulletList.max_items=6`, etc.). The
`compose_deck_plan_parallel` composition pass records `density` (low/medium/
high per slide — `_compose_slide_job`) but render.js does not read it.

**Fix options** (pick one, or combine):
- **A. Density-aware sizing**: have render.js read `spec.component_tree` /
  `spec.render_hints` / the slide's `density` field (already computed) and
  scale up font size and/or line spacing for `low`/`medium` density slides
  so 2–3 bullets fill the available box rather than leaving it mostly empty.
- **B. Density-aware layout selection**: for `comparison`/`two_content`
  slides with ≤3 bullets per column, reduce card height and add a secondary
  element (e.g. a supporting stat, source citation, or larger heading) to
  use the freed space — this is closer to what `InvestmentCaseBlock` /
  `CalloutBox` already do.
- **C. Minimum viable**: at least vertically center bullet blocks within
  their card/lane (`valign: "middle"` instead of `"top"`) so empty space is
  distributed above/below rather than concentrated at the bottom — cheapest
  fix, ships immediately, doesn't fully solve density but stops the
  "unfinished slide" look.

**Acceptance**: re-render the latest deck's slides 5, 6, 7, 8, 11 — visible
content should occupy ≥60% of the content area (`content_top_y` to
`content_bottom_y` per `GRID_TOKENS`).

---

## Priority 3 — Section divider slide near-blank (issue carried from before)

**Symptom**: slide 2 of the latest deck is the deck title only
("ENTERPRISE AI PLATFORM CONSOLIDATION"), rendered as a small caption near
the bottom-left of an otherwise empty slide.

**Location**: `renderSectionSlide` in render.js (dispatched via
`role === "section"` / `canonical_layout` → `"section"`).

**Fix**: confirm `renderSectionSlide` is actually being invoked for this
slide (vs. falling through to a generic content renderer with no bullets).
If it is being invoked, it needs a real two-tone full-bleed treatment per
the catalog's `SectionDividerBlock` spec — large title/kicker, accent block,
optional section number — not a single small caption. If it is *not* being
invoked (likely, given the visual), trace why `canonical_layout`/`archetype`
resolution for this slide isn't routing to `"section"` and fix the
dispatch.

**Acceptance**: section/divider slides use ≥50% of the slide for a
deliberate two-tone hero treatment, matching `SectionDividerBlock` in the
catalog.

---

## Priority 4 — Theme tokens not reaching table/risk-register colors

**Symptom**: slide 10's risk register table header renders medium blue
(`~#5B85B8`), not the `warm-editorial` theme's `table_header_fill`
(`#1F2937`) from `THEME_TOKENS` in `presentation_design_system.py`.

**Root cause**: only one call site in render.js
(`renderTableSlide`, `fill: { color: token("table_header_fill", NAVY) }`)
reads `ACTIVE_DESIGN_SYSTEM` via `token()`. `renderRiskRegisterSlide` and
`renderRiskHeatmapSlide` still use hardcoded `EMPHASIS_COLORS.risk`,
`NAVY`/`TEAL`/`GOLD` constants. Separately,
`design_system_payload("warm-editorial")` is hardcoded in
`_build_js_deck_payload` (line ~3003) regardless of the deck's actual
selected theme/template_id.

**Fix**:
1. Pass the deck's actual theme name into `design_system_payload(...)`
   (derive from `template_id`/theme selection, not a hardcoded string).
2. In render.js, replace hardcoded `NAVY`/`TEAL`/`GOLD`/`EMPHASIS_COLORS.risk`
   in `renderRiskRegisterSlide`, `renderRiskHeatmapSlide`, and
   `renderOperatingModelSlide` with `token("accent", ...)`,
   `token("warn", ...)`, `token("success", ...)`, `token("table_header_fill",
   ...)` as appropriate per the catalog's semantic-token guidance (open
   decision #4 in the catalog doc).

**Acceptance**: risk register/heatmap/operating-model colors visibly change
when the deck theme changes (verify by rendering the same DeckPlan under
`modern-tech` vs `warm-editorial`).

---

## Priority 5 (minor) — Chart data-label collisions

**Symptom**: slide 9, line-chart data labels ("1.2", "1.8") overlap each
other and gridlines where the two series cross or converge.

**Fix**: standard pptxgenjs data-label collision avoidance — offset labels
above/below the point based on which series is higher at that category, or
disable data labels for series with >8 categories and rely on the legend +
endpoint labels only.

---

## Priority 6 (minor) — Title left-margin inconsistency on closing slide

**Symptom**: slide 11's title starts ~40px further right than the
`grid.margin_x` (0.6in) used by every other slide's `addTitle`.

**Fix**: audit `renderRecommendationSlide` (or whichever renderer slide 11
uses) for an extra `x` offset on the title call; align to
`GRID_TOKENS.margin_x`.

---

## Suggested sequencing

1. Priority 1 (truncation helper) — highest content-integrity impact, most
   contained change (Python only, one helper + call-site swap).
2. Priority 3 (section divider) — likely a dispatch bug, should be quick to
   isolate.
3. Priority 2 (whitespace) — start with option C (vertical centering) as a
   same-day mitigation, then evaluate A/B for a follow-up.
4. Priority 4 (theme tokens for risk/operating-model) — depends on Priority 1
   landing first if fit-contract values are reused for layout sizing.
5. Priorities 5–6 — bundle as small polish items with whichever of the above
   ships first.

## Verification

- Re-run `python3 -m pytest -k "document or pptx or deck or chart or
  presentation" -q` (currently 92/92 passing — must stay green).
- Re-render both `design_system_catalog.pptx` (18-template synthetic deck)
  and a fresh real-world deck; confirm the md5 of the catalog deck **changes**
  from `c110121c9596cd1e1d2e9e7e9c1e66d8` (current, pre-fix) and spot-check
  each priority's acceptance criteria above.
