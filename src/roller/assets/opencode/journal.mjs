import { appendFileSync, readFileSync } from "node:fs";

// A local plugin, loaded before provider construction. Capture the serialized
// provider request without copying credentials or modifying the HTTP exchange.
export default async function RollerJournal() {
  const path = process.env.ROLLER_OPENCODE_JOURNAL;
  if (!path) throw new Error("Roller journal path is missing");
  const turn = JSON.parse(readFileSync("/roller/opencode-state/turn.json", "utf8"));
  const write = (value) => appendFileSync(path, JSON.stringify({
    timestamp: new Date().toISOString(), ...value,
  }) + "\n");
  return {
    "experimental.chat.system.transform": async (_input, output) => {
      // Retain OpenCode's native instructions and append the task contract.
      output.system.push(turn.system_prompt);
    },
    config: async (config) => {
      const provider = config.provider?.roller;
      if (!provider) throw new Error("Roller provider is missing");
      provider.options.fetch = async (input, init) => {
        const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
        if (body && Array.isArray(body.messages)) write({ type: "request", body });
        return fetch(input, init);
      };
      write({ type: "bridge_ready", version: "1.18.30" });
    },
    event: ({ event }) => {
      if (["message.updated", "message.part.updated", "session.error"].includes(event.type)) {
        write({ type: "event", event });
      }
    },
  };
}
