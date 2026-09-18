# Roller documentation

Roller collects agent trajectories across models and harnesses. Each task/model pair runs in its own disposable Docker container and produces:

- `trace.jsonl`: harness-native raw trace
- `trace_ACTF.json`: unified analysis and visualization format (`ACTF_v1.0`)
- `trace_SFT.json`: one standard training sample
- `results/`: files produced by the agent
- `info.json`: status, usage, and run provenance

```text
tasks.jsonl × models
        │
        ▼
Agent harness in Docker
        │
        ├─ trace.jsonl       harness-native raw trace
        ├─ trace_ACTF.json   unified analysis format
        ├─ trace_SFT.json    one standard training sample
        ├─ results/          files produced by the agent
        └─ info.json         status, usage, and provenance
```

Roller requires Python 3.10+ and uses task schema `roller.task/v1` and trace format `ACTF_v1.0`. The project is in Alpha and licensed under Apache 2.0. Source: [GitHub](https://github.com/Liuziyu77/Roller).

## What it is for

- Batch-collect single-turn, multi-turn, or tool-using agent traces
- Compare multiple models or harnesses on the same tasks
- Emit SFT JSONL whose top level is strictly `messages` and `tools`
- Keep reasoning, tool calls, tool results, and task file artifacts
- Check run configuration with run locks and continue collection with retries and resume
- Inspect each task's reasoning and tool operations in the built-in Viewer

## Choose a harness

| Harness | Model API | Best for | Capabilities and limits |
| --- | --- | --- | --- |
| OpenClaw (default) | OpenAI Responses or Chat Completions | Custom/open models, dynamic User Simulator, multimodal input | Models are configured in JSON; preserves native reasoning |
| Codex | Responses | Codex CLI tool traces, GPT and Responses-compatible services | Preserves native reasoning; can be the task agent or the User Simulator |
| Claude Code | Anthropic `/v1/messages` | Claude Code tool traces and native thinking | Preserves native thinking; can be the task agent or the User Simulator |
| Hermes | OpenAI Chat Completions | Native request/message journal → ACTF/SFT | Local terminal/file tools; Agent and Simulator |
| OpenCode | OpenAI Chat Completions | Native CLI sessions and tools → ACTF/SFT | Pinned version; Task Agent or User Simulator |
| DeepSeek Harness | DeepSeek Chat Completions | Native SDK / persistent Bash → ACTF/SFT | sdk-minimal; Agent / Simulator |
| Pi Agent | OpenAI Chat Completions | Native AgentSession and seven tools → ACTF/SFT | Pinned SDK; Agent / Simulator |

All seven use the same Task, Sample, ACTF, and SFT formats, along with a shared Viewer, retry, and Resume workflow. Images, APIs, and reasoning flags differ; see the matching harness guide.

## Suggested reading order

| Stage | Read | Done when |
| --- | --- | --- |
| 1. Run once | [Getting started](start/getting-started.md) | Dry-run, collect, and open the Viewer |
| 2. Pick a harness | [OpenClaw](harnesses/openclaw-configuration.md), [Codex](harnesses/codex-configuration.md), or [Claude Code](harnesses/claude-code-configuration.md), [Hermes](harnesses/hermes-configuration.md) · [OpenCode](harnesses/opencode-configuration.md) · [DeepSeek](harnesses/deepseek-configuration.md) · [Pi Agent](harnesses/pi-configuration.md) | Image, API protocol, and reasoning flags match |
| 3. Write tasks | [Task format](tasks/task-format.md) | You can declare prompt/messages, inputs, context, and an output directory |
| 4. Check outputs | [Trajectory outputs](traces/trajectory-outputs.md) | You can tell raw, ACTF, SFT, wire, and results apart |
| 5. Run at scale | [Task bundles and storage](tasks/task-bundle-and-storage.md) | You understand snapshots, run locks, retry, and Resume |

For runnable commands, see [Examples](start/examples.md).

```{toctree}
:maxdepth: 1
:caption: Getting started

start/getting-started
start/examples
start/cli-reference
```

```{toctree}
:maxdepth: 1
:caption: Harness setup

harnesses/openclaw-configuration
harnesses/codex-configuration
harnesses/claude-code-configuration
harnesses/hermes-configuration
harnesses/opencode-configuration
harnesses/deepseek-configuration
harnesses/pi-configuration
```

```{toctree}
:maxdepth: 1
:caption: Tasks and runs

tasks/task-format
tasks/task-bundle-and-storage
tasks/user-simulator
```

```{toctree}
:maxdepth: 1
:caption: Traces and training data

traces/trajectory-outputs
traces/sft-format
traces/viewer
```

```{toctree}
:maxdepth: 1
:caption: Bench

benches/bench-format
benches/wildclawbench
benches/skillsbench
```
