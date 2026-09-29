# Pi Agent Harness guide

This page covers only Pi-specific images, the API, the native session, and troubleshooting. Roller runs the official [Pi Coding Agent SDK](https://pi.dev/docs/latest/sdk), pinning `@earendil-works/pi-coding-agent`, `pi-agent-core`, and `pi-ai` to **0.85.1**. Its native `AgentSession` owns the model loop and tools. For Task fields see [Task format reference](../tasks/task-format.md). For output semantics shared by all Harnesses see [Trajectory output file guide](../traces/trajectory-outputs.md).

## 1. Supported scope

The Pi Harness supports:

- single-turn `prompt` and predefined multi-turn `messages` (one native `AgentSession` per participant across turns).
- Task Agent and User Simulator, both accepting `"harness": "pi"`.
- Task Bundle, read-only inputs, Context, setup, explicit environment variables, custom `output_dir`, artifact collection, and grading before container teardown.
- streaming **OpenAI Chat Completions** with tool calls.
- seven native tools: `read`, `bash`, `edit`, `write`, `grep`, `find`, `ls`.
- local-image loading through `read` when the model and service support image input.
- ACTF, SFT, reasoning export, and usage (cache tokens counted separately).
- `--wire-trace actf|full|off`.
- `--thinking off/minimal/low/medium/high/xhigh/max` (sent as `reasoning_effort`).

Not currently supported:

- direct multimodal task messages and video.
- interactive login.
- other Pi provider protocols.
- custom extensions/plugins.
- automatic compaction.
- the OpenClaw cache trace (Resume does not require it).

## 2. Image preparation and first run

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

This adapter supports streaming **OpenAI Chat Completions** with tool calls. Model IDs reach the API unchanged, including IDs containing `/`. The host needs Roller, Python 3.10+, and Docker. Node and the SDK run inside the container. The default base is `node:22.22.0-bookworm-slim`. A `BASE_IMAGE` override must provide Node 22.19+ and npm on Debian/Ubuntu.

The image also installs the official `fd 10.2.0` binary with SHA-256 verification for the native `find` tool. Runtime tool downloads remain disabled.

For `DEBIAN_MIRROR` and `DEBIAN_SECURITY_MIRROR`, see [Docker builds and package mirrors](../start/docker-builds.md). Set `NPM_REGISTRY` to override npm downloads, or leave it unset to keep the existing npm configuration.

## 3. Environment variables and API

The Pi Harness connects to an OpenAI Chat Completions service with streaming and tool-call support. A first run needs an API address, an API key, and a model ID.

| Setting | Default source | CLI override |
| --- | --- | --- |
| API address | `OPENAI_BASE_URL`, then `https://api.openai.com/v1` | `--pi-api-base`, or `--api-base` for a single-Harness run |
| API key | `OPENAI_API_KEY` | `--pi-api-key-env`, or `--api-key-env` for a single-Harness run |
| Model ID | The task `model` or `--model` | Sent to the service unchanged |

The value of `--pi-api-key-env` or `--api-key-env` is the name of a host environment variable, not the key itself. The key is read at execution time and is not written to public configuration.

(pi-cli-flags)=
## 4. Pi-specific flags

This table lists Pi-specific runtime flags only. See section 3 for API addresses and keys. See the [CLI reference](../start/cli-reference.md) for flags shared by all Harnesses.

| Flag | Default and behavior |
| --- | --- |
| `--pi-image` | Precedes `--docker-image`, `ROLLER_PI_IMAGE`, and `roller-pi:0.85.1` |
| `--pi-max-steps` | At most 12 model requests per user message, also bounded by task/turn deadlines |
| `--pi-context-length` | 128000, at least 1024. Match the actual model service |
| `--pi-output-tokens` | 8192. Positive and smaller than the context window. Sent as `max_tokens` |
| `--thinking` / `runtime.thinking` | `off/minimal/low/medium/high/xhigh/max`. Defaults to `off`, with `none` as an alias |
| `--wire-trace` | Every mode journals native requests. `full` additionally captures HTTP requests and requires an explicit API base and host networking |
| `--no-keep-thinking` | Removes reasoning only from ACTF/SFT. Raw events and continuation history retain it |

Thinking is sent as `reasoning_effort`: `off` becomes `none`, and other levels retain their names. The endpoint must support the selected value. Vendor-specific thinking protocols are not translated automatically.

Pi-specific flags configure both participant roles. Mixed runs can specify separate API settings and images for other Harnesses. Run locks record versions, image digests, tools, connection settings, and execution bounds. Resume validates each participant's artifacts.

## 5. Sessions, workspace, and tools

Each participant has its own container and `PI_CODING_AGENT_DIR`, retaining one native `AgentSession` across turns. Conversation history and workspace files persist. Pi starts a separate Shell for each Bash call: `export` and `cd` do not persist across calls. Store persistent state in files or combine related operations in a single command.

The seven native tools are `read`, `bash`, `edit`, `write`, `grep`, `find`, and `ls`. Inputs are mounted read-only. When the selected model supports images, native image results from `read` are sent to the model and preserved in raw, ACTF, and multimodal SFT. Context is copied to its declared paths. Non-skill Context is added to the native system prompt through the official loader. Skill files remain accessible but are not automatically registered as Pi skills. Setup, explicit environment variables, custom output directories, artifact collection, and grading before container teardown are supported.

Automatic compaction, session/provider retries, telemetry, remote model catalog updates, and extension discovery are disabled. Roller preserves Pi's native system prompt and appends workspace, output, and Context guidance.

## 6. Trace outputs

`trace.jsonl` uses `roller.pi-events/v1`, containing native events, model contexts, actual requests, and Roller turn boundaries. ACTF/SFT use the captured system prompt and tool schemas and preserve text, reasoning, tool arguments, and results. Cache tokens are counted separately.

Empty replies, truncation, API errors, step limits, and unmatched tool calls do not produce successful trajectories. Conversion rejects changes to the system prompt/tool schemas and non-text content that would be lost. Timeouts stop the container and recover existing output files. Pi does not require an OpenClaw cache trace.

Roller supports Pi text and file tasks. When the model and OpenAI-compatible service support image input, it also supports opening local images through `read`. Direct user-message image attachments, video, interactive login, other Pi provider protocols, custom extensions/plugins, and automatic compaction remain unsupported.

## 7. Validation

```bash
python examples/coverage/suite.py validate --case '*/pi'
python examples/coverage/suite.py run --case '*/pi' --dry-run
ROLLER_PI_DOCKER_TEST=1 PYTHONPATH=src \
  python -m unittest discover -s tests -p test_pi_docker.py -v
```

Docker tests use a deterministic local streaming endpoint and a fake key with the real Pi SDK and tools, consuming no model quota. Public input coverage has 204 tasks, including four vision tasks. The 13 Agent × Simulator pairings involving Pi add 52 tasks. Select them with `--case 'simulators/pair/pi--*' --case 'simulators/pair/*--pi'`. External vision tests also require a model and service that support image input.

With a DeepSeek image available, also set `ROLLER_PI_MIXED_DOCKER_TEST=1` to verify Pi ↔ DeepSeek pairings and Resume in both directions.

See [Coverage](https://github.com/Liuziyu77/Roller/blob/main/examples/coverage/README.en.md). Offline configuration validation, real-container tests, and external model acceptance are distinct verification layers.

## 8. Common issues

Common errors:

- a trajectory is not counted as successful: empty replies, truncation, API errors, step limits, and unmatched tool calls all fail. Inspect `trace.jsonl` to locate the cause.
- the converter rejects a trajectory: changes to the system prompt or tool schemas, and non-text content that would be lost, are rejected.
- Bash state disappears: Pi starts a separate Shell per Bash call, so `export`/`cd` do not persist. Store persistent state in files or complete the work in a single command.
- long tasks exceed the context window: automatic compaction is disabled. Raise `--pi-context-length` to match the real model service.
- rollout timeout: the container is stopped and existing output files recovered. Adjust `--timeout` or `--pi-max-steps` as needed.
- `--wire-trace full` fails to start: an explicit API base and `--network host` are required.
