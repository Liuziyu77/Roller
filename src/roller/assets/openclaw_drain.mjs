// Use the pinned native registry's pending-descendant semantics. Completion
// includes announcement cleanup, not merely the child's final model response.
import fs from "node:fs";
import path from "node:path";
const files = fs.readdirSync("/app/dist").filter(n => /^subagent-registry-state-.*\.js$/.test(n));
if (files.length !== 1) throw new Error("Pinned OpenClaw registry capture hook unavailable");
const registry = await import(path.join("/app/dist", files[0]));
const sid = process.env.ROLLER_CAPTURE_ROOT_SESSION;
const deadline = Date.now() + Number(process.env.ROLLER_CAPTURE_TIMEOUT_MS);
while (true) {
  const cache = fs.existsSync("/tmp/roller-cache-trace.jsonl")
    ? fs.readFileSync("/tmp/roller-cache-trace.jsonl", "utf8").split("\n").filter(Boolean).map(JSON.parse) : [];
  const key = cache.find(r => r.sessionId === sid)?.sessionKey;
  const runs = new Map();
  registry.i({runs, mergeOnly: false});
  fs.writeFileSync("/tmp/roller-subagent-runs.json", JSON.stringify([...runs.values()]));
  if (!key || registry._(runs, key) === 0) break;
  if (Date.now() >= deadline) throw new Error("Timed out draining OpenClaw subagents and announcements");
  await new Promise(resolve => setTimeout(resolve, 200));
}
