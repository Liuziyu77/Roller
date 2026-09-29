# SkillsBench

[文档中心](../index.md) · [Benchmark 运行规范](benchmark-standard.md) · [Bench 接入与评分](bench-format.md) · [OpenCode 配置](../harnesses/opencode-configuration.md)

Roller 可以直接运行 SkillsBench 的 `with-skill` 和 `no-skill` 两组实验。数据来自
<https://github.com/benchflow-ai/skillsbench>。

v1.1 共 87 题，每题都由仓库内的 `verifier/test.sh` 评分，不调用 LLM judge。上游
README 里的 GPT-5.5 指被测 Agent。只有把 `collect --model` 接到 GPT-5.5 服务，结果才能和
该条目比较；换成其他模型后，verifier 虽然相同，模型分数并不等价。

## 准备并运行

```bash
export SKILLSBENCH_ROOT="$PWD/.benchmarks/skillsbench-v1.1"
scripts/prepare-benchmark.sh skillsbench "$SKILLSBENCH_ROOT"

export ROLLER_MODEL_ID=example-model
export ROLLER_MODEL_BASE_URL=https://api.example.com/v1
export ROLLER_MODEL_API_KEY=replace-with-your-api-key
export ROLLER_BENCH_OUTPUT="$PWD/runs/skillsbench"

# 检查并运行 with-skill
scripts/run-benchmark.sh --config examples/benchmarks/base.yaml \
  --extra-config examples/benchmarks/skillsbench.yaml --dry-run
scripts/run-benchmark.sh --config examples/benchmarks/base.yaml \
  --extra-config examples/benchmarks/skillsbench.yaml

# no-skill 只多一层小配置
scripts/run-benchmark.sh --config examples/benchmarks/base.yaml \
  --extra-config examples/benchmarks/skillsbench.yaml \
  --extra-config examples/benchmarks/skillsbench-no-skill.yaml --dry-run
scripts/run-benchmark.sh --config examples/benchmarks/base.yaml \
  --extra-config examples/benchmarks/skillsbench.yaml \
  --extra-config examples/benchmarks/skillsbench-no-skill.yaml
```

`prepare-skillsbench.sh` 接受任意持久化目录。目录不存在时，它会下载 v1.1；目录已存在时，
它只检查 commit，不会改动数据。脚本还会构建 `roller-opencode:1.18.30`，但不会读取或写入
API key。手工准备也可以：checkout SkillsBench 的 `v1.1` tag，再构建
`docker/opencode`。

运行前，请把示例 API 地址、key 和模型 ID 换成模型服务商提供的值。

两条运行命令会在各自的 run 目录写入 `tasks.jsonl` 和 `bench-report.json`。
SkillsBench 默认每题跑 3 次，每个 OpenCode turn 最多 100 步，单次模型响应最多
32768 tokens。需要修改时，在额外的 `--extra-config` 层覆盖 `execution.samples`、
`harness.extra.max_steps` 或 `harness.extra.output_tokens`。

正式对比请固定在 `v1.1` tag，对应 commit
`b63b7b2850226b6aa4fb5929a8c1ac7bc4d9a6af`。建议先在 `benchmark.task_ids`
中只选 `3d-scan-calc`，确认模型、镜像和输出目录都正常。
`benchmark.categories` 和 `benchmark.task_ids` 都是列表。模型较慢时可设置
`benchmark.timeout_multiplier: 2`，同时延长 Agent 与 verifier 的时间上限。

## Roller 如何执行任务

Roller 先读取 `integrations/agentbeats/task_sets/skillsbench-v1.1.json` 中的任务顺序。
该文件不存在时，才扫描 `tasks/`。

每个任务都有自己的 `environment/Dockerfile`。Roller 先构建这个环境，再叠加固定版本的
OpenCode harness。镜像 tag 包含环境内容摘要，因此相同环境可以复用；
`--rebuild-task-images` 会强制重建。Agent 的工作目录取 Dockerfile 最后一个
`WORKDIR`。

基础环境中的 skill copy 会在构建时移除。运行 `with-skill` 时，Roller 注入
`environment/skills/`；运行 `no-skill` 时，它会清理各 harness 支持的 skill 目录。
两组实验使用同一个任务环境，只在 skill 是否可见这一点上不同。

Agent 结束后，Roller 才把 `verifier/` 放进容器，执行 `/verifier/test.sh`，再读取
`/logs/verifier/reward.txt`。Agent 在作答期间看不到 verifier 文件。Agent timeout、
Agent error、verifier error 和基础设施错误都会写进报告，但不计入 pass rate 和 mean reward。

## 网络、缓存和已知限制

第一次跑某题时，镜像可能需要下载不少依赖。Docker build、任务容器和 verifier 会继承当前
shell 的 `HTTP_PROXY`、`HTTPS_PROXY` 和 `NO_PROXY`，这些值不会写进导出的 task
JSONL。不经过代理的模型地址应放进 `NO_PROXY`。Verifier 会重试测试开始前出现的临时下载或 DNS
错误；重试后仍失败的样本标为不可计分。正式输出应写到持久化共享盘。

当前 adapter 只读取上游默认的 `tasks/`，不会自动加入 `tasks-extra/`。任务声明的
taxonomy、CPU、内存、存储、GPU、网络和 OS 会保存在 bench metadata 中，但 Roller 不会把
这些值自动转换成 Docker resource limit。需要严格控制资源时，请在运行环境外层统一设置。
少量任务带有 compose 文件；它们的 verifier 可以在主容器内启动所需服务，因此 Roller 仍按
主 Dockerfile 构建。
