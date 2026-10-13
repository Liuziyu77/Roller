# Getting started: from install to the first trajectory

This guide uses **OpenClaw** as the example and takes one Task from installation to a saved trajectory. Other Harnesses follow the same main workflow: prepare the configuration, run a dry-run, collect trajectories, check the results, and open the Viewer. Images, API configuration, and Harness-specific options differ, so refer to the corresponding Harness guide when adapting this workflow.

By the end, you will have checked the configuration with a dry-run, completed a real collection, found the output files, and opened the result in the Viewer. If Task, Harness, rollout, run, or trace are unfamiliar terms, read the [Introduction](introduction.md) first.

## 1. Prepare the runtime environment

You need:

- Python 3.10+.
- Docker (install separately).
- the OpenClaw image.
- a model API that supports OpenAI Responses or Chat Completions.

Use an isolated Python environment. This example uses Conda and Python 3.11. Run it from the repository root:

```bash
conda create -n roller python=3.11 -y
conda activate roller

python -m pip install -e .
roller --version
docker version
```

Using `python -m pip` ensures that pip belongs to the activated Python environment. After Docker is installed and started, `docker version` shows both Client and Server information.

On macOS, open **Docker Desktop → Settings → Resources → Network**, enable **Enable host networking**, and restart Docker Desktop. Roller uses host networking so OpenClaw can reach its local request proxy.

## 2. Prepare the OpenClaw image

OpenClaw supports both OpenAI Responses and Chat Completions.

Pull the pinned image online:

```bash
docker pull ghcr.io/openclaw/openclaw:2026.7.1-2
```

Alternatively, load an existing archive:

```bash
docker load -i /path/to/openclaw-2026.7.1-2-linux-amd64.tar.gz
```

If you use a custom image tag, pass it with `--docker-image` or set `ROLLER_OPENCLAW_IMAGE`.

Keep the default reasoning settings for the first run. Roller saves the visible reasoning text returned by the model service. If the service reports only reasoning token counts, there is no reasoning text to save.

## 3. Prepare the OpenClaw API configuration

Copy the local environment template:

```bash
cp examples/env/api.env.example examples/env/api.env
```

In `examples/env/api.env`, set `MY_PROXY_BASE_URL` and `MY_PROXY_API_KEY` to the actual model service URL and API key. This guide does not use the other Harnesses or Web Search, so leave the remaining variables empty.

Save the file, then load the variables into the current shell:

```bash
set -a
source examples/env/api.env
set +a
```


Open the repository template at [`examples/config/openclaw/models.json`](../../../examples/config/openclaw/models.json) and change the model `id` to the name of the model being used. In later commands, the model ID after the provider name must exactly match this `id`. For example, if you set `id` to `qwen3.8-flash`, use `--model my-openai-proxy/qwen3.8-flash` in the commands that follow.

## 4. Meet the example Task

[`examples/tasks/tasks.jsonl`](../../../examples/tasks/tasks.jsonl) contains four tasks from simple to more involved:

| ID | What it shows |
| --- | --- |
| `01_text_only` | Single-turn plain text |
| `02_local_markdown` | Read an input file and write to `outputs/` |
| `03_user_memory_context` | Inject `USER.md` and `MEMORY.md` |
| `04_multi_turn` | Two predefined user messages share one session |

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

A dry-run checks that Roller can read the tasks and options before real collection, then previews the rollouts it would run. It does not start containers, call the model API, or create run results. Continue to real collection after it finishes without errors.

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

1. Whether `state` in the top-level `info.json` for the run is `finished`, `summary.failed` is `0`, and `summary.completed` matches the expected number of executions.
2. Whether `status` in every sample `info.json` is `ok` and `usage.request_count` is greater than `0`.
3. Whether `trace_ACTF.json` contains the complete user input and model output. If tools were called, verify that each call is paired with its result.
4. Whether the top level of `trace_SFT.json` contains only `messages` and
   `tools` (image-bearing samples additionally carry `images`, with one
   `<image>` placeholder per entry).
5. For tasks that must deliver files, whether the sample `results/` contains the expected files.
6. Whether the number of lines in run `sft.jsonl` equals the number of samples that successfully produced `trace_SFT.json`.

For when each file is written and what is retained on failure, see [Trajectory outputs](../traces/trajectory-outputs.md).

## 8. Open the Viewer

```bash
roller view runs/quickstart
```

It listens on `127.0.0.1:8765` by default. After the command starts, open <http://127.0.0.1:8765> in a browser.

The Viewer shows the ACTF timeline. Inspector is for the system prompt, tools schema, run info, and raw content. See the [Viewer guide](../traces/viewer.md).

## 9. Next steps

- Write your own data: [Task format](../tasks/task-format.md)
- Configure reasoning: [OpenClaw Harness guide](../harnesses/openclaw-configuration.md#6-thinking-configuration)
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
