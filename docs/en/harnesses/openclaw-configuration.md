# OpenClaw Harness guide

The OpenClaw Harness runs text, file, image, and Web Search tasks through OpenClaw Agent. It can connect to OpenAI Responses or Chat Completions-compatible services. This guide follows the order needed for a first run: image, model API, Harness-specific flags, thinking, trajectories, and search.

For a first collection, follow [Getting started](../start/getting-started.md). Use this guide to continue with custom models and OpenClaw-specific features. For Task fields, see [Task format reference](../tasks/task-format.md). For shared outputs, see [Trajectory output file guide](../traces/trajectory-outputs.md).

## 1. Scope

Use the OpenClaw Harness when a task needs:

- an OpenAI Responses or Chat Completions-compatible model service.
- text, ordinary file, or image processing.
- single-turn or multi-turn execution.
- optional thinking and Web Search.

Task messages cannot contain image blocks directly. Add images through `files.inputs`, then let the OpenClaw image tool read them from the workspace.

## 2. Image preparation

Before collection, prepare:

- Python 3.10+.
- a working Docker daemon.
- an OpenClaw Docker image.
- the API endpoint and credentials required by the model service.

Roller defaults to the pinned official stable image `ghcr.io/openclaw/openclaw:2026.7.1-2`, which includes
OpenClaw, Node.js, and Python 3. Roller runs it without source changes or a derived image.
Pull the pinned image when online:

```bash
docker pull --platform linux/amd64 ghcr.io/openclaw/openclaw:2026.7.1-2
```

Alternatively, load an existing archive:

```bash
docker load -i openclaw-2026.7.1-2-linux-amd64.tar.gz
```

Another image can be selected through a command-line option or environment variable:

```bash
roller collect ... --docker-image custom-openclaw-image:tag

# Or set the default
export ROLLER_OPENCLAW_IMAGE=custom-openclaw-image:tag
```

If the Docker daemon is on another machine, use the Docker CLI's own `DOCKER_HOST` and related settings. Roller invokes the local `docker` command, so when `docker version` can connect, Roller can usually use the same daemon.

```bash
docker version
docker image inspect custom-openclaw-image:tag
```

The official OpenClaw image does not require any additional container-user configuration. Roller automatically prepares a writable workspace and local Gateway settings inside each one-shot container. These settings do not change host permissions. Pass `--container-user image` only when a custom image already provides a writable workspace for its default user and should run as that user.

## 3. Environment variables and API

OpenClaw reads the model name, API protocol, service address, and credentials from a model config file. Sensitive values use environment-variable placeholders and are loaded from the current shell before the run.

### 3.1 Model configuration

`--model-config` accepts an OpenClaw models JSON. The basic text template is
[`examples/config/openclaw/models.json`](../../../examples/config/openclaw/models.json). The GPT and
Chat Completions reasoning template is
[`examples/config/openclaw/models-reasoning.json`](../../../examples/config/openclaw/models-reasoning.json):

```json
{
  "mode": "merge",
  "providers": {
    "my-openai-proxy": {
      "baseUrl": "${MY_PROXY_BASE_URL}",
      "apiKey": "${MY_PROXY_API_KEY}",
      "api": "openai-responses",
      "models": [
        {"id": "my-model", "name": "My model"}
      ]
    }
  }
}
```

The `--model` value uses the `provider/model-id` format. `provider` is the name under `providers`, and `model-id` is the value of `models[].id`. The example above uses:

```text
my-openai-proxy/my-model
```

Change the model ID to the name used by the model service. The command value must exactly match the configuration. `${ENV_NAME}` is read from the current shell. Roller reports an error before starting the task if a referenced variable is missing or empty.

### 3.2 API protocol

The `api` value selects the protocol supported by the model service:

| `api` | Use for |
| --- | --- |
| `openai-responses` | A service that implements the OpenAI Responses API |
| `openai-completions` | A service that only implements a Chat Completions-compatible API |

`apiKey` must be non-empty. For a service that does not require authentication, use `"apiKey":"not-required"`.

### 3.3 Load environment variables

The API address and credentials in the model config are read from the current shell through `${ENV_NAME}` placeholders. Create a local file from the example, then replace `MY_PROXY_BASE_URL` and `MY_PROXY_API_KEY` with the actual values from the model service:

```bash
cp examples/env/api.env.example examples/env/api.env

# After saving the file, load it into the current shell
set -a
source examples/env/api.env
set +a
```

After configuring the model and loading the environment variables, run one task to check the setup:

```bash
roller collect \
  --harness openclaw \
  --tasks examples/tasks/tasks.jsonl \
  --model my-openai-proxy/my-model \
  --model-config examples/config/openclaw/models.json \
  --limit 1 \
  --output runs \
  --run-name openclaw-demo
```

The `my-openai-proxy/my-model` value must match the model configuration in section 3.1. The first command runs one task so the configuration can be checked before a larger run.

These common variables are passed into the OpenClaw container automatically when they exist and are non-empty on the host:

```text
OPENROUTER_API_KEY        OPENROUTER_BASE_URL
BRAVE_API_KEY             TAVILY_API_KEY
HTTP_PROXY                HTTPS_PROXY               NO_PROXY
http_proxy                https_proxy               no_proxy
```

(openclaw-cli-flags)=
## 4. OpenClaw-specific flags

This section lists the flags that need special attention when running OpenClaw. For task, parallelism, retry, and output flags shared by all Harnesses, see [CLI reference](../start/cli-reference.md).

| Flag | Default | Purpose |
| --- | --- | --- |
| `--docker-image` / `--openclaw-image` | `ROLLER_OPENCLAW_IMAGE` or the built-in value | Overrides the OpenClaw image, use the specific form in mixed-Harness runs |
| `--container-user` | `0` | User inside the one-shot task container. `image` uses the image default user |
| `--docker-binary` | `docker` | Docker CLI name or path |
| `--model-config` | none | Custom provider/model JSON |
| `--openclaw-model-request-timeout` | OpenClaw default | Model request timeout in seconds, also used as the streaming idle timeout. Requires `--model-config` |
| `--openclaw-config-set KEY=VALUE` | none | Run `openclaw config set KEY VALUE` in every OpenClaw container. Repeatable |
| `--network` | `host` | Container network mode |
| `--gateway-port` | `18789` | Gateway start port |
| `--gateway-port-step` | `10` | Port interval between workers |
| `--gateway-startup-timeout` | `240` | Seconds to wait for the gateway to become ready |
| `--session-id` | `roller` | OpenClaw session ID prefix |
| `--thinking` | none | Sets model reasoning effort. See section 6 for common levels and retention behavior |
| `--keep-thinking` / `--no-keep-thinking` | on | Whether to keep thinking in the trajectory and request history |
| `--cache-trace` / `--no-cache-trace` | on | Whether to record each assembled system prompt. ACTF persists the first valid value |
| `--wire-trace` | `actf` | Tool-definition or complete-request recording mode |
| `--search-provider` | none | OpenClaw Web Search provider |
| `--pass-env NAME` | none | Add one environment variable name. Repeatable |
| `--set-image-model` / `--no-set-image-model` | on | Whether to set the OpenClaw image model |
| `--keep-container-on-error` | off | Keep the container after an error for debugging |
| `--collect-path` | none | Legacy: also collect an absolute path from the container |

To change an OpenClaw setting that Roller has no dedicated flag for, use `--openclaw-config-set`:

```bash
roller collect ... \
  --openclaw-config-set diagnostics.stuckSessionAbortMs=2700000 \
  --openclaw-config-set agents.defaults.timeoutSeconds=3600
```

- Settings are applied after Roller's own settings and before the gateway starts, in every OpenClaw container of the run, including an OpenClaw user simulator.
- Values follow OpenClaw's parsing: anything that parses as JSON/JSON5 is written with that type, everything else as a string. A path OpenClaw does not recognize makes the rollout fail during startup.
- Paths Roller manages are rejected with a pointer to the Roller flag to use instead, for example `gateway.port` (`--gateway-port`), `models` (`--model-config`), and `agents.defaults.thinkingDefault` (`--thinking`).
- Settings are recorded in `lock.json` and `info.json`, with credential-like values redacted. Use a new `--run-name` after changing them; `--resume` refuses a run whose settings differ.
- Do not pass API keys this way: values appear on the `docker exec` command line.

Check the flags available in the installed version:

```bash
roller collect --help
```

## 5. Multimodal capabilities

Ordinary text models use:

```json
{"id":"my-model","name":"My model"}
```

Vision-input models must declare image capability:

```json
{
  "id": "my-multimodal-model",
  "name": "My Multimodal Model",
  "input": ["text", "image"]
}
```

The full template is [`examples/config/openclaw/models-multimodal.json`](../../../examples/config/openclaw/models-multimodal.json). `input` only tells OpenClaw how to use the model. The actual API backend must still support image input under the selected API protocol.

Images enter the task workspace through `files.inputs` and are then opened with OpenClaw's image-viewing tool. Task `messages[].content` currently accepts strings only and cannot contain image blocks directly. Text-only models can still process text and ordinary files, but they cannot complete tasks that require understanding image content.

By default Roller also tries to set the task model as OpenClaw's image model. Older OpenClaw versions that do not support this setting only log a warning. Disable with `--no-set-image-model`.

## 6. Thinking configuration

Thinking is the reasoning a model performs before producing its final answer. `--thinking` sets the reasoning effort requested by OpenClaw. It affects how the model generates a response and does not change the task content.

### 6.1 When to set it

For a first run, `--thinking` can be omitted so OpenClaw and the model use their defaults. Add the flag when a specific reasoning effort is needed:

```bash
roller collect ... --thinking high
```

The same setting can be placed in task `runtime.thinking`. A task value takes precedence over the command line. If neither is set, OpenClaw or the model default is used.

### 6.2 Choosing a level

The commonly used levels are:

| Level | Meaning |
| --- | --- |
| `off` | Requests no additional reasoning. Some models or API services may not support this and may still use their own default reasoning behavior |
| `low` | Uses less reasoning and usually responds faster with lower usage |
| `medium` | Balances response speed and reasoning effort |
| `high` | Uses more reasoning for harder tasks, usually with more time and tokens |

Roller also recognizes `minimal`, `xhigh`, `adaptive`, `max`, and `ultra`. These are extended levels and work only when OpenClaw, the model, and the API service all support them. `none` is treated as `off`.

A level recognized by Roller is not necessarily supported by the selected model. The value passes through Roller, OpenClaw, and the model API, and any of these layers can reject it. For a new model, choose a level documented by the model service and validate it with one task first.

> [!NOTE]
> The older OpenClaw version in `wildclawbench-ubuntu:v1.3` reliably supports levels only through `high`. Do not use `xhigh`, `adaptive`, `max`, or `ultra` with this image.

### 6.3 Running a check

The following command runs one task and is suitable for checking a thinking configuration before a larger collection run:

```bash
roller collect \
  --harness openclaw \
  --tasks examples/tasks/tasks.jsonl \
  --model my-provider/my-model \
  --model-config examples/config/openclaw/models.json \
  --thinking high \
  --limit 1 \
  --output runs \
  --run-name thinking-check
```

`my-provider` must match a provider name in the model configuration, and `my-model` must match a model `id` under that provider. Replace both example names with the configuration in use.

When thinking is set to a level other than `off` and the model configuration does not declare `reasoning`, Roller adds `reasoning: true` to the configuration copy used for that run. An explicit `reasoning: false` is preserved, so confirm that the model supports thinking before enabling it.

Some API services accept only certain levels or use service-specific reasoning fields. Configure `compat` according to that service's API documentation. Do not copy another model's `compat` settings. See [CLI flag reference](../start/cli-reference.md) for the shared rules.

### 6.4 Saving reasoning content

`--thinking` controls the requested reasoning effort. `--keep-thinking` controls whether visible reasoning is written to trajectories. The two flags have different purposes.

`--keep-thinking` is enabled by default. If the model service returns visible reasoning, Roller includes it in ACTF and SFT and retains it in later multi-turn request history. `--no-keep-thinking` removes that content but does not disable model reasoning.

Some services return only a reasoning-token count and no reasoning text. In that case, `reasoning_content` may be empty even though the model reasoned. Roller does not generate or reconstruct reasoning text from token counts.

### 6.5 Checking whether thinking works

Run one short task with `--limit 1` before starting a batch. Check these items in order:

1. The command finishes normally and the run `info.json` contains no error.
2. The sample `info.json` has `status` set to `ok`.
3. ACTF and SFT files are generated as expected.

To confirm the thinking parameters received by the model, temporarily add `--wire-trace full` and inspect `wire_trace.jsonl` in the sample directory. This file may contain request content, so use it only for troubleshooting.

If OpenClaw reports that a level is unsupported, switch to a level explicitly supported by the model service. If support is still unclear, remove `--thinking` first and confirm that the default configuration runs successfully before adjusting it again.

## 7. Trajectories and request recording

This section explains which OpenClaw runtime information Roller records and how to keep complete model requests when troubleshooting.

A run produces these main trajectory files:

| File | Purpose |
| --- | --- |
| `trace.jsonl` | Records native OpenClaw session events for troubleshooting |
| `trace_ACTF.json` | Normalizes replies, thinking, tool calls, and results |
| `trace_SFT.json` | Converts a successful trajectory into training-data format |


The default `--cache-trace` writes the system prompt assembled by OpenClaw into `trace_ACTF.json`. `--wire-trace` controls whether Roller also records tool definitions or complete requests:

| `--wire-trace` | Recorded content | Use for |
| --- | --- | --- |
| `actf` (default) | Writes tool definitions to `trace_ACTF.json` without keeping complete requests | Normal collection |
| `full` | Also writes complete requests to `wire_trace.jsonl` and creates a redacted `cache_trace.jsonl` | Troubleshooting or auditing what the model received |
| `off` | Keeps no wire data and sets ACTF `tools` to `null` | Runs that do not need request information |

`trace.jsonl` contains OpenClaw session events and may not match the complete request sent to the model. Use `--wire-trace full` to inspect the actual messages, tools, and parameters received by the model.

`full` creates larger files that may contain sensitive prompts, reasoning, tool arguments, and results. Do not leave it enabled for large collection runs unless needed. When using `actf` or `full`, keep the default `--network host` and provide an accessible absolute `baseUrl` in the model configuration. See [Trajectory outputs](../traces/trajectory-outputs.md#32-session-history-is-not-the-model-request-context) for details.

## 8. Web Search

Web Search lets the OpenClaw agent look up information on the web while running a task. Configure it only when a task needs online search. Tasks that use local files, existing context, or no web information can skip this section.

Web Search requires both a search provider and its API key. The following example uses Brave Search.

First, set the key in `examples/env/api.env`:

```bash
BRAVE_API_KEY=actual-Brave-API-key
```

Save the file and load its variables into the current shell:

```bash
set -a
source examples/env/api.env
set +a
```

Select Brave when running the task:

```bash
roller collect \
  ... \
  --search-provider brave
```

## 9. First-run check

Use `--limit 1` for the first run, then start a larger collection after the configuration works. After the command finishes, check:

1. `info.json` in the run directory has `state` set to `finished`.
2. The sample `info.json` has `status` set to `ok`.
3. The sample contains `trace.jsonl`, `trace_ACTF.json`, and `trace_SFT.json`.
4. If the task creates files, the expected files exist under `results/`.

## 10. Common issues

**(1) Docker preflight failed**

Run `docker version` first. If Docker uses a remote daemon, confirm that the current shell's Docker context, `DOCKER_HOST`, and certificate settings can connect.

**(2) `model config references missing environment variable`**

A `${ENV_NAME}` in the model JSON is unset in the current shell, or its value is empty. Load the local env file and retry.

**(3) Model request returns 404 or a protocol error**

Usually the trailing path on `baseUrl` or the selected `api` protocol does not match. Confirm whether the server implements the Responses API or the Chat Completions API.

**(4) Gateway port conflict**

If the gateway port is already in use, choose a different OpenClaw gateway start port:

```bash
roller collect ... --gateway-port 22000
```

**(5) The task replied successfully but the sample failed**

Inspect the sample `info.json` and `trace.jsonl` first. Common causes are a trajectory-conversion failure or symlinks and special files that cannot be collected safely from the output directory. See [Trajectory output file guide](../traces/trajectory-outputs.md) and [Result storage](../tasks/task-bundle-and-storage.md#5-output-collection) for the detailed rules.

**(6) Interrupted while writing long tool arguments: `LLM idle timeout (120s)` or `OPENCLAW_DIRECT_ABORT`**

Some OpenAI-compatible services do not stream tool-call arguments and send them in one piece once they are complete. When the agent writes a large file in one call, the connection can stay silent for minutes and trip two OpenClaw watchdogs:

- the model idle timeout, 120 seconds by default, reported as `LLM idle timeout (120s): no response from model`;
- stuck-session recovery, which aborts the run after 6 minutes without stream progress, reported as `AbortError: agent run aborted: code=OPENCLAW_DIRECT_ABORT`, with `stalled session ... reason=active_work_without_progress` in the gateway log.

Raise both:

```bash
roller collect ... \
  --timeout 3600 \
  --openclaw-model-request-timeout 3000 \
  --openclaw-config-set diagnostics.stuckSessionAbortMs=2700000
```

`--openclaw-model-request-timeout` also caps the total duration of one model request, so it must cover the model's longest single call, reasoning plus the tool arguments it writes.
