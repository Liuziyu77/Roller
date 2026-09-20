# CLI reference

This page lists Roller CLI flags shared across Harnesses and links to each Harness-specific reference. For the flags in your installed version, trust the command output:

```bash
roller --help
roller collect --help
roller view --help
```

## 1. Command structure

```text
roller collect [OPTIONS]   # 采集轨迹
roller view RUN_DIR        # 浏览已有 run
roller bench ...           # bench 翻译与评分汇总，见 §11
roller --version
```

The smallest collect command needs `--tasks`, and the model must come from repeated `--model` flags or each task's own `model` field:

```bash
roller collect --tasks tasks.jsonl --model provider/model ...
```

## 2. Configuration precedence

When a value can be set on both the task and the global CLI:

| Setting | Precedence |
| --- | --- |
| Model | If any `--model` is on the command line, use the CLI models. Otherwise use each task's `model` |
| Thinking | task `runtime.thinking` wins. Otherwise use `--thinking`. If both are omitted, the Harness/model default applies |
| Workspace | task `workspace` wins. Otherwise use the global `--workspace` |
| User Simulator model | `user_simulator.model` wins. Otherwise follow the current task Agent model |

Roller removes duplicate `--model` values and runs each task with each remaining model. `--start-index` and `--limit` select tasks from the normalized list before pairing them with models.

## 3. Shared collection flags

These flags apply to every Harness:

| Flag | Default | Description |
| --- | --- | --- |
| `--tasks PATH` | Required | JSON or JSONL task list |
| `--model MODEL` | task `model` | Repeatable, runs every selected model for each task |
| `--model-label LABEL` | Actual service model ID | Safe local name for sample directories and containers, requires one final model and does not change the upstream model ID |
| `--harness NAME` | task `harness` or `openclaw` | Selects `openclaw`, `codex`, `claude-code`, `hermes`, `opencode`, `deepseek`, or `pi` |
| `--parallel N` | `1` | Maximum parallel rollouts, at least 1 |
| `--grader-parallel N` | `--parallel` | Maximum parallel graders, useful for limiting judge-service load |
| `--samples N` | `3` for SkillsBench, otherwise `1` | Independent repetitions per task/model, each with its own sample directory |
| `--retries N` | `0` | Additional attempts after the first failure |
| `--timeout SECONDS` | `600` | Total timeout per rollout, greater than 0 |
| `--task-timeouts` | Off | Uses each task's `timeout_seconds`, falling back to `--timeout` |
| `--start-index N` | `0` | Starts at task N after normalization |
| `--limit N` | Unlimited | Selects at most N tasks |
| `--output DIR` | `runs` | Parent directory for run roots |
| `--run-name NAME` | UTC timestamp | Directory name for this run |
| `--resume` | Off | Validates the run lock and skips complete successful samples |
| `--dry-run` | Off | Validates tasks and files and prints the plan without starting Docker |
| `--workspace DIR` | None | Legacy global workspace, new tasks use `file_root` and `files` |
| `--prompt-key NAME` | `prompt` | Prompt field for legacy data |
| `--id-key NAME` | `id` | ID field for legacy data |
| `--log-level LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |

File limits:

| Flag | Default | Description |
| --- | --- | --- |
| `--max-input-file-bytes` | 10 GiB | Maximum size of one input or context file |
| `--max-task-input-bytes` | 50 GiB | Maximum total input size for one task |

## 4. Shared Harness runtime flags

These flags control container, reasoning, and trace behavior shared by all Harnesses. See the matching Harness guide for supported values and additional requirements.

| Flag | Default | Description |
| --- | --- | --- |
| `--docker-image IMAGE` | Pinned image for the Harness | Overrides the Harness image for this run |
| `--container-user USER` | `0` | User for the disposable container, `image` keeps the image default |
| `--docker-binary PATH` | `docker` | Docker CLI name or path |
| `--network MODE` | `host` | Container network mode, some full wire-trace paths require host networking |
| `--api-base URL` | Harness environment variable | Shared API address for a single-Harness run, OpenClaw uses its model config instead |
| `--api-key-env NAME` | Harness default variable | Host environment variable containing the model API key |
| `--pass-env NAME` | None | Passes one additional non-empty host environment variable, repeatable |
| `--thinking LEVEL` | Unset | Harness reasoning or thinking level |
| `--keep-thinking` / `--no-keep-thinking` | On | Controls whether ACTF and SFT export visible reasoning |
| `--wire-trace MODE` | `actf` | `off`, `actf`, or `full` |
| `--keep-container-on-error` | Off | Keeps the stopped disposable container after failure for debugging |

The CLI accepts the following logical thinking values. Each Harness guide defines its supported subset, defaults, and mappings.

```text
off | minimal | low | medium | high | xhigh | adaptive | max | ultra
```

Input `none` is normalized to `off`. Unsupported values fail before the rollout starts.

### Wire trace

| Mode | Output and purpose |
| --- | --- |
| `off` | Does not capture HTTP requests through the local reverse proxy |
| `actf` | Default, writes the actual tools schema into ACTF and keeps no extra wire file |
| `full` | Also keeps `wire_trace.jsonl` for auditing actual HTTP requests |

`full` can retain complete request text including prompts, reasoning, tool arguments, and results, so it is larger and more sensitive. See the matching Harness guide for support and network requirements, and see {ref}`Trajectory outputs <wire-trace-jsonl>` for the file format.

### Compatibility note

`--external-thinking` has been removed. Replace it with `--thinking` and delete obsolete arguments from old commands.

## 5. Harness-specific flag index

Each Harness guide owns its image, API, key, execution-limit, and native configuration flags. This page does not repeat those defaults, which prevents the same value from drifting across multiple pages.

| Harness | Flag and configuration guide |
| --- | --- |
| OpenClaw | {ref}`OpenClaw Harness guide <openclaw-cli-flags>` |
| Codex | {ref}`Codex Harness guide <codex-cli-flags>` |
| Claude Code | {ref}`Claude Code Harness guide <claude-code-cli-flags>` |
| Hermes | {ref}`Hermes Harness guide <hermes-cli-flags>` |
| OpenCode | {ref}`OpenCode Harness guide <opencode-cli-flags>` |
| DeepSeek | {ref}`DeepSeek Harness guide <deepseek-cli-flags>` |
| Pi Agent | {ref}`Pi Agent Harness guide <pi-cli-flags>` |

To inspect the complete flag set for the current checkout, run:

```bash
roller collect --help
```

## 6. Resume

```bash
roller collect [原命令] --resume
```

Resume first compares the settings that affect execution and the input digests in `lock.json`, then checks the required artifacts of successful samples. It skips a successful sample only when the settings and inputs match and the artifacts are complete. Changed settings or inputs are rejected. Samples with damaged artifacts are rerun.

Use `--resume` to continue the same run. Use a new `--run-name` when you change the model, image, thinking, wire, or any other semantic setting.

## 7. Viewer flags

```bash
roller view RUN_DIR [--host HOST] [--port PORT]
```

| Flag | Default | Description |
| --- | --- | --- |
| `RUN_DIR` | Required | A Roller run that contains `info.json` and `samples/` |
| `--host` | `127.0.0.1` | Listen address |
| `--port` | `8765` | Listen port |

Listening on `0.0.0.0` exposes trajectory contents to the network. For remote access, use the default address and SSH port forwarding.

## 8. Bench flags

```bash
roller bench list
roller collect --bench NAME --bench-root ROOT [--bench-category CAT]... [--bench-task-id ID]... [--bench-variant NAME] [--bench-timeout-multiplier N] ...
roller bench export NAME ROOT --output TASKS.jsonl [--category CAT]... [--task-id ID]... [--variant NAME] [--timeout-multiplier N]
roller bench report RUN_DIR [--output REPORT.json]
```

Prefer `collect --bench`: one command converts tasks, runs containers, grades, and
aggregates, saving `tasks.jsonl` and `bench-report.json` in the run directory. It
automatically honors each benchmark task's timeout. `--bench-category` and
`--bench-task-id` are repeatable. `--bench-variant` and
`--bench-timeout-multiplier` correspond to the export flags below.

| Flag | Default | Description |
| --- | --- | --- |
| `NAME` | Required | Adapter name from `roller bench list` |
| `ROOT` | Required | Checked-out benchmark directory |
| `--output` / `-o` | Required for export | Task JSONL written by export. Optional report JSON for report |
| `--category CAT` | Unlimited | Export only this category. Repeatable |
| `--task-id ID` | Unlimited | Export only this task. Repeatable. A wrong id errors instead of being skipped silently |
| `--variant NAME` | Adapter default | Select a bench condition, such as SkillsBench `with-skill` / `no-skill` |
| `RUN_DIR` | Required | Completed run directory. It is a **positional argument** (not `--run`) |
| `--rebuild-task-images` | Off | Rebuilds task-specific benchmark images even when their tags already exist |

`bench export` only translates. It does not run any task, go online, or start a container.
`--timeout-multiplier` must be finite and positive and scales each task's own timeout at export time.
For the manual export + collect workflow, remember `--task-timeouts` on collect. Both
workflows use `--pass-env` to pass through variables the grader needs.
`swebench-pro` keeps export offline; the first collection of an instance pulls its official image and builds a local Harness-derived image.

Field semantics, scoring timeline, `stage` leak-prevention rules, and aggregation rules are in [Bench integration and scoring](../benches/bench-format.md).
