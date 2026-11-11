# Roller documentation

Roller runs tool-using AI agents in disposable Docker containers and records the full process: prompts, reasoning, tool calls, tool results, generated files, and run metadata. It is designed for repeatable data collection and evaluation across different models and agent runtimes.

New users should read the [Introduction](start/introduction.md) first. It explains the main concepts and requires no setup. Then follow [Getting started](start/getting-started.md) to create a first trajectory.

## What Roller does

Roller takes a set of Tasks, one or more models, and a Harness as input. It creates an isolated rollout for every Task and model combination, then writes the results to a structured run directory.

~~~text
tasks.jsonl × models
        │
        ▼
Agent Harness in Docker
        │
        ├─ trace.jsonl       Harness-native events
        ├─ trace_ACTF.json   unified analysis trace
        ├─ trace_SFT.json    standard training sample
        ├─ results/          files produced by the agent
        └─ info.json         status, usage, and provenance
~~~

Common uses include:

- collecting single-turn, multi-turn, and tool-using Agent trajectories.
- comparing models or Harnesses on the same Tasks.
- reviewing reasoning, tool calls, tool results, and generated files.
- preparing normalized traces or SFT JSONL.
- resuming interrupted collection and inspecting results in the built-in Viewer.

Roller requires Python 3.10+. The project currently uses Task schema <code>roller.task/v1</code>. It is in Alpha and licensed under Apache 2.0. Source: [GitHub](https://github.com/Liuziyu77/Roller).

## Choose a Harness

The first-run guide uses OpenClaw. That path is sufficient until one successful trajectory has been produced. The other Harnesses apply when a specific agent runtime or API protocol is required.

| Harness | Model API | Typical use |
| --- | --- | --- |
| OpenClaw (default) | OpenAI Responses or Chat Completions | Custom/open models, multimodal input, and dynamic User Simulator |
| Codex | Responses | Codex CLI tool trajectories |
| Claude Code | Anthropic <code>/v1/messages</code> | Claude Code tools and native thinking |
| Hermes | OpenAI Chat Completions | Native request and tool-message traces |
| OpenCode | OpenAI Chat Completions | Native CLI sessions and file tools |
| DeepSeek Harness | DeepSeek Chat Completions | Native SDK sessions and persistent Bash |
| Pi Agent | OpenAI Chat Completions | Native AgentSession and coding tools |

All seven use the same Task, Sample, trace, and SFT formats, as well as the same Viewer, retry, and Resume workflow. Their images, APIs, tools, and reasoning options differ.

## Where to begin

1. Read [Introduction](start/introduction.md) to understand Task, Harness, rollout, run, and trace.
2. Follow [Getting started](start/getting-started.md) to install Roller, run a dry-run, collect one batch, and open the Viewer.
3. Use [Task format](tasks/task-format.md) when writing custom Tasks.
4. Read the matching Harness guide when a runtime other than the OpenClaw example is required.
5. Use [Trajectory outputs](traces/trajectory-outputs.md) for the exact trace, SFT, wire-trace, or failure-retention rules.

~~~{toctree}
:maxdepth: 1
:caption: Getting started

start/introduction
start/getting-started
start/cli-reference
start/docker-builds
~~~

~~~{toctree}
:maxdepth: 1
:caption: Harness setup

harnesses/openclaw-configuration
harnesses/codex-configuration
harnesses/claude-code-configuration
harnesses/hermes-configuration
harnesses/opencode-configuration
harnesses/deepseek-configuration
harnesses/pi-configuration
~~~

~~~{toctree}
:maxdepth: 1
:caption: Tasks and runs

tasks/task-format
tasks/task-bundle-and-storage
tasks/user-simulator
~~~

~~~{toctree}
:maxdepth: 1
:caption: Traces and training data

traces/trajectory-outputs
traces/sft-format
traces/viewer
~~~

~~~{toctree}
:maxdepth: 1
:caption: Bench

benches/bench-format
benches/wildclawbench
benches/skillsbench
benches/pinchbench
benches/qwenclawbench
~~~
