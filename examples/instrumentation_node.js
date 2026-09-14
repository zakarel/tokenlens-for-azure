// Optional manual instrumentation. No prompts, responses, headers, or tool
// arguments are written; rotate TOKENLENS_EVENTS_PATH in the host process.
const fs = require("node:fs");
const crypto = require("node:crypto");

const path = process.env.TOKENLENS_EVENTS_PATH || "tokenlens-events.jsonl";
function emit(event) {
  const payload = {
    schema_version: 2,
    event_id: crypto.randomUUID(),
    timestamp: new Date().toISOString(),
    ...event,
  };
  fs.appendFileSync(path, JSON.stringify(payload) + "\n", { encoding: "utf8" });
}

module.exports = { emit };
