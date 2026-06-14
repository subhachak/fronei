# Document Rendering Engine: python-pptx vs. PptxGenJS

**Status:** Superseded — a hybrid PptxGenJS/python-pptx renderer was adopted
on 2026-06-14, overriding the "do not migrate" recommendation below.
**Date:** 2026-06-14
**Context:** Item #5 of the "no-AI-slop document generation" roadmap (items #1–4 below).

## Update: hybrid renderer adopted

The "do not migrate" recommendation below was the initial evaluation. The
decision was subsequently overridden: PPTX generation now uses a **hybrid
renderer**.

- **Default decks (no template / `fronei-default`)** are rendered by
  **PptxGenJS**, via a Node subprocess (`apps/api/pptx_render/render.js`),
  driven by a role-based JSON payload built from the DeckPlan
  (`_build_js_deck_payload` in `document_generator.py`). This covers all
  DeckPlan roles from items #1–2 (section, content, two_content, chart,
  table, executive_summary, recommendation, timeline, architecture).
- **Branded decks (built-in or user-uploaded `.pptx` templates)** continue to
  go through **python-pptx**, preserving item #3's role-based template
  layout mapping (`_pptx_layout_for_role`), which reads an arbitrary
  template's masters/layouts/placeholders directly — something PptxGenJS
  cannot do.
- **Fallback:** if the Node renderer or `pptxgenjs` dependency is unavailable,
  or JS rendering raises for any reason, `generate_pptx_bytes` falls back to
  the python-pptx implementation (`_generate_pptx_bytes_python_pptx`) so
  generation never hard-fails.
- Item #4's render QA (LibreOffice headless) runs identically over the output
  of either renderer.

The comparison and "when to revisit" sections below reflect the *original*
evaluation and are retained for historical context; the runtime-fit and
ecosystem-maintenance tradeoffs they describe were accepted as the cost of
this migration.

## Roadmap recap (items #1–4, completed)

1. Richer DeckPlan layout hints (`executive_summary`, `recommendation`, `timeline`,
   `roadmap`, `architecture`, `risk_matrix`, `financial_model`, `decision_slide`, `appendix`).
2. Native charts — `python-pptx` charts (bar/line/pie) and `openpyxl.chart` bar charts,
   replacing plain-grid renderings of numeric data.
3. Role-based template layout mapping (`_pptx_layout_for_role`) — built-in and
   user-uploaded `.pptx` templates are mapped to semantic roles (title, section,
   content, two-content, title-only) via name matching, placeholder-shape
   heuristics, and standard-index fallback, so uploaded templates become reusable
   branded systems rather than visual shells.
4. Render QA via LibreOffice headless (`pptx_render_qa.py`) — every generated deck
   is converted to PDF and inspected for blank slides, text-dense slides likely to
   overflow, and visually crowded slides; results are attached to the
   `document_preview` payload as `render_qa`.

## The question

Should Fronei replace `python-pptx` (server-side, Python) with **PptxGenJS**
(Node/TypeScript) as the PPTX rendering engine?

## Recommendation

**Do not migrate now.** Items #1–4 close the gap that historically motivated a
switch to PptxGenJS (richer layouts, native charts, branded templates, and a
quality gate). The remaining differentiators of PptxGenJS don't address a
problem Fronei currently has, and a migration would be a substantial rewrite of
the rendering layer with real regression risk for marginal gain.

## Comparison

**Charting and layout fidelity.** Both libraries now support native bar/line/pie
charts and full layout/placeholder control. `python-pptx` reads the existing
`.pptx` master/layouts directly (used by item #3's role mapping); PptxGenJS
defines slide masters in code and has more limited support for *inheriting* an
arbitrary uploaded template's layouts — it's stronger when decks are built from
a master defined by Fronei, weaker for "bring your own branded template,"
which is the use case item #3 was built for.

**Runtime fit.** `apps/api` is a Python/FastAPI service with the full document
pipeline (DOCX/XLSX/PPTX generation, markdown parsing, deck-plan parsing) in
Python. Moving PPTX generation to PptxGenJS means either running a Node
subprocess from FastAPI, standing up a separate Node service, or porting the
DeckPlan rendering logic to TypeScript and maintaining two parallel
implementations (DOCX/XLSX would stay in Python). Any of these adds an
operational dependency and a second language in the document pipeline for one
file format.

**Render QA.** Item #4's LibreOffice-headless QA pass works identically
regardless of which library produced the `.pptx` — this benefit is
library-agnostic and doesn't favor either engine.

**Ecosystem/maintenance.** `python-pptx` is mature and already deeply
integrated (16 layout-role call sites, chart rendering, table/column/timeline
renderers, ~30 tests). A PptxGenJS migration would mean re-implementing all of
this — DeckPlan → slide rendering for every layout type (`bullets`, `two_column`,
`comparison`, `timeline`/`roadmap`, `architecture`, `risk_matrix`,
`financial_model`, `recommendation`/`decision_slide`, `appendix`, charts, tables)
— with no corresponding capability unlock.

## When to revisit

Reconsider PptxGenJS if one of these becomes true:

- The product needs **client-side or interactive** deck preview/editing (PptxGenJS
  runs in-browser; python-pptx does not).
- A future requirement needs animations, slide transitions, or media embedding
  that `python-pptx` genuinely cannot express (none of items #1–4 hit this wall).
- The team standardizes the whole document pipeline (DOCX/XLSX/PPTX) on a
  Node/TypeScript service for other reasons, making a one-format exception costly
  to maintain.

None of these apply today, so item #5 is closed as "evaluated, deferred" rather
than implemented.
