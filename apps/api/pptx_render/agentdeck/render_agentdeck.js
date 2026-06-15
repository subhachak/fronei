#!/usr/bin/env node
/**
 * AgentDeck v1 render entrypoint.
 *
 * Reads a `PptxRenderPlan` JSON document from stdin and writes a .pptx
 * (nodebuffer) to stdout. This module is additive — it does not touch the
 * existing `render.js` / legacy pipeline.
 *
 * Expected payload shape:
 *   {
 *     "design_system": { ... full spec.json contents ... },
 *     "theme": "dark" | "light",
 *     "slides": [ <slidePlan>, ... ]   // see layouts.js for slidePlan shape
 *   }
 *
 * `design_system` is normally the output of
 * `design_systems.registry.design_system_payload("agentdeck_v1")` on the
 * Python side, so this JS module never needs to read spec.json itself.
 *
 * Usage: node render_agentdeck.js < plan.json > deck.pptx
 */

const pptxgen = require("pptxgenjs");
const { renderSlide } = require("./layouts");

function readStdin() {
  return new Promise((resolve, reject) => {
    const chunks = [];
    process.stdin.on("data", (chunk) => chunks.push(chunk));
    process.stdin.on("end", () => resolve(Buffer.concat(chunks).toString("utf-8")));
    process.stdin.on("error", reject);
  });
}

async function main() {
  const raw = await readStdin();
  let payload;
  try {
    payload = JSON.parse(raw || "{}");
  } catch (err) {
    process.stderr.write(`Invalid render plan JSON: ${err}\n`);
    process.exit(1);
    return;
  }

  const spec = payload.design_system;
  if (!spec) {
    process.stderr.write("Missing 'design_system' in render plan payload\n");
    process.exit(1);
    return;
  }
  const theme = payload.theme === "light" ? "light" : "dark";
  const slidePlans = payload.slides || [];

  const pptx = new pptxgen();
  const w = spec.meta.slide_width_inches;
  const h = spec.meta.slide_height_inches;
  pptx.defineLayout({ name: "AGENTDECK_WIDE", width: w, height: h });
  pptx.layout = "AGENTDECK_WIDE";

  for (const slidePlan of slidePlans) {
    const slide = pptx.addSlide();
    try {
      renderSlide(slide, spec, theme, slidePlan);
      addBrandLogoMark(slide, spec, slidePlan);
    } catch (err) {
      process.stderr.write(
        `Error rendering slide (slide_layout=${slidePlan && slidePlan.slide_layout}): ${err.stack || err}\n`
      );
      throw err;
    }
  }

  const buffer = await pptx.write({ outputType: "nodebuffer" });
  const repaired = await _fixShapeXmlForPowerPointCompat(buffer);
  process.stdout.write(repaired);
}

/**
 * #185: place a small brand-logo mark in the top-right corner of every
 * slide, when the design system carries one (`meta.brand_logo`, set by
 * `brand_generator.spec_from_brand_profile` from the uploaded template's
 * `BrandProfile.logo_assets`). No-op for `agentdeck_v1` and any other
 * design system without a logo -- this is purely additive and does not
 * touch `slide_layouts`/zone geometry.
 */
function addBrandLogoMark(slide, spec, slidePlan) {
  const logo = spec.meta && spec.meta.brand_logo;
  if (!logo || !logo.data_base64) return;
  // Skip on closing/section-style slides where a corner mark would clash
  // with full-bleed treatments.
  if (slidePlan && slidePlan.slide_layout === "CLOSING") return;

  const w = spec.meta.slide_width_inches;
  const margin = 0.35;
  const logoW = Math.min(logo.width_in || 1.2, 1.6);
  const logoH = Math.min(logo.height_in || 0.6, 0.8);

  slide.addImage({
    data: `data:${logo.content_type || "image/png"};base64,${logo.data_base64}`,
    x: w - margin - logoW,
    y: margin,
    w: logoW,
    h: logoH,
  });
}

/**
 * pptxgenjs omits <p:txBody> on shapes added without a `text` option (plain
 * decorative rectangles/dividers/bars), and omits <a:effectLst/> from
 * <p:bg><p:bgPr>. Per ECMA-376 both elements are optional in the schema, but
 * PowerPoint's own validator flags their absence as a "problem with content"
 * on first open and offers to "Repair" the file -- silently rewriting every
 * <p:sp> to add an empty txBody and every <p:bgPr> to add effectLst. That
 * repair prompt fires on every generated deck (any slide with a non-text
 * decorative shape). Pre-empt it here by inserting the same empty elements
 * PowerPoint would add, so the file opens cleanly without a repair dialog.
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

main().catch((err) => {
  process.stderr.write(`${err && err.stack ? err.stack : err}\n`);
  process.exit(1);
});
