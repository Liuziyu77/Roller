module.exports = {
  id: "roller-external-thinking",
  name: "Roller External Thinking",
  description: "Local scratchpad tool used by Roller's explicit reasoning protocol",
  register(api) {
    api.registerTool({
      name: "deep_think",
      label: "Deep Think",
      description:
        "Record detailed reasoning and intermediate work. Follow the injected reasoning policy for when to call this tool.",
      parameters: {
        type: "object",
        properties: {
          thoughts: {
            type: "string",
            description: "Detailed reasoning or intermediate work.",
          },
        },
        required: ["thoughts"],
        additionalProperties: false,
      },
      execute: async () => ({
        content: [{ type: "text", text: "------" }],
        details: { local: true },
      }),
    });
  },
};
