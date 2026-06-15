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
    } catch (err) {
      process.stderr.write(
        `Error rendering slide (slide_layout=${slidePlan && slidePlan.slide_layout}): ${err.stack || err}\n`
      );
      throw err;
    }
  }

  const buffer = await pptx.write({ outputType: "nodebuffer" });
  process.stdout.write(buffer);
}

main().catch((err) => {
  process.stderr.write(`${err && err.stack ? err.stack : err}\n`);
  process.exit(1);
});
