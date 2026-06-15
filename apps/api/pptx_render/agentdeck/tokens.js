/**
 * Token resolution helpers for the AgentDeck design system (agentdeck_v1).
 *
 * `spec` is the full design-system spec JSON (apps/api/app/services/
 * design_systems/agentdeck_v1/spec.json, as served by
 * design_systems.registry.design_system_payload()). `theme` is "dark" | "light".
 *
 * Mirrors generation_rules.theme_switching: all fill/text/border colors are
 * resolved per-theme from spec.color_tokens[theme]. Hex strings never carry
 * a leading '#'.
 */

function hex(c) {
  if (typeof c !== "string") return c;
  return c.replace(/^#/, "");
}

/** Resolve a dotted color token, e.g. "accent.primary", "text.on_surface". */
function color(spec, theme, token) {
  const [group, key] = token.split(".");
  const colors = spec.color_tokens[theme];
  const groupObj = colors[group];
  if (!groupObj) {
    throw new Error(`Unknown color group '${group}' in token '${token}'`);
  }
  if (group === "text" && key === "on_surface") {
    return hex(colors.text.on_dark_surface || colors.text.on_light_surface);
  }
  if (key === "series") {
    return [
      colors.chart.series_1,
      colors.chart.series_2,
      colors.chart.series_3,
      colors.chart.series_4,
      colors.chart.series_5,
    ].map(hex);
  }
  const val = groupObj[key];
  if (val === undefined) {
    throw new Error(`Unknown token '${token}' for theme '${theme}'`);
  }
  return hex(val);
}

/** Resolve a type-scale entry, e.g. "h1", "type.stat", "body_sm". */
function type(spec, name) {
  const key = name.includes(".") ? name.split(".")[1] : name;
  const style = spec.typography.scale[key];
  if (!style) throw new Error(`Unknown type style '${name}'`);
  return style;
}

/** Build addText() option fragment from a type-scale entry. */
function textOpts(spec, name, extra) {
  const t = type(spec, name);
  const opts = {
    fontFace: t.fontFace,
    fontSize: t.fontSize_pt,
    bold: !!t.bold,
  };
  if (t.charSpacing) opts.charSpacing = t.charSpacing;
  return Object.assign(opts, extra || {});
}

function spacing(spec, name) {
  const key = name.includes(".") ? name.split(".")[1] : name;
  const val = spec.spacing.tokens[key];
  if (val === undefined) throw new Error(`Unknown spacing token '${name}'`);
  return val;
}

function radius(spec, name) {
  const key = name.includes(".") ? name.split(".")[1] : name;
  const val = spec.radius[key];
  if (val === undefined) throw new Error(`Unknown radius token '${name}'`);
  return val;
}

/**
 * Shadow factory — per generation_rules.mandatory, ALWAYS build a fresh
 * object (never share/reuse an elevation entry) to avoid PptxGenJS's shadow
 * object-mutation bug.
 */
function makeShadow(spec, name) {
  const key = name.includes(".") ? name.split(".")[1] : name;
  const s = spec.elevation[key];
  if (!s) return undefined;
  return {
    type: s.type,
    color: hex(s.color),
    blur: s.blur,
    offset: s.offset,
    angle: s.angle,
    opacity: s.opacity,
  };
}

module.exports = { hex, color, type, textOpts, spacing, radius, makeShadow };
