# DeepSeek Harness

Roller runs the official [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) native `dsh` runtime through its Python SDK. Both SDK and runtime are pinned to 0.1.5rc1, using the official `sdk-minimal` profile. Task Agents and User Simulators accept `"harness": "deepseek"`.

## Build and run

```bash
docker build -t roller-deepseek:0.1.5rc1 docker/deepseek
export DEEPSEEK_API_KEY='your-key'
# Optional compatible gateway; the default is https://api.deepseek.com.
export DEEPSEEK_BASE_URL='https://api.example.com/v1'
python -m roller collect \
  --tasks examples/coverage/cases/formats/jsonl/deepseek.jsonl \
  --harness deepseek --model deepseek-v4-flash \
  --thinking off --parallel 2 --timeout 180 --retries 0 \
  --output runs/deepseek --run-name smoke
```

The endpoint must support streaming Chat Completions, tools, and DeepSeek thinking fields. Model IDs, including gateway prefixes, are sent unchanged. The host needs Python 3.10+ and Docker; SDK dependencies and the official runtime wheel are isolated inside the image. The default base is `python:3.12-slim`; `BASE_IMAGE` can select a Debian/Ubuntu image with Python 3.10+. Official wheels provide Linux x86_64 and aarch64 runtimes.

## Configuration

| Option | Default and behavior |
| --- | --- |
| `--deepseek-image` | Precedes `--docker-image`, `ROLLER_DEEPSEEK_IMAGE`, then `roller-deepseek:0.1.5rc1` |
| `--deepseek-api-base` | Falls back to `--api-base`, `DEEPSEEK_BASE_URL`, then `https://api.deepseek.com` |
| `--deepseek-api-key-env` | Falls back to `--api-key-env`, then `DEEPSEEK_API_KEY`; pass a variable name |
| `--deepseek-max-steps` | 12 model requests per user turn; task and conversation deadlines also apply |
| `--deepseek-context-length` | 134200 tokens, minimum 1024; match the endpoint |
| `--deepseek-output-tokens` | 8192, positive and smaller than the context window |
| `--thinking` / `runtime.thinking` | Native `off/low/high/max`; `none` aliases `off`; omission uses the native adapter default |
| `--wire-trace` | All modes retain native requests and schemas; `full` adds HTTP capture and requires an explicit base URL and host networking |
| `--no-keep-thinking` | Removes reasoning from ACTF/SFT only, preserving native history |

DeepSeek-specific options apply to both the Task Agent and the Simulator. The run lock records image digest, version, profile, tools, endpoint, key variable name, step limit, and token limits. Credentials are read at execution time.

## Sessions and traces

Each participant owns a container and an isolated `DSH_HOME`. One SDK/runtime process and native Bash PTY persist across turns, retaining conversation history, files, shell variables, and working directory. Roller supports read-only inputs, copied Context files listed in the system prompt, setup commands, selected environment variables, custom outputs, and container grading.

The official `sdk-minimal` profile exposes only persistent `bash`; file tasks use that tool. It omits automatic compaction, implicit workspace instructions, skills, MCP, Web tools, and subagents. Context/skill cases check file accessibility. Roller's fixed patch adds request journaling, disables automatic model retries and official API session/plugin upload extensions, and sets the selected model's context/output limits.

`trace.jsonl` uses `roller.deepseek-events/v1`, retaining serialized requests and native SDK notifications. ACTF/SFT preserve actual system prompts, schemas, reasoning, calls, and results. Errors, truncated or empty replies, incomplete tool calls, and content that cannot be represented losslessly as text are rejected. Timeouts stop the container and recover available artifacts. Resume validates each participant's artifacts; DeepSeek does not produce OpenClaw cache traces.

Direct multimodal messages, visual acceptance, interactive login, arbitrary profiles/plugins, and cross-task memory are not supported by this adapter. Provider compatibility must be checked for each model. Long tasks need sufficient context because compaction is disabled.

## Validation

```bash
python examples/coverage/suite.py validate --case '*/deepseek'
python examples/coverage/suite.py run --case '*/deepseek' --exclude-case 'vision/*' --dry-run
ROLLER_DEEPSEEK_DOCKER_TEST=1 PYTHONPATH=src \
  python -m unittest discover -s tests -p test_deepseek_docker.py -v
```

Docker tests execute real SDK tools against a deterministic local API with a fake key. Coverage includes 204 input tasks (200 nonvision) and 52 tasks across all 13 pairings involving DeepSeek. Select pairings with `--case 'simulators/pair/deepseek--*' --case 'simulators/pair/*--deepseek'`. See [coverage instructions](https://github.com/Liuziyu77/Roller/blob/main/examples/coverage/README.en.md).
