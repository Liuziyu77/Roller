# Getting started: from install to the first trajectory

This guide uses **OpenClaw** as the example and takes one Task from installation to a saved trajectory. Other Harnesses follow the same main workflow: prepare the configuration, run a dry-run, collect trajectories, check the results, and open the Viewer. Images, API configuration, and Harness-specific options differ, so refer to the corresponding Harness guide when adapting this workflow.

By the end, you will have checked the configuration with a dry-run, completed a real collection, found the output files, and opened the result in the Viewer. If Task, Harness, rollout, run, or trace are unfamiliar terms, read the [Introduction](introduction.md) first.

## 1. Prepare the runtime environment

You need:

- Python 3.10+.
- Docker CLI.
- a Docker daemon the CLI can reach.
- the OpenClaw image.
- a model API that supports OpenAI Responses or Chat Completions.

Install Roller from the repository root:

```bash
python3 -m pip install -e .
roller --version
docker version
```

`docker version` must return both Client and Server information. With only the Docker CLI and no reachable daemon, Roller cannot start a rollout.

## 2. Prepare the OpenClaw image

OpenClaw supports both OpenAI Responses and Chat Completions. Pull the pinned image online or load an existing archive:

```bash
docker pull --platform linux/amd64 ghcr.io/openclaw/openclaw:2026.7.1-2

# Or
docker load -i /path/to/openclaw-2026.7.1-2-linux-amd64.tar.gz
```

If you use a custom image tag, pass it with `--docker-image` or set `ROLLER_OPENCLAW_IMAGE`.

Keep the default reasoning settings for the first run. Roller saves the visible reasoning text returned by the model service. If the service reports only reasoning token counts, there is no reasoning text to save.

## 3. Prepare the OpenClaw API configuration

Copy the local environment template:

```bash
cp examples/env/api.env.example examples/env/api.env
```

Then load it into the current shell:

```bash
set -a
source examples/env/api.env
set +a
```

`examples/env/*.env` is Git-ignored. Keep real keys out of shell history, tasks, model JSON files, and documentation.

OpenClaw reads the provider and model from `--model-config`:

```json
{
  "mode": "merge",
  "providers": {
    "my-openai-proxy": {
      "baseUrl": "${MY_PROXY_BASE_URL}",
      "apiKey": "${MY_PROXY_API_KEY}",
      "api": "openai-responses",
      "models": [{"id": "my-model", "name": "My model"}]
    }
  }
}
```

- `mode: "merge"` means the providers declared here are merged into OpenClaw's existing model config, instead of replacing the whole config set.
- `--model` uses `provider/model-id`. The example above is `my-openai-proxy/my-model`.
- `${ENV_NAME}` is expanded by Roller on the host. A missing or empty value fails before the rollout starts.
- Responses services use `openai-responses`. Services that only expose `/v1/chat/completions` use `openai-completions`.
- `baseUrl` usually ends at `/v1`. Do not treat `/chat/completions` as the base URL.

The repository template is [`examples/config/openclaw/models.json`](../../../examples/config/openclaw/models.json).

## 4. Meet the example Task

[`examples/tasks/tasks.jsonl`](../../../examples/tasks/tasks.jsonl) contains four tasks from simple to more involved:

| ID | What it shows |
| --- | --- |
| `01_text_only` | Single-turn plain text |
| `02_local_markdown` | Read an input file and write to `outputs/` |
| `03_user_memory_context` | Inject `USER.md` and `MEMORY.md` |
| `04_multi_turn` | Two predefined user messages share one session |

A minimal task has only three fields:

```json
{"schema_version":"roller.task/v1","id":"01_text_only","prompt":"计算 2+3，只回复数字。"}
```

`--model` can be set once on the command line. If omitted, each task must have its own `model` field. Full schema: [Task format](../tasks/task-format.md).

## 5. Dry-run

Using OpenClaw as the example:

```bash
roller collect \
  --tasks examples/tasks/tasks.jsonl \
  --model my-openai-proxy/my-model \
  --model-config examples/config/openclaw/models.json \
  --parallel 4 \
  --dry-run
```

A dry-run will:

- read and normalize JSON/JSONL tasks.
- check duplicate IDs, conversation modes, and Harness limits.
- resolve input, Context, and output paths.
- run path-conflict, symlink, file-size, and content-digest checks.
- print the expanded task × model rollout plan.

It does not check the Docker daemon, whether images exist, or whether the model API is reachable, and it does not create a run directory.

## 6. Collect for real

```bash
roller collect \
  --tasks examples/tasks/tasks.jsonl \
  --model my-openai-proxy/my-model \
  --model-config examples/config/openclaw/models.json \
  --parallel 4 \
  --retries 1 \
  --timeout 600 \
  --output runs \
  --run-name quickstart
```

What the flags mean:

| Flag | Meaning |
| --- | --- |
| `--parallel 4` | Run at most 4 rollouts at once |
| `--retries 1` | After the first failure, retry at most 1 more time |
| `--timeout 600` | Total timeout per rollout is 600 seconds |
| `--output runs` | Host root directory for all runs |
| `--run-name quickstart` | This run is saved as `runs/quickstart` |

If a directory with the same name already exists, Roller refuses to overwrite it by default. Continue the original run with `--resume`. Start a new experiment with a new `--run-name`.

## 7. Check the results

After collection succeeds, inspect the run root:

```text
runs/quickstart/
├── info.json
├── lock.json
├── sft.jsonl
└── samples/
    └── <model>/
        └── <index>_<task-id>/
```

Each successful sample should at least have:

```text
trace.jsonl
trace_ACTF.json
trace_SFT.json
info.json
```

Check in this order:

1. Whether run `info.json.status` is `completed`, and whether success/failure counts match expectations.
2. Whether sample `info.json.status` is `ok`, and whether `usage` and version fields exist.
3. Whether reasoning, tool calls, and results are paired correctly in `trace_ACTF.json`.
4. Whether the top level of `trace_SFT.json` contains only `messages` and
   `tools` (image-bearing samples additionally carry `images`, with one
   `<image>` placeholder per entry).
5. For tasks that must deliver files, whether the sample `results/` contains the expected files.
6. Whether the number of lines in run `sft.jsonl` equals the number of successful samples that can be used for training.

For when each file is written and what is retained on failure, see [Trajectory outputs](../traces/trajectory-outputs.md).

## 8. Open the Viewer

```bash
roller view runs/quickstart
```

It listens on `127.0.0.1:8765` by default. For remote runs, keep the loopback address and forward the port over SSH from your machine:

```bash
ssh -L 8765:127.0.0.1:8765 your-host
```

The Viewer shows the ACTF timeline. Inspector is for the system prompt, tools schema, run info, and raw content. See the [Viewer guide](../traces/viewer.md).

## 9. Next steps

- Write your own data: [Task format](../tasks/task-format.md)
- Configure reasoning: [OpenClaw Harness guide](../harnesses/openclaw-configuration.md#thinking-system-prompt-and-wire-trace)
- Understand the training format: [SFT data format](../traces/sft-format.md)
- See every CLI option: [CLI reference](cli-reference.md)

## Common first-run issues

| Symptom | What to check |
| --- | --- |
| Docker preflight failed | Whether `docker version` can reach the Server. Whether the image has been pulled/loaded |
| OpenClaw cannot find the model | Whether `--model` matches `provider/models[].id` in the JSON exactly |
| Config reports a missing environment variable | Whether you `source examples/env/api.env`. Whether placeholder names match |
| `/responses` returns 404 | The service only supports Chat Completions. Set the OpenClaw provider `api` to `openai-completions` |
| A run with the same name already exists | Use `--resume` for the original experiment. Change `--run-name` for a new one |
| Viewer does not show new tasks | Confirm you passed the run root directory, refresh the page, and check run `info.json` |

The OpenClaw Harness guide lists additional OpenClaw-specific errors.
