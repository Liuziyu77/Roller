# Benchmark 运行规范

Roller 为 benchmark 提供一套配置入口和固定的接入目录。用户通过同一条命令运行不同 benchmark，模型连接、Harness 和并发参数写在基础配置中，benchmark 自己的任务选择和评分设置写在额外配置中。

```bash
scripts/run-benchmark.sh \
  --config examples/benchmarks/base.yaml \
  --extra-config examples/benchmarks/wildclawbench.yaml \
  --dry-run
```

确认 dry-run 后，删除 `--dry-run` 即可开始评测。底层仍然调用 `roller collect --bench`，现有命令和结果格式不变。

## 配置分层

`--config` 接受 YAML 或 JSON 文件。`--extra-config` 可重复传入文件或内联 JSON，按出现顺序覆盖基础配置。对象会递归合并，列表和标量整体替换。后续配置需要丢弃某个可选对象时，将它设为 `null`。例如切换 Harness 时，应清空上一层的 `harness.extra`。

机器可读约束见 [`schemas/bench-run.schema.json`](../../../schemas/bench-run.schema.json)。schema 描述单个配置层，因此允许当前文件省略由后续层提供的字段。Roller 会在启动容器前检查合并后的完整配置。

```yaml
schema_version: roller.bench-run/v1

benchmark:
  id: wildclawbench
  root: ${WILDCLAWBENCH_ROOT}
  variant: null
  categories: []
  task_ids: []
  timeout_multiplier: 1
  environment:
    JUDGE_MODEL: ${ROLLER_JUDGE_MODEL}

model:
  id: ${ROLLER_MODEL_ID}
  base_url: ${ROLLER_MODEL_BASE_URL}
  api_key_env: ROLLER_MODEL_API_KEY
  api_protocol: openai-completions  # 可省略；默认根据 Harness 推断
  thinking: high

harness:
  id: openclaw
  image: wildclawbench-ubuntu:v1.3
  extra:
    model_request_timeout: 600
    max_tokens: 32768

execution:
  output: ${ROLLER_BENCH_OUTPUT}
  run_name: wildclawbench
  parallel: 4
  grader_parallel: 2
  samples: 1
  retries: 0
  timeout: 600
  wire_trace: actf
  keep_thinking: true
  pass_env: []
```

环境变量引用必须占据完整字段，例如 `${ROLLER_MODEL_BASE_URL}`。`https://${HOST}/v1` 会直接报错，避免变量未展开或凭据被拼进普通字符串。模型密钥通过 `model.api_key_env` 指定变量名，配置文件不保存密钥值。

不要把凭据放进 `model.base_url`。endpoint 会作为参数传给 Harness，因此 Roller 会拒绝带 userinfo、query 或 fragment 的 URL。

## 公共字段

| 字段 | 用途 |
| --- | --- |
| `benchmark.id` | `roller bench list` 中注册的 adapter 名称 |
| `benchmark.root` | 已准备好的上游 checkout 或数据目录 |
| `benchmark.variant` | 评测条件，例如 SkillsBench 的 `with-skill` |
| `benchmark.categories` / `task_ids` | 可重复筛选项，空列表表示不筛选 |
| `benchmark.timeout_multiplier` | 同时调整任务声明和实际执行时间 |
| `benchmark.environment` | 仅在本次进程中设置的 benchmark 环境变量，运行结束后恢复原值 |
| `model.id` | 被测模型 ID |
| `model.base_url` | 模型 API 地址 |
| `model.api_key_env` | 保存模型密钥的宿主机环境变量名 |
| `model.api_protocol` | API 协议；OpenClaw 写入自动生成的 provider，其他 Harness 会检查它是否符合自身协议 |
| `model.thinking` | 统一的 thinking / reasoning effort 档位，由各 Harness 校验并映射 |
| `harness.id` / `image` | Harness 名称和容器镜像 |
| `harness.extra` | Harness 专属参数，未知字段会在启动前报错 |
| `execution.*` | 输出目录、并发、重复次数、重试、trace 和容器公共参数 |

OpenClaw 使用相同的 `model.id`、`base_url`、`api_key_env` 和 `api_protocol`。Roller 会在临时目录生成 models 配置，运行结束后删除。`model.id` 在 OpenClaw 下必须写成 `provider/model`。需要完整自定义 OpenClaw provider 时，可改用 `harness.extra.model_config`，并在该层将继承的 `model.base_url`、`model.api_key_env` 和 `model.api_protocol` 设为 `null`，两种配置不能同时使用。

从 benchmark 示例预设的 Harness 切换到其他 Harness 时，应同时覆盖镜像并清空原有的专属参数：

```yaml
harness:
  id: codex
  image: wildclawbench-codex-ubuntu:v0.0
  extra: null
```

Codex 使用 `openai-responses`，Claude Code 使用 `anthropic-messages`，Hermes、OpenCode、DeepSeek 和 Pi 使用 `openai-completions`。配置中的 `api_protocol` 与 Harness 冲突时，Roller 会直接报错。

`benchmark.environment` 适合 judge 和 benchmark 工具需要的配置。例如 PinchBench 可以把统一的 judge 变量映射到上游名称：

```yaml
benchmark:
  environment:
    PINCHBENCH_JUDGE_BASE_URL: ${ROLLER_JUDGE_BASE_URL}
    PINCHBENCH_JUDGE_API_KEY: ${ROLLER_JUDGE_API_KEY}
    PINCHBENCH_JUDGE_MODEL: ${ROLLER_JUDGE_MODEL}
```

这些值不会出现在命令行。dry-run 和运行元数据会记录生效后的非敏感设置、配置文件路径、脱敏配置摘要和环境变量名称，密钥值不会落盘。

## Harness 专属参数

`harness.extra` 使用不带 Harness 前缀的字段。Roller 将其映射到现有 `collect` 参数。

| Harness | 支持的 `extra` 字段 |
| --- | --- |
| OpenClaw | `model_config`, `gateway_port`, `gateway_port_step`, `gateway_startup_timeout`, `model_request_timeout`, `max_tokens`, `session_id`, `search_provider`, `collect_paths`, `set_image_model`, `cache_trace` |
| Codex | `config`, `reasoning_summary`, `web_search`, `websockets` |
| Claude Code | `settings`, `max_turns`, `max_thinking_tokens`, `permission_mode` |
| Hermes | `max_iterations`, `context_length`, `vision` |
| OpenCode | `max_steps`, `context_length`, `output_tokens`, `vision` |
| DeepSeek / Pi | `max_steps`, `context_length`, `output_tokens` |

`model_request_timeout` 需要 OpenClaw 的 provider schema 支持 `timeoutSeconds`。WildClawBench 使用的旧版镜像不支持该字段，因此示例配置没有设置它。

## 固定的三段流程

### 1. 准备

每个正式 adapter 都要提供 `scripts/prepare-<name>.sh [ROOT]`。统一入口是：

```bash
scripts/prepare-benchmark.sh wildclawbench "$WILDCLAWBENCH_ROOT"
scripts/prepare-benchmark.sh pinchbench "$PINCHBENCH_ROOT"
scripts/prepare-benchmark.sh skillsbench "$SKILLSBENCH_ROOT"
```

准备脚本负责获取固定版本的数据和镜像，检查已有 checkout，并打印实际 revision。它不读取或写入模型、judge 密钥。

### 2. 推理与即时评分

```bash
export ROLLER_MODEL_ID=example/model
export ROLLER_MODEL_BASE_URL=https://api.example.com/v1
export ROLLER_MODEL_API_KEY=replace-with-your-api-key
export ROLLER_BENCH_OUTPUT="$PWD/runs"

scripts/run-benchmark.sh \
  --config examples/benchmarks/base.yaml \
  --extra-config examples/benchmarks/wildclawbench.yaml \
  --dry-run

scripts/run-benchmark.sh \
  --config examples/benchmarks/base.yaml \
  --extra-config examples/benchmarks/wildclawbench.yaml
```

`--dry-run` 会加载 adapter、筛选任务、校验 endpoint、密钥变量、thinking 和 Harness 专属字段，并输出生效配置、实际任务数、轨迹数、镜像和每题 timeout。它不启动容器，也不创建 run 目录。

Roller 在 agent 容器销毁前完成评分。运行结束后，run 目录包含 `tasks.jsonl`、样本轨迹、`score.json`、`info.json` 和 `bench-report.json`。

### 3. 重新聚合

一般不需要单独执行报告命令。修复或审计样本后，可重新聚合：

```bash
roller bench report runs/<run-name> --output runs/<run-name>/bench-report.json
```

## 三个参考配置

| Benchmark | 额外配置 | 准备脚本 |
| --- | --- | --- |
| WildClawBench | `examples/benchmarks/wildclawbench.yaml` | `scripts/prepare-wildclawbench.sh` |
| PinchBench | `examples/benchmarks/pinchbench.yaml` | `scripts/prepare-pinchbench.sh` |
| SkillsBench with skill | `examples/benchmarks/skillsbench.yaml` | `scripts/prepare-skillsbench.sh` |
| SkillsBench without skill | 在上一项后再加 `examples/benchmarks/skillsbench-no-skill.yaml` | 同上 |

WildClawBench 是完整标杆：它覆盖独立 judge、搜索环境、任务级 timeout、多种 Harness 和 ground-truth 延迟注入。PinchBench 与 SkillsBench 使用同一个运行入口，只保留各自必要的 overlay。

## 新 benchmark 的合入要求

一个新 benchmark 至少应包含：

1. `BenchAdapter` 实现和 registry 条目；
2. `scripts/prepare-<name>.sh`，可对已有 checkout 重复执行且不静默切换版本；
3. `examples/benchmarks/<name>.yaml`，只写公开默认值和环境变量引用；
4. 中英文文档，按“准备、配置、dry-run、正式运行、评分、限制”组织；
5. adapter 单元测试、配置 dry-run 测试，以及至少一个不依赖 LLM judge 的容器 smoke test；
6. 参数映射表，明确上游支持但 Roller 尚未实现的选项。

adapter 应把 ground truth 放入 `bench.grade.stage`，在 agent 完成后再注入。评分器错误与真实零分必须分开记录。若 benchmark 使用 LLM judge，文档和结果中应记录 judge model、endpoint 配置来源和请求参数，但不得保存密钥。
