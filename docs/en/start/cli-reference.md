# CLI reference

[Documentation home](../index.md) · [Getting started](getting-started.md) · [Task format](../tasks/task-format.md) · [Trajectory outputs](../traces/trajectory-outputs.md)

This page lists Roller CLI flags by scope. For the flags in your installed version, trust the command output:

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
| Model | If any `--model` is on the command line, use the CLI models; otherwise use each task's `model` |
| Thinking | task `runtime.thinking` wins; otherwise use `--thinking`; if both are omitted, the Harness/model default applies |
| Workspace | task `workspace` wins; otherwise use the global `--workspace` |
| User Simulator model | `user_simulator.model` wins; otherwise follow the current task Agent model |

Repeated `--model` values are de-duplicated and Cartesian-expanded with each task. `--start-index` and `--limit` first slice the normalized task list, then models are expanded.

## 3. Shared collection flags

These flags apply to all seven Harnesses:

| Flag | Default | Description |
| --- | --- | --- |
| `--tasks PATH` | Required | JSON or JSONL task list |
| `--model MODEL` | task `model` | Repeatable; run every listed model for each task |
| `--harness NAME` | task `harness` or `openclaw` | `openclaw`, `codex`, `claude-code`, `hermes`, `opencode`, `deepseek`, or `pi`; when set explicitly, overrides the Task Agent field for the whole run |
| `--docker-image IMAGE` | Default image for that Harness | Global image override; mixed participants can set `--openclaw-image`, `--codex-image`, `--claude-code-image`, `--hermes-image`, `--opencode-image`, `--deepseek-image`, and `--pi-image` separately |
| `--api-base URL` / `--api-key-env NAME` | Harness-specific defaults | Legacy-compatible; mixed runs can set `--codex-api-base`, `--claude-api-base`, `--hermes-api-base`, `--opencode-api-base`, `--deepseek-api-base`, `--pi-api-base`, and the matching `*-api-key-env` separately |
| `--parallel N` | `1` | Maximum parallel rollouts; must be at least 1 |
| `--retries N` | `0` | Extra attempts after the first failure |
| `--timeout SECONDS` | `600` | Total timeout per rollout; must be greater than 0 |
| `--task-timeouts` | Off | Use each task's own `timeout_seconds`, falling back to `--timeout` when missing. Bench runs usually must enable this; see [Bench integration and scoring](../benches/bench-format.md#7-cli) |
| `--start-index N` | `0` | Start from the Nth normalized task |
| `--limit N` | Unlimited | Select at most N tasks |
| `--output DIR` | `runs` | Parent directory of the run root |
| `--run-name NAME` | UTC timestamp name | Directory name for this run |
| `--resume` | Off | Validate the run lock and skip complete successful samples |
| `--dry-run` | Off | Validate tasks and files and print the plan; do not start Docker |
| `--workspace DIR` | None | Legacy global workspace; new tasks use `file_root`/`files` |
| `--prompt-key NAME` | `prompt` | Prompt field name in legacy data |
| `--id-key NAME` | `id` | ID field name in legacy data |
| `--log-level LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |

File limits:

| Flag | Default | Description |
| --- | --- | --- |
| `--max-input-file-bytes` | 10 GiB | Per input/context file cap |
| `--max-task-input-bytes` | 50 GiB | Total input cap for one task |

## 4. Shared Harness runtime flags

These flags are shared by all seven Harnesses, but allowed values and semantics are validated by each Harness:

| Flag | Default | Description |
| --- | --- | --- |
| `--docker-image IMAGE` | Fixed image for that Harness | Override the image for this run |
| `--container-user USER` | `0` | One-shot container user; `image` keeps the image default user |
| `--docker-binary PATH` | `docker` | Docker CLI name or path |
| `--network MODE` | `host` | Container network; the local wire proxy requires host network |
| `--pass-env NAME` | None | Repeatable; pass through extra non-empty host environment variables |
| `--thinking LEVEL` | Unset | Harness reasoning/thinking level |
| `--keep-thinking` / `--no-keep-thinking` | On | Whether ACTF/SFT export reasoning; for OpenClaw also controls stripping it from request history |
| `--wire-trace MODE` | `actf` | `off`, `actf`, or `full` |
| `--keep-container-on-error` | Off | Keep the one-shot container on failure for debugging |

Parsed values for `--thinking`:

```text
off | minimal | low | medium | high | xhigh | adaptive | max | ultra
```

Input `none` is first normalized to `off`. That does not mean every Harness or model accepts every level:

| Harness | Current Roller mapping |
| --- | --- |
| OpenClaw 2026.7.1 | Accepts up to `max`; the selected model profile still validates further |
| Codex 0.149.0 | `minimal/low/medium/high/xhigh`; omit to use the default |
| Claude Code 2.1.222 | `low/medium/high/xhigh/max/ultra`, where `ultra` maps to `ultracode` |

Authoritative mappings are in the reasoning sections of each Harness guide.

### Wire trace

| Mode | Outputs and purpose | DeepSeek Harness |
| --- | --- | --- |
| `off` | No HTTP proxy capture; OpenCode/Hermes/DeepSeek still journal native request schemas | Native reasoning |
| `actf` | Default; retain system/tools needed for ACTF and remove intermediate wire files | Not supported |
| `full` | Also retain `wire_trace.jsonl` for auditing the actual HTTP requests | Not supported |

`full` stores complete request text including prompts, reasoning, tool arguments, and results, so size and sensitivity are both higher. See {ref}`Trajectory outputs <wire-trace-jsonl>`.

## 5. OpenClaw-only flags

| Flag | Default | Description |
| --- | --- | --- |
| `--model-config PATH` | None | OpenClaw providers/models JSON; supports `${ENV_NAME}` |
| `--gateway-port N` | `18789` | Starting gateway port for worker 0 |
| `--gateway-port-step N` | `10` | Port stride between worker slots |
| `--gateway-startup-timeout SEC` | `30` | How long to wait for the gateway to become ready |
| `--session-id NAME` | `roller` | OpenClaw session ID prefix |
| `--search-provider NAME` | None | OpenClaw web search provider |
| `--cache-trace` / `--no-cache-trace` | On | Record the assembled system prompt each time |
| `--set-image-model` / `--no-set-image-model` | On | Whether to also try setting the image model |
| `--collect-path PATH` | None | Legacy: also collect a container path into `results/legacy` |

Default image precedence: `--docker-image` → `ROLLER_OPENCLAW_IMAGE` → `ghcr.io/openclaw/openclaw:2026.7.1-2`.

## 6. Codex-only flags

| Flag | Default | Description |
| --- | --- | --- |
| `--api-base URL` | `OPENAI_BASE_URL` | Responses API base |
| `--api-key-env NAME` | `OPENAI_API_KEY` | Host environment variable that holds the API Key |
| `--codex-config PATH` | None | Config copied into an isolated `$CODEX_HOME/config.toml` |
| `--reasoning-summary MODE` | Codex default | `auto`, `concise`, `detailed`, or `none` |
| `--codex-web-search MODE` | `disabled` | `disabled`, `cached`, or `live` |
| `--codex-websockets` / `--no-codex-websockets` | Off | Whether a custom provider may use Responses WebSocket |

Default image precedence: `--docker-image` → `ROLLER_CODEX_IMAGE` → `roller-codex:0.149.0`.

Custom OpenAI-compatible APIs default to Responses HTTP streaming. External thinking or full wire trace requires an explicit `--api-base`, the default host network, and `--no-codex-websockets`.

## 7. Claude Code-only flags

| Flag | Default | Description |
| --- | --- | --- |
| `--api-base URL` / `--anthropic-base-url URL` | `ANTHROPIC_BASE_URL` | Anthropic origin, `/v1` base, or `/v1/messages` endpoint |
| `--api-key-env NAME` | `ANTHROPIC_API_KEY` | Host environment variable that holds the API Key |
| `--claude-settings PATH` | None | Settings JSON injected into the isolated container |
| `--claude-max-turns N` | None | Claude agent turn cap per predefined user message |
| `--claude-max-thinking-tokens N` | None | `MAX_THINKING_TOKENS` inside the container |
| `--claude-permission-mode MODE` | `bypassPermissions` | Claude Code permission mode |
| `--claude-signature-recovery MODE` | `off` | `off`, `fill`, or `prefer` |
| `--claude-signature-recovery-attempts N` | `1` | Maximum replay attempts per signed block |
| `--claude-signature-recovery-max-tokens N` | `8192` | Maximum visible output tokens per replay |

Default image precedence: `--docker-image` → `ROLLER_CLAUDE_CODE_IMAGE` → `roller-claude-code:2.1.222`.

Signature recovery must keep `--keep-thinking` on. It is an extra provider request after the task finishes, not local decryption; see the [Claude Code guide](../harnesses/claude-code-configuration.md#signature-recovery-experimental).

## 8. External thinking

This option is mainly for GPT reasoning models such as `gpt-5.6-sol`. When an open-source model API such as Qwen or GLM returns
plaintext reasoning, keep the default `--keep-thinking` and do not use external thinking. Claude signed
thinking uses `--claude-signature-recovery`, not this option.

```text
--external-thinking off|external|external_always
```

| Mode | OpenClaw | Codex | Claude Code | Hermes | OpenCode |
| --- | --- | --- | --- | --- | --- |
| `off` | Supported, default | Supported, default | Only supported value | Only supported value | Only supported value |
| `external` | Responses provider | Responses HTTP | Not supported | Not supported | Not supported |
| `external_always` | Responses provider | Responses HTTP | Not supported | Not supported | Not supported |

When enabled, Roller turns off native reasoning and normalizes `deep_think` tool arguments into SFT `reasoning_content`. Stage protocol, checks, and safety bounds are in [External Thinking](../traces/external-thinking.md).

## 9. Resume

```bash
roller collect [原命令] --resume
```

Resume is not “ignore every existing file”. Roller first compares semantic config and input digests in `lock.json`, then verifies required artifacts of successful samples; only samples whose lock matches and whose artifacts are complete are skipped. A config change, input change, or damaged artifact is rejected as a mix, or the sample is rerun.

Use `--resume` when you should continue the same run; use a new `--run-name` when you change the model, image, thinking, wire, or any other semantic setting.

## 10. Viewer flags

```bash
roller view RUN_DIR [--host HOST] [--port PORT]
```

| Flag | Default | Description |
| --- | --- | --- |
| `RUN_DIR` | Required | A Roller run that contains `info.json` and `samples/` |
| `--host` | `127.0.0.1` | Listen address |
| `--port` | `8765` | Listen port |

Listening on `0.0.0.0` exposes trajectory contents to the network. For remote access, use the default address and SSH port forwarding.

## 11. Bench flags

```bash
roller bench list
roller bench export NAME ROOT --output TASKS.jsonl [--category CAT]... \
  [--task-id ID]... [--runs-per-task N]
roller bench report RUN_DIR [--output REPORT.json]
```

| Flag | Default | Description |
| --- | --- | --- |
| `NAME` | Required | Adapter name from `roller bench list` |
| `ROOT` | Required | Checked-out benchmark directory |
| `--output` / `-o` | Required for export | Task JSONL written by export; optional report JSON for report |
| `--category CAT` | Unlimited | Export only this category; repeatable |
| `--task-id ID` | Unlimited | Export only this task; repeatable; a wrong id errors instead of being skipped silently |
| `--runs-per-task N` | `1` | Emit N independent rollouts per selected task; ids end in `__run_01`, etc. |
| `RUN_DIR` | Required | Completed run directory; a **positional argument** (not `--run`) |

`bench export` only translates; it does not run any task, go online, or start a container.
When collecting, remember `--task-timeouts`, and use `--pass-env` to pass through variables the grader needs.

Field semantics, scoring timeline, `stage` leak-prevention rules, and aggregation rules are in [Bench integration and scoring](../benches/bench-format.md).

## Hermes

| Flag | Default | Purpose |
| --- | --- | --- |
| `--hermes-image` | `ROLLER_HERMES_IMAGE` or `roller-hermes:d15ed4445207` | Pinned container image |
| `--hermes-api-base` | `--api-base`, then `OPENAI_BASE_URL`, then OpenAI `/v1` | Chat Completions API base |
| `--hermes-api-key-env` | `--api-key-env`, then `OPENAI_API_KEY` | Host key variable name |
| `--hermes-context-length` | Provider metadata | Optional context window override, at least 64000 tokens |
| `--hermes-max-iterations` | `12` | Model iterations per user turn; must be positive |

Hermes accepts `off/minimal/low/medium/high/xhigh` thinking hints, subject to native provider/model handling. External thinking is unsupported. Native request snapshots preserve tool schemas in every wire mode; `full` also retains the HTTP trace and requires an explicit API base and host networking. See [Hermes configuration](../harnesses/hermes-configuration.md).

## OpenCode

| Option | Default |
| --- | --- |
| `--opencode-image` | `roller-opencode:1.18.30` |
| `--opencode-api-base` | `--api-base` / `OPENAI_BASE_URL` |
| `--opencode-api-key-env` | `--api-key-env` / `OPENAI_API_KEY` |
| `--opencode-max-steps` | 12 |
| `--opencode-context-length` | 128000 |
| `--opencode-output-tokens` | 8192 |

See [native sessions, request capture, and limitations](../harnesses/opencode-configuration.md).

## DeepSeek Harness

| Option | Default |
| --- | --- |
| `--deepseek-image` | `roller-deepseek:0.1.5rc1` |
| `--deepseek-api-base` | `--api-base` / `DEEPSEEK_BASE_URL` / `https://api.deepseek.com` |
| `--deepseek-api-key-env` | `--api-key-env` / `DEEPSEEK_API_KEY` |
| `--deepseek-max-steps` | 12 |
| `--deepseek-context-length` | 128000 |
| `--deepseek-output-tokens` | 8192 |

[DeepSeek Harness](../harnesses/deepseek-configuration.md): `sdk-minimal`, persistent Bash, `off/low/high/max` thinking.

## Pi Agent Harness

| Flag | Default |
| --- | --- |
| `--pi-image` | `roller-pi:0.85.1` |
| `--pi-api-base` | `--api-base` / `OPENAI_BASE_URL` / `https://api.openai.com/v1` |
| `--pi-api-key-env` | `--api-key-env` / `OPENAI_API_KEY` |
| `--pi-max-steps` | 12 |
| `--pi-context-length` | 128000 |
| `--pi-output-tokens` | 8192 |

[Pi Agent](../harnesses/pi-configuration.md): `off/minimal/low/medium/high/xhigh/max` thinking; default `off`.
