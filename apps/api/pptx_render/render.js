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

// Rough perceived luminance (0-255) of a hex color, used to decide whether
// a color should be treated as "dark" (suitable as a hero background) or
// "light" (suitable as text on a dark background) regardless of which theme
// token happens to hold it.
function luminance(hex) {
  const clean = String(hex || "").replace("#", "");
  if (clean.length !== 6) return 255;
  const r = parseInt(clean.slice(0, 2), 16);
  const g = parseInt(clean.slice(2, 4), 16);
  const b = parseInt(clean.slice(4, 6), 16);
  if ([r, g, b].some((v) => Number.isNaN(v))) return 255;
  return 0.299 * r + 0.587 * g + 0.114 * b;
}

// Returns { dark, light } hex colors picked from the theme's fg/bg tokens so
// dark-hero components (section dividers, cover strips) render correctly
// whether the theme's "fg" is a dark-on-light text color (warm-editorial) or
// a light-on-dark text color (modern-tech).
function heroTones() {
  const fg = token("fg", TEXT_DARK);
  const bg = token("bg", BG);
  return luminance(fg) <= luminance(bg) ? { dark: fg, light: bg } : { dark: bg, light: fg };
}

function headingFace() {
  return token("heading_font", HEADING_FACE);
}

function bodyFace() {
  return token("body_font", BODY_FACE);
}

function slideBg(slide) {
  slide.background = { color: token("bg", BG) };
}

function addBoardChrome(slide, sectionLabel) {
  slideBg(slide);
  const accent = token("accent", GOLD);
  const accent2 = token("accent2", TEAL);
  slide.addShape("rect", {
    x: 0,
    y: 0,
    w: 0.24,
    h: SLIDE_H,
    fill: { color: accent2 },
    line: { color: accent2 },
  });
  slide.addShape("rect", {
    x: 0.24,
    y: 0,
    w: 0.035,
    h: SLIDE_H,
    fill: { color: accent },
    line: { color: accent },
  });
  if (sectionLabel) {
    slide.addText(String(sectionLabel).toUpperCase(), {
      x: 0.42,
      y: 6.86,
      w: 4.2,
      h: 0.24,
      fontSize: 7.5,
      bold: true,
      color: token("muted", TEXT_MUTED),
      fontFace: bodyFace(),
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

function densityFor(spec) {
  const bp = blueprintFor(spec);
  const d = String(spec.density || bp.density || "medium").toLowerCase();
  return ["low", "medium", "high"].includes(d) ? d : "medium";
}

function densityFontBoost(spec) {
  const d = densityFor(spec);
  if (d === "low") return 1.16;
  if (d === "medium") return 1.06;
  return 1;
}

function contentValign(spec) {
  return densityFor(spec) === "high" ? "top" : "middle";
}

function emphasisColor(spec) {
  const bp = blueprintFor(spec);
  const themed = {
    decision: token("accent", GOLD),
    financial: token("accent", GOLD),
    risk: token("warn", EMPHASIS_COLORS.risk),
    technical: token("accent2", TEAL),
    execution: token("accent2", EMPHASIS_COLORS.execution),
    operational: token("success", TEAL),
  };
  return themed[bp.emphasis] || token("accent2", EMPHASIS_COLORS[bp.emphasis] || TEAL);
}

function emphasisInk(spec) {
  const color = emphasisColor(spec);
  return color === token("accent", GOLD) ? token("fg", NAVY) : WHITE;
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

function addTitle(slide, text, subtitle) {
  addBoardChrome(slide);
  const hasSubtitle = !!String(subtitle || "").trim();
  const ruleY = hasSubtitle ? 1.48 : TITLE_RULE_Y;
  slide.addText(bulletText(text || "Untitled", 78), {
    x: MARGIN_X,
    y: 0.42,
    w: 9.4,
    h: hasSubtitle ? 0.66 : TITLE_BOX_H,
    fontSize: titleFontSize(text),
    bold: true,
    color: token("fg", NAVY),
    fontFace: headingFace(),
    align: "left",
    valign: "top",
    fit: "shrink",
  });
  if (hasSubtitle) {
    slide.addText(bulletText(subtitle, 112), {
      x: MARGIN_X,
      y: 1.08,
      w: 9.55,
      h: 0.28,
      fontSize: 10.5,
      color: token("muted", TEXT_MUTED),
      fontFace: bodyFace(),
      align: "left",
      valign: "top",
      fit: "shrink",
    });
  }
  slide.addShape("rect", {
    x: MARGIN_X,
    y: ruleY,
    w: 1.0,
    h: 0.04,
    fill: { color: token("accent", ACCENT) },
    line: { color: token("accent", ACCENT) },
  });
}

function addSlideTitle(slide, spec) {
  addTitle(slide, spec.title, spec.subtitle || spec.dek);
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

function accentPalette(index = 0) {
  const palette = token("chart_palette", [token("accent", ACCENT), token("accent2", TEAL), token("success", TEAL), token("warn", EMPHASIS_COLORS.risk)]);
  return palette[index % palette.length] || token("accent", ACCENT);
}

function addAccentStrip(slide, x, y, w, color, opts = {}) {
  const orientation = opts.orientation || "top";
  const thickness = opts.thickness || 0.08;
  slide.addShape("rect", {
    x,
    y,
    w: orientation === "left" ? thickness : w,
    h: orientation === "left" ? (opts.h || 1) : thickness,
    fill: { color },
    line: { color },
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
  const { dark: heroBg, light: heroFg } = heroTones();
  const accent = token("accent", ACCENT);
  const accent2 = token("accent2", TEAL);
  const muted = token("muted", TEXT_MUTED);
  slide.background = { color: heroBg };
  slide.addShape("rect", {
    x: 0,
    y: 0,
    w: 4.25,
    h: SLIDE_H,
    fill: { color: accent2 },
    line: { color: accent2 },
  });
  slide.addShape("rect", {
    x: 4.25,
    y: 0,
    w: 0.06,
    h: SLIDE_H,
    fill: { color: accent },
    line: { color: accent },
  });

  if (spec.section_number) {
    const label = `${String(spec.section_number).padStart(2, "0")}`;
    slide.addShape("rect", {
      x: 1.1,
      y: 2.25,
      w: 1.35,
      h: 0.05,
      fill: { color: accent },
      line: { color: accent },
    });
    slide.addText(label, {
      x: 1.1,
      y: 2.48,
      w: 2.1,
      h: 0.9,
      fontSize: 34,
      bold: true,
      color: heroBg,
      fontFace: headingFace(),
      align: "left",
      valign: "middle",
      fit: "shrink",
    });
  }

  slide.addText(bulletText(spec.title || "Untitled", 70), {
    x: 4.85,
    y: 2.35,
    w: 7.4,
    h: 1.8,
    fontSize: 36,
    bold: true,
    color: heroFg,
    fontFace: headingFace(),
    align: "left",
    valign: "middle",
    fit: "shrink",
  });
  slide.addText("SECTION", {
    x: 4.9,
    y: 4.5,
    w: 2.0,
    h: 0.26,
    fontSize: 9,
    bold: true,
    color: muted,
    fontFace: bodyFace(),
    charSpacing: 2,
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
  const boost = densityFontBoost(spec);
  const headingFontSize = (n >= 3 ? 14 : 15) * boost;
  const bulletFontSize = (n >= 3 ? 11 : 12) * boost;
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
    const stripColor = idx % 2 === 0 ? accent : accentPalette(idx);
    addAccentStrip(slide, left, top, colW, stripColor);
    addIconBadge(slide, left + 0.2, top + 0.28, icon, { fill: stripColor, color: idx % 2 === 0 ? accentInk : NAVY, w: 0.46, h: 0.46, fontSize: 14 });
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
      valign: contentValign(spec),
      fontFace: bodyFace(),
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
  const riskColor = token("warn", EMPHASIS_COLORS.risk);
  const fg = token("fg", NAVY);
  const card = token("card", CARD_BG);
  const cardLine = token("card_line", ACCENT_LINE);
  const { dark: heroDark, light: heroLight } = heroTones();
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
      fill: { color: idx === 0 ? heroDark : card },
      line: { color: idx === 0 ? heroDark : cardLine },
      rectRadius: 0.05,
    });
    addAccentStrip(slide, 0.72, y, 11.65, riskColor, { orientation: "left", h: rowH });
    addIconBadge(slide, 0.95, y + 0.34, "!", { fill: riskColor, color: WHITE, w: 0.42, h: 0.42, fontSize: 13 });
    slide.addText(bulletText(row.risk, 48), {
      x: 1.48,
      y: y + 0.26,
      w: 2.05,
      h: 0.72,
      fontSize: 12,
      bold: true,
      color: idx === 0 ? heroLight : fg,
      fontFace: bodyFace(),
      fit: "shrink",
      wrap: true,
    });
    slide.addText(bulletText(row.signal, 90), {
      x: 3.95,
      y: y + 0.24,
      w: 2.9,
      h: 0.76,
      fontSize: 11,
      color: idx === 0 ? heroLight : token("fg", TEXT_DARK),
      fontFace: bodyFace(),
      fit: "shrink",
      wrap: true,
    });
    slide.addText(bulletText(row.mitigation, 105), {
      x: 7.2,
      y: y + 0.24,
      w: 4.8,
      h: 0.76,
      fontSize: 11,
      color: idx === 0 ? heroLight : token("fg", TEXT_DARK),
      fontFace: bodyFace(),
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
    fontFace: headingFace(),
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
    low: token("success", "D7EFE7"),
    medium: token("accent", "F6DF9D"),
    high: token("warn", "E89186"),
  };
  const order = ["low", "medium", "high"];
  slide.addText("Impact", {
    x: 0.4,
    y: gridY + 1.05,
    w: 0.32,
    h: 0.3,
    fontSize: 8,
    bold: true,
    color: token("muted", TEXT_MUTED),
    fontFace: bodyFace(),
    rotate: 270,
  });
  slide.addText("Likelihood", {
    x: gridX + 1.05,
    y: gridY + cell * 3 + 0.18,
    w: 1.6,
    h: 0.2,
    fontSize: 8,
    bold: true,
    color: token("muted", TEXT_MUTED),
    fontFace: bodyFace(),
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
        color: token("muted", TEXT_MUTED),
        fontFace: bodyFace(),
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
      fill: { color: token("fg", NAVY) },
      line: { color: token("accent", GOLD), width: 1 },
    });
    slide.addText(String(idx + 1), {
      x,
      y: y + 0.02,
      w: 0.32,
      h: 0.22,
      fontSize: 8,
      bold: true,
      color: WHITE,
      fontFace: bodyFace(),
      align: "center",
    });
  });
  slide.addShape("roundRect", {
    x: 5.05,
    y: 1.65,
    w: 7.15,
    h: 4.45,
    fill: { color: token("card", CARD_BG) },
    line: { color: token("card_line", ACCENT_LINE) },
    rectRadius: 0.06,
  });
  addAccentStrip(slide, 5.05, 1.65, 7.15, token("warn", EMPHASIS_COLORS.risk), { orientation: "left", h: 4.45 });
  slide.addText("Risk register", {
    x: 5.32,
    y: 1.92,
    w: 2.4,
    h: 0.28,
    fontSize: 13,
    bold: true,
    color: token("warn", EMPHASIS_COLORS.risk),
    fontFace: headingFace(),
  });
  items.forEach((item, idx) => {
    const y = 2.45 + idx * 0.68;
    addIconBadge(slide, 5.32, y, String(idx + 1), { fill: token("warn", EMPHASIS_COLORS.risk), color: WHITE, w: 0.32, h: 0.32, fontSize: 9 });
    slide.addText(`${bulletText(item.label, 44)} · ${item.likelihood}/${item.impact}`, {
      x: 5.78,
      y: y - 0.03,
      w: 5.85,
      h: 0.26,
      fontSize: 10.5,
      bold: true,
      color: token("fg", NAVY),
      fontFace: bodyFace(),
      fit: "shrink",
    });
    if (item.mitigation) {
      slide.addText(bulletText(item.mitigation, 78), {
        x: 5.78,
        y: y + 0.25,
        w: 5.85,
        h: 0.22,
        fontSize: 8.5,
        color: token("muted", TEXT_MUTED),
        fontFace: bodyFace(),
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
  const fontBoost = densityFontBoost(spec);
  const fg = token("fg", NAVY);
  const card = token("card", CARD_BG);
  const cardLine = token("card_line", ACCENT_LINE);
  const accent = token("accent", GOLD);
  const accent2 = token("accent2", TEAL);
  const { dark: heroDark, light: heroLight } = heroTones();
  lanes.slice(0, 4).forEach((lane, idx) => {
    const y = top + idx * (laneH + 0.22);
    const dark = idx === 0;
    slide.addShape("roundRect", {
      x: 0.72,
      y,
      w: 11.65,
      h: laneH,
      fill: { color: dark ? heroDark : card },
      line: { color: dark ? heroDark : cardLine },
      rectRadius: 0.05,
    });
    addAccentStrip(slide, 0.72, y, 11.65, accentPalette(idx), { orientation: "left", h: laneH });
    addIconBadge(slide, 0.96, y + 0.25, "✓", { fill: dark ? accent : accent2, color: dark ? NAVY : WHITE, w: 0.4, h: 0.4, fontSize: 12 });
    slide.addText(bulletText(lane.heading || `Owner ${idx + 1}`, 42), {
      x: 1.55,
      y: y + 0.2,
      w: 2.55,
      h: 0.5,
      fontSize: 13 * fontBoost,
      bold: true,
      color: dark ? heroLight : fg,
      fontFace: headingFace(),
      fit: "shrink",
    });
    slide.addText((lane.bullets || []).slice(0, 2).map((b) => bulletText(b, 78)).join("  |  "), {
      x: 4.28,
      y: y + 0.22,
      w: 7.62,
      h: 0.48,
      fontSize: 11 * fontBoost,
      color: dark ? heroLight : token("fg", TEXT_DARK),
      fontFace: bodyFace(),
      fit: "shrink",
      wrap: true,
      valign: contentValign(spec),
    });
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
  const boost = densityFontBoost(spec);
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
      fontSize: 15 * boost,
      bold: true,
      color: recommended ? WHITE : NAVY,
      fontFace: HEADING_FACE,
      fit: "shrink",
      wrap: true,
    });
    slide.addText(bulletsToTextProps((col.bullets || []).slice(0, 4).map((b) => ({ level: 0, text: bulletText(b, 76) })), { fontSize: 10.6 * boost, color: recommended ? WHITE : TEXT_DARK }), {
      x: x + 0.24,
      y: top + 1.55,
      w: colW - 0.48,
      h: 2.25,
      fontFace: bodyFace(),
      fit: "shrink",
      wrap: true,
      valign: contentValign(spec),
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

  // De-duplicate series names: when two series truncate to the same legend
  // text, distinguish them so the legend doesn't show identical labels.
  const seenNames = new Map();
  const chartData = series.map((s, idx) => {
    let name = s.name || `Series ${idx + 1}`;
    if (seenNames.has(name)) {
      const count = seenNames.get(name) + 1;
      seenNames.set(name, count);
      name = `${name} (${count})`;
    } else {
      seenNames.set(name, 1);
    }
    return {
      name,
      labels: categories,
      values: s.values || [],
    };
  });

  const options = {
    x: 1.0,
    y: 1.6,
    w: 11.3,
    h: 5.3,
    showLegend: series.length > 1 || chartType === "pie",
    legendPos: "b",
    showTitle: false,
    showValue: false,
    showCatName: false,
    showSerName: false,
    chartColors: token("chart_palette", [ACCENT, NAVY, "8C6F5D", "C9A14A"]),
    catAxisLabelFontSize: 11,
    valAxisLabelFontSize: 11,
    dataLabelFontSize: 10,
  };
  if (chartType === "pie") {
    options.dataBorder = { pt: 1, color: WHITE };
  }
  // Reduce val-axis gridline density so tick labels don't overlap when the
  // data range is small (e.g. all values between 0 and 2).
  if (chartType !== "pie") {
    const allValues = chartData.flatMap((s) => (s.values || []).map(Number).filter((v) => !Number.isNaN(v)));
    if (allValues.length) {
      const maxAbs = Math.max(...allValues.map((v) => Math.abs(v)), 0);
      if (maxAbs > 0) {
        const rawStep = maxAbs / 4;
        const magnitude = Math.pow(10, Math.floor(Math.log10(rawStep)));
        const niceStep = Math.ceil(rawStep / magnitude) * magnitude;
        options.valAxisMajorUnit = niceStep;
      }
    }
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
  const boost = densityFontBoost(spec);
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
  // Lower section: fill the area between the recommendation banner (ends
  // ~3.0) and the footer (~6.88) with rationale cards. When there are fewer
  // than 3 rationale bullets, pad the row with stat cards (if available) so
  // the slide doesn't end with a large empty bottom area.
  const cardTop = 3.25;
  const cardH = 3.35;
  const gap = 0.22;
  const cardW = (SLIDE_W - 2 * 0.72 - 2 * gap) / 3;
  const stats = (spec.stats || []).slice(0, 3);
  const cards = rationale.slice(0, 3);
  const extraSlots = Math.max(0, 3 - cards.length);
  const statCards = stats.slice(0, extraSlots);

  cards.forEach((b, idx) => {
    const x = 0.72 + idx * (cardW + gap);
    slide.addShape("roundRect", {
      x,
      y: cardTop,
      w: cardW,
      h: cardH,
      fill: { color: CARD_BG },
      line: { color: ACCENT_LINE },
      rectRadius: 0.06,
    });
    addAccentStrip(slide, x, cardTop, cardW, accentPalette(idx));
    addIconBadge(slide, x + 0.22, cardTop + 0.32, iconFor(b), { fill: TEAL, color: WHITE, w: 0.46, h: 0.46, fontSize: 13 });
    slide.addText(bulletText(b, 140), {
      x: x + 0.22,
      y: cardTop + 1.0,
      w: cardW - 0.44,
      h: cardH - 1.2,
      fontSize: 12.5 * boost,
      color: TEXT_DARK,
      fontFace: bodyFace(),
      valign: "top",
      fit: "shrink",
      wrap: true,
    });
  });

  statCards.forEach((stat, idx) => {
    const col = cards.length + idx;
    const x = 0.72 + col * (cardW + gap);
    slide.addShape("roundRect", {
      x,
      y: cardTop,
      w: cardW,
      h: cardH,
      fill: { color: CARD_BG },
      line: { color: ACCENT_LINE },
      rectRadius: 0.06,
    });
    addAccentStrip(slide, x, cardTop, cardW, accentPalette(col));
    slide.addText(stat.value || "", {
      x: x + 0.22,
      y: cardTop + 0.32,
      w: cardW - 0.44,
      h: 0.7,
      fontSize: 28,
      bold: true,
      color: NAVY,
      fontFace: headingFace(),
      fit: "shrink",
    });
    slide.addText(stat.label || "", {
      x: x + 0.22,
      y: cardTop + 1.1,
      w: cardW - 0.44,
      h: cardH - 1.3,
      fontSize: 12 * boost,
      color: TEXT_MUTED,
      fontFace: bodyFace(),
      valign: "top",
      fit: "shrink",
      wrap: true,
    });
  });

  // If neither rationale bullets nor stats are available, fall back to a
  // full-width callout so the bottom of the slide isn't left empty.
  if (!cards.length && !statCards.length) {
    const callout = spec.callout && spec.callout.text;
    if (callout) {
      slide.addShape("roundRect", {
        x: 0.72,
        y: cardTop,
        w: SLIDE_W - 2 * 0.72,
        h: cardH,
        fill: { color: CARD_BG },
        line: { color: ACCENT_LINE },
        rectRadius: 0.06,
      });
      addAccentStrip(slide, 0.72, cardTop, SLIDE_W - 2 * 0.72, accentPalette(0));
      if (spec.callout.label) {
        slide.addText(String(spec.callout.label).toUpperCase(), {
          x: 1.0,
          y: cardTop + 0.28,
          w: SLIDE_W - 2 * 1.0,
          h: 0.3,
          fontSize: 11,
          bold: true,
          color: token("accent", ACCENT),
          fontFace: bodyFace(),
          charSpacing: 1.4,
        });
      }
      slide.addText(callout, {
        x: 1.0,
        y: cardTop + 0.7,
        w: SLIDE_W - 2 * 1.0,
        h: cardH - 1.0,
        fontSize: 16 * boost,
        color: TEXT_DARK,
        fontFace: bodyFace(),
        valign: "top",
        fit: "shrink",
        wrap: true,
      });
    }
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
  const boost = densityFontBoost(spec);

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
    addAccentStrip(slide, left, top + 0.72 + 3.43, boxW, accentPalette(idx), { thickness: 0.09 });
    const lines = [];
    if (ph.label) lines.push(ph.label);
    if (ph.title) lines.push(ph.title);
    if (ph.description) lines.push(bulletText(ph.description, 82));
    slide.addText(lines.join("\n"), {
      x: left + 0.16,
      y: top + 0.95,
      w: boxW - 0.32,
      h: 2.95,
      valign: contentValign(spec),
      fontFace: bodyFace(),
      fontSize: 10.4 * boost,
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

function renderCoverMetricStripSlide(pptx, spec) {
  const slide = pptx.addSlide();
  const bg = token("bg", "0B1220");
  const card = token("card", "172033");
  const accent = token("accent", "38BDF8");
  const accent2 = token("accent2", "34D399");
  slide.background = { color: bg };
  addAccentStrip(slide, 0, 0, SLIDE_W, accent, { thickness: 0.13 });
  slide.addShape("rect", { x: 8.8, y: 0.13, w: 4.53, h: 7.37, fill: { color: "10233D" }, line: { color: "10233D" } });
  slide.addText("STEERING COMMITTEE", { x: 0.62, y: 0.35, w: 4.8, h: 0.24, fontSize: 9.5, bold: true, color: accent, fontFace: bodyFace(), charSpacing: 1.6 });
  slide.addText(bulletText(spec.title || "Decision pack", 82), { x: 0.62, y: 1.05, w: 7.4, h: 0.75, fontSize: 34, bold: true, color: WHITE, fontFace: headingFace(), fit: "shrink" });
  if (spec.subtitle) {
    slide.addText(spec.subtitle, { x: 0.62, y: 1.92, w: 7.0, h: 0.38, fontSize: 16, color: token("muted", "CBD5E1"), fontFace: bodyFace(), fit: "shrink" });
  }
  const decision = (spec.bullets || [])[0] || (spec.callout && spec.callout.text) || "";
  if (decision) {
    slide.addText(bulletText(decision, 120), { x: 0.62, y: 2.78, w: 6.9, h: 0.78, fontSize: 18, color: WHITE, fontFace: bodyFace(), fit: "shrink", wrap: true });
  }
  const stats = (spec.stats || []).slice(0, 3);
  stats.forEach((stat, idx) => {
    const colors = [accent, token("accent", "FBBF24"), accent2];
    const x = 0.62 + idx * 2.58;
    slide.addShape("roundRect", { x, y: 4.22, w: 2.35, h: 1.35, fill: { color: card }, line: { color: card }, rectRadius: 0.04 });
    addAccentStrip(slide, x, 4.22, 2.35, colors[idx], { orientation: "left", h: 1.35, thickness: 0.06 });
    slide.addText(stat.value || "", { x: x + 0.22, y: 4.45, w: 2.0, h: 0.34, fontSize: 25, bold: true, color: WHITE, fontFace: headingFace(), fit: "shrink" });
    slide.addText(stat.label || "", { x: x + 0.22, y: 4.9, w: 1.98, h: 0.42, fontSize: 8.8, color: token("muted", "CBD5E1"), fontFace: bodyFace(), fit: "shrink", wrap: true });
  });
  slide.addText("Prepared with Fronei", { x: 0.62, y: 6.35, w: 3.4, h: 0.2, fontSize: 8.5, color: token("muted", "CBD5E1"), fontFace: bodyFace() });
  addNotes(slide, spec.notes);
  return slide;
}

function renderCurrentStateEstateMapSlide(pptx, spec) {
  const slide = pptx.addSlide();
  addSlideTitle(slide, spec);
  const units = (spec.units || []).slice(0, 4);
  const top = 2.0;
  const cardW = 2.55;
  const gap = 0.45;
  units.forEach((unit, idx) => {
    const x = 0.62 + idx * (cardW + gap);
    slide.addShape("roundRect", { x, y: top, w: cardW, h: 3.75, fill: { color: token("card", CARD_BG) }, line: { color: token("card_line", ACCENT_LINE) }, rectRadius: 0.05 });
    addAccentStrip(slide, x, top, cardW, accentPalette(idx));
    slide.addText(unit.name || `BU ${idx + 1}`, { x: x + 0.18, y: top + 0.18, w: 2.1, h: 0.24, fontSize: 13, bold: true, color: token("fg", NAVY), fontFace: headingFace(), fit: "shrink" });
    (unit.tools || []).slice(0, 3).forEach((tool, tIdx) => {
      const y = top + 0.78 + tIdx * 0.62;
      slide.addShape("roundRect", { x: x + 0.25, y, w: 2.05, h: 0.43, fill: { color: "1E2A3F" }, line: { color: "1E2A3F" }, rectRadius: 0.03 });
      slide.addText(tool, { x: x + 0.42, y: y + 0.12, w: 1.7, h: 0.16, fontSize: 8.8, color: WHITE, fontFace: bodyFace(), fit: "shrink" });
    });
    slide.addText(unit.note || "local security | review", { x: x + 0.35, y: top + 3.04, w: 1.8, h: 0.35, fontSize: 8.5, color: token("muted", TEXT_MUTED), fontFace: bodyFace(), fit: "shrink", align: "center" });
  });
  const bullets = slideBullets(spec, 3);
  if (bullets.length) {
    slide.addText("What this creates", { x: 0.62, y: 6.05, w: 2.3, h: 0.25, fontSize: 10.5, bold: true, color: token("fg", NAVY), fontFace: headingFace() });
    bullets.forEach((b, idx) => {
      const x = 3.25 + idx * 3.05;
      addAccentStrip(slide, x - 0.18, 6.13, 0.09, accentPalette(idx), { thickness: 0.09 });
      slide.addText(b, { x, y: 6.05, w: 2.7, h: 0.34, fontSize: 9.2, color: token("fg", TEXT_DARK), fontFace: bodyFace(), fit: "shrink", wrap: true });
    });
  }
  addFooter(slide);
  addNotes(slide, spec.notes);
  return slide;
}

function renderImpactScorecardBarsSlide(pptx, spec) {
  const slide = pptx.addSlide();
  addSlideTitle(slide, spec);
  const stats = (spec.stats || []).slice(0, 4);
  stats.forEach((stat, idx) => {
    const x = 0.62 + idx * 2.93;
    slide.addShape("roundRect", { x, y: 1.95, w: 2.65, h: 1.35, fill: { color: token("card", CARD_BG) }, line: { color: token("card_line", ACCENT_LINE) }, rectRadius: 0.04 });
    addAccentStrip(slide, x, 1.95, 2.65, accentPalette(idx), { orientation: "left", h: 1.35, thickness: 0.06 });
    slide.addText(stat.value || "", { x: x + 0.22, y: 2.18, w: 2.2, h: 0.34, fontSize: 25, bold: true, color: token("fg", NAVY), fontFace: headingFace(), fit: "shrink" });
    slide.addText(stat.label || "", { x: x + 0.22, y: 2.58, w: 2.25, h: 0.38, fontSize: 8.8, color: token("muted", TEXT_MUTED), fontFace: bodyFace(), fit: "shrink", wrap: true });
  });
  const bars = (spec.bars || []).length ? spec.bars : stats.slice(0, 3).map((s, idx) => ({ label: s.label, display: s.value, value: idx === 0 ? 100 : idx === 1 ? 68 : 28 }));
  const maxValue = Math.max(...bars.map(b => Number(b.value || 0)), 1);
  slide.addText("Cost exposure vs investment", { x: 0.62, y: 4.0, w: 4.2, h: 0.25, fontSize: 11, bold: true, color: token("fg", NAVY), fontFace: headingFace() });
  bars.slice(0, 4).forEach((bar, idx) => {
    const y = 4.47 + idx * 0.5;
    const color = bar.color || accentPalette(idx);
    slide.addText(bar.display || bar.label || "", { x: 0.62, y: y + 0.02, w: 1.3, h: 0.18, fontSize: 8.2, color: token("muted", TEXT_MUTED), fontFace: bodyFace(), fit: "shrink" });
    slide.addShape("rect", { x: 2.12, y, w: 8.6 * (Number(bar.value || 0) / maxValue), h: 0.22, fill: { color }, line: { color } });
  });
  addFooter(slide);
  addNotes(slide, spec.notes);
  return slide;
}

function renderOptionScoreMatrixSlide(pptx, spec) {
  const slide = pptx.addSlide();
  addSlideTitle(slide, spec);
  const options = (spec.options && spec.options.length ? spec.options : (spec.columns || []).map((c, idx) => ({ name: c.heading || `Option ${idx + 1}`, summary: (c.bullets || []).join(" "), scores: {} }))).slice(0, 3);
  const top = 1.9;
  options.forEach((opt, idx) => {
    const x = 0.62 + idx * 4.03;
    const recommended = opt.recommended || idx === options.length - 1;
    slide.addShape("roundRect", { x, y: top, w: 3.35, h: 3.78, fill: { color: token("card", CARD_BG) }, line: { color: token("card_line", ACCENT_LINE) }, rectRadius: 0.05 });
    addAccentStrip(slide, x, top, 3.35, recommended ? token("success", accentPalette(idx)) : accentPalette(idx), { orientation: "left", h: 3.78, thickness: 0.06 });
    slide.addText(opt.name || `Option ${idx + 1}`, { x: x + 0.2, y: top + 0.18, w: 2.95, h: 0.3, fontSize: 12.5, bold: true, color: token("fg", NAVY), fontFace: headingFace(), fit: "shrink" });
    slide.addText(opt.summary || (opt.bullets || []).join(" "), { x: x + 0.2, y: top + 0.72, w: 2.9, h: 1.35, fontSize: 10.2, color: token("fg", TEXT_DARK), fontFace: bodyFace(), fit: "shrink", wrap: true });
    const scores = opt.scores || {};
    ["Cost", "Control", "Adoption"].forEach((label, sIdx) => {
      const score = scores[label.toLowerCase()] || scores[label] || (recommended ? 3 : idx === 1 ? 2 : 1);
      slide.addText(label, { x: x + 0.25, y: top + 2.62 + sIdx * 0.32, w: 0.75, h: 0.15, fontSize: 7.5, color: token("muted", TEXT_MUTED), fontFace: bodyFace(), fit: "shrink" });
      for (let d = 0; d < 3; d++) {
        slide.addShape("ellipse", { x: x + 1.1 + d * 0.22, y: top + 2.62 + sIdx * 0.32, w: 0.11, h: 0.11, fill: { color: d < score ? accentPalette(idx) : "334155" }, line: { color: d < score ? accentPalette(idx) : "334155" } });
      }
    });
  });
  addFooter(slide);
  addNotes(slide, spec.notes);
  return slide;
}

function renderPlatformHubSlide(pptx, spec) {
  const slide = pptx.addSlide();
  addSlideTitle(slide, spec);
  const platform = spec.platform || {};
  const domains = platform.domains || ["BU 1", "BU 2", "BU 3", "BU 4"];
  const caps = platform.capabilities || slideBullets(spec, 3);
  [["BU 1", 0.62, 1.95], ["BU 2", 0.62, 4.0], ["BU 3", 9.85, 1.95], ["BU 4", 9.85, 4.0]].forEach((fallback, idx) => {
    const [label, x, y] = fallback;
    slide.addShape("roundRect", { x, y, w: 2.35, h: 0.82, fill: { color: token("card", CARD_BG) }, line: { color: token("card_line", ACCENT_LINE) }, rectRadius: 0.05 });
    slide.addText(domains[idx] || label, { x: x + 0.18, y: y + 0.24, w: 1.95, h: 0.2, fontSize: 11.5, bold: true, color: token("fg", NAVY), fontFace: headingFace(), fit: "shrink", align: "center" });
  });
  slide.addShape("roundRect", { x: 4.35, y: 2.35, w: 4.45, h: 1.45, fill: { color: token("accent", ACCENT) }, line: { color: token("accent", ACCENT) }, rectRadius: 0.08 });
  slide.addText(platform.name || "Enterprise AI Platform", { x: 4.65, y: 2.7, w: 3.85, h: 0.3, fontSize: 17, bold: true, color: WHITE, fontFace: headingFace(), fit: "shrink", align: "center" });
  slide.addText(platform.subtitle || "shared controls | shared infra | reusable patterns", { x: 4.75, y: 3.15, w: 3.65, h: 0.2, fontSize: 8.6, color: WHITE, fontFace: bodyFace(), fit: "shrink", align: "center" });
  caps.slice(0, 3).forEach((cap, idx) => {
    const x = 0.62 + idx * 3.45;
    slide.addShape("roundRect", { x, y: 5.5, w: 3.2, h: 0.8, fill: { color: token("card", CARD_BG) }, line: { color: token("card_line", ACCENT_LINE) }, rectRadius: 0.04 });
    addAccentStrip(slide, x, 5.5, 3.2, accentPalette(idx), { orientation: "left", h: 0.8, thickness: 0.06 });
    const text = typeof cap === "string" ? cap : cap.label || cap.name || "";
    slide.addText(text, { x: x + 0.2, y: 5.72, w: 2.8, h: 0.36, fontSize: 10.2, bold: true, color: token("fg", NAVY), fontFace: bodyFace(), fit: "shrink", wrap: true });
  });
  addFooter(slide);
  addNotes(slide, spec.notes);
  return slide;
}

function renderRiskControlRowsSlide(pptx, spec) {
  const slide = pptx.addSlide();
  addSlideTitle(slide, spec);
  const rows = (spec.columns || []).slice(0, 4).map((c, idx) => ({ risk: c.heading || `Risk ${idx + 1}`, mitigation: (c.bullets || [])[0] || c.mitigation || "" }));
  rows.forEach((row, idx) => {
    const y = 1.9 + idx * 1.12;
    slide.addShape("roundRect", { x: 0.62, y, w: 11.95, h: 0.86, fill: { color: token("card", CARD_BG) }, line: { color: token("card_line", ACCENT_LINE) }, rectRadius: 0.04 });
    addAccentStrip(slide, 0.62, y, 11.95, accentPalette(idx), { orientation: "left", h: 0.86, thickness: 0.06 });
    slide.addText(row.risk, { x: 0.87, y: y + 0.18, w: 2.8, h: 0.22, fontSize: 11.5, bold: true, color: token("fg", NAVY), fontFace: headingFace(), fit: "shrink" });
    slide.addText(row.mitigation, { x: 3.87, y: y + 0.17, w: 8.1, h: 0.28, fontSize: 10.5, color: token("fg", TEXT_DARK), fontFace: bodyFace(), fit: "shrink", wrap: true });
  });
  addFooter(slide);
  addNotes(slide, spec.notes);
  return slide;
}

function renderDecisionAskPanelSlide(pptx, spec) {
  const slide = pptx.addSlide();
  addSlideTitle(slide, spec);
  const stats = (spec.stats || []).slice(0, 1);
  const hero = stats[0] || { value: "$1.2M", label: "Phase 1 budget ask" };
  slide.addShape("roundRect", { x: 0.62, y: 2.0, w: 4.0, h: 3.9, fill: { color: token("accent", ACCENT) }, line: { color: token("accent", ACCENT) }, rectRadius: 0.07 });
  slide.addText(hero.value || "", { x: 0.87, y: 2.45, w: 3.5, h: 0.55, fontSize: 36, bold: true, color: WHITE, fontFace: headingFace(), fit: "shrink" });
  slide.addText(hero.label || "", { x: 1.07, y: 3.25, w: 3.1, h: 0.25, fontSize: 12, color: WHITE, fontFace: bodyFace(), fit: "shrink", align: "center" });
  if (hero.source) slide.addText(hero.source, { x: 1.07, y: 3.78, w: 3.1, h: 0.95, fontSize: 11.5, color: WHITE, fontFace: bodyFace(), fit: "shrink", wrap: true });
  const decisions = (spec.decisions || []).slice(0, 4);
  decisions.forEach((d, idx) => {
    const x = 5.05 + (idx % 2) * 3.9;
    const y = 2.0 + Math.floor(idx / 2) * 1.42;
    slide.addShape("roundRect", { x, y, w: idx % 2 === 0 ? 3.45 : 3.4, h: 1.04, fill: { color: token("card", CARD_BG) }, line: { color: token("card_line", ACCENT_LINE) }, rectRadius: 0.04 });
    addAccentStrip(slide, x, y, 3.4, accentPalette(idx), { orientation: "left", h: 1.04, thickness: 0.06 });
    slide.addText(d.label || `Decision ${idx + 1}`, { x: x + 0.2, y: y + 0.18, w: 3.0, h: 0.18, fontSize: 8.6, color: token("muted", TEXT_MUTED), fontFace: bodyFace(), fit: "shrink" });
    slide.addText(d.text || "", { x: x + 0.2, y: y + 0.48, w: 3.0, h: 0.48, fontSize: 11.2, bold: true, color: token("fg", NAVY), fontFace: headingFace(), fit: "shrink", wrap: true });
  });
  const rec = (spec.bullets || [])[0] || "Recommended decision: proceed.";
  slide.addText(rec, { x: 5.05, y: 5.52, w: 5.8, h: 0.4, fontSize: 14, bold: true, color: token("success", accentPalette(1)), fontFace: headingFace(), fit: "shrink" });
  addFooter(slide);
  addNotes(slide, spec.notes);
  return slide;
}

function renderArchitectureSlide(pptx, spec) {
  const slide = pptx.addSlide();
  addSlideTitle(slide, spec);
  const fg = token("fg", NAVY);
  const card = token("card", CARD_BG);
  const cardLine = token("card_line", ACCENT_LINE);
  const accent = token("accent", GOLD);
  const accent2 = token("accent2", TEAL);
  const { dark: heroDark, light: heroLight } = heroTones();
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
    fill: { color: card },
    line: { color: cardLine },
    rectRadius: 0.06,
  });
  slide.addText("Target flow", {
    x: diagramX + 0.25,
    y: diagramY + 0.18,
    w: 2.0,
    h: 0.25,
    fontSize: 11,
    bold: true,
    color: token("muted", TEXT_MUTED),
    fontFace: bodyFace(),
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
    const fill = idx === 1 || idx === 3 ? heroDark : idx === 2 ? accent2 : token("bg", SOFT_BG);
    const dark = fill === heroDark || fill === accent2;
    slide.addShape("roundRect", {
      x,
      y,
      w: 1.45,
      h: 0.82,
      fill: { color: fill },
      line: { color: dark ? fill : cardLine },
      rectRadius: 0.05,
    });
    slide.addText(node, {
      x: x + 0.08,
      y: y + 0.12,
      w: 1.29,
      h: 0.52,
      fontSize: 8.6,
      bold: true,
      color: dark ? heroLight : token("fg", TEXT_DARK),
      fontFace: bodyFace(),
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
        line: { color: accent, width: 1.5, beginArrowType: "none", endArrowType: "triangle" },
      });
    }
  });
  const panelX = 8.0;
  slide.addShape("roundRect", {
    x: panelX,
    y: 1.62,
    w: 4.45,
    h: 4.85,
    fill: { color: heroDark },
    line: { color: heroDark },
    rectRadius: 0.06,
  });
  slide.addText("Design implication", {
    x: panelX + 0.25,
    y: 1.95,
    w: 3.8,
    h: 0.35,
    fontSize: 13,
    bold: true,
    color: accent,
    fontFace: headingFace(),
  });
  slide.addText(bulletsToTextProps(bullets.slice(0, 4).map((b) => ({ level: 0, text: bulletText(typeof b === "object" ? b.text : b, 80) })), { fontSize: 11.2, color: heroLight }), {
    x: panelX + 0.28,
    y: 2.62,
    w: 3.85,
    h: 2.95,
    valign: "top",
    fontFace: bodyFace(),
    lineSpacingMultiple: 1.08,
  });
  addFooter(slide);
  addNotes(slide, spec.notes);
  return slide;
}

function renderSlide(pptx, spec) {
  const at = archetype(spec);
  const explicitBoardRole = [
    "cover_metric_strip",
    "current_state_estate_map",
    "impact_scorecard_bars",
    "option_score_matrix",
    "platform_operating_model_hub",
    "roadmap_phase_cards",
    "risk_control_rows",
    "decision_ask_panel",
  ].includes(spec.role);
  if (!explicitBoardRole && !["chart", "table"].includes(spec.role)) {
    if (spec.role === "risk_heatmap") return renderRiskHeatmapSlide(pptx, spec);
    if (at === "risk_register") return renderRiskRegisterSlide(pptx, spec);
    if (at === "operating_model") return renderOperatingModelSlide(pptx, spec);
    if (at === "investment_case") return renderInvestmentCaseSlide(pptx, spec);
    if (at === "comparison_matrix") return renderComparisonMatrixSlide(pptx, spec);
  }
  switch (spec.role) {
    case "cover_metric_strip":
      return renderCoverMetricStripSlide(pptx, spec);
    case "current_state_estate_map":
      return renderCurrentStateEstateMapSlide(pptx, spec);
    case "impact_scorecard_bars":
      return renderImpactScorecardBarsSlide(pptx, spec);
    case "option_score_matrix":
      return renderOptionScoreMatrixSlide(pptx, spec);
    case "platform_operating_model_hub":
      return renderPlatformHubSlide(pptx, spec);
    case "roadmap_phase_cards":
      return renderTimelineSlide(pptx, spec);
    case "risk_control_rows":
      return renderRiskControlRowsSlide(pptx, spec);
    case "decision_ask_panel":
      return renderDecisionAskPanelSlide(pptx, spec);
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
  const repaired = await _fixShapeXmlForPowerPointCompat(colored);
  process.stdout.write(repaired);
}

/**
 * pptxgenjs omits <p:txBody> on shapes added without a `text` option (plain
 * decorative rectangles/dividers/bars), and omits <a:effectLst/> from
 * <p:bg><p:bgPr>. Per ECMA-376 both elements are optional in the schema, but
 * PowerPoint's own validator flags their absence as a "problem with content"
 * on first open and offers to "Repair" the file -- silently rewriting every
 * <p:sp> to add an empty txBody and every <p:bgPr> to add effectLst. That
 * repair prompt fires on 100% of generated decks (any slide with a
 * non-text decorative shape). Pre-empt it here by inserting the same empty
 * elements PowerPoint would add, so the file opens cleanly without a repair
 * dialog.
 */
async function _fixShapeXmlForPowerPointCompat(buffer) {
  const JSZip = require("jszip");
  const zip = await JSZip.loadAsync(buffer);
  const EMPTY_TX_BODY =
    '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr lang="en-US"/></a:p></p:txBody>';

  const slideFiles = Object.keys(zip.files).filter((name) => /^ppt\/slides\/slide\d+\.xml$/.test(name));
  for (const name of slideFiles) {
    let xml = await zip.file(name).async("string");

    // Insert an empty txBody on any <p:sp> that has none (i.e. ends
    // </p:spPr></p:sp> with nothing in between).
    xml = xml.split("</p:spPr></p:sp>").join(`</p:spPr>${EMPTY_TX_BODY}</p:sp>`);

    // Insert <a:effectLst/> into any <p:bgPr> that doesn't already have one.
    xml = xml.replace(/<p:bgPr>([\s\S]*?)<\/p:bgPr>/g, (match, inner) =>
      inner.includes("effectLst") ? match : `<p:bgPr>${inner}<a:effectLst/></p:bgPr>`
    );

    zip.file(name, xml);
  }
  return zip.generateAsync({ type: "nodebuffer" });
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
