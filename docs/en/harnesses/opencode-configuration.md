# OpenCode Harness

Roller runs pinned [OpenCode](https://opencode.ai/docs/cli/) **1.18.30** in a disposable Docker container. Both Task Agents and User Simulators accept `"harness": "opencode"`. The adapter uses native noninteractive runs, session continuation, and exports.

## Build and run

```bash
docker build -t roller-opencode:1.18.30 docker/opencode
export OPENAI_API_KEY='your-key'
export OPENAI_BASE_URL='https://api.example.com/v1'
python -m roller collect \
  --tasks examples/coverage/cases/formats/jsonl/opencode.jsonl \
  --model your-model-id --harness opencode \
  --parallel 2 --timeout 180 --retries 0 \
  --output runs/opencode --run-name smoke
```

The initial adapter requires OpenAI-compatible **streaming Chat Completions with tools**. The model ID reaches the API unchanged; the internal `roller/` provider prefix is added by the adapter. The image installs a versioned standalone binary with npm SHA-512 verification. The default base is `python:3.12-slim-bookworm`; `BASE_IMAGE` may select another Linux image with Python 3.10+ and apt-get. Linux x86_64 and aarch64 packages are supported. `NPM_REGISTRY` selects a compatible download mirror.

The image also prepares native plugin dependencies at build time. Each isolated container uses the image dependencies with its own configuration directory, avoiding package downloads during task startup.

## Configuration

| Option | Behavior |
| --- | --- |
| `--opencode-image` | Overrides `--docker-image`, then `ROLLER_OPENCODE_IMAGE`, then `roller-opencode:1.18.30` |
| `--opencode-api-base` | Falls back to `--api-base`, `OPENAI_BASE_URL`, then `https://api.openai.com/v1` |
| `--opencode-api-key-env` | Key variable name; falls back to `--api-key-env`, then `OPENAI_API_KEY` |
| `--opencode-max-steps` | 12 native model steps per user turn; also bounded by the rollout timeout |
| `--opencode-context-length` | 134200 tokens by default, minimum 1024; match the actual model |
| `--opencode-output-tokens` | 8192 by default, positive and smaller than the context window |
| `--thinking` / `runtime.thinking` | `off/minimal/low/medium/high/xhigh`; `off` maps to `none`; provider support is required |
| `--wire-trace` | Native requests and schemas are captured in every mode; `full` adds HTTP records and requires an explicit API base and host networking |
| `--no-keep-thinking` | Removes reasoning from ACTF/SFT only; native logs and continuation history are retained |

Dedicated options apply to both participants. The run lock includes the binary version, image digest, endpoint, key variable name, tools, token limits, step limit, and export settings. Credentials are read during execution and are not included in public configuration.

## Sessions and artifacts

Each participant gets an independent container and HOME/XDG directories. Inputs are mounted read-only. Context files are copied to declared paths and listed in the prompt. Setup, selected environment variables, output collection, and in-container grading use the shared Roller task contract.

The adapter permits bash, read, write, edit, apply_patch, glob, and grep; native model selection determines which tools are exposed. Each user turn resumes the same native session and workspace with a new CLI process. Background process handles do not survive. Auto-sharing, updates, model catalog refresh, LSP, formatters, compaction, pruning, and implicit project configuration are disabled. Context/skill coverage checks file accessibility; skill tools, MCP, subagents, and network tools are outside the initial scope.

A local plugin records the serialized provider request without rewriting it.
`trace.jsonl` contains requests, native message/part updates, and session
exports. ACTF merges native IDs and captures actual tool schemas, visible
reasoning, tool calls, and results. `opencode_export.raw.json` also preserves
the raw stdout from `opencode export` for diagnosing truncated or malformed CLI
exports.

If `opencode export` returns malformed JSON, Roller recovers from the journal
only when the CLI itself succeeded and the journal strictly reconstructs a
non-empty reply for the same session ending in `stop` or `end-turn`. Incomplete
journals, API errors, truncated replies, changing or missing tool schemas, and
nontext attachments still fail explicitly. Timeout recovery preserves
available artifacts and removes the container.

OpenCode emits no OpenClaw cache trace. Resume validates each participant separately; full mode still requires HTTP traces, and `capture: none` remains non-reusable.

## Validation

```bash
python examples/coverage/suite.py validate --case '*/opencode'
python examples/coverage/suite.py run --case '*/opencode' --exclude-case 'vision/*' --dry-run
PYTHONPATH=src python -m unittest discover -s tests
ROLLER_OPENCODE_DOCKER_TEST=1 PYTHONPATH=src \
  python -m unittest discover -s tests -p test_opencode_docker.py -v
```

Docker tests use a deterministic local streaming API and a fake key while executing real OpenCode tools. No external model charges are incurred. The suite adds 204 input tasks and 36 simulator tasks, four per group. Select 200 nonvision inputs with `--case '*/opencode' --exclude-case 'vision/*'`. Select the nine relevant pairings with `--case 'simulators/pair/opencode--*' --case 'simulators/pair/*--opencode'`; overlaps are deduplicated. See the [coverage guide](../../../examples/coverage/README.en.md).

External thinking, direct multimodal messages, visual acceptance, native account/OAuth login, custom provider SDKs, MCP, subagents, and persistent memory are not supported. Vision groups document a capability boundary and are optional. Long tasks need enough configured context because automatic compaction is disabled.
