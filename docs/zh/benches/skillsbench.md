# SkillsBench

[文档中心](../index.md) · [Bench 接入与评分](bench-format.md) · [OpenCode 配置](../harnesses/opencode-configuration.md)

Roller 支持 SkillsBench 的 `with-skill` 与 `no-skill` 两种条件，并直接运行每个 task
自带的 `verifier/test.sh`。上游：<https://github.com/benchflow-ai/skillsbench>。

SkillsBench v1.1 的评分不调用 LLM judge；87 个任务均以各自的 `verifier/test.sh` 为准。
上游 README 中的 GPT-5.5 是被测 agent 模型，不是评分模型。若要复现 GPT-5.5 条目，
应把 `collect --model` 配成真实的 GPT-5.5 服务；用 Kimi-K3 跑得到的是 Kimi-K3 条目，
两者共享相同 verifier，但不能互称模型对齐。

## 最短路径

```bash
# 下载并校验 SkillsBench v1.1，然后构建 Roller OpenCode 基础镜像。
scripts/prepare-skillsbench.sh "$PWD/.benchmarks/skillsbench-v1.1"
export SKILLSBENCH_ROOT="$PWD/.benchmarks/skillsbench-v1.1"

set -a
. /mnt/public/users/dingshengyuan/_shared/harness_evals/.env
set +a
export ROLLER_RUN_ROOT=/mnt/public/users/$USER/_shared/roller-runs/skillsbench
mkdir -p "$ROLLER_RUN_ROOT"

roller collect --bench skillsbench --bench-root "$SKILLSBENCH_ROOT" \
  --bench-variant with-skill --model Qwen3-32B-for-Game-Judge \
  --opencode-api-base https://joyrouter.jd.com/v1 \
  --opencode-api-key-env HE_JUDGE_KEY --parallel 1 --grader-parallel 1 \
  --output "$ROLLER_RUN_ROOT" --run-name kimi-with-skill

roller collect --bench skillsbench --bench-root "$SKILLSBENCH_ROOT" \
  --bench-variant no-skill --model Qwen3-32B-for-Game-Judge \
  --opencode-api-base https://joyrouter.jd.com/v1 \
  --opencode-api-key-env HE_JUDGE_KEY --parallel 1 --grader-parallel 1 \
  --output "$ROLLER_RUN_ROOT" --run-name kimi-no-skill
```

准备脚本可重复执行：已有 checkout 时不会修改数据，但会确认它正好是固定的
v1.1 commit，并重新检查/构建 harness 基础镜像。参数可换成任意持久化目录；
脚本不读取或写入 API key。若不使用脚本，等价的手工步骤是按 `v1.1` clone
<https://github.com/benchflow-ai/skillsbench> 并构建 `docker/opencode`。

每条命令都会自动写出 run 内的 `tasks.jsonl` 和 `bench-report.json`。SkillsBench
一键运行默认每题 3 次、每个 OpenCode turn 最多 100 步、单次模型响应最多 32768 tokens；
仍可用 `--samples`、`--opencode-max-steps` 和 `--opencode-output-tokens` 覆盖。正式对比应固定到 `v1.1` tag（peeled commit
`b63b7b2850226b6aa4fb5929a8c1ac7bc4d9a6af`），不要使用持续变化的 `main`。

先用 `--bench-task-id 3d-scan-calc` 做单题 smoke test，再扩大到全部任务。
`--bench-category` 和 `--bench-task-id` 都可重复使用。慢模型可加
`--bench-timeout-multiplier 2`，它会同时放大 agent 与 verifier 的预算；非标准重复次数可用
`--samples N`。

## Roller 做了什么

- 按 `integrations/agentbeats/task_sets/skillsbench-v1.1.json` 导出任务；若该文件不存在，回退到扫描 `tasks/`。
- 根据每个 `environment/Dockerfile` 构建独立环境，再叠加固定的 OpenCode harness。镜像 tag
  包含环境内容摘要，已存在时复用；需要强制重建可传 `--rebuild-task-images`。
- 从 Dockerfile 的最终 `WORKDIR` 得到 Agent 工作目录。
- 构建基础环境时移除 Dockerfile 中预装 skill 的 `COPY`；`with-skill` 在运行时注入
  `environment/skills/`，`no-skill` 清理所有支持的 skill 搜索目录。因此两组共用同一任务环境，
  差异只在 skill 条件。
- Agent 完成后才把 `verifier/` 放进容器，执行 `/verifier/test.sh`，读取
  `/logs/verifier/reward.txt`。Verifier 不会提前暴露给 Agent。
- 按上游口径报告 score eligibility：agent timeout/error 以及 verifier/基础设施
  错误会保留在报告中，但不进入 pass rate 和 mean reward。

首次跑一个任务可能需要较长时间下载系统依赖。Docker build、任务容器和 verifier 都会继承当前
shell 的 `HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY`，但不会把代理值写入 task JSONL；使用内网模型
端点时应同时把该主机加入 `NO_PROXY`。Verifier 在测试开始前遇到瞬时下载或 DNS 错误会自动重试，
最终仍失败的样本标记为不具备计分资格。正式输出应放在持久化共享盘。

## 边界

当前 adapter 面向上游默认 `tasks/` 集合，不自动包含 `tasks-extra/`。任务声明的 taxonomy、CPU、内存、
存储、GPU、网络和 OS 会保留在 bench metadata，但 Roller 当前不把资源值转换成 Docker resource limit；
如需严格资源可比性，应在运行环境外层统一限制。上游少量 compose 文件的 verifier 已支持
单容器启动其服务，因此 Roller 按主 Dockerfile 构建。
