# Addendum — Two Patterns From a Hand-Built Reference Deck

## Context

A hand-built reference deck (`AI_Platform_Consolidation_Steering_Committee.pptx`,
8 slides, dark `modern-tech`-style theme) was produced for the same prompt as
`enterprise-ai-platform-consolidation.pptx`. It's noticeably higher quality —
not because of anything exotic, but two repeatable patterns that the renderer
doesn't currently produce. Both are cheap, generalizable, and don't require
new archetypes.

Note: `densityFor`/`densityFontBoost`/`contentValign` (render.js ~135-150)
already implement part of Priority 2 from the main follow-up spec (font-size
boost + middle-valign for low/medium density slides). The reference deck
suggests this isn't enough on its own — see item 2 below.

---

## Item 1 — Subtitle / "dek" line under the slide title

**What the reference deck does**: every content slide has a one-sentence
subtitle directly under the title, in a muted color, e.g.:

- Title: "Consolidate on one platform with federated ownership"
- Dek: "Centralize the foundation. Keep business units accountable for
  domain use cases and outcomes."

This gives every slide a thesis statement and visually fills the gap between
the title and the content area — directly helping with Priority 2's
whitespace problem at zero layout-engine cost.

**Current state**: `addSlideTitle` (render.js:254) calls `addTitle` +
`addBlueprintKicker` only. There's no subtitle slot for content slides — only
`renderTitleSlide` (render.js:352) renders `payload.subtitle`, and that's the
deck-level title slide, not per-slide.

**Fix**:
1. Add an optional `spec.subtitle` (or `spec.dek`) field to the per-slide
   `SlideSpec` schema in `document_generator.py`'s `_js_slide_from_deck_spec`
   / `parse_deck_plan`, sourced from a new DeckPlan slide field (e.g.
   `"subtitle"`), with a fit-contract-derived char limit (~90-110 chars) via
   `_shorten_at_boundary`.
2. In `addSlideTitle`/`addTitle`, render the subtitle as a single line below
   the title (between `TITLE_BOX_H` and `TITLE_RULE_Y`, or push
   `CONTENT_TOP_Y` down slightly when present), using `token("muted",
   TEXT_MUTED)` and `bodyFace()`.
3. Update the presentation-generation prompt (`documents.py`) to ask the
   model for a one-sentence subtitle per slide — this is a content task, not
   a layout one, so the model should produce it directly.

**Acceptance**: re-rendered slides show a muted one-line subtitle under the
title; `parse_deck_plan` round-trips a `subtitle` field; speaker notes
unaffected.

---

## Item 2 — Accent-bar / colored-underline treatment for cards and lanes

**What the reference deck does**: every card/lane gets a small colored
accent — either a top strip (slide 6's phase cards: colored underline bar at
the bottom matching the phase's badge color) or a left border (slide 5's
Controls/Economics/Delivery strip: `blue` / `green` / `gold` left-edge bars).
This is cheap (one `addShape("rect", ...)`) but makes each card feel
deliberately designed rather than a generic gray box, and reinforces the
theme's accent palette per the catalog's semantic-token guidance.

**Current state**: `renderTwoContentSlide` (render.js:724-781) already adds a
thin top-strip (`idx % 2 === 0 ? accent : GOLD`, render.js:754-761) on
3-column cards — so this pattern partially exists there. It is **not** applied
to:
- `renderOperatingModelSlide` lanes (no accent bar at all, just fill color)
- `renderRiskRegisterSlide` / `renderRiskHeatmapSlide` cards
- Timeline/phase cards (if/when a dedicated timeline renderer exists)

**Fix**: extract the top-strip logic from `renderTwoContentSlide` into a small
shared helper (e.g. `addAccentStrip(slide, x, y, w, color)`), and apply it to
operating-model lanes (left edge, using `accent`/`accent2`/`success` rotation)
and any timeline/phase cards, using `token()`-resolved colors so it follows
theme changes automatically (ties into Priority 4's theme-token work, already
landed).

**Acceptance**: operating-model lanes and any timeline cards show a colored
accent edge consistent with the deck's theme tokens; re-rendered catalog md5
changes again from the current `e01970d983876935cfec7fd1c4146f53`.

---

## Sequencing

Both items are small, additive, and independent of each other and of the
still-open Priority 2 (whitespace) / Priority 3 (section divider) items in
the main spec. Either can ship standalone. Item 1 (subtitle line) has the
larger visual impact relative to effort and also reduces whitespace, so do it
first if choosing one.
