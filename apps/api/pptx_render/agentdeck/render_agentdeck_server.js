#!/usr/bin/env node
/**
 * Warm AgentDeck renderer.
 *
 * JSONL protocol over stdio:
 *   stdin line:  { "id": "...", "payload": <PptxRenderPlan payload> }
 *   stdout line: { "id": "...", "ok": true, "pptx_base64": "..." }
 *            or { "id": "...", "ok": false, "error": "..." }
 *
 * Keeping pptxgenjs and layout modules loaded avoids the cold Node startup
 * cost for every render/repair iteration. The Python caller owns request
 * serialization with a process-level lock.
 */

const readline = require("readline");
const { renderPayload } = require("./render_agentdeck");

const rl = readline.createInterface({
  input: process.stdin,
  crlfDelay: Infinity,
});

rl.on("line", async (line) => {
  if (!line.trim()) return;
  let request;
  try {
    request = JSON.parse(line);
  } catch (err) {
    process.stdout.write(JSON.stringify({
      id: null,
      ok: false,
      error: `Invalid request JSON: ${err && err.message ? err.message : err}`,
    }) + "\n");
    return;
  }

  const id = request.id || null;
  try {
    const buffer = await renderPayload(request.payload || {});
    process.stdout.write(JSON.stringify({
      id,
      ok: true,
      pptx_base64: buffer.toString("base64"),
    }) + "\n");
  } catch (err) {
    process.stdout.write(JSON.stringify({
      id,
      ok: false,
      error: err && err.stack ? err.stack : String(err),
    }) + "\n");
  }
});
