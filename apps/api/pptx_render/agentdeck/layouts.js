/**
 * Slide-layout zone renderers for the AgentDeck design system (agentdeck_v1).
 *
 * Each layout renderer has the signature:
 *   render(slide, spec, theme, slidePlan) -> void
 *
 * `slidePlan` is one entry of `PptxRenderPlan.slides[]`:
 *   {
 *     slide_layout: "CONTENT_2COL",
 *     header_bar?: { section_number?, section_title, variant? },
 *     title?: "Slide title text",
 *     zones?: { <zone_name>: { component_id, props } | { component_id, props }[] },
 *     callout?: { text, variant?, icon? },
 *     notes?: "speaker notes"
 *   }
 *
 * Geometry for every zone comes from spec.slide_layouts[layout].zones
 * (apps/api/app/services/design_systems/agentdeck_v1/spec.json), so layout
 * renderers never hardcode coordinates — only the AgentDeck-specific
 * decorative elements (title-slide color panel, accent rules, etc.) that
 * aren't component-driven.
 */

const { color, textOpts, makeShadow } = require("./tokens");
const components = require("./components");

// Components addressable directly from a slide_layout zone.
const COMPONENT_RENDERERS = {
  header_bar: components.renderHeaderBar,
  card: components.renderCard,
  stat_card: components.renderStatCard,
  bullet_list: components.renderBulletList,
  table: components.renderTable,
  callout_bar: components.renderCalloutBar,
  stat_strip: components.renderStatStrip,
  decision_list: components.renderDecisionList,
  timeline: components.renderTimeline,
};

function zoneGeom(spec, layoutName, zoneName) {
  const layout = spec.slide_layouts[layoutName];
  if (!layout) throw new Error(`Unknown slide_layout '${layoutName}'`);
  const zone = layout.zones[zoneName];
  if (!zone) throw new Error(`Unknown zone '${zoneName}' in slide_layout '${layoutName}'`);
  return zone;
}

/** Render a single zone instance: { component_id, props }. */
function renderZoneInstance(slide, spec, theme, zoneRect, instance) {
  const renderer = COMPONENT_RENDERERS[instance.component_id];
  if (!renderer) {
    throw new Error(`Component '${instance.component_id}' is not zone-fillable`);
  }
  renderer(slide, spec, theme, zoneRect, instance.props || {});
}

/** Render whatever is assigned to a named zone — single instance or array. */
function fillZone(slide, spec, theme, layoutName, zoneName, assignment) {
  if (!assignment) return;
  const zoneRect = zoneGeom(spec, layoutName, zoneName);
  if (Array.isArray(assignment)) {
    assignment.forEach((instance) => renderZoneInstance(slide, spec, theme, zoneRect, instance));
  } else {
    renderZoneInstance(slide, spec, theme, zoneRect, assignment);
  }
}

function renderSlideTitle(slide, spec, theme, layoutName, title) {
  if (!title) return;
  const zone = zoneGeom(spec, layoutName, "slide_title");
  slide.addText(title, {
    x: zone.x, y: zone.y, w: zone.w, h: zone.h,
    valign: "middle",
    ...textOpts(spec, "h1", { color: color(spec, theme, "text.primary") }),
  });
}

function renderHeaderBarZone(slide, spec, theme, layoutName, slidePlan) {
  if (!slidePlan.header_bar) return;
  const zone = zoneGeom(spec, layoutName, "header_bar");
  components.renderHeaderBar(slide, spec, theme, zone, slidePlan.header_bar);
}

function renderCalloutIfPresent(slide, spec, theme, layoutName, slidePlan) {
  if (!slidePlan.callout) return;
  // callout_bar has fixed geometry (x=0.4, w=12.5, h=0.9) anchored to the
  // bottom of the content area; "body"/"col_*"/"hero" zones all share the
  // same bottom edge (y + h = 7.05), so any content zone works as the anchor.
  const contentArea = spec.spacing.content_area;
  const zone = { x: contentArea.x_start, y: contentArea.y_start, w: contentArea.width, h: contentArea.height };
  components.renderCalloutBar(slide, spec, theme, zone, slidePlan.callout);
}

// ---------------------------------------------------------------------------
// generic content layouts (header_bar + slide_title + N zones)
// ---------------------------------------------------------------------------

function renderGenericContentLayout(layoutName, zoneNames) {
  return function (slide, spec, theme, slidePlan) {
    slide.background = { color: color(spec, theme, "bg.canvas") };
    renderHeaderBarZone(slide, spec, theme, layoutName, slidePlan);
    renderSlideTitle(slide, spec, theme, layoutName, slidePlan.title);
    const zones = slidePlan.zones || {};
    zoneNames.forEach((zoneName) => fillZone(slide, spec, theme, layoutName, zoneName, zones[zoneName]));
    renderCalloutIfPresent(slide, spec, theme, layoutName, slidePlan);
  };
}

const renderContent1Col = renderGenericContentLayout("CONTENT_1COL", ["body"]);
const renderContent2Col = renderGenericContentLayout("CONTENT_2COL", ["col_left", "col_right"]);
const renderContent3Col = renderGenericContentLayout("CONTENT_3COL", ["col_1", "col_2", "col_3"]);
const renderContent4Col = renderGenericContentLayout("CONTENT_4COL", ["col_1", "col_2", "col_3", "col_4"]);
const renderContentHeroStat = renderGenericContentLayout("CONTENT_HERO_STAT", ["hero", "supporting_row"]);
const renderContentTableSidebar = renderGenericContentLayout("CONTENT_TABLE_SIDEBAR", ["table", "sidebar"]);
const renderContentSplitDecisions = renderGenericContentLayout("CONTENT_SPLIT_DECISIONS", ["left_panel", "right_panel"]);

// ---------------------------------------------------------------------------
// special layouts: TITLE, SECTION_HEADER, CLOSING
// ---------------------------------------------------------------------------

function renderTitle(slide, spec, theme, slidePlan) {
  const layoutName = "TITLE";
  slide.background = { color: color(spec, theme, "bg.canvas") };

  const leftPanel = zoneGeom(spec, layoutName, "left_panel");
  slide.addShape("rect", {
    x: leftPanel.x, y: leftPanel.y, w: leftPanel.w, h: leftPanel.h,
    fill: { color: color(spec, theme, "accent.secondary") },
    line: { type: "none" },
  });

  if (slidePlan.deck_type_label) {
    slide.addText(slidePlan.deck_type_label.toUpperCase(), {
      x: leftPanel.x + 0.5, y: leftPanel.h - 1.6, w: leftPanel.w - 1.0, h: 0.3,
      ...textOpts(spec, "label", { color: color(spec, theme, "text.on_accent") }),
    });
  }
  if (slidePlan.date_label || slidePlan.confidentiality) {
    const footer = [slidePlan.date_label, slidePlan.confidentiality].filter(Boolean).join("   |   ");
    slide.addText(footer, {
      x: leftPanel.x + 0.5, y: leftPanel.h - 1.0, w: leftPanel.w - 1.0, h: 0.5,
      ...textOpts(spec, "body_sm", { color: color(spec, theme, "text.on_accent") }),
    });
  }

  const heroZone = zoneGeom(spec, layoutName, "hero_title");
  if (slidePlan.hero_title) {
    slide.addText(slidePlan.hero_title, {
      x: heroZone.x, y: heroZone.y, w: heroZone.w, h: heroZone.h,
      valign: "bottom",
      ...textOpts(spec, "display", { color: color(spec, theme, "text.primary") }),
    });
  }

  const accentRule = zoneGeom(spec, layoutName, "accent_rule");
  slide.addShape("rect", {
    x: accentRule.x, y: accentRule.y, w: accentRule.w, h: accentRule.h,
    fill: { color: color(spec, theme, "accent.gold") },
    line: { type: "none" },
  });

  if (slidePlan.subtitle) {
    const subtitleZone = zoneGeom(spec, layoutName, "subtitle");
    slide.addText(slidePlan.subtitle, {
      x: subtitleZone.x, y: subtitleZone.y, w: subtitleZone.w, h: subtitleZone.h,
      valign: "top",
      ...textOpts(spec, "h2", { color: color(spec, theme, "text.secondary"), bold: false }),
    });
  }

  if (slidePlan.presenter) {
    const presenterZone = zoneGeom(spec, layoutName, "presenter");
    slide.addText(slidePlan.presenter, {
      x: presenterZone.x, y: presenterZone.y, w: presenterZone.w, h: presenterZone.h,
      ...textOpts(spec, "body_sm", { color: color(spec, theme, "text.muted") }),
    });
  }
}

function renderSectionHeader(slide, spec, theme, slidePlan) {
  const layoutName = "SECTION_HEADER";
  const bgFull = zoneGeom(spec, layoutName, "bg_full");
  slide.background = { color: color(spec, theme, "bg.canvas") };
  slide.addShape("rect", {
    x: bgFull.x, y: bgFull.y, w: bgFull.w, h: bgFull.h,
    fill: { color: color(spec, theme, "bg.canvas") },
    line: { type: "none" },
  });

  const stripe = zoneGeom(spec, layoutName, "accent_stripe");
  slide.addShape("rect", {
    x: stripe.x, y: stripe.y, w: stripe.w, h: stripe.h,
    fill: { color: color(spec, theme, "accent.primary") },
    line: { type: "none" },
  });

  if (slidePlan.section_number) {
    const numZone = zoneGeom(spec, layoutName, "section_number");
    slide.addText(slidePlan.section_number, {
      x: numZone.x, y: numZone.y, w: numZone.w, h: numZone.h,
      ...textOpts(spec, "display", { color: color(spec, theme, "accent.primary"), transparency: 85 }),
    });
  }

  const titleZone = zoneGeom(spec, layoutName, "section_title");
  slide.addText(slidePlan.section_title || "", {
    x: titleZone.x, y: titleZone.y, w: titleZone.w, h: titleZone.h,
    ...textOpts(spec, "display", { color: color(spec, theme, "text.primary") }),
  });

  if (slidePlan.section_subtitle) {
    const subZone = zoneGeom(spec, layoutName, "section_subtitle");
    slide.addText(slidePlan.section_subtitle, {
      x: subZone.x, y: subZone.y, w: subZone.w, h: subZone.h,
      ...textOpts(spec, "h2", { color: color(spec, theme, "text.secondary"), bold: false }),
    });
  }
}

function renderClosing(slide, spec, theme, slidePlan) {
  const layoutName = "CLOSING";
  slide.background = { color: color(spec, theme, "bg.canvas") };

  const leftPanel = zoneGeom(spec, layoutName, "left_panel");
  slide.addShape("rect", {
    x: leftPanel.x, y: leftPanel.y, w: leftPanel.w, h: leftPanel.h,
    fill: { color: color(spec, theme, "accent.secondary") },
    line: { type: "none" },
  });

  const closingTextZone = zoneGeom(spec, layoutName, "closing_text");
  slide.addText(slidePlan.closing_text || "", {
    x: closingTextZone.x, y: closingTextZone.y, w: closingTextZone.w, h: closingTextZone.h,
    ...textOpts(spec, "display", { color: color(spec, theme, "text.primary") }),
  });

  const accentRule = zoneGeom(spec, layoutName, "accent_rule");
  slide.addShape("rect", {
    x: accentRule.x, y: accentRule.y, w: accentRule.w, h: accentRule.h,
    fill: { color: color(spec, theme, "accent.gold") },
    line: { type: "none" },
  });

  if (slidePlan.closing_body) {
    const bodyZone = zoneGeom(spec, layoutName, "closing_body");
    slide.addText(slidePlan.closing_body, {
      x: bodyZone.x, y: bodyZone.y, w: bodyZone.w, h: bodyZone.h,
      valign: "top",
      ...textOpts(spec, "body", { color: color(spec, theme, "text.secondary") }),
    });
  }

  if (slidePlan.presenter) {
    const presenterZone = zoneGeom(spec, layoutName, "presenter");
    slide.addText(slidePlan.presenter, {
      x: presenterZone.x, y: presenterZone.y, w: presenterZone.w, h: presenterZone.h,
      valign: "top",
      ...textOpts(spec, "body_sm", { color: color(spec, theme, "text.muted") }),
    });
  }
}

// ---------------------------------------------------------------------------
// dispatcher
// ---------------------------------------------------------------------------

const SLIDE_LAYOUT_RENDERERS = {
  TITLE: renderTitle,
  SECTION_HEADER: renderSectionHeader,
  CONTENT_1COL: renderContent1Col,
  CONTENT_2COL: renderContent2Col,
  CONTENT_3COL: renderContent3Col,
  CONTENT_4COL: renderContent4Col,
  CONTENT_HERO_STAT: renderContentHeroStat,
  CONTENT_TABLE_SIDEBAR: renderContentTableSidebar,
  CONTENT_SPLIT_DECISIONS: renderContentSplitDecisions,
  CLOSING: renderClosing,
};

/** Render one slidePlan onto `slide` per its `slide_layout`. */
function renderSlide(slide, spec, theme, slidePlan) {
  const renderer = SLIDE_LAYOUT_RENDERERS[slidePlan.slide_layout];
  if (!renderer) {
    throw new Error(`Unknown slide_layout '${slidePlan.slide_layout}'`);
  }
  renderer(slide, spec, theme, slidePlan);
  if (slidePlan.notes) {
    slide.addNotes(slidePlan.notes);
  }
}

module.exports = {
  SLIDE_LAYOUT_RENDERERS,
  renderSlide,
  zoneGeom,
};
