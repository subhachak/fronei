#!/usr/bin/env node
/**
 * Fronei PPTX renderer (PptxGenJS).
 *
 * Reads a normalized "deck payload" JSON object from stdin and writes a
 * .pptx file to stdout as raw bytes.
 *
 * Payload shape (produced by document_generator._build_js_deck_payload):
 * {
 *   "title": "...",
 *   "subtitle": "..." | null,
 *   "slides": [
 *     { "role": "section", "title": "...", "notes": "..."|null },
 *     { "role": "content", "title": "...", "bullets": [{"level":0,"text":"..."}], "notes": ... },
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

const NAVY = "383838";
const NAVY_LIGHT = "E04F00";
const SLATE = "73665F";
const TEXT_DARK = "282421";
const TEXT_MUTED = "6F655F";
const WHITE = "FFFFFF";
const BG = "F7F1EE";
const CARD_BG = "FFFDFC";
const SOFT_BG = "EFE7E2";
const ACCENT = "E04F00";
const ACCENT_LINE = "D8CDC6";
const HEADING_FACE = "Georgia";
const BODY_FACE = "Segoe UI";

const SLIDE_W = 13.333;
const SLIDE_H = 7.5;
const MARGIN_X = 0.65;

const MAX_BULLETS_PER_SLIDE = 6;
const MAX_APPENDIX_BULLETS = 10;

function slideBg(slide) {
  slide.background = { color: BG };
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
  slideBg(slide);
  slide.addText(bulletText(text || "Untitled", 78), {
    x: MARGIN_X,
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
    x: MARGIN_X,
    y: TITLE_RULE_Y,
    w: 1.0,
    h: 0.04,
    fill: { color: ACCENT },
    line: { color: ACCENT },
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
  slideBg(slide);
  slide.addShape("rect", {
    x: 0,
    y: 0,
    w: 0.18,
    h: SLIDE_H,
    fill: { color: ACCENT },
    line: { color: ACCENT },
  });
  slide.addText(bulletText(title || "Fronei deck", 88), {
    x: MARGIN_X,
    y: 1.7,
    w: 9.5,
    h: 1.7,
    fontSize: 34,
    bold: true,
    color: NAVY,
    fontFace: HEADING_FACE,
    align: "left",
    valign: "middle",
    fit: "shrink",
  });
  if (subtitle) {
    slide.addText(subtitle, {
      x: MARGIN_X,
      y: 3.55,
      w: 8.7,
      h: 0.8,
      fontSize: 18,
      color: TEXT_MUTED,
      fontFace: BODY_FACE,
      align: "left",
      valign: "top",
      fit: "shrink",
    });
  }
  slide.addText("Prepared with Fronei", {
    x: MARGIN_X,
    y: 6.75,
    w: 3.0,
    h: 0.25,
    fontSize: 9,
    color: TEXT_MUTED,
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
  addTitle(slide, spec.title);
  const cap = spec.appendix ? MAX_APPENDIX_BULLETS : MAX_BULLETS_PER_SLIDE;
  const visibleCap = spec.appendix ? cap : Math.min(cap, 4);
  const bullets = (spec.bullets || []).slice(0, visibleCap).map((b) => ({
    level: b.level || 0,
    text: bulletText(b.text || b, 96),
  }));
  slide.addText(bulletsToTextProps(bullets), {
    x: MARGIN_X,
    y: 1.65,
    w: 8.4,
    h: 4.9,
    valign: "top",
    fontFace: BODY_FACE,
    lineSpacingMultiple: 1.08,
  });
  addNotes(slide, spec.notes);
  return slide;
}

function renderTwoContentSlide(pptx, spec) {
  const slide = pptx.addSlide();
  addTitle(slide, spec.title);
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
    slide.addShape("roundRect", {
      x: left,
      y: top,
      w: colW,
      h: height,
      fill: { color: CARD_BG },
      line: { color: ACCENT_LINE, transparency: 20 },
      rectRadius: 0.04,
    });
    const parts = [];
    if (col.heading) {
      parts.push({ text: bulletText(col.heading, 42), options: { fontSize: headingFontSize, bold: true, color: NAVY, fontFace: HEADING_FACE, breakLine: true } });
    }
    const bullets = bulletsToTextProps((col.bullets || []).slice(0, bulletCap).map((b) => ({ level: 0, text: bulletText(b, 78) })), { fontSize: bulletFontSize });
    slide.addText(parts.concat(bullets), {
      x: left + 0.18,
      y: top + 0.18,
      w: colW - 0.36,
      h: height - 0.36,
      valign: "top",
      fontFace: BODY_FACE,
      lineSpacingMultiple: 1.1,
    });
  });
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
  addTitle(slide, spec.title);
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
    chartColors: [ACCENT, NAVY, "8C6F5D", "C9A14A"],
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
  addTitle(slide, spec.title);
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
          color: rIdx === 0 ? WHITE : TEXT_DARK,
          fill: rIdx === 0 ? { color: NAVY } : { color: rIdx % 2 === 0 ? SOFT_BG : WHITE },
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
  addTitle(slide, spec.title);
  const bullets = spec.bullets || [];
  const headline = bullets[0] || "";
  const support = bullets.slice(1, MAX_BULLETS_PER_SLIDE);
  if (headline) {
    slide.addText(headline, {
      x: MARGIN_X,
      y: 1.5,
      w: 9.6,
      h: 1.7,
      fontSize: 24,
      bold: true,
      color: TEXT_DARK,
      fontFace: HEADING_FACE,
      valign: "top",
      wrap: true,
    });
  }
  if (support.length) {
    slide.addText(bulletsToTextProps(support.slice(0, 3).map((b) => ({ level: 0, text: bulletText(b, 90) })), { fontSize: 14 }), {
      x: MARGIN_X,
      y: 3.3,
      w: SLIDE_W - MARGIN_X * 2,
      h: 3.2,
      valign: "top",
      fontFace: BODY_FACE,
      lineSpacingMultiple: 1.15,
    });
  }
  addNotes(slide, spec.notes);
  return slide;
}

function renderRecommendationSlide(pptx, spec) {
  const slide = pptx.addSlide();
  addTitle(slide, spec.title);
  const bullets = spec.bullets || [];
  const primary = bullets[0] || "";
  const rationale = bullets.slice(1, 1 + MAX_BULLETS_PER_SLIDE);
  if (primary) {
    slide.addShape("roundRect", {
      x: MARGIN_X,
      y: 1.5,
      w: SLIDE_W - MARGIN_X * 2,
      h: 1.3,
      fill: { color: TEXT_DARK },
      line: { color: TEXT_DARK },
      rectRadius: 0.08,
    });
    slide.addText(`Recommendation: ${primary}`, {
      x: MARGIN_X + 0.2,
      y: 1.5,
      w: SLIDE_W - MARGIN_X * 2 - 0.4,
      h: 1.3,
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
    const parts = [{ text: "Rationale", options: { fontSize: 15, bold: true, color: NAVY, fontFace: HEADING_FACE, breakLine: true } }];
    slide.addText(
      parts.concat(bulletsToTextProps(rationale.slice(0, 3).map((b) => ({ level: 0, text: bulletText(b, 90) })), { fontSize: 13 })),
      {
        x: MARGIN_X,
        y: 3.1,
        w: SLIDE_W - MARGIN_X * 2,
        h: 3.4,
        valign: "top",
        fontFace: BODY_FACE,
        lineSpacingMultiple: 1.1,
      }
    );
  }
  addNotes(slide, spec.notes);
  return slide;
}

function renderTimelineSlide(pptx, spec) {
  const slide = pptx.addSlide();
  addTitle(slide, spec.title);
  const phases = (spec.phases || [])
    .filter((p) => p && (p.title || p.label || p.description))
    .slice(0, 6);
  if (!phases.length) {
    addNotes(slide, spec.notes);
    return slide;
  }
  const totalW = 12.0;
  const gap = 0.25;
  const n = phases.length;
  const boxW = (totalW - gap * (n - 1)) / n;
  const top = 2.0;

  phases.forEach((ph, idx) => {
    const left = MARGIN_X + idx * (boxW + gap);
    if (idx > 0) {
      slide.addShape("rect", {
        x: left - gap,
        y: top + 0.4,
        w: gap,
        h: 0.04,
        fill: { color: ACCENT_LINE },
        line: { color: ACCENT_LINE },
      });
    }
    slide.addShape("ellipse", {
      x: left + boxW / 2 - 0.15,
      y: top + 0.25,
      w: 0.3,
      h: 0.3,
      fill: { color: ACCENT },
      line: { color: ACCENT },
    });
    const lines = [];
    if (ph.label) lines.push(ph.label);
    if (ph.title) lines.push(ph.title);
    if (ph.description) lines.push(bulletText(ph.description, 82));
    slide.addText(bulletsToTextProps(lines.map((l) => ({ level: 0, text: l })), { fontSize: 11 }), {
      x: left,
      y: top + 0.7,
      w: boxW,
      h: 3.8,
      valign: "top",
      fontFace: BODY_FACE,
    });
  });
  addNotes(slide, spec.notes);
  return slide;
}

function renderStatCardsSlide(pptx, spec) {
  const slide = pptx.addSlide();
  addTitle(slide, spec.title);
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
    const parts = [{ text: stat.value || "", options: { fontSize: 28, bold: true, color: ACCENT, fontFace: HEADING_FACE, breakLine: true, align: "center" } }];
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

function renderArchitectureSlide(pptx, spec) {
  const slide = pptx.addSlide();
  addTitle(slide, spec.title);
  slide.addText(
    [
      { text: "Architecture diagram", options: { fontSize: 15, bold: true, breakLine: true, color: NAVY, fontFace: HEADING_FACE } },
      {
        text: "(diagram placeholder — describe components and data flow)",
        options: { fontSize: 13, color: TEXT_MUTED },
      },
    ],
    {
      x: MARGIN_X,
      y: 1.55,
      w: 5.6,
      h: 4.9,
      valign: "top",
      fontFace: BODY_FACE,
      line: { color: ACCENT_LINE, width: 1, dashType: "dash" },
    }
  );
  const bullets = spec.bullets && spec.bullets.length ? spec.bullets : [""];
  slide.addText(bulletsToTextProps(bullets.slice(0, 4).map((b) => ({ level: 0, text: bulletText(b, 90) })), { fontSize: 13 }), {
    x: 6.5,
    y: 1.55,
    w: 5.8,
    h: 4.9,
    valign: "top",
    fontFace: BODY_FACE,
    lineSpacingMultiple: 1.1,
  });
  addNotes(slide, spec.notes);
  return slide;
}

function renderSlide(pptx, spec) {
  switch (spec.role) {
    case "section":
      return renderSectionSlide(pptx, spec);
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
