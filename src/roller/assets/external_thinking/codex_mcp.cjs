#!/usr/bin/env node

"use strict";

const readline = require("node:readline");

const TOOL_NAME = "deep_think";
const PLACEHOLDER = "------";
const input = readline.createInterface({
  input: process.stdin,
  crlfDelay: Infinity,
});

function send(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

function result(id, value) {
  send({ jsonrpc: "2.0", id, result: value });
}

function error(id, code, message) {
  send({ jsonrpc: "2.0", id, error: { code, message } });
}

input.on("line", (line) => {
  if (!line.trim()) return;
  let message;
  try {
    message = JSON.parse(line);
  } catch {
    return;
  }

  if (message.method === "initialize") {
    result(message.id, {
      protocolVersion: message.params?.protocolVersion || "2024-11-05",
      capabilities: { tools: {} },
      serverInfo: { name: "roller-external-thinking", version: "1.0.0" },
    });
    return;
  }
  if (message.method === "ping") {
    result(message.id, {});
    return;
  }
  if (message.method === "tools/list") {
    result(message.id, {
      tools: [
        {
          name: TOOL_NAME,
          description:
            "Record detailed reasoning and intermediate work for Roller SFT export.",
          inputSchema: {
            type: "object",
            properties: {
              thoughts: {
                type: "string",
                description: "Detailed reasoning and intermediate work.",
              },
            },
            required: ["thoughts"],
            additionalProperties: false,
          },
          annotations: {
            title: "Deep Think",
            readOnlyHint: true,
            destructiveHint: false,
            idempotentHint: true,
            openWorldHint: false,
          },
        },
      ],
    });
    return;
  }
  if (message.method === "tools/call") {
    const thoughts = message.params?.arguments?.thoughts;
    if (message.params?.name !== TOOL_NAME) {
      error(message.id, -32602, `Unknown tool: ${message.params?.name}`);
    } else if (typeof thoughts !== "string" || !thoughts.trim()) {
      error(message.id, -32602, "deep_think requires non-empty thoughts");
    } else {
      result(message.id, {
        content: [{ type: "text", text: PLACEHOLDER }],
        isError: false,
      });
    }
    return;
  }
  if (Object.prototype.hasOwnProperty.call(message, "id")) {
    error(message.id, -32601, `Method not found: ${message.method}`);
  }
});
