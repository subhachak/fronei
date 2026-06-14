# PPTX Design System — Component & Template Catalog

Exhaustive inventory of the design tokens, primitives, slide templates, and
layout-routing rules that should govern deck generation. This is the source
catalog for the implemented design-system registry in
`apps/api/app/services/presentation_design_system.py`, with `render.js` and
`document_generator.py` consuming the registry through component trees.

---

## 1. Design Tokens

### 1.1 Theme palette (5 built-in themes, current state in
`PPTX_TEMPLATE_THEMES`)

| Token | warm-editorial | modern-tech | executive-navy | data-product-os | clean-light |
|---|---|---|---|---|---|
| `bg` | `#F6F0E6` | `#080C11` | `#101827` | `#0B1220` | `#F8FAFC` |
| `card` | `#FFFDF8` | `#121A24` | `#172033` | `#11182 7` | `#FFFFFF` |
| `card_line` | `#D8CDC6` | `#24303D` | `#2A3752` | `#1E293B` | `#E2E8F0` |
| `fg` | `#1F2937` | `#EFF6FF` | `#F8FAFC` | `#F1F5F9` | `#0F172A` |
| `muted` | `#6B5E52` | `#AAB8C7` | `#A7B2C5` | `#CBD5E1` | `#475569` |
| `accent` | `#B45009` | `#22D3EE` | `#38BDF8` | `#34D399` | `#2563EB` |
| `accent2` | `#0F766E` | `#A3E635` | `#7C3AED` | `#F59E0B` | `#10B981` |
| `heading_font` | Georgia | Calibri | Calibri | Calibri | Calibri |
| `body_font` | Calibri | Calibri | Calibri | Calibri | Calibri |

**Gaps to fill per theme**: chart series palette (currently hardcoded
`ACCENT`-based in render.js, not theme-aware), table header fill/text,
section-divider background/foreground pairing, bullet marker color (should
= `accent`).

### 1.2 Typography scale (point sizes, to be theme-independent ratios)

| Token | Size | Weight | Used by |
|---|---|---|---|
| `type.kicker` | 12–13pt | Bold, uppercase, letter-spaced | Eyebrow/kicker label |
| `type.title` | 24–32pt (auto-shrink by length) | Bold | Slide title |
| `type.title_hero` | 36–44pt | Bold | Title slide, section divider |
| `type.headline` | 28–34pt | Bold | Callout / executive summary headline |
| `type.body` | 15–18pt | Regular | Bullet text |
| `type.body_sub` | 13–14pt | Regular | Sub-bullets |
| `type.stat_value` | 36–48pt | Bold | Stat card number |
| `type.stat_label` | 12–13pt | Regular | Stat card caption |
| `type.caption` | 10–11pt | Regular, muted | Sources, footnotes |
| `type.table_header` | 12–13pt | Bold | Table column headers |
| `type.table_cell` | 11–12pt | Regular | Table cell text |

### 1.3 Spacing / grid tokens

| Token | Value | Notes |
|---|---|---|
| `slide.width` / `slide.height` | 13.33in × 7.5in | 16:9 |
| `grid.margin_x` | 0.6in | Left/right safe margin |
| `grid.title_y` | 0.42in | Title block top |
| `grid.accent_rule_y` | 1.32in | Rule under title |
| `grid.content_top_y` | 1.65in | Body content start |
| `grid.content_bottom_y` | ~6.9in | Hard floor before footer |
| `grid.gutter` | 0.3in | Between cards/columns |
| `grid.card_radius` | 6pt | Card corner radius |

### 1.4 Markers / iconography tokens

| Token | Glyph | Color | Indent |
|---|---|---|---|
| `bullet.accent` | `▪` (25AA) | `accent` | 18pt |
| `bullet.sub` | `–` (2013) | `muted` | 14pt |
| `bullet.check` (proposed, for recommendation/decision) | `✓` | `accent2` | 18pt |
| `bullet.risk` (proposed, for risk register) | `▲` | warning color (new token `warn`) | 18pt |

---

## 2. Primitive Components (atoms)

Each primitive needs a **content-fit contract**: given its box geometry and
font size, a max-chars-per-line × max-lines budget, with boundary-aware
truncation and notes-overflow when exceeded.

| Primitive | Description | Current impl | Fit contract status |
|---|---|---|---|
| `TitleBlock` | Slide title + accent rule, auto-shrinks font by length | `addTitle()` | Has clause-boundary shortening (#86) ✅ |
| `KickerLabel` | Small uppercase eyebrow text above title | inline in several renderers | None — needs char cap |
| `BodyBulletList` | Vertical list, accent markers, optional sub-bullets | `bulletsToTextProps()` | ❌ naive truncation (root cause of issue #1) |
| `StatCard` | Big number + label + optional source | `renderStatCardsSlide` | Has char caps but not boundary-aware |
| `CalloutBox` | Large highlighted insight/quote with label tag | callout layout | Partial |
| `ComparisonCard` (2-col / 3-col) | Column with heading + bullet list | `renderComparisonMatrixSlide` | Bullet list inherits same gap |
| `Chart` (bar/line/pie/combo) | Native chart w/ legend, axis, data labels | `renderChartSlide` | ❌ legend/series names use naive `_shorten`; no label-collision handling |
| `Table` | Header row + body rows | `renderTableSlide` | ❌ not theme-colored (issue #3); cell text uses naive `_shorten` |
| `Timeline/PhaseTrack` | Horizontal sequence of phase markers + descriptions | `renderTimelineSlide` | Needs review |
| `RiskMatrix` (heatmap grid) | 2D grid w/ colored severity cells | `renderRiskHeatmapSlide` | Needs review |
| `RiskRegisterTable` | Specialized table for risk rows | `renderRiskRegisterSlide` | Shares table issues |
| `ArchitectureDiagram` | Boxes/arrows representing system components | `renderArchitectureSlide` | Needs density rules |
| `OperatingModelGrid` | Grid of role/responsibility cards | `renderOperatingModelSlide` | Needs review |
| `InvestmentCaseBlock` | Cost/benefit/ROI structured block | `renderInvestmentCaseSlide` | Needs review |
| `SectionDividerBlock` | Full-bleed two-tone title treatment | `renderSectionSlide` | ❌ not reliably triggered (issue #5) |
| `Footer/PageNumber` | Bottom-of-slide page index + optional logo | inline | Not theme-aware |
| `SourceCitation` | Small caption under chart/stat with attribution | inline | None |

---

## 3. Slide Templates (composed layouts)

Each template = ordered composition of primitives + slot rules (required
vs. optional fields) + minimum/maximum content guidance.

| Template (canonical `layout`) | Composition | Min/max content guidance | Render fn |
|---|---|---|---|
| `title` | TitleBlock (hero) + subtitle + optional date/author | n/a | `renderTitleSlide` |
| `section` | SectionDividerBlock (full-bleed, two-tone) + optional section number | Title only, short (≤8 words) | `renderSectionSlide` |
| `content` | KickerLabel + TitleBlock + BodyBulletList | 3–6 bullets, 60–140 chars each | `renderContentSlide` |
| `two_content` | TitleBlock + 2× BodyBulletList (split columns) | 2–5 bullets per column | `renderTwoContentSlide` |
| `comparison` | TitleBlock + 2–3× ComparisonCard | 3–5 bullets per card | `renderComparisonMatrixSlide` |
| `stat_cards` | TitleBlock + 3–4× StatCard (+ optional supporting bullets) | 3–4 stats, label ≤60 chars | `renderStatCardsSlide` |
| `callout` | TitleBlock(headline) + CalloutBox + optional supporting bullets | 1 headline ≤200 chars + 0–3 bullets | `renderExecutiveSummarySlide`-family |
| `executive_summary` | TitleBlock + headline (from bullets[0]) + BodyBulletList | 1 headline + 2–4 bullets | `renderExecutiveSummarySlide` |
| `recommendation` | TitleBlock + decision headline + supporting bullets + optional next-steps | 1 decision + 3–5 bullets | `renderRecommendationSlide` |
| `chart` | TitleBlock + Chart + optional source citation | 1 chart, ≤6 series/categories | `renderChartSlide` |
| `financial_model` | TitleBlock + Chart(s) + StatCard row | combo | `renderChartSlide` variant |
| `table` | TitleBlock + Table + optional source | ≤6 rows × ≤5 cols visible | `renderTableSlide` |
| `timeline` | TitleBlock + Timeline/PhaseTrack | 3–6 phases | `renderTimelineSlide` |
| `architecture` | TitleBlock + ArchitectureDiagram + optional callout | 3–6 components | `renderArchitectureSlide` |
| `risk_matrix` | TitleBlock + RiskMatrix heatmap | fixed 5×5 or 4×4 grid | `renderRiskHeatmapSlide` |
| `risk_register` | TitleBlock + RiskRegisterTable | ≤8 rows | `renderRiskRegisterSlide` |
| `operating_model` | TitleBlock + OperatingModelGrid | 3–6 role cards | `renderOperatingModelSlide` |
| `investment_case` | TitleBlock + InvestmentCaseBlock | cost/benefit/ROI sections | `renderInvestmentCaseSlide` |
| `appendix` | KickerLabel("Appendix") + TitleBlock + BodyBulletList (denser cap) | up to 8 short bullets (≤90 chars) — needs overflow-aware cap (issue #2) | `renderContentSlide` variant |

---

## 4. Layout Alias Registry (LLM `layout`/`type` string → canonical template)

Current (`DECK_LAYOUT_ALIASES`) — needs to be exhaustive and validated
(unknown values should fall back explicitly, logged, not silently produce a
near-blank slide as in issue #5):

| Alias(es) | Canonical template |
|---|---|
| `cover`, `hero_cover` | `section` |
| `decision`, `decision_slide`, `decision_recommendation` | `recommendation` |
| `roadmap`, `process`, `process_steps` | `timeline` |
| `architecture_map`, `system_map` | `architecture` |
| `financial_exhibit`, `data_exhibit` | `financial_model` |
| `three_card_system`, `governance_grid`, `principles_grid` | `comparison` |
| `takeaways` | `executive_summary` |
| `stats`, `metrics`, `kpi`, `kpi_grid`, `market_context`, `by_the_numbers` | `stat_cards` |
| *(missing)* `divider`, `section_break`, `intro`, `chapter` | → `section` |
| *(missing)* `agenda`, `toc`, `outline` | → new `agenda` template (not yet defined) |
| *(missing)* `quote`, `testimonial` | → `callout` variant |
| *(missing)* `closing`, `thank_you`, `next_steps` | → `recommendation` or new `closing` template |
| *(fallback)* anything unrecognized | → `content` (with logged warning) |

**Proposed additions to the template set**: `agenda`/`toc` (numbered list of
section titles), `closing`/`thank_you` (mirrors `title` template), `quote`
(large quotation mark + attribution, callout variant).

---

## 5. Content-Fit Contract Spec (per primitive, to replace flat char limits)

Instead of static `MAX_BULLET_CHARS = 90` applied uniformly, each primitive
declares:

| Primitive | Box width (in) | Font size | Approx chars/line | Max lines | Resulting char budget |
|---|---|---|---|---|---|
| `BodyBulletList` (content, 1-col) | ~11.5 | 16pt | ~95 | 4–5 lines total across all bullets | dynamic — depends on bullet count |
| `BodyBulletList` (two_content, 1 of 2 cols) | ~5.5 | 15pt | ~48 | 4–5 | dynamic |
| `ComparisonCard` bullet | ~3.6 (3-col) | 13pt | ~38 | 4 | ~150 total |
| `StatCard.label` | ~2.8 | 12pt | ~28 | 2 | ~56 |
| `CalloutBox` headline | ~10 | 28pt | ~32 | 3 | ~96 |
| `Chart` series name (legend) | legend column width-dependent | 10pt | ~22 | 1 | ~22 |
| `Table` cell | column-width-dependent | 11pt | varies | 2 | varies |
| `TitleBlock` | ~11.5 | 24–32pt (auto-shrink) | ~40–55 | 2 | ~80–110 (current `MAX_SLIDE_TITLE_CHARS`) |

All truncation across these should route through **one shared
clause/word-boundary helper** (generalize `_shorten_title_to_notes`), with
overflow always captured to speaker notes — never a silent drop, never a
mid-word `…`.

---

## 6. Open Design Decisions

1. Should `appendix` be a distinct template with its own (smaller) type
   scale, or just `content` with a tighter fit contract?
2. Chart series palette per theme — derive from `accent`/`accent2` plus N
   generated tints, or hand-author a palette per theme?
3. Table header styling — `accent` fill with `bg`-colored text, or `card`
   fill with `accent`-colored text + bottom border? (Depends on theme
   contrast.)
4. New `warn`/`danger`/`success` semantic tokens for risk matrix and
   investment-case primitives — currently absent from the theme dict.
5. Section divider (`section` template) — confirm trigger conditions; add
   alias coverage for `divider`/`intro`/`chapter` so it's never silently
   skipped.
