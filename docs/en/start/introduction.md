# Introduction

Roller is a command-line tool for running tool-using AI agents in isolated environments and keeping a complete record of what happened. You provide tasks, model access, and a Harness. Roller runs each task and model combination in a disposable Docker container, then copies the trace and any requested files back to the host.

This makes repeated agent runs easier to compare, inspect, and reuse. A team can use the same task set to evaluate several models, review tool calls, build training data, or reproduce a failed run.

## 1. The problem Roller solves

An agent run contains more than its final answer. It may also depend on:

- the prompt, input files, and workspace instructions.
- the model and API protocol.
- the tools exposed by the agent runtime.
- reasoning returned by the model.
- tool calls, tool results, and files written during the task.
- timeout, retry, and version settings.

Saving only the final response loses most of that context. Roller keeps the task definition, runtime settings, native events, normalized trace, and file outputs together under one run directory.

## 2. How a run works

1. **Describe the work.** Write one or more Tasks in JSON or JSONL. A Task can contain a prompt, input files, Context files, setup commands, and an output directory.
2. **Choose how the agent runs.** Select a Harness and a model. The Harness starts the agent runtime and connects it to its tools and model API.
3. **Check the plan.** A dry-run validates the tasks and shows which rollouts Roller will create.
4. **Run in isolation.** Every Task and model combination gets its own Docker container.
5. **Inspect or reuse the result.** Open the run in the Viewer, compare traces, or collect the generated SFT records.

~~~text
Tasks + models + Harness
            │
            ▼
    isolated rollouts
            │
            ├─ native trace
            ├─ normalized ACTF trace
            ├─ SFT sample
            └─ generated files and run metadata
~~~

## 3. Five terms used throughout the handbook

| Term | Meaning |
| --- | --- |
| **Task** | The work given to the agent, together with its inputs, Context, setup, and expected output location. |
| **Harness** | The agent runtime integration that starts the agent, exposes tools, and talks to the model service. |
| **Rollout** | One attempt to run one Task with one model. |
| **Run** | A named batch containing one or more rollouts. |
| **Trace** | The recorded conversation, reasoning, tool calls, and tool results from a rollout. |

You do not need to memorize the file formats before your first run. The [Getting started](getting-started.md) guide uses OpenClaw to produce one trajectory and open it in the Viewer.

## 4. What Roller produces

A successful rollout normally contains:

| Output | Why it exists |
| --- | --- |
| <code>trace.jsonl</code> | Keeps the Harness-native events for debugging and future conversion. |
| <code>trace_ACTF.json</code> | Presents different Harnesses in one analysis and visualization format. |
| <code>trace_SFT.json</code> | Stores one standard sample for training-data workflows. |
| <code>results/</code> | Contains files that the Task asked the agent to create. |
| <code>info.json</code> | Records status, timing, usage, versions, and provenance. |

The run also contains a combined <code>sft.jsonl</code> for successful samples. See [Trajectory outputs](../traces/trajectory-outputs.md) when you need the exact retention and failure rules.

## 5. Choosing a Harness

A Harness determines which agent runtime and tools perform the task. Roller currently supports OpenClaw, Codex, Claude Code, Hermes, OpenCode, DeepSeek Harness, and Pi Agent.

Start with **OpenClaw** if you are learning Roller and do not already need a specific agent runtime. The first-run guide uses it because it supports both OpenAI Responses and Chat Completions style services. When you need another runtime, follow its dedicated Harness guide.

## 6. Where to go next

- Run Roller for the first time: [Getting started](getting-started.md)
- Define your own inputs and prompts: [Task format](../tasks/task-format.md)
- Choose or configure an agent runtime: [Harness setup](../harnesses/openclaw-configuration.md)
- Understand every generated file: [Trajectory outputs](../traces/trajectory-outputs.md)
