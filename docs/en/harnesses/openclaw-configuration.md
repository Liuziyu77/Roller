# OpenClaw Harness guide

This guide covers OpenClaw-specific images, model providers, API adapters, thinking, search, gateway, and troubleshooting. For Task fields, see [Task format reference](../tasks/task-format.md). For the outputs shared by all Harnesses, see [Trajectory output file guide](../traces/trajectory-outputs.md).

## 1. Scope

The OpenClaw Harness is for running tasks with the OpenClaw agent. It supports OpenAI Responses and Chat Completions compatible models, along with text and image input, thinking, multi-turn sessions, and Web Search.

For a first run, follow [Getting started](../start/getting-started.md) to complete one OpenClaw collection. Use this guide when configuring custom models, environment variables, and OpenClaw-specific features.

## 2. Image preparation

Before collection you need:

- Python 3.10+.
- a working Docker daemon.
- an OpenClaw Docker image.
- the API endpoint and credentials required by the model service.

Roller defaults to the pinned official stable image `ghcr.io/openclaw/openclaw:2026.7.1-2`, which includes
OpenClaw, Node.js, and Python 3. Roller runs it without source changes or a derived image.
Pull it online or load an offline archive:

```bash
docker pull --platform linux/amd64 ghcr.io/openclaw/openclaw:2026.7.1-2

# docker load can read a gzip-compressed docker save archive directly
docker load -i /path/to/openclaw-2026.7.1-2-linux-amd64.tar.gz
```

You can still point at another image via a flag or environment variable:

```bash
roller collect ... --docker-image your-openclaw-image:tag

# Or set the default
export ROLLER_OPENCLAW_IMAGE=your-openclaw-image:tag
```

If the Docker daemon is on another machine, use the Docker CLI's own `DOCKER_HOST` and related settings. Roller invokes the local `docker` command, so when `docker version` can connect, Roller can usually use the same daemon.

```bash
docker version
docker image inspect your-openclaw-image:tag
```

The official OpenClaw image does not require any additional container-user configuration. Roller automatically prepares a writable workspace and local Gateway settings inside each one-shot container. These settings do not change host permissions. Pass `--container-user image` only when a custom image already provides a writable workspace for its default user and should run as that user.

## 3. Environment variables and API

OpenClaw reads the model name, API protocol, service address, and credentials from a model config file. Sensitive values use environment-variable placeholders and are loaded from the current shell before the run.

### Model configuration

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

Change the model ID to the name used by the model service. The command value must exactly match the configuration. `${ENV_NAME}` is read from the current shell. Roller reports an error before starting the rollout if a referenced variable is missing or empty.

### API protocol

The `api` value selects the protocol supported by the model service:

| `api` | Use for |
| --- | --- |
| `openai-responses` | A service that implements the OpenAI Responses API |
| `openai-completions` | A service that only implements a Chat Completions-compatible API |

`apiKey` must be non-empty. For a service that does not require authentication, use `"apiKey":"not-required"`.

### Load environment variables

The API address and credentials in the model config are read from the current shell through `${ENV_NAME}` placeholders. Start by copying the repository's environment-variable template:

Create a local file from the example and do not commit it to Git:

```bash
cp examples/env/api.env.example examples/env/api.env

# Edit the file, then load it into the current shell
set -a
source examples/env/api.env
set +a
```

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
| `--network` | `host` | Container network mode |
| `--gateway-port` | `18789` | Gateway start port |
| `--gateway-port-step` | `10` | Port interval between workers |
| `--gateway-startup-timeout` | `30` | Seconds to wait for the gateway to become ready |
| `--session-id` | `roller` | OpenClaw session ID prefix |
| `--thinking` | none | Sets model reasoning effort. See section 6 for common levels and retention behavior |
| `--keep-thinking` / `--no-keep-thinking` | on | Whether to keep thinking in the trajectory and request history |
| `--cache-trace` / `--no-cache-trace` | on | Whether to record each assembled system prompt. ACTF persists the first valid value |
| `--wire-trace` | `actf` | Tools schema / full request-body recording mode |
| `--search-provider` | none | OpenClaw Web Search provider |
| `--pass-env NAME` | none | Add one environment variable name. Repeatable |
| `--set-image-model` / `--no-set-image-model` | on | Whether to set the OpenClaw image model |
| `--keep-container-on-error` | off | Keep the container after an error for debugging |
| `--collect-path` | none | Legacy: also collect an absolute path from the container |

Check the flags available in your installed version:

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

The full template is [`examples/config/openclaw/models-multimodal.json`](../../../examples/config/openclaw/models-multimodal.json). `input` only tells OpenClaw how to use the model. The actual API backend must still support image input under the chosen adapter.

Images enter the task workspace through `files.inputs` and are then opened with OpenClaw's image-viewing tool. Task `messages[].content` currently accepts strings only and cannot contain image blocks directly. Text-only models can still process text and ordinary files, but they cannot complete tasks that require understanding image content.

By default Roller also tries to set the task model as OpenClaw's image model. Older OpenClaw versions that do not support this setting only log a warning. Disable with `--no-set-image-model`.

## 6. Thinking configuration

Common OpenClaw thinking levels are `off`, `low`, `medium`, and `high`. For example:

```bash
roller collect ... --thinking high
```

OpenClaw treats `none` as `off`. Other levels depend on the OpenClaw version, model, and API service.

Whether a trace can store reasoning text depends on what the model service returns. If the service reports only a reasoning-token count and no visible text, `reasoning_content` is empty. Roller does not reconstruct text from hidden tokens.

`--keep-thinking` is enabled by default and keeps visible reasoning returned by the model. Use `--no-keep-thinking` to remove it from the trace and later request history.

## 7. Trajectories and request recording

This section explains which OpenClaw runtime information Roller records and how to keep complete model requests when troubleshooting.

The default `--cache-trace` writes the system prompt assembled by OpenClaw into `trace_ACTF.json`. `--wire-trace` controls whether Roller also records the tools schema or complete requests:

| `--wire-trace` | Recorded content | Use for |
| --- | --- | --- |
| `actf` (default) | Writes the tools schema to `trace_ACTF.json` without keeping complete requests | Normal collection |
| `full` | Also writes complete requests to `wire_trace.jsonl` and creates a redacted `cache_trace.jsonl` | Troubleshooting or auditing what the model received |
| `off` | Keeps no wire data and sets ACTF `tools` to `null` | Runs that do not need request information |

`trace.jsonl` contains OpenClaw session events and may not match the complete request sent to the model. Use `--wire-trace full` when you need to inspect the actual messages, tools, and parameters received by the model.

`full` creates larger files that may contain sensitive prompts, reasoning, tool arguments, and results. Do not leave it enabled for large collection runs unless needed. When using `actf` or `full`, keep the default `--network host` and provide an accessible absolute `baseUrl` in the model configuration. See [Trajectory outputs](../traces/trajectory-outputs.md#32-session-history-is-not-the-model-request-context) for details.

## 8. Web Search

Web Search lets the OpenClaw agent look up information on the web while running a task. Configure it only when a task needs online search. Tasks that use local files, existing context, or no web information can skip this section.

Web Search requires both a search provider and its API key. The following example uses Brave Search.

First, set the key in `examples/env/api.env`:

```bash
BRAVE_API_KEY=your-actual-Brave-API-key
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

## 9. Common issues

**(1) Docker preflight failed**

Run `docker version` first. If Docker uses a remote daemon, confirm that the current shell's Docker context, `DOCKER_HOST`, and certificate settings can connect.

**(2) `model config references missing environment variable`**

A `${ENV_NAME}` in the model JSON is unset in the current shell, or its value is empty. Load the local env file and retry.

**(3) Model request returns 404 or a protocol error**

Usually the trailing path on `baseUrl` or the `api` adapter does not match. Confirm whether the server implements the Responses API or the Chat Completions API.

**(4) Gateway port conflict**

If the gateway port is already in use, choose a different OpenClaw gateway start port:

```bash
roller collect ... --gateway-port 22000
```

**(5) The task replied successfully but the sample failed**

Roller requires a successful sample to have an OpenClaw raw trace. ACTF/SFT are normally required too. A structured `runtime.trajectory_conversion` warning records the rare case where native attachment or multimodal content cannot be projected losslessly without invalidating the benchmark score. Also check the output directory for rejected symlinks or special files. For output conditions see [Trajectory output file guide](../traces/trajectory-outputs.md). For result safety rules see [Result storage](../tasks/task-bundle-and-storage.md#5-output-collection).
