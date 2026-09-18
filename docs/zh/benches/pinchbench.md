# PinchBench

[文档中心](../index.md) · [Bench 接入与评分](bench-format.md) · [OpenClaw 配置](../harnesses/openclaw-configuration.md)

Roller 从 PinchBench 的 canonical `tasks/manifest.yaml` 导出任务，支持 automated、LLM judge、
hybrid grader 和真正的多 session 任务。上游：<https://github.com/pinchbench/skill>。

## 最短路径

```bash
scripts/prepare-pinchbench.sh "$PWD/.benchmarks/pinchbench"
export PINCHBENCH_ROOT="$PWD/.benchmarks/pinchbench"

set -a
. /mnt/public/users/dingshengyuan/_shared/harness_evals/.env
set +a
export ROLLER_RUN_ROOT=/mnt/public/users/$USER/_shared/roller-runs/pinchbench
mkdir -p "$ROLLER_RUN_ROOT"
```

准备脚本会 clone 上游数据并拉取固定的 `linux/amd64` OpenClaw 镜像。脚本可重复执行，
已有 checkout 时不会自动 pull 或改写它，且不读取或写入任何 key。脚本最后会打印
实际 commit，正式对比时应记录该 revision。若手工准备，等价步骤是 clone
<https://github.com/pinchbench/skill> 并拉取 `ghcr.io/openclaw/openclaw:2026.7.1-2`。

准备一个不含真实 key 的 OpenClaw 模型配置，例如 `kimi.models.json`。这里的 model ID
虽然是兼容名称，实际后端是 Kimi-K3：

```json
{
  "mode": "merge",
  "providers": {
    "kimi": {
      "baseUrl": "https://joyrouter.jd.com/v1",
      "apiKey": "${HE_JUDGE_KEY}",
      "api": "openai-completions",
      "timeoutSeconds": 600,
      "models": [{"id": "Qwen3-32B-for-Game-Judge", "name": "Kimi-K3"}]
    }
  }
}
```

先跑无需 judge 的自动评分 smoke test：

```bash
roller collect --bench pinchbench --bench-root "$PINCHBENCH_ROOT" \
  --bench-variant automated-only \
  --bench-task-id task_sanity --bench-task-id task_files \
  --model kimi/Qwen3-32B-for-Game-Judge --model-config kimi.models.json \
  --parallel 1 --grader-parallel 1 \
  --output "$ROLLER_RUN_ROOT" --run-name kimi-smoke
```

这条命令会自动导出任务、按逐题 timeout 执行并评分，最后写出
`kimi-smoke/tasks.jsonl` 与 `kimi-smoke/bench-report.json`。

运行 `all` 或 `core` 中的 LLM/hybrid 任务时，正式可比口径使用 PinchBench 当前的
默认 judge `anthropic/claude-haiku-4.5`。只需提供 OpenRouter key；Roller 默认使用
`https://openrouter.ai/api/v1`：

```bash
export OPENROUTER_API_KEY=...

roller collect --bench pinchbench --bench-root "$PINCHBENCH_ROOT" \
  --bench-variant core --bench-timeout-multiplier 2 \
  --model kimi/Qwen3-32B-for-Game-Judge --model-config kimi.models.json \
  --samples 1 --parallel 1 --grader-parallel 1 \
  --output "$ROLLER_RUN_ROOT" --run-name kimi-core
```

## Variant 与执行语义

| Variant | 内容 |
| --- | --- |
| `all` | manifest 中的全部任务；默认值 |
| `automated-only` | 只含纯自动 grader 的任务，适合便宜的 smoke test |
| `core` | manifest 的代表性 core 子集 |

- `workspace_files` 会先做内容寻址快照，再复制为每个 sample 的可写工作区；源 checkout 不会被修改。
- `sessions[].new_session: true` 会创建新的 OpenClaw session，但保留同一容器与文件系统；评分时合并所有 session transcript。
- 私有图片答案只在 Agent 完成后进入容器，避免答案泄漏。
- judge 默认模型、prompt、system message 和直连请求参数均与当前上游一致：
  `anthropic/claude-haiku-4.5`、`temperature=0`、`max_completion_tokens=2048`、非流式、
  300 秒超时和单次请求。grader 总墙钟上限为 3600 秒；它只防止外围 grader 进程挂死，
  不改变 judge 请求参数。
- 自定义 OpenAI-compatible 网关可通过 `PINCHBENCH_JUDGE_BASE_URL`、
  `PINCHBENCH_JUDGE_API_KEY`、`PINCHBENCH_JUDGE_MODEL` 覆盖。若兼容服务只接受
  `max_tokens`，显式设置 `PINCHBENCH_JUDGE_MAX_TOKENS_FIELD=max_tokens`；若必须流式，
  显式设置 `PINCHBENCH_JUDGE_STREAM=true`。这些运行属于兼容性验证，judge 模型不同则
  分数不能宣称与官方口径可比。
- 思考型兼容 judge 可能在输出 JSON 前耗尽官方默认的 2048 token；这类模型可将
  `PINCHBENCH_JUDGE_MAX_TOKENS` 调大（例如 `8192`）。设置非零
  `PINCHBENCH_JUDGE_RETRIES` 后，Roller 不仅重试传输错误，也会重试 HTTP 成功但没有可解析
  分数的响应；流式响应正文为空时，还会回退解析 `reasoning_content`。
- frontmatter 的 `prerequisites` 会保留在 bench metadata。对于 4 个 FWS/GWS/GitHub integration 任务，Roller 会在一次性任务容器内自动安装固定兼容版本的 `fws`、`gws` 和 `gh`，启动上游 FWS mock service，并把 CLI 暴露给 OpenClaw；下载会遵循当前 shell 的标准 HTTP(S) 代理环境变量。

## 上游参数映射

| PinchBench | Roller |
| --- | --- |
| `--model`、`--base-url`、`--thinking` | `collect --model`、模型配置/`--api-base`、`--thinking` |
| `--judge` | 默认与官方 judge 一致；也可用 `PINCHBENCH_JUDGE_MODEL` 与 judge base URL/key 环境变量覆盖 |
| `--suite all/automated-only`、task ID、core/category | `collect --bench-variant`、`--bench-task-id`、`--bench-category` |
| `--runs N` | `collect --samples N` |
| `--timeout-multiplier N` | `collect --bench-timeout-multiplier N` |
| parallel judge 开关 | `collect --grader-parallel N`；设为 1 即串行判分 |
| `--no-fail-fast` | Roller 默认跑完所有任务并逐条记录失败 |
| upload/register/trend/judge-cache | 不属于 rollout 执行；Roller 报告只写本地，不自动上传 |

Roller 本地报告不会自动上传 PinchBench leaderboard。其他依赖搜索、第三方 integration 或
skill registry 的任务仍可能需要对应网络与凭据；上游 4 个 FWS mock 任务会由 Roller 自动准备。
