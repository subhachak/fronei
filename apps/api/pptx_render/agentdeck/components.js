/**
 * Component render functions for the AgentDeck design system (agentdeck_v1).
 *
 * Each renderer has the signature:
 *   render(slide, spec, theme, zone, props) -> void
 *
 * - `slide`  : PptxGenJS slide object
 * - `spec`   : full design-system spec JSON
 * - `theme`  : "dark" | "light"
 * - `zone`   : { x, y, w, h } in inches (from spec.slide_layouts[layout].zones)
 * - `props`  : validated content payload matching the component's
 *              content_schema (apps/api/app/services/components/content_schemas.py)
 *
 * Mirrors spec.json `components` definitions + `generation_rules.mandatory`:
 *  - hex colors without '#'
 *  - makeShadow() factory per shape (never reuse shadow objects)
 *  - ROUNDED_RECTANGLE + rectRadius for cards, never RECTANGLE + radius
 *  - bullet:true + breakLine:true for lists, paraSpaceAfter not lineSpacing
 */

const { color, textOpts, spacing, radius, makeShadow } = require("./tokens");

// ---------------------------------------------------------------------------
// inline helpers
// ---------------------------------------------------------------------------

const BADGE_VARIANTS = {
  default: { fill: "bg.surface_2", text: "text.secondary" },
  primary: { fill: "accent.primary_muted", text: "accent.primary" },
  success: { fill: "accent.success_muted", text: "accent.success" },
  danger: { fill: "accent.danger_muted", text: "accent.danger" },
  gold: { fill: "accent.gold_muted", text: "accent.gold" },
  solid_primary: { fill: "accent.primary", text: "text.on_accent" },
  solid_danger: { fill: "accent.danger", text: "text.on_accent" },
  solid_success: { fill: "accent.success", text: "text.on_accent" },
  solid_gold: { fill: "accent.gold", text: "text.on_accent" },
};

/** Compute the rendered pill width (inches) for a badge's text, without drawing it. */
function badgeWidth(spec, badge) {
  const padX = 0.12;
  const fontSize = spec.typography.scale.label.fontSize_pt;
  const charSpacing = spec.typography.scale.label.charSpacing || 0;
  const text = (badge.text || "").toUpperCase();
  const charW = (fontSize / 72) * 0.78 + charSpacing / 72;
  return Math.max(0.55, text.length * charW + padX * 2);
}

/** Inline pill badge. Returns the rendered width (inches) for layout chaining. */
function addBadge(slide, spec, theme, x, y, badge) {
  const variant = BADGE_VARIANTS[badge.variant || "default"] || BADGE_VARIANTS.default;
  const padY = 0.06;
  const fontSize = spec.typography.scale.label.fontSize_pt;
  const text = (badge.text || "").toUpperCase();
  const w = badgeWidth(spec, badge);
  const h = fontSize / 72 + padY * 2 + 0.05;

  slide.addShape("roundRect", {
    x, y, w, h,
    rectRadius: radius(spec, "pill"),
    fill: { color: color(spec, theme, variant.fill) },
    line: { type: "none" },
  });
  slide.addText(text, {
    x, y, w, h,
    align: "center",
    valign: "middle",
    ...textOpts(spec, "label", { color: color(spec, theme, variant.text) }),
  });
  return w;
}

const ICON_CIRCLE_SIZES = {
  sm: { diameter: 0.45, icon: 0.25 },
  md: { diameter: 0.65, icon: 0.38 },
  lg: { diameter: 0.9, icon: 0.5 },
};

// Maps semantic icon names (e.g. "info", "warning") to a glyph that reads
// sensibly inside a small circle badge. Anything not in this map falls back
// to its first character -- but "info"/"insight" must map to lowercase "i"
// (the conventional info-icon glyph), not an uppercased "I", which a viewer
// reads as a stray capital letter rather than an icon.
const ICON_GLYPHS = {
  info: "i",
  insight: "i",
  note: "i",
  check: "✓",
  success: "✓",
  done: "✓",
  warning: "!",
  risk: "!",
  danger: "!",
  alert: "!",
  cost: "$",
  money: "$",
  budget: "$",
};

function iconGlyph(icon) {
  const key = String(icon || "info").trim().toLowerCase();
  if (ICON_GLYPHS[key]) return ICON_GLYPHS[key];
  const ch = key.slice(0, 1);
  return ch === "i" ? "i" : ch.toUpperCase();
}

/** Icon-in-circle placeholder (renders a glyph/number, not a real icon asset). */
function addIconCircle(slide, spec, theme, x, y, props) {
  const size = ICON_CIRCLE_SIZES[props.size || "md"];
  slide.addShape("ellipse", {
    x, y, w: size.diameter, h: size.diameter,
    fill: { color: color(spec, theme, "accent.primary") },
    line: { type: "none" },
  });
  const label = props.number != null ? String(props.number) : iconGlyph(props.icon);
  slide.addText(label, {
    x, y, w: size.diameter, h: size.diameter,
    align: "center",
    valign: "middle",
    ...textOpts(spec, "h3", { color: color(spec, theme, "text.on_accent") }),
  });
  return size.diameter;
}

function addProgressBar(slide, spec, theme, x, y, w, props) {
  const h = 0.14;
  slide.addShape("roundRect", {
    x, y, w, h,
    rectRadius: radius(spec, "pill"),
    fill: { color: color(spec, theme, "bg.surface_3") },
    line: { type: "none" },
  });
  const fillW = Math.max(h, w * Math.min(1, Math.max(0, props.value)));
  slide.addShape("roundRect", {
    x, y, w: fillW, h,
    rectRadius: radius(spec, "pill"),
    fill: { color: color(spec, theme, "accent.primary") },
    line: { type: "none" },
  });
  let bottom = y + h;
  if (props.label) {
    slide.addText(props.label, {
      x, y: bottom + 0.02, w, h: 0.25,
      ...textOpts(spec, "body_sm", { color: color(spec, theme, "text.secondary") }),
    });
    bottom += 0.27;
  }
  return bottom;
}

function bulletParagraphs(spec, theme, items) {
  return items.map((item, idx) => {
    const level = item.level || 0;
    const base = level === 0
      ? { fontSize: spec.typography.scale.body.fontSize_pt, color: color(spec, theme, "text.primary"), indentLevel: 0 }
      : { fontSize: spec.typography.scale.body.fontSize_pt - 0.5, color: color(spec, theme, "text.secondary"), indentLevel: 1 };
    return {
      text: item.text,
      options: {
        fontFace: spec.typography.fontFace.body,
        bullet: true,
        breakLine: true,
        paraSpaceAfter: 4,
        ...base,
      },
    };
  });
}

// ---------------------------------------------------------------------------
// zone-fillable components
// ---------------------------------------------------------------------------

const HEADER_BAR_VARIANTS = {
  dark_navy: { fill: "bg.canvas", text: "text.primary" },
  accent_blue: { fill: "accent.primary", text: "text.on_accent" },
  surface: { fill: "bg.surface_1", text: "text.secondary" },
};

function renderHeaderBar(slide, spec, theme, zone, props) {
  const variant = HEADER_BAR_VARIANTS[props.variant || "dark_navy"] || HEADER_BAR_VARIANTS.dark_navy;
  slide.addShape("rect", {
    x: zone.x, y: zone.y, w: zone.w, h: zone.h,
    fill: { color: color(spec, theme, variant.fill) },
    line: { type: "none" },
  });
  const label = props.section_number
    ? `${props.section_number}   |   ${props.section_title}`.toUpperCase()
    : props.section_title.toUpperCase();
  slide.addText(label, {
    x: zone.x + 0.5, y: zone.y, w: zone.w - 1.0, h: zone.h,
    valign: "middle",
    ...textOpts(spec, "label", { color: color(spec, theme, variant.text), charSpacing: 4 }),
  });
}

const CARD_VARIANTS = {
  default: { fill: null, border: "border.subtle", borderWidth: 0 },
  outlined: { fill: null, border: "border.default", borderWidth: 0.5 },
  filled: { fill: "bg.surface_2", border: "border.subtle", borderWidth: 0 },
  accent: { fill: "accent.primary_muted", border: "accent.primary", borderWidth: 0.75 },
};

const CARD_COLOR_VARIANTS = {
  blue: { header_fill: "accent.primary", header_text: "text.on_accent" },
  teal: { header_fill: "accent.secondary", header_text: "text.on_accent" },
  gold: { header_fill: "accent.gold", header_text: "text.on_accent" },
  danger: { header_fill: "accent.danger", header_text: "text.on_accent" },
  success: { header_fill: "accent.success", header_text: "text.on_accent" },
  surface: { header_fill: "bg.surface_3", header_text: "text.primary" },
};

function defaultCardFill(spec, theme) {
  return theme === "dark" ? color(spec, theme, "bg.surface_1") : color(spec, theme, "bg.canvas");
}

function renderCard(slide, spec, theme, zone, props) {
  const variant = CARD_VARIANTS[props.variant || "default"] || CARD_VARIANTS.default;
  const fill = variant.fill ? color(spec, theme, variant.fill) : defaultCardFill(spec, theme);
  const line = variant.borderWidth > 0
    ? { color: color(spec, theme, variant.border), width: variant.borderWidth }
    : { type: "none" };

  slide.addShape("roundRect", {
    x: zone.x, y: zone.y, w: zone.w, h: zone.h,
    rectRadius: radius(spec, "md"),
    fill: { color: fill },
    line,
    shadow: makeShadow(spec, "card"),
  });

  const padX = 0.2;
  const padY = 0.18;
  let cursorY = zone.y + padY;
  const colorVariant = props.color_variant && CARD_COLOR_VARIANTS[props.color_variant];

  if (colorVariant) {
    const headerH = 0.5;
    slide.addShape("roundRect", {
      x: zone.x, y: zone.y, w: zone.w, h: headerH,
      rectRadius: radius(spec, "md"),
      fill: { color: color(spec, theme, colorVariant.header_fill) },
      line: { type: "none" },
    });
    if (props.title) {
      slide.addText(props.title, {
        x: zone.x + padX, y: zone.y, w: zone.w - padX * 2, h: headerH,
        valign: "middle",
        ...textOpts(spec, "h3", { color: color(spec, theme, colorVariant.header_text) }),
      });
    }
    cursorY = zone.y + headerH + padY;
  } else if (props.title) {
    const bw = props.badge ? badgeWidth(spec, props.badge) : 0;
    slide.addText(props.title, {
      x: zone.x + padX, y: cursorY, w: zone.w - padX * 2 - (bw ? bw + 0.15 : 0), h: 0.4,
      ...textOpts(spec, "h2", { color: color(spec, theme, "text.primary") }),
    });
    if (props.badge) {
      addBadge(slide, spec, theme, zone.x + zone.w - padX - bw, cursorY + 0.04, props.badge);
    }
    cursorY += 0.5;
  }

  const bodyX = zone.x + padX;
  const bodyW = zone.w - padX * 2;
  const bodyH = zone.y + zone.h - padY - cursorY;

  if (props.bullets && props.bullets.length) {
    slide.addText(bulletParagraphs(spec, theme, props.bullets), {
      x: bodyX, y: cursorY, w: bodyW, h: bodyH,
      valign: "top",
    });
  } else if (props.body) {
    slide.addText(props.body, {
      x: bodyX, y: cursorY, w: bodyW, h: bodyH,
      valign: "top",
      ...textOpts(spec, "body", { color: color(spec, theme, "text.primary") }),
    });
  }
}

function renderStatCard(slide, spec, theme, zone, props) {
  slide.addShape("roundRect", {
    x: zone.x, y: zone.y, w: zone.w, h: zone.h,
    rectRadius: radius(spec, "md"),
    fill: { color: defaultCardFill(spec, theme) },
    line: { type: "none" },
    shadow: makeShadow(spec, "card"),
  });

  const pad = 0.2;
  let cursorY = zone.y + pad;

  if (props.icon) {
    addIconCircle(slide, spec, theme, zone.x + pad, cursorY, { icon: props.icon, size: "sm" });
    cursorY += 0.55;
  }

  const statStyle = zone.h >= 2.0 ? "stat" : "stat_sm";
  slide.addText(props.value, {
    x: zone.x + pad, y: cursorY, w: zone.w - pad * 2, h: 0.7,
    ...textOpts(spec, statStyle, { color: color(spec, theme, "text.primary") }),
  });
  cursorY += 0.7;

  slide.addText(props.label, {
    x: zone.x + pad, y: cursorY, w: zone.w - pad * 2, h: 0.5,
    valign: "top",
    ...textOpts(spec, "body", { color: color(spec, theme, "text.secondary") }),
  });
  cursorY += 0.45;

  if (props.delta) {
    const arrow = props.delta_direction === "negative" ? "▼ " : "▲ ";
    const deltaColor = props.delta_direction === "negative" ? "accent.danger" : "accent.success";
    slide.addText(arrow + props.delta, {
      x: zone.x + pad, y: cursorY, w: zone.w - pad * 2, h: 0.3,
      ...textOpts(spec, "body_sm", { color: color(spec, theme, deltaColor) }),
    });
    cursorY += 0.3;
  }

  if (props.caption) {
    slide.addText(props.caption, {
      x: zone.x + pad, y: zone.y + zone.h - pad - 0.25, w: zone.w - pad * 2, h: 0.25,
      ...textOpts(spec, "body_sm", { color: color(spec, theme, "text.muted") }),
    });
  }
}

function renderBulletList(slide, spec, theme, zone, props) {
  let cursorY = zone.y;
  if (props.title) {
    slide.addText(props.title, {
      x: zone.x, y: cursorY, w: zone.w, h: 0.45,
      ...textOpts(spec, "h2", { color: color(spec, theme, "text.primary") }),
    });
    cursorY += 0.5;
  }
  slide.addText(bulletParagraphs(spec, theme, props.items), {
    x: zone.x, y: cursorY, w: zone.w, h: zone.y + zone.h - cursorY,
    valign: "top",
  });
}

const SEMANTIC_CELL = {
  critical: { token: "accent.danger", bold: true },
  warning: { token: "accent.gold", bold: false },
  positive: { token: "accent.success", bold: false },
  muted: { token: "text.muted", bold: false },
};

function normalizeCell(cell) {
  if (typeof cell === "string") return { text: cell };
  return cell;
}

function renderTable(slide, spec, theme, zone, props) {
  const headerFill = theme === "dark" ? color(spec, theme, "bg.canvas") : color(spec, theme, "text.primary");
  const headerText = color(spec, theme, "text.on_accent");
  const evenFill = color(spec, theme, "bg.canvas");
  const oddFill = color(spec, theme, "bg.surface_1");
  const bodyText = color(spec, theme, "text.primary");
  const h3 = spec.typography.scale.h3;
  const body = spec.typography.scale.body;

  const headerRow = props.headers.map((text) => ({
    text,
    options: {
      fill: { color: headerFill },
      color: headerText,
      fontFace: h3.fontFace,
      fontSize: h3.fontSize_pt,
      bold: true,
      valign: "middle",
    },
  }));

  const bodyRows = props.rows.map((row, ri) => row.map((rawCell) => {
    const cell = normalizeCell(rawCell);
    const semantic = cell.semantic && SEMANTIC_CELL[cell.semantic];
    return {
      text: cell.text,
      options: {
        fill: { color: ri % 2 === 0 ? evenFill : oddFill },
        color: semantic ? color(spec, theme, semantic.token) : bodyText,
        fontFace: body.fontFace,
        fontSize: body.fontSize_pt,
        bold: cell.bold || (semantic ? semantic.bold : false),
        valign: "middle",
      },
    };
  }));

  slide.addTable([headerRow, ...bodyRows], {
    x: zone.x, y: zone.y, w: zone.w,
    border: { type: "solid", color: color(spec, theme, "border.subtle"), pt: 0.5 },
    rowH: 0.48,
    margin: 0.1,
    autoPage: false,
  });
}

const CALLOUT_VARIANTS = {
  insight: { fill: "accent.gold_muted", icon: "accent.gold" },
  info: { fill: "accent.primary_muted", icon: "accent.primary" },
  danger: { fill: "accent.danger_muted", icon: "accent.danger" },
  success: { fill: "accent.success_muted", icon: "accent.success" },
};

function renderCalloutBar(slide, spec, theme, zone, props) {
  const variant = CALLOUT_VARIANTS[props.variant || "insight"] || CALLOUT_VARIANTS.insight;
  // Fixed geometry per spec.json components.callout_bar: x=0.4, h=0.9, w=12.5,
  // anchored to the bottom of the available content area for whichever zone
  // it's attached to.
  const x = 0.4;
  const w = 12.5;
  const h = 0.9;
  const y = zone.y + zone.h - h;

  slide.addShape("roundRect", {
    x, y, w, h,
    rectRadius: radius(spec, "md"),
    fill: { color: color(spec, theme, variant.fill) },
    line: { type: "none" },
  });

  addIconCircle(slide, spec, theme, x + 0.3, y + (h - 0.45) / 2, { icon: props.icon || "i", size: "sm" });

  slide.addText(props.text, {
    x: x + 1.3, y, w: w - 1.3 - 0.3, h,
    valign: "middle",
    ...textOpts(spec, "body", { color: color(spec, theme, "text.primary") }),
  });
}

// ---------------------------------------------------------------------------
// composites
// ---------------------------------------------------------------------------

function renderStatStrip(slide, spec, theme, zone, props) {
  const stats = props.stats || [];
  const gap = spacing(spec, "sm");
  const n = Math.max(1, stats.length);
  const cardW = (zone.w - gap * (n - 1)) / n;
  stats.forEach((stat, idx) => {
    renderStatCard(slide, spec, theme, {
      x: zone.x + idx * (cardW + gap), y: zone.y, w: cardW, h: zone.h,
    }, stat);
  });
}

function renderDecisionList(slide, spec, theme, zone, props) {
  const cards = props.cards || [];
  let cursorY = zone.y;
  if (props.title) {
    slide.addText(props.title, {
      x: zone.x, y: cursorY, w: zone.w, h: 0.45,
      ...textOpts(spec, "h2", { color: color(spec, theme, "text.primary") }),
    });
    cursorY += 0.55;
  }
  const gap = spacing(spec, "sm");
  const n = Math.max(1, cards.length);
  const cardH = (zone.y + zone.h - cursorY - gap * (n - 1)) / n;
  cards.forEach((card) => {
    renderCard(slide, spec, theme, { x: zone.x, y: cursorY, w: zone.w, h: cardH }, card);
    cursorY += cardH + gap;
  });
}

function renderTimeline(slide, spec, theme, zone, props) {
  const nodes = props.nodes || [];
  const n = Math.max(1, nodes.length);
  const nodeCircleD = 0.3;

  if ((props.orientation || "horizontal") === "horizontal") {
    const stepW = zone.w / n;
    const lineY = zone.y + 0.4;
    slide.addShape("rect", {
      x: zone.x, y: lineY - 0.02, w: zone.w, h: 0.04,
      fill: { color: color(spec, theme, "border.default") },
      line: { type: "none" },
    });
    nodes.forEach((node, idx) => {
      const cx = zone.x + stepW * idx + stepW / 2;
      slide.addShape("ellipse", {
        x: cx - nodeCircleD / 2, y: lineY - nodeCircleD / 2, w: nodeCircleD, h: nodeCircleD,
        fill: { color: color(spec, theme, "accent.primary") },
        line: { type: "none" },
      });
      slide.addText(node.step_label.toUpperCase(), {
        x: zone.x + stepW * idx, y: zone.y, w: stepW, h: 0.25,
        align: "center",
        ...textOpts(spec, "label", { color: color(spec, theme, "text.muted") }),
      });
      slide.addText(node.title, {
        x: zone.x + stepW * idx, y: lineY + 0.2, w: stepW, h: 0.4,
        align: "center",
        ...textOpts(spec, "h3", { color: color(spec, theme, "text.primary") }),
      });
      if (node.body) {
        slide.addText(node.body, {
          x: zone.x + stepW * idx + 0.05, y: lineY + 0.65, w: stepW - 0.1, h: zone.h - (lineY + 0.65 - zone.y),
          align: "center",
          valign: "top",
          ...textOpts(spec, "body_sm", { color: color(spec, theme, "text.secondary") }),
        });
      }
    });
  } else {
    const stepH = zone.h / n;
    const lineX = zone.x + 0.2;
    slide.addShape("rect", {
      x: lineX - 0.02, y: zone.y, w: 0.04, h: zone.h,
      fill: { color: color(spec, theme, "border.default") },
      line: { type: "none" },
    });
    nodes.forEach((node, idx) => {
      const cy = zone.y + stepH * idx + stepH / 2;
      slide.addShape("ellipse", {
        x: lineX - nodeCircleD / 2, y: cy - nodeCircleD / 2, w: nodeCircleD, h: nodeCircleD,
        fill: { color: color(spec, theme, "accent.primary") },
        line: { type: "none" },
      });
      const textX = lineX + 0.3;
      slide.addText(
        [
          { text: node.step_label.toUpperCase() + "\n", options: textOpts(spec, "label", { color: color(spec, theme, "text.muted") }) },
          { text: node.title + (node.body ? "\n" : ""), options: textOpts(spec, "h3", { color: color(spec, theme, "text.primary") }) },
          ...(node.body ? [{ text: node.body, options: textOpts(spec, "body_sm", { color: color(spec, theme, "text.secondary") }) }] : []),
        ],
        { x: textX, y: zone.y + stepH * idx, w: zone.w - (textX - zone.x), h: stepH, valign: "middle" }
      );
    });
  }
}

module.exports = {
  addBadge,
  addIconCircle,
  addProgressBar,
  renderHeaderBar,
  renderCard,
  renderStatCard,
  renderBulletList,
  renderTable,
  renderCalloutBar,
  renderStatStrip,
  renderDecisionList,
  renderTimeline,
};
