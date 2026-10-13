# DeepSeek Harness guide

This page covers only DeepSeek-Harness-specific images, the API, the native session, and troubleshooting. Roller drives the official [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) native `dsh` runtime through its Python SDK. Both SDK and runtime are pinned to **0.1.5rc1**, using the official `sdk-minimal` profile. For Task fields see [Task format reference](../tasks/task-format.md). For output semantics shared by all Harnesses see [Trajectory output file guide](../traces/trajectory-outputs.md).

## 1. Scope

The DeepSeek Harness supports:

- single-turn `prompt` and predefined multi-turn `messages` (one SDK/runtime process and native Bash PTY persist across turns, retaining conversation history, files, shell variables, and working directory).
- Task Agents and User Simulators, both accepting `"harness": "deepseek"`.
- Task Bundle, read-only inputs, Context, `runtime.setup`, task environment variables, custom `output_dir`, artifact collection, and in-container grading.
- streaming Chat Completions, tool calls, and DeepSeek thinking fields.
- persistent `bash` from the official `sdk-minimal` profile.
- ACTF, SFT, provenance, and usage.
- `--wire-trace actf|full|off`.
- native `--thinking off/low/high/max` mapping.

Not currently supported:

- direct multimodal messages and visual acceptance.
- interactive login.
- arbitrary profiles/plugins.
- cross-task memory.
- the OpenClaw cache trace (Resume does not require it).

## 2. Image preparation

```bash
docker build -t roller-deepseek:0.1.5rc1 docker/deepseek
export DEEPSEEK_API_KEY='your-key'
# Optional compatible gateway. The default is https://api.deepseek.com.
export DEEPSEEK_BASE_URL='https://api.example.com/v1'
python -m roller collect \
  --tasks examples/coverage/cases/formats/jsonl/deepseek.jsonl \
  --harness deepseek --model deepseek-v4-flash \
  --thinking off --parallel 2 --timeout 180 --retries 0 \
  --output runs/deepseek --run-name smoke
```

The endpoint must support streaming Chat Completions, tools, and DeepSeek thinking fields. Model IDs, including gateway prefixes, are sent unchanged. The host needs Python 3.10+ and Docker. SDK dependencies and the official runtime wheel are isolated inside the image. The default base is `python:3.12-slim`. `BASE_IMAGE` can select a Debian/Ubuntu image with Python 3.10+. Official wheels provide Linux x86_64 and aarch64 runtimes.

For `DEBIAN_MIRROR` and `DEBIAN_SECURITY_MIRROR`, see [Docker builds and package mirrors](../start/docker-builds.md).

## 3. Environment variables and API

The DeepSeek Harness connects to a Chat Completions service that supports streaming, tool calls, and DeepSeek thinking fields. A first run needs an API address, an API key, and a model ID.

| Setting | Default source | CLI override |
| --- | --- | --- |
| API address | `DEEPSEEK_BASE_URL`, then `https://api.deepseek.com` | `--deepseek-api-base`, or `--api-base` for a single-Harness run |
| API key | `DEEPSEEK_API_KEY` | `--deepseek-api-key-env`, or `--api-key-env` for a single-Harness run |
| Model ID | The task `model` or `--model` | Sent to the service unchanged |

The value of `--deepseek-api-key-env` or `--api-key-env` is the name of a host environment variable, not the key itself. The key is read at execution time and is not written to the task, run configuration, or public outputs.

(deepseek-cli-flags)=
## 4. DeepSeek-specific flags

This table lists DeepSeek-specific runtime flags only. See section 3 for API addresses and keys. See the [CLI reference](../start/cli-reference.md) for flags shared by all Harnesses.

| Option | Default / behavior |
| --- | --- |
| `--deepseek-image` | Precedes `--docker-image`, `ROLLER_DEEPSEEK_IMAGE`, then `roller-deepseek:0.1.5rc1` |
| `--deepseek-max-steps` | 12 model requests per user turn. Task and conversation deadlines also apply |
| `--deepseek-context-length` | 128000 tokens, minimum 1024, match the endpoint |
| `--deepseek-output-tokens` | 8192, positive and smaller than the context window |
| `--thinking` / `runtime.thinking` | Native `off/low/high/max`. `none` aliases `off`. Omission uses the native adapter default |
| `--wire-trace` | All modes retain native requests and schemas. `full` adds HTTP capture and requires an explicit base URL and host networking |
| `--no-keep-thinking` | Removes reasoning from ACTF/SFT only, preserving native history |

Options apply to both roles. The run lock records image digest, version, profile, tools, endpoint, key variable name, step limit, and token limits. Credentials are read at execution time.

## 5. Multimodal capabilities

The DeepSeek Harness currently uses the official `sdk-minimal` profile, which exposes only persistent `bash` and has no image-viewing tool. It therefore supports text and ordinary file tasks, but not tasks that require the model to understand image content.

Image files can enter the workspace through `files.inputs`, but they remain ordinary files and are not sent to the model as image content. Task `messages[].content` currently accepts strings only and cannot contain image blocks directly. Use another Harness with an image tool for vision tasks.

## 6. Sessions and workspace

Each participant owns a container and an isolated `DSH_HOME`. One SDK/runtime process and native Bash PTY persist across turns, retaining conversation history, files, shell variables, and working directory. Roller supports read-only inputs, copied Context files listed in the system prompt, setup commands, selected environment variables, custom outputs, and container grading.

## 7. Tools and capability limits

The official `sdk-minimal` profile exposes only persistent `bash`. File operations and search also use this tool. The profile does not provide automatic compaction, implicit workspace instructions, skills, MCP, Web tools, or subagents. Context and skill cases only verify that the relevant files are accessible. Roller's fixed patch adds request journaling, disables automatic model retries and official API session/plugin upload extensions, and sets the selected model's context and output limits.

Provider compatibility must be checked for each model. Long tasks need sufficient context because compaction is disabled.

## 8. Trajectories and request recording

`trace.jsonl` uses `roller.deepseek-events/v1`, retaining serialized requests and native SDK notifications. ACTF/SFT preserve actual system prompts, schemas, reasoning, calls, and results. Errors, truncated or empty replies, incomplete tool calls, and content that cannot be represented losslessly as text are rejected. Timeouts stop the container and recover available artifacts. Resume validates each participant's artifacts. DeepSeek does not produce OpenClaw cache traces.

## 9. Validation

```bash
python examples/coverage/suite.py validate --case '*/deepseek'
python examples/coverage/suite.py run --case '*/deepseek' \
  --exclude-case 'vision/*' --dry-run
ROLLER_DEEPSEEK_DOCKER_TEST=1 PYTHONPATH=src \
  python -m unittest discover -s tests -p test_deepseek_docker.py -v
```

Docker tests execute real SDK tools against a deterministic local API with a fake key. Coverage includes 204 input tasks (200 nonvision) and 52 tasks across all 13 pairings involving DeepSeek. Select pairings with `--case 'simulators/pair/deepseek--*' --case 'simulators/pair/*--deepseek'`. See [coverage instructions](https://github.com/Liuziyu77/Roller/blob/main/examples/coverage/README.en.md).

## 10. Common issues

**(1) A trajectory is rejected**

Errors, truncated or empty replies, incomplete tool calls, and content that cannot be represented losslessly as text are all rejected. Inspect `trace.jsonl` to locate the cause.

**(2) Long tasks exceed the context window**

Automatic compaction is disabled. Raise `--deepseek-context-length` to match the real service.

**(3) Rollout times out**

The container is stopped and available artifacts recovered. Adjust `--timeout` or `--deepseek-max-steps` as needed.

**(4) Resume refused**

The image digest, version, profile, tools, endpoint, step limit, or token limits recorded in the run lock changed. Use a fresh run directory.

**(5) `--wire-trace full` fails to start**

An explicit base URL and host networking are required.
