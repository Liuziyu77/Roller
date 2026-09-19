# OpenCode Harness guide

This page covers only OpenCode-specific images, the API, sessions, and troubleshooting. Roller runs pinned [OpenCode](https://opencode.ai/docs/cli/) **1.18.30** in a disposable Docker container, using native noninteractive runs, session continuation, and exports. For Task fields see [Task format reference](../tasks/task-format.md). For output semantics shared by all Harnesses see [Trajectory output file guide](../traces/trajectory-outputs.md).

> Jump to: supported scope (section 1) · image and run (section 2) · configuration (section 3) · session and workspace (section 4) · traces and Resume (section 5) · verification (section 6) · troubleshooting (section 7)

## 1. Current supported scope

The OpenCode Harness supports:

- single-turn `prompt` and predefined multi-turn `messages` (resuming the same native session with `opencode run --session`);
- Task Agents and User Simulators, both accepting `"harness": "opencode"`;
- Task Bundle, read-only inputs, Context, setup, task environment variables, custom `output_dir`, and in-container grading;
- streaming **OpenAI Chat Completions** with tool calls;
- `bash/read/write/edit/apply_patch/glob/grep` tool permissions; the actual toolset is determined by the pinned version and the model;
- image attachments produced by OpenCode's `read` tool for image-capable models;
- ACTF, SFT, reasoning export, and usage;
- `--wire-trace actf|full|off`;
- parallelism, retry, run lock, and mixed Resume.

Not currently supported:

- direct multimodal task messages;
- native account/OAuth login;
- custom provider SDKs, MCP, subagents, and persistent memory;
- automatic compaction (long tasks need enough configured context; exceeding the window fails).

## 2. Build and run

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

The image also prepares native plugin dependencies at build time. Each isolated container uses the image dependencies with its own configuration directory, avoiding package downloads during task startup. The host does not need Node.js, Bun, or OpenCode.

For `DEBIAN_MIRROR` and `DEBIAN_SECURITY_MIRROR`, see [Docker builds and package mirrors](../start/docker-builds.md).

(opencode-cli-flags)=
## 3. Configuration

| Option | Default / behavior |
| --- | --- |
| `--opencode-image` | Overrides `--docker-image`, then `ROLLER_OPENCODE_IMAGE`, then `roller-opencode:1.18.30` |
| `--opencode-api-base` | Falls back to `--api-base`, `OPENAI_BASE_URL`, then `https://api.openai.com/v1` |
| `--opencode-api-key-env` | Key variable name; falls back to `--api-key-env`, then `OPENAI_API_KEY` |
| `--opencode-max-steps` | 100 for SkillsBench and 12 otherwise, also bounded by the rollout timeout |
| `--opencode-context-length` | 128000 tokens by default, minimum 1024, match the actual model |
| `--opencode-output-tokens` | 32768 for SkillsBench and 8192 otherwise, positive and smaller than the context window |
| `--opencode-vision` / `--no-opencode-vision` | Declares image-input support to OpenCode; enabled by default and explicitly disableable for text-only models |
| `--thinking` / `runtime.thinking` | `off/minimal/low/medium/high/xhigh`; `off` maps to `none`; provider support is required |
| `--wire-trace` | Native requests and schemas are captured in every mode; `full` adds HTTP records and requires an explicit API base and host networking |
| `--no-keep-thinking` | Removes reasoning from ACTF/SFT only; native logs and continuation history are retained |

Dedicated options apply to both participants. The run lock includes the binary version, image digest, endpoint, key variable name, tools, token limits, step limit, and export settings. Credentials are read during execution and are not included in public configuration.

## 4. Sessions and workspace

Each participant gets an independent container and HOME/XDG directories. Inputs are mounted read-only. Context files are copied to declared paths and listed in the prompt. Setup, selected environment variables, output collection, and in-container grading use the shared Roller task contract.

The adapter permits bash, read, write, edit, apply_patch, glob, and grep; native model selection determines which tools are exposed. Each user turn resumes the same native session and workspace with a new CLI process. Background process handles do not survive. Auto-sharing, updates, model catalog refresh, LSP, formatters, compaction, pruning, and implicit project configuration are disabled. Context/skill coverage checks file accessibility; skill tools, MCP, subagents, and network tools are outside the initial scope.

## 5. Traces, errors, and Resume

A local plugin records the serialized provider request without rewriting it. `trace.jsonl` contains requests, native message/part updates, and session exports. ACTF merges native IDs and captures actual tool schemas, visible reasoning, tool calls, and results. Large inline attachment payloads remain losslessly available in the native trace; ACTF/SFT records bounded metadata and a content hash.

Incomplete or empty replies, API errors, truncated replies, invalid journals, and changing/missing schemas fail explicitly. Timeout recovery preserves available artifacts and removes the container.

OpenCode emits no OpenClaw cache trace. Resume validates each participant separately; full mode still requires HTTP traces, and `capture: none` remains non-reusable.

## 6. Validation

```bash
python examples/coverage/suite.py validate --case '*/opencode'
python examples/coverage/suite.py run --case '*/opencode' --exclude-case 'vision/*' --dry-run
PYTHONPATH=src python -m unittest discover -s tests
ROLLER_OPENCODE_DOCKER_TEST=1 PYTHONPATH=src \
  python -m unittest discover -s tests -p test_opencode_docker.py -v
```

Docker tests use a deterministic local streaming API and a fake key while executing real OpenCode tools. No external model charges are incurred. The suite adds 204 input tasks and 36 simulator tasks, four per group. Select 200 nonvision inputs with `--case '*/opencode' --exclude-case 'vision/*'`. Select the nine relevant pairings with `--case 'simulators/pair/opencode--*' --case 'simulators/pair/*--opencode'`; overlaps are deduplicated. See the {download}`coverage guide <../../../examples/coverage/README.en.md>`.

## 7. Troubleshooting

Common errors:

- the endpoint does not support streaming or tool calls: the initial adapter requires streaming Chat Completions with tools; use a compatible service;
- a trajectory fails: incomplete sessions, empty replies, API errors, truncated replies, missing or changed tool schemas, and corrupt JSONL all fail; inspect raw to locate the cause;
- long tasks exceed the context window: automatic compaction is disabled; raise `--opencode-context-length` to match the real service capability;
- rollout timeout: the container is stopped and available artifacts recovered; adjust `--timeout` or `--opencode-max-steps` as needed;
- `--wire-trace full` fails to start: an explicit API base and host networking are required.
