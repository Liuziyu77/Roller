// Container-only bridge to the pinned official Pi AgentSession SDK.
import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import {
  createAgentSession, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager,
} from "/opt/roller-pi/node_modules/@earendil-works/pi-coding-agent/dist/index.js";
import { configureHttpDispatcher } from "/opt/roller-pi/node_modules/@earendil-works/pi-coding-agent/dist/core/http-dispatcher.js";

const VERSION = "0.85.1";
const STATE = "/roller/pi-state";
const agentDir = "/roller/pi-home/agent";
const redact = (value) => {
  const text = JSON.stringify(value);
  return process.env.PI_API_KEY ? text.replaceAll(process.env.PI_API_KEY, "[REDACTED]") : text;
};
const journal = (value) => fs.appendFileSync(`${STATE}/trace.jsonl`, redact(value) + "\n");

function check() {
  for (const name of ["pi-coding-agent", "pi-agent-core", "pi-ai"]) {
    const installed = JSON.parse(fs.readFileSync(`/opt/roller-pi/node_modules/@earendil-works/${name}/package.json`, "utf8")).version;
    if (installed !== VERSION) throw new Error(`${name} version mismatch: expected ${VERSION}, got ${installed}`);
  }
  return VERSION;
}

async function serve(config) {
  check();
  // Use Pi's own HTTP transport, including HTTP(S)_PROXY and NO_PROXY.
  configureHttpDispatcher(Math.max(1, Math.ceil(config.timeout_seconds * 1000)));
  journal({type: "metadata", schema: "roller.pi-events/v1", version: VERSION,
    session_id: config.session_id, model: config.model, api: "openai-completions"});
  const settingsManager = SettingsManager.inMemory({
    compaction: {enabled: false}, retry: {enabled: false, provider: {maxRetries: 0}},
    enableInstallTelemetry: false, enableAnalytics: false, enableSkillCommands: false,
    packages: [], extensions: [], skills: [], prompts: [], themes: [],
  });
  const loader = new DefaultResourceLoader({
    cwd: process.cwd(), agentDir, settingsManager,
    noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
    // Keep Pi's native system prompt and add only task-authorized Context.
    systemPromptOverride: () => undefined,
    appendSystemPromptOverride: () => [config.system_prompt],
    agentsFilesOverride: () => ({agentsFiles: (config.context_paths ?? []).filter((target) => fs.existsSync(target)).map((target) => {
      const file = path.resolve(target);
      return {path: file, content: fs.readFileSync(file, "utf8")};
    })}),
  });
  await loader.reload();
  const modelRuntime = await ModelRuntime.create({
    authPath: `${agentDir}/auth.json`, modelsPath: null,
    modelsStorePath: `${agentDir}/models-store.json`, allowModelNetwork: false, refreshOnCreate: false,
  });
  modelRuntime.registerProvider("roller", {
    api: "openai-completions", baseUrl: process.env.PI_BASE_URL, apiKey: process.env.PI_API_KEY,
    models: [{id: config.model, name: config.model, reasoning: true, input: ["text"],
      thinkingLevelMap: {off: "none", xhigh: "xhigh", max: "max"},
      cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0},
      contextWindow: config.context_length, maxTokens: config.output_tokens,
      compat: {supportsStore: false, supportsDeveloperRole: false, supportsReasoningEffort: true,
        maxTokensField: "max_tokens"},
    }],
  });
  const model = modelRuntime.getModel("roller", config.model);
  if (!model) throw new Error("Pi did not register the requested model");
  const {session} = await createAgentSession({
    cwd: process.cwd(), agentDir, modelRuntime, model, settingsManager,
    thinkingLevel: config.thinking ?? "off", tools: config.tools, resourceLoader: loader,
    sessionManager: SessionManager.inMemory(process.cwd()),
  });
  let currentTurn = 0;
  let steps = 0;
  let active = false;
  const nativeStream = session.agent.streamFunction;
  session.agent.streamFunction = async (model, context, options) => {
    if (steps >= config.max_steps) throw new Error(`Pi max steps exceeded (${config.max_steps})`);
    steps += 1;
    journal({type: "context", turn: currentTurn, context});
    return nativeStream(model, context, {...options, maxTokens: config.output_tokens, maxRetries: 0});
  };
  session.agent.onPayload = (body) => {
    journal({type: "request", turn: currentTurn, body});
    return body;
  };
  session.subscribe((event) => {
    // Final message events are authoritative. Deltas remain in the raw trace.
    journal({type: "event", turn: currentTurn, event});
  });
  const server = net.createServer((socket) => {
    socket.setEncoding("utf8");
    let buffer = "";
    let received = false;
    socket.on("error", () => {}); // The host may close the socket at its deadline.
    socket.on("data", async (chunk) => {
      buffer += chunk;
      // Split strictly on LF; prompts may contain Unicode line separators.
      const end = buffer.indexOf("\n");
      if (received || end < 0) return;
      received = true;
      if (active) {
        socket.end(redact({ok: false, error: "Pi session already has an active turn"}) + "\n");
        return;
      }
      active = true;
      let timer;
      let timedOut = false;
      try {
        const payload = JSON.parse(buffer.slice(0, end));
        if (payload.turn !== currentTurn + 1 || typeof payload.message !== "string") {
          throw new Error("Invalid Pi turn sequence or message");
        }
        currentTurn = payload.turn;
        steps = 0;
        const before = session.messages.length;
        journal({type: "turn_start", turn: currentTurn});
        timer = setTimeout(() => {
          timedOut = true;
          void session.abort();
        }, Math.max(1, payload.timeout_seconds * 1000));
        await session.prompt(payload.message, {expandPromptTemplates: false});
        const messages = session.messages.slice(before);
        const assistants = messages.filter((message) => message.role === "assistant");
        const last = assistants.at(-1);
        const content = last?.content.filter((block) => block.type === "text").map((block) => block.text).join("\n");
        const failure = assistants.find((message) => !["stop", "toolUse"].includes(message.stopReason));
        if (timedOut) throw new Error("Pi turn timed out");
        if (failure || last?.stopReason !== "stop" || !content?.trim()) {
          throw new Error(failure?.errorMessage || `Pi turn did not complete: ${failure?.stopReason ?? last?.stopReason ?? "empty response"}`);
        }
        journal({type: "turn_end", turn: currentTurn});
        socket.end(redact({ok: true, content}) + "\n");
      } catch (error) {
        journal({type: "runner_error", turn: currentTurn, error: String(error)});
        socket.end(redact({ok: false, error: String(error), timeout: timedOut}) + "\n");
        server.close();
        session.dispose();
      } finally {
        clearTimeout(timer);
        active = false;
      }
    });
  });
  server.on("error", (error) => {
    journal({type: "runner_error", error: String(error)});
    process.exitCode = 1;
    session.dispose();
  });
  server.listen(`${STATE}/runner.sock`, () => fs.chmodSync(`${STATE}/runner.sock`, 0o600));
}

try {
  if (process.argv[2] === "--check") console.log(check());
  else if (process.argv[2] === "--serve") await serve(JSON.parse(fs.readFileSync(process.argv[3], "utf8")));
  else throw new Error("Expected --check or --serve <config>");
} catch (error) {
  if (process.argv[2] !== "--check") journal({type: "runner_error", error: String(error)});
  console.error(redact({error: String(error)}));
  process.exitCode = 1;
}
