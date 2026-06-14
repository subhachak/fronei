#!/usr/bin/env node
/**
 * Fronei PPTX renderer (PptxGenJS).
 *
 * Reads a normalized "deck payload" JSON object from stdin and writes a
 * .pptx file to stdout as raw bytes.
 *
 * Payload shape (produced by document_generator._build_js_deck_payload):
 * {
 *   "version": 2,
 *   "design_system": {"name": "fronei_board_briefing", "mode": "..."},
 *   "title": "...",
 *   "subtitle": "..." | null,
 *   "slides": [
 *     { "role": "section", "title": "...", "blueprint": {"archetype","density","proof_object","emphasis"}, "notes": "..."|null },
 *     { "role": "content", "title": "...", "bullets": [{"level":0,"text":"..."}], "blueprint": {...}, "notes": ... },
 *     { "role": "two_content", "title": "...", "columns": [{"heading":"...","bullets":["..."]}], "notes": ... },
 *     { "role": "chart", "title": "...", "chart": {"type":"bar|line|pie","categories":[...],"series":[{"name","values"}]}, "notes": ... },
 *     { "role": "table", "title": "...", "rows": [["..."]], "notes": ... },
 *     { "role": "executive_summary", "title": "...", "bullets": ["..."], "notes": ... },
 *     { "role": "recommendation", "title": "...", "bullets": ["..."], "notes": ... },
 *     { "role": "timeline", "title": "...", "phases": [{"label","title","description"}], "notes": ... },
 *     { "role": "architecture", "title": "...", "bullets": ["..."], "notes": ... }
 *   ]
 * }
 *
 * This mirrors the layout semantics of the python-pptx renderer
 * (_pptx_render_deck_plan in document_generator.py) for the "no template"
 * (fronei-default) path. Decks built from a user-uploaded or built-in
 * branded .pptx template continue to be rendered by python-pptx, which can
 * read that template's layouts/placeholders directly.
 */

const pptxgen = require("pptxgenjs");

const NAVY = "132341";
const TEAL = "137C7A";
const GOLD = "F0B23A";
const NAVY_LIGHT = "1F365E";
const SLATE = "73665F";
const TEXT_DARK = "282421";
const TEXT_MUTED = "667085";
const WHITE = "FFFFFF";
const BG = "F7F1EE";
const CARD_BG = "FFFDFC";
const SOFT_BG = "EFE7E2";
const ACCENT = GOLD;
const ACCENT_LINE = "D8CDC6";
const HEADING_FACE = "Georgia";
const BODY_FACE = "Segoe UI";
const EMPHASIS_COLORS = {
  decision: GOLD,
  financial: GOLD,
  risk: "D9544D",
  technical: TEAL,
  execution: "6C8FB5",
  operational: TEAL,
};

const SLIDE_W = 13.333;
const SLIDE_H = 7.5;
const MARGIN_X = 0.65;

const MAX_BULLETS_PER_SLIDE = 6;
const MAX_APPENDIX_BULLETS = 10;
let ACTIVE_DESIGN_SYSTEM = {};

function themeTokens() {
  return (((ACTIVE_DESIGN_SYSTEM || {}).tokens || {}).theme || {});
}

function token(name, fallback) {
  return themeTokens()[name] || fallback;
}

function slideBg(slide) {
  slide.background = { color: BG };
}

function addBoardChrome(slide, sectionLabel) {
  slideBg(slide);
  slide.addShape("rect", {
    x: 0,
    y: 0,
    w: 0.24,
    h: SLIDE_H,
    fill: { color: TEAL },
    line: { color: TEAL },
  });
  slide.addShape("rect", {
    x: 0.24,
    y: 0,
    w: 0.035,
    h: SLIDE_H,
    fill: { color: GOLD },
    line: { color: GOLD },
  });
  if (sectionLabel) {
    slide.addText(String(sectionLabel).toUpperCase(), {
      x: 0.42,
      y: 6.86,
      w: 4.2,
      h: 0.24,
      fontSize: 7.5,
      bold: true,
      color: TEXT_MUTED,
      fontFace: BODY_FACE,
      charSpacing: 1.2,
    });
  }
}

function blueprintFor(spec) {
  return spec && spec.blueprint && typeof spec.blueprint === "object" ? spec.blueprint : {};
}

function componentTreeFor(spec) {
  return spec && spec.component_tree && typeof spec.component_tree === "object" ? spec.component_tree : {};
}

function templateFor(spec) {
  return componentTreeFor(spec).template || blueprintFor(spec).template || spec.role || "content";
}

function emphasisColor(spec) {
  const bp = blueprintFor(spec);
  return EMPHASIS_COLORS[bp.emphasis] || TEAL;
}

function emphasisInk(spec) {
  const color = emphasisColor(spec);
  return color === GOLD ? NAVY : WHITE;
}

function blueprintLabel(spec) {
  const bp = blueprintFor(spec);
  const label = templateFor(spec) || bp.proof_object || bp.archetype || bp.layout || spec.role || "insight";
  return String(label).replace(/_/g, " ").toUpperCase();
}

function archetype(spec) {
  return blueprintFor(spec).archetype || "";
}

function slideBullets(spec, cap = MAX_BULLETS_PER_SLIDE) {
  return (spec.bullets || []).slice(0, cap).map((b) => bulletText(typeof b === "object" ? (b.text || b) : b, 96)).filter(Boolean);
}

function addBlueprintKicker(slide, spec, x = 10.0, y = 0.58, w = 2.35) {
  const label = blueprintLabel(spec);
  slide.addText(label, {
    x,
    y,
    w,
    h: 0.2,
    fontSize: 7,
    bold: true,
    color: TEXT_MUTED,
    fontFace: BODY_FACE,
    align: "right",
    charSpacing: 1.1,
    fit: "shrink",
  });
}

function titleFontSize(text) {
  const len = (text || "").length;
  if (len <= 42) return 28;
  if (len <= 64) return 24;
  return 20;
}

function bulletText(text, limit) {
  return String(text || "").replace(/\s+/g, " ").trim();
}

function readStdin() {
  return new Promise((resolve, reject) => {
    const chunks = [];
    process.stdin.on("data", (chunk) => chunks.push(chunk));
    process.stdin.on("end", () => resolve(Buffer.concat(chunks).toString("utf-8")));
    process.stdin.on("error", reject);
  });
}

// Title box/accent-rule geometry. Titles can wrap to two lines at the
// smaller font sizes (e.g. ~60-72 char titles at 24pt within a 9.4in box),
// so the title box is tall enough for two lines and the accent rule sits
// below it — avoiding the accent rule overlapping a wrapped second line.
const TITLE_BOX_H = 1.0;
const TITLE_RULE_Y = 1.32;
const CONTENT_TOP_Y = 1.65;

function addTitle(slide, text) {
  addBoardChrome(slide);
  slide.addText(bulletText(text || "Untitled", 78), {
    x: 0.72,
    y: 0.42,
    w: 9.4,
    h: TITLE_BOX_H,
    fontSize: titleFontSize(text),
    bold: true,
    color: NAVY,
    fontFace: HEADING_FACE,
    align: "left",
    valign: "top",
    fit: "shrink",
  });
  slide.addShape("rect", {
    x: 0.72,
    y: TITLE_RULE_Y,
    w: 1.0,
    h: 0.04,
    fill: { color: ACCENT },
    line: { color: ACCENT },
  });
}

function addSlideTitle(slide, spec) {
  addTitle(slide, spec.title);
  addBlueprintKicker(slide, spec);
}

function iconFor(text) {
  const low = String(text || "").toLowerCase();
  if (/(risk|security|compliance|privacy|legal|gate|control)/.test(low)) return "!";
  if (/(cost|revenue|roi|budget|spend|margin|payback|savings|m\\$|\\$)/.test(low)) return "$";
  if (/(data|identity|profile|cdp|store|model|api|platform|architecture|system)/.test(low)) return "◎";
  if (/(time|phase|roadmap|q[1-4]|date|deadline|aug|oct|black friday)/.test(low)) return "→";
  if (/(owner|fte|team|ops|cio|cfo|vp|procurement|decision)/.test(low)) return "✓";
  return "•";
}

function addIconBadge(slide, x, y, label, opts = {}) {
  const fill = opts.fill || TEAL;
  const color = opts.color || WHITE;
  slide.addShape("roundRect", {
    x,
    y,
    w: opts.w || 0.46,
    h: opts.h || 0.46,
    fill: { color: fill },
    line: { color: fill },
    rectRadius: 0.08,
  });
  slide.addText(label, {
    x,
    y: y + 0.005,
    w: opts.w || 0.46,
    h: opts.h || 0.46,
    fontSize: opts.fontSize || 15,
    bold: true,
    color,
    fontFace: BODY_FACE,
    align: "center",
    valign: "middle",
    fit: "shrink",
  });
}

function addFooter(slide) {
  slide.addShape("rect", {
    x: 0.72,
    y: 6.88,
    w: 11.8,
    h: 0.01,
    fill: { color: ACCENT_LINE },
    line: { color: ACCENT_LINE, transparency: 30 },
  });
  slide.addText("Fronei board briefing", {
    x: 10.0,
    y: 6.98,
    w: 2.5,
    h: 0.18,
    fontSize: 7,
    color: TEXT_MUTED,
    fontFace: BODY_FACE,
    align: "right",
  });
}

function addNotes(slide, notes) {
  if (notes) slide.addNotes(notes);
}

// Top-level bullets use a small accent-colored square marker (rather than the
// default round bullet) to echo the colored accent markers seen on the
// Claude reference deck's bullet slides. Nested bullets keep a plain dash so
// the accent marker stays a top-level "this is a key point" signal.
const ACCENT_BULLET = { code: "25AA", color: ACCENT, indent: 18 };
const SUB_BULLET = { code: "2013", indent: 14 };

function bulletsToTextProps(bullets, opts) {
  opts = opts || {};
  const items = (bullets && bullets.length ? bullets : [{ level: 0, text: "" }]);
  return items.map((b) => {
    const level = typeof b === "object" ? (b.level || 0) : 0;
    const text = typeof b === "object" ? (b.text || "") : String(b || "");
    const bulletStyle = !text ? false : level === 0 ? ACCENT_BULLET : SUB_BULLET;
    return {
      text,
      options: Object.assign(
        {
          bullet: bulletStyle,
          indentLevel: Math.max(0, Math.min(level, 4)),
          fontSize: opts.fontSize || 16,
          color: opts.color || TEXT_DARK,
          fontFace: BODY_FACE,
          breakLine: true,
        },
        opts.extra || {}
      ),
    };
  });
}

function renderTitleSlide(pptx, title, subtitle) {
  const slide = pptx.addSlide();
  slide.background = { color: NAVY };
  slide.addShape("rect", {
    x: 0,
    y: 0,
    w: 4.25,
    h: SLIDE_H,
    fill: { color: TEAL },
    line: { color: TEAL },
  });
  slide.addShape("rect", {
    x: 4.25,
    y: 0,
    w: 0.05,
    h: SLIDE_H,
    fill: { color: GOLD },
    line: { color: GOLD },
  });
  addIconBadge(slide, 1.65, 1.05, "AI", { w: 0.86, h: 0.86, fill: WHITE, color: TEAL, fontSize: 18 });
  slide.addText("BOARD BRIEFING", {
    x: 1.15,
    y: 2.35,
    w: 2.4,
    h: 0.28,
    fontSize: 10,
    bold: true,
    color: "D5ECEA",
    fontFace: BODY_FACE,
    align: "center",
    charSpacing: 2.5,
  });
  slide.addShape("rect", {
    x: 1.1,
    y: 3.1,
    w: 2.2,
    h: 0.035,
    fill: { color: GOLD },
    line: { color: GOLD },
  });
  slide.addText("CONFIDENTIAL", {
    x: 1.1,
    y: 6.18,
    w: 2.2,
    h: 0.25,
    fontSize: 9,
    bold: true,
    color: "D5ECEA",
    fontFace: BODY_FACE,
    align: "center",
  });
  slide.addText(bulletText(title || "Fronei deck", 88), {
    x: 4.72,
    y: 1.18,
    w: 7.3,
    h: 2.2,
    fontSize: 39,
    bold: true,
    color: WHITE,
    fontFace: HEADING_FACE,
    align: "left",
    valign: "middle",
    fit: "shrink",
  });
  if (subtitle) {
    slide.addText(subtitle, {
      x: 4.78,
      y: 4.22,
      w: 7.4,
      h: 0.8,
      fontSize: 15,
      color: "B9D8D6",
      fontFace: BODY_FACE,
      align: "left",
      valign: "top",
      fit: "shrink",
    });
  }
  slide.addShape("rect", {
    x: 4.78,
    y: 3.8,
    w: 7.5,
    h: 0.035,
    fill: { color: GOLD },
    line: { color: GOLD },
  });
  slide.addText("Prepared with Fronei", {
    x: 4.78,
    y: 6.7,
    w: 4.0,
    h: 0.25,
    fontSize: 9,
    color: "8EA9B8",
    fontFace: BODY_FACE,
  });
}

function renderSectionSlide(pptx, spec) {
  const slide = pptx.addSlide();
  slide.background = { color: TEXT_DARK };

  const titleY = SLIDE_H / 2 - 0.8;

  if (spec.section_number) {
    const label = `${String(spec.section_number).padStart(2, "0")}`;
    // Small accent rule above the number, echoing the orange underline used
    // on regular slide titles, to tie section breaks to the deck's visual language.
    slide.addShape("rect", {
      x: MARGIN_X,
      y: titleY - 0.55,
      w: 0.6,
      h: 0.05,
      fill: { color: ACCENT },
      line: { color: ACCENT },
    });
    slide.addText(label, {
      x: MARGIN_X,
      y: titleY - 0.5,
      w: 3.0,
      h: 0.5,
      fontSize: 16,
      bold: true,
      color: ACCENT,
      fontFace: HEADING_FACE,
      align: "left",
      valign: "top",
      charSpacing: 2,
    });
  }

  slide.addText(bulletText(spec.title || "Untitled", 70), {
    x: MARGIN_X,
    y: titleY,
    w: 9.5,
    h: 1.6,
    fontSize: 32,
    bold: true,
    color: WHITE,
    fontFace: HEADING_FACE,
    align: "left",
    valign: "middle",
    fit: "shrink",
  });
  addNotes(slide, spec.notes);
  return slide;
}

function renderContentSlide(pptx, spec) {
  const slide = pptx.addSlide();
  addSlideTitle(slide, spec);
  const accent = emphasisColor(spec);
  const accentInk = emphasisInk(spec);
  const cap = spec.appendix ? MAX_APPENDIX_BULLETS : MAX_BULLETS_PER_SLIDE;
  const visibleCap = spec.appendix ? cap : Math.min(cap, 4);
  const bullets = (spec.bullets || []).slice(0, visibleCap).map((b) => ({
    level: b.level || 0,
    text: bulletText(b.text || b, 96),
  }));
  if (spec.appendix) {
    slide.addText(bulletsToTextProps(bullets, { fontSize: 12 }), {
      x: 0.72,
      y: 1.65,
      w: 10.9,
      h: 4.9,
      valign: "top",
      fontFace: BODY_FACE,
      lineSpacingMultiple: 1.08,
    });
    addFooter(slide);
    addNotes(slide, spec.notes);
    return slide;
  }

  const lead = bullets[0]?.text || "";
  if (lead) {
    slide.addShape("roundRect", {
      x: 0.72,
      y: 1.58,
      w: 4.05,
      h: 4.75,
      fill: { color: NAVY },
      line: { color: NAVY },
      rectRadius: 0.08,
    });
    addIconBadge(slide, 1.02, 1.92, iconFor(lead), { fill: accent, color: accentInk, w: 0.58, h: 0.58, fontSize: 18 });
    slide.addText(lead, {
      x: 1.02,
      y: 2.72,
      w: 3.35,
      h: 2.6,
      fontSize: 22,
      bold: true,
      color: WHITE,
      fontFace: HEADING_FACE,
      valign: "mid",
      fit: "shrink",
      wrap: true,
    });
  }

  const supporting = bullets.slice(1, 4);
  const cardX = 5.05;
  const cardW = 7.35;
  const cardH = 1.28;
  supporting.forEach((b, idx) => {
    const y = 1.62 + idx * (cardH + 0.28);
    slide.addShape("roundRect", {
      x: cardX,
      y,
      w: cardW,
      h: cardH,
      fill: { color: CARD_BG },
      line: { color: ACCENT_LINE, transparency: 10 },
      rectRadius: 0.05,
    });
    addIconBadge(slide, cardX + 0.18, y + 0.34, iconFor(b.text), { fill: accent, color: accentInk, w: 0.45, h: 0.45, fontSize: 14 });
    slide.addText(b.text, {
      x: cardX + 0.78,
      y: y + 0.22,
      w: cardW - 1.0,
      h: cardH - 0.32,
      fontSize: 13,
      color: TEXT_DARK,
      fontFace: BODY_FACE,
      valign: "middle",
      fit: "shrink",
      wrap: true,
    });
  });
  if (!supporting.length && !lead) {
    slide.addText("No slide content provided.", {
      x: 0.72,
      y: 1.65,
      w: 8.4,
      h: 1.0,
      fontSize: 14,
      color: TEXT_MUTED,
      fontFace: BODY_FACE,
    });
  }
  addFooter(slide);
  addNotes(slide, spec.notes);
  return slide;
}

function renderAgendaSlide(pptx, spec) {
  const slide = pptx.addSlide();
  addSlideTitle(slide, spec);
  const items = slideBullets(spec, 7);
  items.forEach((item, idx) => {
    const y = 1.62 + idx * 0.62;
    slide.addText(String(idx + 1).padStart(2, "0"), {
      x: 0.86,
      y,
      w: 0.6,
      h: 0.32,
      fontSize: 13,
      bold: true,
      color: GOLD,
      fontFace: HEADING_FACE,
      align: "right",
    });
    slide.addShape("rect", {
      x: 1.72,
      y: y + 0.16,
      w: 0.42,
      h: 0.025,
      fill: { color: ACCENT_LINE },
      line: { color: ACCENT_LINE },
    });
    slide.addText(item, {
      x: 2.34,
      y: y - 0.02,
      w: 8.8,
      h: 0.38,
      fontSize: 15,
      color: NAVY,
      fontFace: BODY_FACE,
      fit: "shrink",
      wrap: true,
    });
  });
  addFooter(slide);
  addNotes(slide, spec.notes);
  return slide;
}

function renderCalloutSlide(pptx, spec) {
  const slide = pptx.addSlide();
  addSlideTitle(slide, spec);
  const callout = spec.callout || {};
  const bullets = slideBullets(spec, 4);
  const headline = callout.text || bullets[0] || "";
  slide.addShape("roundRect", {
    x: 0.92,
    y: 1.78,
    w: 11.15,
    h: 2.55,
    fill: { color: NAVY },
    line: { color: NAVY },
    rectRadius: 0.08,
  });
  slide.addText(callout.label || "Key insight", {
    x: 1.28,
    y: 2.1,
    w: 2.3,
    h: 0.24,
    fontSize: 9,
    bold: true,
    color: GOLD,
    fontFace: BODY_FACE,
    charSpacing: 1.2,
  });
  slide.addText(headline, {
    x: 1.28,
    y: 2.55,
    w: 10.0,
    h: 1.12,
    fontSize: 24,
    bold: true,
    color: WHITE,
    fontFace: HEADING_FACE,
    fit: "shrink",
    wrap: true,
  });
  bullets.slice(callout.text ? 0 : 1, callout.text ? 3 : 4).forEach((b, idx) => {
    slide.addText(b, {
      x: 1.08 + idx * 3.9,
      y: 4.85,
      w: 3.45,
      h: 0.72,
      fontSize: 12,
      bold: true,
      color: TEXT_DARK,
      fontFace: BODY_FACE,
      fit: "shrink",
      wrap: true,
    });
  });
  addFooter(slide);
  addNotes(slide, spec.notes);
  return slide;
}

function renderTwoContentSlide(pptx, spec) {
  const slide = pptx.addSlide();
  addSlideTitle(slide, spec);
  const accent = emphasisColor(spec);
  const accentInk = emphasisInk(spec);
  // Up to 3 cards (comparison / three_card_system / governance_grid /
  // principles_grid all route here with 2-3 columns).
  const cols = (spec.columns || []).slice(0, 3);
  const n = Math.max(cols.length, 1);
  const totalW = SLIDE_W - MARGIN_X * 2;
  const gap = 0.35;
  const colW = (totalW - gap * (n - 1)) / n;
  const top = CONTENT_TOP_Y;
  const height = SLIDE_H - top - 0.35;
  const headingFontSize = n >= 3 ? 14 : 15;
  const bulletFontSize = n >= 3 ? 11 : 12;
  const bulletCap = n >= 3 ? 4 : 3;
  cols.forEach((col, idx) => {
    const left = MARGIN_X + idx * (colW + gap);
    const icon = iconFor(`${col.heading || ""} ${(col.bullets || []).join(" ")}`);
    slide.addShape("roundRect", {
      x: left,
      y: top,
      w: colW,
      h: height,
      fill: { color: CARD_BG },
      line: { color: ACCENT_LINE, transparency: 20 },
      rectRadius: 0.04,
    });
    slide.addShape("rect", {
      x: left,
      y: top,
      w: colW,
      h: 0.08,
      fill: { color: idx % 2 === 0 ? accent : GOLD },
      line: { color: idx % 2 === 0 ? accent : GOLD },
    });
    addIconBadge(slide, left + 0.2, top + 0.28, icon, { fill: idx % 2 === 0 ? accent : GOLD, color: idx % 2 === 0 ? accentInk : NAVY, w: 0.46, h: 0.46, fontSize: 14 });
    const parts = [];
    if (col.heading) {
      parts.push({ text: bulletText(col.heading, 42), options: { fontSize: headingFontSize, bold: true, color: NAVY, fontFace: HEADING_FACE, breakLine: true } });
    }
    const bullets = bulletsToTextProps((col.bullets || []).slice(0, bulletCap).map((b) => ({ level: 0, text: bulletText(b, 78) })), { fontSize: bulletFontSize });
    slide.addText(parts.concat(bullets), {
      x: left + 0.2,
      y: top + 0.92,
      w: colW - 0.4,
      h: height - 1.05,
      valign: "top",
      fontFace: BODY_FACE,
      lineSpacingMultiple: 1.1,
    });
  });
  addFooter(slide);
  addNotes(slide, spec.notes);
  return slide;
}

function renderRiskRegisterSlide(pptx, spec) {
  const slide = pptx.addSlide();
  addSlideTitle(slide, spec);
  const riskColor = EMPHASIS_COLORS.risk;
  const rows = (spec.columns && spec.columns.length)
    ? spec.columns.slice(0, 3).map((col) => ({
        risk: col.heading || "Risk",
        signal: (col.bullets || [])[0] || "",
        mitigation: (col.bullets || [])[1] || (col.bullets || [])[0] || "",
      }))
    : slideBullets(spec, 4).slice(0, 3).map((b, idx) => ({
        risk: idx === 0 ? "Primary risk" : `Risk ${idx + 1}`,
        signal: b,
        mitigation: "Define owner, control, and escalation path.",
      }));
  const top = 1.62;
  const rowH = 1.25;
  const headers = ["Risk", "Signal", "Mitigation"];
  const xs = [0.72, 3.95, 7.2];
  const ws = [2.9, 2.9, 5.2];
  headers.forEach((h, idx) => {
    slide.addText(h.toUpperCase(), {
      x: xs[idx],
      y: 1.28,
      w: ws[idx],
      h: 0.22,
      fontSize: 8,
      bold: true,
      color: TEXT_MUTED,
      fontFace: BODY_FACE,
      charSpacing: 1.2,
    });
  });
  rows.forEach((row, idx) => {
    const y = top + idx * (rowH + 0.22);
    slide.addShape("roundRect", {
      x: 0.72,
      y,
      w: 11.65,
      h: rowH,
      fill: { color: idx === 0 ? NAVY : CARD_BG },
      line: { color: idx === 0 ? NAVY : ACCENT_LINE },
      rectRadius: 0.05,
    });
    addIconBadge(slide, 0.95, y + 0.34, "!", { fill: riskColor, color: WHITE, w: 0.42, h: 0.42, fontSize: 13 });
    slide.addText(bulletText(row.risk, 48), {
      x: 1.48,
      y: y + 0.26,
      w: 2.05,
      h: 0.72,
      fontSize: 12,
      bold: true,
      color: idx === 0 ? WHITE : NAVY,
      fontFace: BODY_FACE,
      fit: "shrink",
      wrap: true,
    });
    slide.addText(bulletText(row.signal, 90), {
      x: 3.95,
      y: y + 0.24,
      w: 2.9,
      h: 0.76,
      fontSize: 11,
      color: idx === 0 ? WHITE : TEXT_DARK,
      fontFace: BODY_FACE,
      fit: "shrink",
      wrap: true,
    });
    slide.addText(bulletText(row.mitigation, 105), {
      x: 7.2,
      y: y + 0.24,
      w: 4.8,
      h: 0.76,
      fontSize: 11,
      color: idx === 0 ? WHITE : TEXT_DARK,
      fontFace: BODY_FACE,
      fit: "shrink",
      wrap: true,
    });
  });
  slide.addText("Risk posture", {
    x: 0.72,
    y: 5.92,
    w: 2.4,
    h: 0.28,
    fontSize: 13,
    bold: true,
    color: riskColor,
    fontFace: HEADING_FACE,
  });
  addFooter(slide);
  addNotes(slide, spec.notes);
  return slide;
}

function renderRiskHeatmapSlide(pptx, spec) {
  const slide = pptx.addSlide();
  addSlideTitle(slide, spec);
  const items = (spec.heatmap || []).slice(0, 5);
  const gridX = 0.95;
  const gridY = 1.72;
  const cell = 1.18;
  const colors = {
    low: "D7EFE7",
    medium: "F6DF9D",
    high: "E89186",
  };
  const order = ["low", "medium", "high"];
  slide.addText("Impact", {
    x: 0.4,
    y: gridY + 1.05,
    w: 0.32,
    h: 0.3,
    fontSize: 8,
    bold: true,
    color: TEXT_MUTED,
    fontFace: BODY_FACE,
    rotate: 270,
  });
  slide.addText("Likelihood", {
    x: gridX + 1.05,
    y: gridY + cell * 3 + 0.18,
    w: 1.6,
    h: 0.2,
    fontSize: 8,
    bold: true,
    color: TEXT_MUTED,
    fontFace: BODY_FACE,
    align: "center",
  });
  for (let yIdx = 0; yIdx < 3; yIdx++) {
    for (let xIdx = 0; xIdx < 3; xIdx++) {
      const impact = order[2 - yIdx];
      const likelihood = order[xIdx];
      const severity = xIdx + (2 - yIdx);
      const fill = severity <= 1 ? colors.low : severity <= 3 ? colors.medium : colors.high;
      slide.addShape("roundRect", {
        x: gridX + xIdx * cell,
        y: gridY + yIdx * cell,
        w: cell - 0.04,
        h: cell - 0.04,
        fill: { color: fill },
        line: { color: WHITE, transparency: 10 },
        rectRadius: 0.03,
      });
      slide.addText(`${impact[0].toUpperCase()} / ${likelihood[0].toUpperCase()}`, {
        x: gridX + xIdx * cell + 0.08,
        y: gridY + yIdx * cell + 0.08,
        w: cell - 0.2,
        h: 0.18,
        fontSize: 6.5,
        bold: true,
        color: TEXT_MUTED,
        fontFace: BODY_FACE,
      });
    }
  }
  items.forEach((item, idx) => {
    const xIdx = order.indexOf(String(item.likelihood || "").toLowerCase());
    const yIdx = 2 - order.indexOf(String(item.impact || "").toLowerCase());
    if (xIdx < 0 || yIdx < 0) return;
    const x = gridX + xIdx * cell + 0.34 + (idx % 2) * 0.18;
    const y = gridY + yIdx * cell + 0.42 + (idx % 2) * 0.12;
    slide.addShape("ellipse", {
      x,
      y,
      w: 0.32,
      h: 0.32,
      fill: { color: NAVY },
      line: { color: GOLD, width: 1 },
    });
    slide.addText(String(idx + 1), {
      x,
      y: y + 0.02,
      w: 0.32,
      h: 0.22,
      fontSize: 8,
      bold: true,
      color: WHITE,
      fontFace: BODY_FACE,
      align: "center",
    });
  });
  slide.addShape("roundRect", {
    x: 5.05,
    y: 1.65,
    w: 7.15,
    h: 4.45,
    fill: { color: CARD_BG },
    line: { color: ACCENT_LINE },
    rectRadius: 0.06,
  });
  slide.addText("Risk register", {
    x: 5.32,
    y: 1.92,
    w: 2.4,
    h: 0.28,
    fontSize: 13,
    bold: true,
    color: EMPHASIS_COLORS.risk,
    fontFace: HEADING_FACE,
  });
  items.forEach((item, idx) => {
    const y = 2.45 + idx * 0.68;
    addIconBadge(slide, 5.32, y, String(idx + 1), { fill: EMPHASIS_COLORS.risk, color: WHITE, w: 0.32, h: 0.32, fontSize: 9 });
    slide.addText(`${bulletText(item.label, 44)} · ${item.likelihood}/${item.impact}`, {
      x: 5.78,
      y: y - 0.03,
      w: 5.85,
      h: 0.26,
      fontSize: 10.5,
      bold: true,
      color: NAVY,
      fontFace: BODY_FACE,
      fit: "shrink",
    });
    if (item.mitigation) {
      slide.addText(bulletText(item.mitigation, 78), {
        x: 5.78,
        y: y + 0.25,
        w: 5.85,
        h: 0.22,
        fontSize: 8.5,
        color: TEXT_MUTED,
        fontFace: BODY_FACE,
        fit: "shrink",
      });
    }
  });
  addFooter(slide);
  addNotes(slide, spec.notes);
  return slide;
}

function renderOperatingModelSlide(pptx, spec) {
  const slide = pptx.addSlide();
  addSlideTitle(slide, spec);
  const cols = (spec.columns || []).slice(0, 4);
  const lanes = cols.length ? cols : slideBullets(spec, 4).map((b, idx) => ({ heading: `Lane ${idx + 1}`, bullets: [b] }));
  const top = 1.58;
  const laneH = 0.98;
  lanes.slice(0, 4).forEach((lane, idx) => {
    const y = top + idx * (laneH + 0.22);
    const dark = idx === 0;
    slide.addShape("roundRect", {
      x: 0.72,
      y,
      w: 11.65,
      h: laneH,
      fill: { color: dark ? NAVY : CARD_BG },
      line: { color: dark ? NAVY : ACCENT_LINE },
      rectRadius: 0.05,
    });
    addIconBadge(slide, 0.96, y + 0.25, "✓", { fill: dark ? GOLD : TEAL, color: dark ? NAVY : WHITE, w: 0.4, h: 0.4, fontSize: 12 });
    slide.addText(bulletText(lane.heading || `Owner ${idx + 1}`, 42), {
      x: 1.55,
      y: y + 0.2,
      w: 2.55,
      h: 0.5,
      fontSize: 13,
      bold: true,
      color: dark ? WHITE : NAVY,
      fontFace: HEADING_FACE,
      fit: "shrink",
    });
    slide.addText((lane.bullets || []).slice(0, 2).map((b) => bulletText(b, 78)).join("  |  "), {
      x: 4.28,
      y: y + 0.22,
      w: 7.62,
      h: 0.48,
      fontSize: 11,
      color: dark ? WHITE : TEXT_DARK,
      fontFace: BODY_FACE,
      fit: "shrink",
      wrap: true,
    });
  });
  slide.addText("Operating lanes", {
    x: 0.72,
    y: 6.05,
    w: 2.6,
    h: 0.28,
    fontSize: 13,
    bold: true,
    color: TEAL,
    fontFace: HEADING_FACE,
  });
  addFooter(slide);
  addNotes(slide, spec.notes);
  return slide;
}

function renderComparisonMatrixSlide(pptx, spec) {
  const slide = pptx.addSlide();
  addSlideTitle(slide, spec);
  const cols = (spec.columns || []).slice(0, 3);
  if (!cols.length) return renderTwoContentSlide(pptx, spec);
  const top = 1.65;
  const colW = 3.65;
  cols.forEach((col, idx) => {
    const x = 0.72 + idx * 3.92;
    const recommended = /recommend|preferred|target|selected/i.test(`${col.heading} ${(col.bullets || []).join(" ")}`) || idx === cols.length - 1;
    slide.addShape("roundRect", {
      x,
      y: top,
      w: colW,
      h: 4.25,
      fill: { color: recommended ? NAVY : CARD_BG },
      line: { color: recommended ? NAVY : ACCENT_LINE },
      rectRadius: 0.06,
    });
    slide.addText(recommended ? "RECOMMENDED" : `OPTION ${idx + 1}`, {
      x: x + 0.22,
      y: top + 0.22,
      w: colW - 0.44,
      h: 0.22,
      fontSize: 7.5,
      bold: true,
      color: recommended ? GOLD : TEXT_MUTED,
      fontFace: BODY_FACE,
      charSpacing: 1.1,
    });
    slide.addText(bulletText(col.heading || `Option ${idx + 1}`, 46), {
      x: x + 0.22,
      y: top + 0.62,
      w: colW - 0.44,
      h: 0.62,
      fontSize: 15,
      bold: true,
      color: recommended ? WHITE : NAVY,
      fontFace: HEADING_FACE,
      fit: "shrink",
      wrap: true,
    });
    slide.addText(bulletsToTextProps((col.bullets || []).slice(0, 4).map((b) => ({ level: 0, text: bulletText(b, 76) })), { fontSize: 10.6, color: recommended ? WHITE : TEXT_DARK }), {
      x: x + 0.24,
      y: top + 1.55,
      w: colW - 0.48,
      h: 2.25,
      fontFace: BODY_FACE,
      fit: "shrink",
      wrap: true,
    });
  });
  addFooter(slide);
  addNotes(slide, spec.notes);
  return slide;
}

const CHART_TYPE_MAP = {
  bar: "bar",
  line: "line",
  pie: "pie",
};

function renderChartSlide(pptx, spec) {
  const slide = pptx.addSlide();
  addSlideTitle(slide, spec);
  const chart = spec.chart || {};
  const categories = chart.categories || [];
  let series = chart.series || [];
  const chartType = CHART_TYPE_MAP[chart.type] || "bar";
  if (chartType === "pie") series = series.slice(0, 1);

  const chartData = series.map((s) => ({
    name: s.name || "Series",
    labels: categories,
    values: s.values || [],
  }));

  const options = {
    x: 1.0,
    y: 1.6,
    w: 11.3,
    h: 5.3,
    showLegend: series.length > 1 || chartType === "pie",
    legendPos: "b",
    showTitle: false,
    chartColors: token("chart_palette", [ACCENT, NAVY, "8C6F5D", "C9A14A"]),
    catAxisLabelFontSize: 11,
    valAxisLabelFontSize: 11,
    dataLabelFontSize: 10,
  };
  if (chartType === "pie") {
    options.showValue = false;
    options.dataBorder = { pt: 1, color: WHITE };
  }

  slide.addChart(pptx.charts[chartType.toUpperCase()] || chartType, chartData, options);
  addNotes(slide, spec.notes);
  return slide;
}

function renderTableSlide(pptx, spec) {
  const slide = pptx.addSlide();
  addSlideTitle(slide, spec);
  const rows = spec.rows || [];
  if (!rows.length) {
    addNotes(slide, spec.notes);
    return slide;
  }
  const nCols = Math.max(...rows.map((r) => r.length));
  const tableRows = rows.map((row, rIdx) => {
    const cells = [];
    for (let c = 0; c < nCols; c++) {
      const text = c < row.length ? String(row[c] != null ? row[c] : "") : "";
      cells.push({
        text,
        options: {
          fontSize: 12,
          bold: rIdx === 0,
          color: rIdx === 0 ? token("table_header_text", WHITE) : TEXT_DARK,
          fill: rIdx === 0 ? { color: token("table_header_fill", NAVY) } : { color: rIdx % 2 === 0 ? SOFT_BG : WHITE },
          valign: "middle",
        },
      });
    }
    return cells;
  });

  slide.addTable(tableRows, {
    x: 0.5,
    y: 1.6,
    w: SLIDE_W - 1.0,
    h: Math.min(0.5 + 0.4 * rows.length, SLIDE_H - 1.8),
    fontFace: BODY_FACE,
    border: { type: "solid", color: "E2E6EB", pt: 0.5 },
    autoPage: false,
  });
  addNotes(slide, spec.notes);
  return slide;
}

function renderExecutiveSummarySlide(pptx, spec) {
  const slide = pptx.addSlide();
  addSlideTitle(slide, spec);
  const accent = emphasisColor(spec);
  const accentInk = emphasisInk(spec);
  const bullets = spec.bullets || [];
  const headline = bullets[0] || "";
  const support = bullets.slice(1, MAX_BULLETS_PER_SLIDE);
  if (headline) {
    slide.addShape("roundRect", {
      x: 0.72,
      y: 1.56,
      w: 11.7,
      h: 1.78,
      fill: { color: NAVY },
      line: { color: NAVY },
      rectRadius: 0.08,
    });
    addIconBadge(slide, 1.03, 2.05, iconFor(`${spec.title} ${headline}`), { fill: accent, color: accentInk, w: 0.56, h: 0.56, fontSize: 18 });
    slide.addText(headline, {
      x: 1.82,
      y: 1.76,
      w: 9.85,
      h: 1.32,
      fontSize: 22,
      bold: true,
      color: WHITE,
      fontFace: HEADING_FACE,
      valign: "middle",
      wrap: true,
      fit: "shrink",
    });
  }
  if (support.length) {
    const cards = support.slice(0, 3);
    const gap = 0.28;
    const w = (11.7 - gap * (cards.length - 1)) / cards.length;
    cards.forEach((b, idx) => {
      const x = 0.72 + idx * (w + gap);
      slide.addShape("roundRect", {
        x,
        y: 3.72,
        w,
        h: 1.9,
        fill: { color: CARD_BG },
        line: { color: ACCENT_LINE },
        rectRadius: 0.06,
      });
      addIconBadge(slide, x + 0.22, 4.05, iconFor(b), { fill: idx === 1 ? GOLD : accent, color: idx === 1 ? NAVY : accentInk, w: 0.46, h: 0.46, fontSize: 13 });
      slide.addText(bulletText(b, 90), {
        x: x + 0.22,
        y: 4.65,
        w: w - 0.44,
        h: 0.72,
        fontSize: 12,
        bold: true,
        color: TEXT_DARK,
        fontFace: BODY_FACE,
        fit: "shrink",
        wrap: true,
      });
    });
  }
  addFooter(slide);
  addNotes(slide, spec.notes);
  return slide;
}

function renderRecommendationSlide(pptx, spec) {
  const slide = pptx.addSlide();
  addSlideTitle(slide, spec);
  const bullets = spec.bullets || [];
  const primary = bullets[0] || "";
  const rationale = bullets.slice(1, 1 + MAX_BULLETS_PER_SLIDE);
  if (primary) {
    slide.addShape("roundRect", {
      x: 0.72,
      y: 1.5,
      w: 11.7,
      h: 1.55,
      fill: { color: NAVY },
      line: { color: NAVY },
      rectRadius: 0.08,
    });
    addIconBadge(slide, 1.02, 1.98, "✓", { fill: GOLD, color: NAVY, w: 0.52, h: 0.52, fontSize: 16 });
    slide.addText(`Recommendation: ${primary}`, {
      x: 1.72,
      y: 1.66,
      w: 10.2,
      h: 1.18,
      fontSize: 18,
      bold: true,
      color: WHITE,
      fontFace: HEADING_FACE,
      valign: "middle",
      align: "left",
      wrap: true,
    });
  }
  if (rationale.length) {
    const cards = rationale.slice(0, 3);
    cards.forEach((b, idx) => {
      const x = 0.72 + idx * 3.95;
      slide.addShape("roundRect", {
        x,
        y: 3.48,
        w: 3.65,
        h: 1.65,
        fill: { color: CARD_BG },
        line: { color: ACCENT_LINE },
        rectRadius: 0.06,
      });
      addIconBadge(slide, x + 0.18, 3.78, iconFor(b), { fill: TEAL, color: WHITE, w: 0.42, h: 0.42, fontSize: 12 });
      slide.addText(bulletText(b, 88), {
        x: x + 0.74,
        y: 3.68,
        w: 2.65,
        h: 1.1,
        fontSize: 11.5,
        color: TEXT_DARK,
        fontFace: BODY_FACE,
        valign: "middle",
        fit: "shrink",
        wrap: true,
      });
    });
  }
  addFooter(slide);
  addNotes(slide, spec.notes);
  return slide;
}

function renderTimelineSlide(pptx, spec) {
  const slide = pptx.addSlide();
  addSlideTitle(slide, spec);
  const phases = (spec.phases || [])
    .filter((p) => p && (p.title || p.label || p.description))
    .slice(0, 6);
  if (!phases.length) {
    addNotes(slide, spec.notes);
    return slide;
  }
  const totalW = 11.7;
  const gap = 0.25;
  const n = phases.length;
  const boxW = (totalW - gap * (n - 1)) / n;
  const top = 1.82;

  phases.forEach((ph, idx) => {
    const left = 0.72 + idx * (boxW + gap);
    if (idx > 0) {
      slide.addShape("rect", {
        x: left - gap,
        y: top + 0.35,
        w: gap,
        h: 0.04,
        fill: { color: ACCENT_LINE },
        line: { color: ACCENT_LINE },
      });
    }
    slide.addShape("ellipse", {
      x: left + boxW / 2 - 0.15,
      y: top + 0.2,
      w: 0.3,
      h: 0.3,
      fill: { color: ACCENT },
      line: { color: ACCENT },
    });
    slide.addShape("roundRect", {
      x: left,
      y: top + 0.72,
      w: boxW,
      h: 3.52,
      fill: { color: idx === 1 ? NAVY : CARD_BG },
      line: { color: idx === 1 ? NAVY : ACCENT_LINE },
      rectRadius: 0.06,
    });
    const lines = [];
    if (ph.label) lines.push(ph.label);
    if (ph.title) lines.push(ph.title);
    if (ph.description) lines.push(bulletText(ph.description, 82));
    slide.addText(lines.join("\n"), {
      x: left + 0.16,
      y: top + 0.95,
      w: boxW - 0.32,
      h: 2.95,
      valign: "top",
      fontFace: BODY_FACE,
      fontSize: 10.4,
      bold: false,
      color: idx === 1 ? WHITE : TEXT_DARK,
      fit: "shrink",
      wrap: true,
    });
  });
  addFooter(slide);
  addNotes(slide, spec.notes);
  return slide;
}

function renderStatCardsSlide(pptx, spec) {
  const slide = pptx.addSlide();
  addSlideTitle(slide, spec);
  const accent = emphasisColor(spec);
  const stats = (spec.stats || []).filter((s) => s && (s.value || s.label)).slice(0, 4);
  if (!stats.length) {
    addNotes(slide, spec.notes);
    return slide;
  }

  const top = 1.5;
  const cardH = 2.0;
  const gap = 0.25;
  const totalW = 12.0;
  const n = stats.length;
  const cardW = (totalW - gap * (n - 1)) / n;

  stats.forEach((stat, idx) => {
    const left = MARGIN_X + idx * (cardW + gap);
    slide.addShape("roundRect", {
      x: left,
      y: top,
      w: cardW,
      h: cardH,
      fill: { color: CARD_BG },
      line: { color: ACCENT_LINE, width: 1 },
      rectRadius: 0.06,
    });
    const parts = [{ text: stat.value || "", options: { fontSize: 28, bold: true, color: accent, fontFace: HEADING_FACE, breakLine: true, align: "center" } }];
    if (stat.label) {
      parts.push({ text: stat.label, options: { fontSize: 13, color: TEXT_DARK, fontFace: BODY_FACE, breakLine: true, align: "center" } });
    }
    if (stat.source) {
      parts.push({ text: stat.source, options: { fontSize: 9, italic: true, color: TEXT_MUTED, fontFace: BODY_FACE, align: "center" } });
    }
    slide.addText(parts, {
      x: left + 0.1,
      y: top,
      w: cardW - 0.2,
      h: cardH,
      valign: "middle",
      align: "center",
      wrap: true,
    });
  });

  const callout = spec.callout;
  if (callout && (callout.text || "").trim()) {
    slide.addShape("roundRect", {
      x: MARGIN_X,
      y: 3.85,
      w: SLIDE_W - MARGIN_X * 2,
      h: 1.6,
      fill: { color: TEXT_DARK },
      line: { color: TEXT_DARK },
      rectRadius: 0.08,
    });
    slide.addText(
      [
        { text: callout.label || "Key Insight", options: { fontSize: 14, bold: true, color: WHITE, fontFace: HEADING_FACE, breakLine: true } },
        { text: callout.text || "", options: { fontSize: 14, color: WHITE, fontFace: BODY_FACE } },
      ],
      {
        x: MARGIN_X + 0.2,
        y: 3.85,
        w: SLIDE_W - MARGIN_X * 2 - 0.4,
        h: 1.6,
        valign: "middle",
        align: "left",
        wrap: true,
        fontFace: BODY_FACE,
        lineSpacingMultiple: 1.1,
      }
    );
  }

  addNotes(slide, spec.notes);
  return slide;
}

function renderInvestmentCaseSlide(pptx, spec) {
  const slide = pptx.addSlide();
  addSlideTitle(slide, spec);
  const stats = (spec.stats || []).filter((s) => s && (s.value || s.label)).slice(0, 4);
  if (!stats.length) return renderStatCardsSlide(pptx, spec);
  const hero = stats[0];
  slide.addShape("roundRect", {
    x: 0.72,
    y: 1.55,
    w: 4.15,
    h: 4.65,
    fill: { color: NAVY },
    line: { color: NAVY },
    rectRadius: 0.08,
  });
  slide.addText("Investment case", {
    x: 1.02,
    y: 1.92,
    w: 2.8,
    h: 0.28,
    fontSize: 11,
    bold: true,
    color: GOLD,
    fontFace: BODY_FACE,
    charSpacing: 1.2,
  });
  slide.addText(hero.value || "", {
    x: 1.02,
    y: 2.55,
    w: 3.35,
    h: 0.82,
    fontSize: 32,
    bold: true,
    color: WHITE,
    fontFace: HEADING_FACE,
    fit: "shrink",
  });
  slide.addText(hero.label || "", {
    x: 1.04,
    y: 3.55,
    w: 3.35,
    h: 0.75,
    fontSize: 13,
    color: "D5ECEA",
    fontFace: BODY_FACE,
    fit: "shrink",
    wrap: true,
  });
  const remaining = stats.slice(1, 4);
  remaining.forEach((stat, idx) => {
    const y = 1.62 + idx * 1.36;
    slide.addShape("roundRect", {
      x: 5.2,
      y,
      w: 7.1,
      h: 1.08,
      fill: { color: CARD_BG },
      line: { color: ACCENT_LINE },
      rectRadius: 0.05,
    });
    addIconBadge(slide, 5.44, y + 0.28, "$", { fill: GOLD, color: NAVY, w: 0.42, h: 0.42, fontSize: 12 });
    slide.addText(stat.value || "", {
      x: 6.08,
      y: y + 0.18,
      w: 1.45,
      h: 0.48,
      fontSize: 18,
      bold: true,
      color: NAVY,
      fontFace: HEADING_FACE,
      fit: "shrink",
    });
    slide.addText(stat.label || "", {
      x: 7.65,
      y: y + 0.22,
      w: 4.1,
      h: 0.42,
      fontSize: 11.5,
      color: TEXT_DARK,
      fontFace: BODY_FACE,
      fit: "shrink",
      wrap: true,
    });
  });
  const callout = spec.callout;
  if (callout && callout.text) {
    slide.addText(`${callout.label || "Decision signal"}: ${callout.text}`, {
      x: 5.2,
      y: 5.7,
      w: 6.9,
      h: 0.42,
      fontSize: 11,
      bold: true,
      color: TEXT_DARK,
      fontFace: BODY_FACE,
      fit: "shrink",
      wrap: true,
    });
  }
  addFooter(slide);
  addNotes(slide, spec.notes);
  return slide;
}

function renderArchitectureSlide(pptx, spec) {
  const slide = pptx.addSlide();
  addSlideTitle(slide, spec);
  const bullets = spec.bullets && spec.bullets.length ? spec.bullets : [""];
  const nodes = bullets.slice(0, 5).map((b) => bulletText(typeof b === "object" ? b.text : b, 48)).filter(Boolean);
  const diagramX = 0.72;
  const diagramY = 1.62;
  const diagramW = 7.0;
  const diagramH = 4.85;
  slide.addShape("roundRect", {
    x: diagramX,
    y: diagramY,
    w: diagramW,
    h: diagramH,
    fill: { color: CARD_BG },
    line: { color: ACCENT_LINE },
    rectRadius: 0.06,
  });
  slide.addText("Target flow", {
    x: diagramX + 0.25,
    y: diagramY + 0.18,
    w: 2.0,
    h: 0.25,
    fontSize: 11,
    bold: true,
    color: TEXT_MUTED,
    fontFace: BODY_FACE,
    charSpacing: 1,
  });
  const coordinates = [
    [diagramX + 0.42, diagramY + 1.0],
    [diagramX + 3.0, diagramY + 1.0],
    [diagramX + 5.5, diagramY + 1.0],
    [diagramX + 1.6, diagramY + 3.05],
    [diagramX + 4.25, diagramY + 3.05],
  ];
  nodes.forEach((node, idx) => {
    const [x, y] = coordinates[idx] || coordinates[coordinates.length - 1];
    const fill = idx === 1 || idx === 3 ? NAVY : idx === 2 ? TEAL : SOFT_BG;
    const dark = fill === NAVY || fill === TEAL;
    slide.addShape("roundRect", {
      x,
      y,
      w: 1.45,
      h: 0.82,
      fill: { color: fill },
      line: { color: dark ? fill : ACCENT_LINE },
      rectRadius: 0.05,
    });
    slide.addText(node, {
      x: x + 0.08,
      y: y + 0.12,
      w: 1.29,
      h: 0.52,
      fontSize: 8.6,
      bold: true,
      color: dark ? WHITE : TEXT_DARK,
      fontFace: BODY_FACE,
      align: "center",
      valign: "middle",
      fit: "shrink",
      wrap: true,
    });
    if (idx < nodes.length - 1) {
      const [nx, ny] = coordinates[idx + 1] || coordinates[idx];
      slide.addShape("line", {
        x: x + 1.45,
        y: y + 0.41,
        w: nx - (x + 1.45),
        h: ny + 0.41 - (y + 0.41),
        line: { color: GOLD, width: 1.5, beginArrowType: "none", endArrowType: "triangle" },
      });
    }
  });
  const panelX = 8.0;
  slide.addShape("roundRect", {
    x: panelX,
    y: 1.62,
    w: 4.45,
    h: 4.85,
    fill: { color: NAVY },
    line: { color: NAVY },
    rectRadius: 0.06,
  });
  slide.addText("Design implication", {
    x: panelX + 0.25,
    y: 1.95,
    w: 3.8,
    h: 0.35,
    fontSize: 13,
    bold: true,
    color: GOLD,
    fontFace: HEADING_FACE,
  });
  slide.addText(bulletsToTextProps(bullets.slice(0, 4).map((b) => ({ level: 0, text: bulletText(typeof b === "object" ? b.text : b, 80) })), { fontSize: 11.2, color: WHITE }), {
    x: panelX + 0.28,
    y: 2.62,
    w: 3.85,
    h: 2.95,
    valign: "top",
    fontFace: BODY_FACE,
    lineSpacingMultiple: 1.08,
  });
  addFooter(slide);
  addNotes(slide, spec.notes);
  return slide;
}

function renderSlide(pptx, spec) {
  const at = archetype(spec);
  if (!["chart", "table"].includes(spec.role)) {
    if (spec.role === "risk_heatmap") return renderRiskHeatmapSlide(pptx, spec);
    if (at === "risk_register") return renderRiskRegisterSlide(pptx, spec);
    if (at === "operating_model") return renderOperatingModelSlide(pptx, spec);
    if (at === "investment_case") return renderInvestmentCaseSlide(pptx, spec);
    if (at === "comparison_matrix") return renderComparisonMatrixSlide(pptx, spec);
  }
  switch (spec.role) {
    case "section":
      return renderSectionSlide(pptx, spec);
    case "agenda":
      return renderAgendaSlide(pptx, spec);
    case "callout":
      return renderCalloutSlide(pptx, spec);
    case "chart":
      return renderChartSlide(pptx, spec);
    case "table":
      return renderTableSlide(pptx, spec);
    case "two_content":
      return renderTwoContentSlide(pptx, spec);
    case "executive_summary":
      return renderExecutiveSummarySlide(pptx, spec);
    case "recommendation":
      return renderRecommendationSlide(pptx, spec);
    case "timeline":
      return renderTimelineSlide(pptx, spec);
    case "architecture":
      return renderArchitectureSlide(pptx, spec);
    case "stat_cards":
      return renderStatCardsSlide(pptx, spec);
    default:
      return renderContentSlide(pptx, spec);
  }
}

async function main() {
  const raw = await readStdin();
  let payload;
  try {
    payload = JSON.parse(raw || "{}");
  } catch (err) {
    process.stderr.write(`Invalid deck payload JSON: ${err}\n`);
    process.exit(1);
  }

  const pptx = new pptxgen();
  ACTIVE_DESIGN_SYSTEM = payload.design_system || {};
  pptx.defineLayout({ name: "FRONEI_WIDE", width: SLIDE_W, height: SLIDE_H });
  pptx.layout = "FRONEI_WIDE";

  renderTitleSlide(pptx, payload.title, payload.subtitle);

  for (const spec of payload.slides || []) {
    renderSlide(pptx, spec);
  }

  const buffer = await pptx.write({ outputType: "nodebuffer" });
  const colored = await _applyAccentBulletColors(buffer);
  process.stdout.write(colored);
}

/**
 * pptxgenjs has no API for bullet character color, so the ACCENT_BULLET
 * markers from bulletsToTextProps come out of pptx.write() as plain
 * "<a:buSzPct.../><a:buChar char="▪"/>" runs (default text color). Patch the
 * generated slide XML directly via JSZip to insert an "<a:buClr>" element
 * (OOXML requires buClr before buSzPct/buFont/buChar) so the small square
 * markers render in the accent color, matching the orange accent rules used
 * elsewhere in the deck. Sub-bullet "–" markers (SUB_BULLET) are left as-is.
 */
async function _applyAccentBulletColors(buffer) {
  const JSZip = require("jszip");
  const zip = await JSZip.loadAsync(buffer);
  const buClr = `<a:buClr><a:srgbClr val="${ACCENT}"/></a:buClr>`;
  const needle = '<a:buSzPct val="100000"/><a:buChar char="&#x25AA;"/>';
  const replacement = `${buClr}${needle}`;

  const slideFiles = Object.keys(zip.files).filter((name) => /^ppt\/slides\/slide\d+\.xml$/.test(name));
  for (const name of slideFiles) {
    const xml = await zip.file(name).async("string");
    if (xml.includes(needle)) {
      zip.file(name, xml.split(needle).join(replacement));
    }
  }
  return zip.generateAsync({ type: "nodebuffer" });
}

main().catch((err) => {
  process.stderr.write(`PPTX render failed: ${err && err.stack ? err.stack : err}\n`);
  process.exit(1);
});
