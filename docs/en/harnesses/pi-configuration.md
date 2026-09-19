# Pi Agent Harness guide

This page covers only Pi-specific images, the API, the native session, and troubleshooting. Roller runs the official [Pi Coding Agent SDK](https://pi.dev/docs/latest/sdk), pinning `@earendil-works/pi-coding-agent`, `pi-agent-core`, and `pi-ai` to **0.85.1**. Its native `AgentSession` owns the model loop and tools. For Task fields see [Task format reference](../tasks/task-format.md). For output semantics shared by all Harnesses see [Trajectory output file guide](../traces/trajectory-outputs.md).

> Jump to: supported scope (section 1) · image and run (section 2) · configuration (section 3) · session and tools (section 4) · trace outputs (section 5) · verification (section 6) · troubleshooting (section 7)

## 1. Current supported scope

The Pi Harness supports:

- single-turn `prompt` and predefined multi-turn `messages` (one native `AgentSession` per participant across turns);
- Task Agent and User Simulator, both accepting `"harness": "pi"`;
- Task Bundle, read-only inputs, Context, setup, explicit environment variables, custom `output_dir`, artifact collection, and grading before container teardown;
- streaming **OpenAI Chat Completions** with tool calls;
- seven native tools: `read`, `bash`, `edit`, `write`, `grep`, `find`, `ls`;
- ACTF, SFT, reasoning export, and usage (cache tokens counted separately);
- `--wire-trace actf|full|off`;
- `--thinking off/minimal/low/medium/high/xhigh/max` (sent as `reasoning_effort`).

Not currently supported:

- visual acceptance and direct multimodal messages;
- interactive login;
- other Pi provider protocols;
- custom extensions/plugins;
- automatic compaction;
- the OpenClaw cache trace (Resume does not require it).

## 2. Build and run

```bash
docker build -t roller-pi:0.85.1 docker/pi
export OPENAI_API_KEY='your-key'
export OPENAI_BASE_URL='https://api.example.com/v1'

python -m roller collect \
  --tasks examples/coverage/cases/formats/jsonl/pi.jsonl \
  --harness pi --model your-model \
  --thinking off --parallel 2 --timeout 180 --retries 0 \
  --output runs/pi --run-name smoke
```

This adapter supports streaming **OpenAI Chat Completions** with tool calls. Model IDs reach the API unchanged, including IDs containing `/`. The host needs Roller, Python 3.10+, and Docker; Node and the SDK run inside the container. The default base is `node:22.22.0-bookworm-slim`. A `BASE_IMAGE` override must provide Node 22.19+ and npm on Debian/Ubuntu.

The image also installs the official `fd 10.2.0` binary with SHA-256 verification for the native `find` tool. Runtime tool downloads remain disabled.

For `DEBIAN_MIRROR` and `DEBIAN_SECURITY_MIRROR`, see [Docker builds and package mirrors](../start/docker-builds.md). Set `NPM_REGISTRY` to override npm downloads, or leave it unset to keep the existing npm configuration.

(pi-cli-flags)=
## 3. Configuration

| Flag | Default and behavior |
| --- | --- |
| `--pi-image` | Precedes `--docker-image`, `ROLLER_PI_IMAGE`, and `roller-pi:0.85.1` |
| `--pi-api-base` | Falls back to `--api-base`, `OPENAI_BASE_URL`, then `https://api.openai.com/v1` |
| `--pi-api-key-env` | Falls back to `--api-key-env`, then `OPENAI_API_KEY`; supply a variable name |
| `--pi-max-steps` | At most 12 model requests per user message, also bounded by task/turn deadlines |
| `--pi-context-length` | 128000, at least 1024; match the actual model service |
| `--pi-output-tokens` | 8192; positive and smaller than the context window; sent as `max_tokens` |
| `--thinking` / `runtime.thinking` | `off/minimal/low/medium/high/xhigh/max`; defaults to `off`, with `none` as an alias |
| `--wire-trace` | Every mode journals native requests; `full` additionally captures HTTP requests and requires an explicit API base and host networking |
| `--no-keep-thinking` | Removes reasoning only from ACTF/SFT; raw events and continuation history retain it |

Thinking is sent as `reasoning_effort`: `off` becomes `none`, and other levels retain their names. The endpoint must support the selected value. Vendor-specific thinking protocols are not translated automatically.

Pi-specific flags configure both participant roles. Mixed runs can specify separate API settings and images for other Harnesses. Run locks record versions, image digests, tools, connection settings, and execution bounds. Resume validates each participant's artifacts.

## 4. Sessions, Context, and tools

Each participant has its own container and `PI_CODING_AGENT_DIR`, retaining one native `AgentSession` across turns. Conversation history and workspace files persist. Pi starts a separate Shell for each Bash call: `export` and `cd` do not persist across calls. Store persistent state in files or combine related operations in a single command.

Inputs are mounted read-only. Context is copied to its declared paths; non-skill Context is added to the native system prompt through the official loader. Skill files remain accessible but are not automatically registered as Pi skills. Setup, explicit environment variables, custom output directories, artifact collection, and grading before container teardown are supported.

Automatic compaction, session/provider retries, telemetry, remote model catalog updates, and extension discovery are disabled. Roller preserves Pi's native system prompt and appends workspace, output, and Context guidance.

## 5. Trace outputs

`trace.jsonl` uses `roller.pi-events/v1`, containing native events, model contexts, actual requests, and Roller turn boundaries. ACTF/SFT use the captured system prompt and tool schemas and preserve text, reasoning, tool arguments, and results. Cache tokens are counted separately.

Empty replies, truncation, API errors, step limits, and unmatched tool calls do not produce successful trajectories. Conversion rejects changes to the system prompt/tool schemas and non-text content that would be lost. Timeouts stop the container and recover existing output files. Pi does not require an OpenClaw cache trace.

The initial scope is text and file tasks; it does not expose every feature of the underlying Pi product.

## 6. Reusable verification

```bash
python examples/coverage/suite.py validate --case '*/pi'
python examples/coverage/suite.py run --case '*/pi' \
  --exclude-case 'vision/*' --dry-run
ROLLER_PI_DOCKER_TEST=1 PYTHONPATH=src \
  python -m unittest discover -s tests -p test_pi_docker.py -v
```

Docker tests use a deterministic local streaming endpoint and a fake key with the real Pi SDK and tools, consuming no model quota. Public input coverage has 204 tasks, including 200 nonvisual tasks. The 13 Agent × Simulator pairings involving Pi add 52 tasks; select them with `--case 'simulators/pair/pi--*' --case 'simulators/pair/*--pi'`. The vision group records the initial capability boundary.

With a DeepSeek image available, also set `ROLLER_PI_MIXED_DOCKER_TEST=1` to verify Pi ↔ DeepSeek pairings and Resume in both directions.

See [Coverage](https://github.com/Liuziyu77/Roller/blob/main/examples/coverage/README.en.md). Offline configuration validation, real-container tests, and external model acceptance are distinct verification layers.

## 7. Troubleshooting

Common errors:

- a trajectory is not counted as successful: empty replies, truncation, API errors, step limits, and unmatched tool calls all fail; inspect `trace.jsonl` to locate the cause;
- the converter rejects a trajectory: changes to the system prompt or tool schemas, and non-text content that would be lost, are rejected;
- Bash state disappears: Pi starts a separate Shell per Bash call, so `export`/`cd` do not persist; store persistent state in files or complete the work in a single command;
- long tasks exceed the context window: automatic compaction is disabled; raise `--pi-context-length` to match the real model service;
- rollout timeout: the container is stopped and existing output files recovered; adjust `--timeout` or `--pi-max-steps` as needed;
- `--wire-trace full` fails to start: an explicit API base and `--network host` are required.
