# WildClawBench

[Documentation home](../index.md) · [Bench integration and scoring](bench-format.md) · [CLI reference](../start/cli-reference.md)

60 real-scenario OpenClaw tasks in 6 categories. Upstream:
<https://github.com/InternLM/WildClawBench>.

This page covers **only this bench's own configuration surface**: which knobs
the upstream has, what they map to on the Roller side, and which were not
ported. Generic bench-layer mechanics (`bench` block, grading timing, `stage`
leak prevention, aggregation) are in
[Bench integration and scoring](bench-format.md).

## 1. Shortest path

```bash
# 1) 上游 checkout（Roller 不提供 setup 脚本，bench export 就是那层翻译）
git clone https://github.com/InternLM/WildClawBench ~/WildClawBench

# 2) 上游的环境变量照常 source —— Roller 复用同一批变量名，见 §3
set -a; . ~/WildClawBench/.env; set +a

# 3) 翻译成 task 行（不执行、不联网、不起容器）
roller bench export wildclawbench ~/WildClawBench -o /tmp/wcb.jsonl

# 4) 采集 + 判分（--task-timeouts 必须开：每个任务自带超时，见 §4）
roller collect --tasks /tmp/wcb.jsonl --model <provider>/<model> \
  --harness openclaw --openclaw-image wildclawbench-ubuntu:v1.3 \
  --model-config my_api.json --task-timeouts --parallel 4 \
  --output /tmp/wcb --run-name run1 \
  --pass-env NODE_EXTRA_CA_CERTS

# 5) 汇总
roller bench report /tmp/wcb/run1
```

## 2. Upstream knobs → Roller equivalents

CLI flags of `eval/run_batch.py`:

| Upstream | Roller | Notes |
| --- | --- | --- |
| `--task` / `--category` | `bench export --task-id` / `--category` | Roller can pass several; a wrong id errors instead of silently running empty |
| `--model` | `collect --model` | Repeatable; multi-model Cartesian expansion |
| `--parallel` | `collect --parallel` | |
| `--agent-backend` | `collect --harness` | The adapter writes `openclaw`; `codex`/`claude-code` can override with `--harness`; `hermesagent` has no equivalent |
| `--models-config` | `collect --model-config` | Both sides expand `${ENV}` |
| `--thinking` | `collect --thinking` | The adapter does **not** hard-code thinking in the task, or the task level would override the command line |
| `--lobster-env` | `collect --pass-env` | |
| `--lobster-name` | `collect --run-name` | Affects only the artifact directory name |
| `--global-parallel` | none (Roller has one global pool) | Equivalent to the upstream `--global-parallel` always being on; upstream default is to finish one category pool serially, then the next |
| `--lobster-workspace` | **not ported** | Upstream uses it to inject a persona workspace such as SOUL.md / USER.md. To use that now, add `files.context` to the task row yourself |
| `--openclaw-image-model` | partial: `--set-image-model` / `--no-set-image-model` | Roller can only "use the same chat model or set none"; it cannot assign **another** model to the image tool |

Environment variables (`.env`):

| Upstream | Roller | Notes |
| --- | --- | --- |
| `DOCKER_IMAGE` | `collect --openclaw-image` | |
| `GATEWAY_PORT` | `collect --gateway-port` | Roller also has `--gateway-port-step` so parallel workers can stagger |
| `TASKS_SUBDIR` | `ROOT` positional argument of `bench export` | You can also pass `<root>/tasks_x25`: use `ROOT/tasks` when it exists, otherwise treat `ROOT` itself as the task directory |
| `OUTPUT_SUBDIR` | `collect --output` + `--run-name` | |
| `DEFAULT_MODEL` / `DEFAULT_PARALLEL` | no default-value mechanism | Pass `--model` / `--parallel` explicitly |
| `HTTP_PROXY_INNER` / `HTTPS_PROXY_INNER` / `NO_PROXY_INNER` | host `http_proxy` / `https_proxy` / `no_proxy` | The OpenClaw harness already passes these names by default; see §3 |
| `BRAVE_API_KEY` | same name, passed through by default | Also the search shim token |
| `SEARCH_SHIM_HOST_IP` / `SEARCH_SHIM_HOSTNAME` / `SEARCH_SHIM_CERT` | read the same-name variables **at export** | See §5 |
| `OPENROUTER_API_KEY` / `OPENROUTER_BASE_URL` / `JUDGE_MODEL` | same name | Graders for 46/60 tasks depend on them for scoring; see §3 |
| `WILDCLAW_TIMEOUT_MULTIPLIER` | read the same-name variable **at export** | See §4 |

## 3. How environment variables enter the container

Three paths, with different semantics:

| Source | If missing |
| --- | --- |
| Task `## Env` → `runtime.env` (name list) | **Hard failure**, reports `requires missing environment variable X` |
| OpenClaw harness default list (`OPENROUTER_*`, `BRAVE_API_KEY`, `TAVILY_API_KEY`, both-case proxy names) | Silently skipped (empty values are not passed) |
| `--pass-env NAME` | Silently skipped |
| `bench.grade.env` (also from `## Env`) | **Warn only**: `grading environment variable X is unset` |

The scoring path injects host env with `-e` into `docker exec`. Extra
`--pass-env` is not required.

⚠️ **Scores are incomparable when the scoring gateway or judge model differs.**
46 of the 60 tasks are scored by a judge, so `OPENROUTER_BASE_URL` +
`JUDGE_MODEL` are part of the score, not a runtime detail. Check both before
comparing two runs.

## 4. Timeouts

Each task carries its own `timeout_seconds` (five measured values:
300/360/600/900/1200), so **`--task-timeouts` must be on**. Otherwise everything
falls back to a single `--timeout`.

To relax timeouts, use the same-name upstream environment variable, read at
export:

```bash
WILDCLAW_TIMEOUT_MULTIPLIER=2 roller bench export wildclawbench ~/WildClawBench -o /tmp/wcb_x2.jsonl
```

The value is `ceil(base × multiplier)`, matching upstream
`tools/run_with_timeout_multiplier.py`. Illegal values (non-numeric, 0,
negative) error immediately and do not silently run at 1×.

⚠️ **Do not use `--timeout` as a stand-in.** The upstream multiplier works by
rewriting `timeout_seconds` in the task file, and **the same value is
interpolated into the prompt prefix** (§6), so after scaling, "the budget told
to the Agent" still matches the real wall clock. `--timeout` changes only the
wall clock — the Agent thinks it has 300 seconds when it actually has 600. The
adapter scales before building the task row, from the same source, so the two
cannot disagree.

## 5. Search: upstream does not hit real Brave

Upstream points `api.search.brave.com` at a local shim with a **Brave protocol
and a Serper backend** (`tools/serper_brave_shim.py`, self-signed TLS on port
443), using `docker run --add-host` plus a mounted certificate. Roller has
neither `--add-host` nor an extra mount flag, so the adapter writes the same
wiring as two `runtime.setup` commands (as root, before the Agent):

1. Write `<SEARCH_SHIM_HOST_IP> <SEARCH_SHIM_HOSTNAME>` into `/etc/hosts`;
2. Inline the contents of `SEARCH_SHIM_CERT` into
   `/usr/local/share/ca-certificates/wildclaw-search-shim.crt`, run
   `update-ca-certificates`, and append it to certifi's bundle
   (requests/httpx do not read the system store).

These are read **at export** (same names and defaults as upstream; a relative
`SEARCH_SHIM_CERT` is likewise resolved against the checkout root), so source
`.env` before export. If unset, they are skipped, with one log line.

⚠️ **Node does not read the system certificate store**, and OpenClaw
`web_search` is a Node request. So you also need:

```bash
export NODE_EXTRA_CA_CERTS=/usr/local/share/ca-certificates/wildclaw-search-shim.crt
roller collect ... --pass-env NODE_EXTRA_CA_CERTS
```

If you skip this step, **every search returns
`{"status":"error","tool":"web_search","error":"fetch failed"}`**, while the
Agent keeps working and the run still produces scores — a silent capability
gap, not an error. After a run, check:

```bash
grep -c '"toolName": "web_search"' <run>/samples/*/*/trace.jsonl
grep -c 'fetch failed'             <run>/samples/*/*/trace.jsonl
```

`web_fetch` goes through the proxy and is unrelated to the shim, so
"`web_fetch` is 200" does not prove search is working.

## 6. Prompt: the upstream runner prepends a prefix

Upstream `eval/run_batch.py` **prepends** a 277-character prefix to every
task prompt (the variable is named `system_prompt`, but it is not a system
message; it is concatenated into the user prompt) and interpolates that task's
`timeout_seconds`. The adapter reproduces this prefix, so the exported prompt
matches upstream byte for byte. Omitting it means all 60 tasks are asking a
different question.

## 7. `## Workspace Path` is not the Agent workspace

```
<declared>/
├── exec/    # 种子文件 —— 这才是 Agent 的工作目录（60/60 个任务都有）
├── tmp/     # warmup 用的 fixture（5 个任务有）→ files.context，落到 <workspace>/tmp
└── gt/      # ground truth（26 个任务有）→ bench.grade.stage，评分时才进容器
```

Mounting the entire `<declared>` directory does two things at once: seed files
are one level wrong (skills and warmup write `/tmp_workspace/utils.py`, but the
file is actually under `/tmp_workspace/exec/`), and `gt/` is placed in front of
the Agent. So `workspace` points at `<declared>/exec` — **leakage is closed by
directory structure, not by filtering**.

## 8. Section mapping

| Upstream section | Maps to |
| --- | --- |
| `## Prompt` | `prompt` (with the §6 prefix prepended) |
| `## Env` | `runtime.env` and `bench.grade.env` |
| `## Warmup` | `runtime.setup` (after the §5 shim wiring) |
| `## Skills` | `files.context` entries with `role: skill` |
| `## Workspace Path` | See §7 |
| `## Automated Checks` | `bench.grade.code` |
| frontmatter `timeout_seconds` | `timeout_seconds` |

## 9. Known pitfalls

- **The image may bake `http_proxy` into environment variables**
  (`wildclawbench-ubuntu:v1.3` does, and has no matching `no_proxy`). Requests
  to a self-hosted gateway are black-holed. The symptom is `request_count=0`
  and every task exiting in the same second — that tidy exit time is a proxy
  timeout, not a slow model. Use the `*_INNER` proxy group in the WCB `.env`
  (their `NO_PROXY_INNER` allowlists intranet addresses). A side effect:
  **`curl` failing inside the container is not a reachability verdict**.
  OpenClaw's HTTP client does not read `http_proxy` and may still be fine.
- **The model's output budget must cover reasoning.** Models that return
  `reasoning_content` emit reasoning first, then the body. A small `maxTokens`
  yields `finish_reason: length` plus empty `content` — it looks like the model
  did nothing. The same model was empty at `max_tokens=16` and finished
  normally at 400. Size `maxTokens` in `--model-config` for long tasks.
- **`SEARCH_SHIM_CERT` is a relative path in the upstream `.env`** (relative to
  the WCB checkout root). Roller resolves it against `ROOT` at export. If you
  `--pass-env` it into the container, that relative path is meaningless there.
- **`info.json` `status` is not the failure rate.** `bench report` reads
  `score.json`. `status: error` has four common shapes, and three of them (an
  image in an observation, the wall clock cutting mid-tool-call, a last turn
  that is only a tool call with no closing text) **still have a valid score**.
  See
  [Bench integration and scoring §3.2](bench-format.md#32-a-grading-failure-is-not-a-0-score).
