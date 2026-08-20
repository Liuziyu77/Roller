# 轨迹产出文件指南

本文说明 `roller collect` 会产出哪些文件、每个文件何时生成和保留、它们之间的
信息差异，以及训练、分析、调试时应选择哪一层。

## 1. 先看结论

| 需求 | 首选文件 | 原因 |
| --- | --- | --- |
| 直接训练 Task Agent | Run 级 `sft.jsonl` | 每行一个最小 SFT record，可直接流式读取 |
| 查看单条训练样本 | Sample 级 `trace_SFT.json` | 与 `sft.jsonl` 中对应 record 完全相同 |
| 轨迹分析、打分、统计 token | `trace_ACTF.json` | 有 step、reasoning、observation、metric 和时间信息 |
| 追查 Harness 原始事件 | `trace.jsonl` | OpenClaw session 的原始事实来源，改写最少 |
| 核对实际 system prompt | `trace_ACTF.json` 顶层 | 默认从 cache trace 提取并固化一次 |
| 核对实际请求历史和 tools | `--wire-trace full` 的 `wire_trace.jsonl` | 保存每次完整请求体，适合诊断 |
| 查看 Task Agent 交付文件 | `results/` | 只回收 task 声明的输出目录和额外 collect path |
| 查看动态双方可见对话 | `conversation.json` | 不包含隐藏 reasoning、tool call 和 tool result |

训练使用 SFT，分析使用 ACTF，排查适配器或上游行为时再看 raw/full wire。不要把
`wire_trace.jsonl` 当成训练集：多轮请求会重复历史，体积接近 O(N²)。

## 2. 目录结构

### 2.1 默认普通 rollout

默认参数是 `--cache-trace --wire-trace actf --keep-thinking`。成功 run 的主要结构为：

```text
<output>/<run-name>/
├── info.json
├── sft.jsonl
└── samples/
    └── <model>/
        └── <index>_<task>/
            ├── trace.jsonl
            ├── trace_ACTF.json
            ├── trace_SFT.json
            ├── info.json
            └── results/                 # Task 输出含文件或目录时存在
```

默认的 `actf` wire 模式会临时捕获 tools schema，写入 ACTF/SFT 后删除中间
`wire_trace.jsonl` 和 `cache_trace.jsonl`，因此最终 sample 保持紧凑。

### 2.2 `--wire-trace full`

`full` 模式额外保留诊断层：

```text
<index>_<task>/
├── trace.jsonl
├── trace_ACTF.json
├── trace_SFT.json
├── cache_trace.jsonl
├── wire_trace.jsonl
├── info.json
└── results/                       # 可选
```

### 2.3 User Simulator rollout

动态任务在根 sample 保存 Task Agent，在子目录保存 Simulator：

```text
<index>_<task>/
├── trace.jsonl                    # Task Agent raw
├── trace_ACTF.json                # Task Agent ACTF
├── trace_SFT.json                 # Task Agent SFT
├── conversation.json              # 双方可见的干净交替对话
├── info.json                      # 总 usage 与双方索引
├── user_simulator/
│   ├── trace.jsonl                # Simulator raw
│   ├── trace_ACTF.json            # Simulator ACTF
│   ├── trace_SFT.json             # Simulator SFT
│   └── info.json                  # Simulator 独立状态和 usage
└── results/                       # 只属于 Task Agent，可选
```

Run 级 `sft.jsonl` 只汇总根目录的 Task Agent，不混入 User Simulator 的 SFT。

## 3. 从运行到训练集的数据链路

一条轨迹按以下顺序生成：

```text
OpenClaw session
  ├─ session JSONL ───────────────> trace.jsonl
  ├─ diagnostics.cacheTrace ──────> cache_trace.jsonl ──┐
  └─ local wire proxy ────────────> wire_trace.jsonl ───┤
                                                        v
trace.jsonl + system prompt + tools schema ─────> trace_ACTF.json
trace_ACTF.json ────────────────────────────────> trace_SFT.json
全部成功/Resume sample 的 trace_SFT.json ───────> run/sft.jsonl
```

`trace.jsonl` 是事实来源。ACTF 是分析投影，SFT 是进一步删除训练无关字段后的训练投影。
SFT 不反向参与 ACTF 或 raw 的生成。

## 4. 核心轨迹文件的区别

### 4.1 `trace.jsonl`：Harness 原始轨迹

内容包括 OpenClaw session 中的 user、assistant、thinking、tool call、tool result、usage
等原始 message event。Roller 不为了训练而删除或重排这些事件。

生成条件：

- OpenClaw 容器已经启动，并且结束阶段成功复制指定 session 文件或最新 session
  fallback；
- Harness 成功返回但找不到 raw trace 时，sample 会改为 `error`；
- Harness 本身为 `error` 或 `timeout` 时，只要能复制到 session，raw 仍会保留。

用途与边界：

- 适合追查上游字段、事件顺序和转换问题；
- 结构属于 Harness，不作为稳定训练 schema；
- `--no-keep-thinking` 不修改 raw，因此 raw 仍可包含当前轮产生的 thinking；该参数影响
  ACTF/SFT 和后续请求历史，不是 raw 脱敏开关。

### 4.2 `trace_ACTF.json`：统一分析轨迹

ACTF 顶层保存 `schema_version`、`system_prompt`、完整 `tools`、`steps` 和轨迹时间。
每个 assistant message 形成一个 step，step 内包含：

- `user_content`：本轮用户输入；
- `assistant_content.reasoning_content`：可见 reasoning/thinking；
- `assistant_content.content`：可见回复；
- `assistant_content.tool_calls`：工具调用；
- `observation`：该次调用之后的工具结果；
- `metric`、开始/结束时间和 stop reason。

生成条件：

- sample 已取得 `trace.jsonl`；
- raw 可以被 ACTF 转换器解析；
- system prompt 来自 `cache_trace.jsonl`；未启用、复制失败或没有有效记录时为 `""`；
- tools 来自临时或保留的 `wire_trace.jsonl`；没有捕获到时为 `null`。

只要 raw 存在，Roller 就会尝试转换，包括 Harness 已经失败或超时的 sample。因此错误
sample 可能仍有 ACTF。转换失败会把 sample 状态设为 `error`。

### 4.3 `trace_SFT.json`：单条最小训练样本

SFT 从 ACTF 生成，顶层严格只有：

```json
{"messages": [], "tools": []}
```

它删除 schema version、task/model、usage、metric、step ID、stop reason、时间戳、运行
路径以及 observation metadata。Assistant 固定保存 `role`、`reasoning_content`、
`content` 和 `tool_calls`；tool message 只保存 `role`、`tool_call_id` 和字符串
`content`。

生成条件：

- ACTF 转换成功；
- 至少有一个 user message 和一个非空 assistant message；
- 每个 tool call 有非空且全局唯一的 ID；
- 每个 tool call 在同一 step 恰好对应一个 tool result；
- 被调用工具存在于顶层 tools schema；
- arguments 可编码为 JSON object；
- tool result 是字符串或纯 text blocks。

GPT Responses 的扁平 tools 与 Chat Completions 的嵌套 tools 都会统一为嵌套 function
schema。遇到 image 等非文本工具结果时，Roller 不会静默丢失内容，而会让 SFT 转换
失败并把 sample 标记为 `error`。多模态 SFT 需要另行定义 content-block schema。

完整字段定义见 [SFT 数据格式](sft-format.md)。

### 4.4 `sft.jsonl`：Run 级训练集

Collector 完成所有 request 后，按 request index 读取 sample 级 `trace_SFT.json` 并原子、
流式写入 run 根目录：

- `status=ok` 的 sample 会进入；
- `--resume` 复用的 `status=skipped` sample 原先必须是 `ok`，也会进入；
- `error` 和 `timeout` 不会进入，即使它们保留了部分 raw/ACTF/SFT；
- 没有成功样本时仍生成空文件；
- User Simulator 的子轨迹不会进入。

每行与对应 `trace_SFT.json` 的 JSON object 相同，不增加 task ID 或其他 metadata。样本
来源需要通过相邻 run/sample `info.json` 和目录位置追溯。

## 5. 诊断与辅助文件

### 5.1 Run `info.json`

CLI 在正式采集前先写入初始 run 信息，正常收尾时补充：

- Roller 版本和采集配置；
- task/model request 数量；
- `ok`、`error`、`timeout`、`skipped` 汇总；
- token、cache、cost、请求数汇总；
- 每个 sample 的状态、错误、路径与 usage。

进程被强制终止时，文件可能只包含启动配置，没有最终 summary。

### 5.2 Sample `info.json`

每个完成 finalize 的 sample 都会写入，包括失败 sample。它保存 task/model、Harness、
最终状态、错误、attempt 历史、usage、耗时、raw/ACTF/SFT 相对路径、输出回收信息、
runtime 和输入 provenance。训练数据刻意删除的 metadata 在这里保留。

### 5.3 `cache_trace.jsonl`

OpenClaw 的 `diagnostics.cacheTrace` 记录每次 LLM call 组装后的 system prompt。默认启用，
Roller 复制后按敏感 key 递归脱敏，再用它填充 ACTF 顶层 `system_prompt`。

- 默认 `--wire-trace actf`：用于转换后删除；
- `--wire-trace full`：最终保留；
- `--no-cache-trace`：不创建，ACTF/SFT 的 system content 为 `""`。

脱敏基于 JSON key，不会扫描 system prompt 自由文本中的秘密；该文件和下游 ACTF/SFT
仍应按敏感数据管理。

### 5.4 `wire_trace.jsonl`

本地反向代理在请求发往模型服务前记录 wire 数据：

- `actf` 只记录 model、非消息参数、消息数量以及首次非空 tools schema，不记录完整
  messages/input；该临时文件在转换后删除；
- `full` 保存每次完整请求体并最终保留；
- HTTP Authorization 等 header 不写入文件，但完整 request body 可能包含 system prompt、
  用户数据、tool result 和历史 reasoning；`full` 不对这些自由内容做自动脱敏。

多轮请求会重复历史，因此文件体积接近 O(N²)，只适合审计和调试。

### 5.5 `results/`

只有 task 输出目录中存在普通文件或目录时才创建。Roller：

- 复制所有安全的普通文件并保留相对路径；
- 在 sample `info.json` 中记录 path、大小、MIME 和 SHA-256；
- 拒绝符号链接和特殊文件；
- 即使存在不安全文件，安全文件仍会复制，但 sample 最终状态变为 `error`；
- `--collect-path` 的兼容输出放入 `results/legacy/`。

`results/` 是 Agent 交付物，不属于对话训练数据，也不会写入 `sft.jsonl`。

### 5.6 `conversation.json`

仅 User Simulator task 生成。它保存双方可见的 user/assistant 轮次、终止原因、待发送但
未交付的 instruction、参与模型和时间，不复制双方隐藏 reasoning、tool call 或 tool
result。需要训练或分析任一参与方时，应使用各自的 raw/ACTF/SFT。

## 6. 参数组合如何影响轨迹

### 6.1 Wire trace

| `--wire-trace` | 代理要求 | ACTF tools | 最终辅助文件 | 适用场景 |
| --- | --- | --- | --- | --- |
| `actf`（默认） | 有可代理 `baseUrl` 时启用；缺少时降级 | 捕获成功时为完整 schema，否则 `null` | 不保留 cache/wire | 常规批量采集 |
| `full` | 必须提供选中 provider 的 `baseUrl` | 完整 schema | 保留成功捕获的 cache/wire | 请求级审计与调试 |
| `off` | keep-thinking=true 时不启用 | `null` | 不保留 | 不需要 tools/schema 的最小诊断关闭模式 |

`--wire-trace off` 或 `actf` 降级后，如果轨迹实际调用了工具，SFT 校验会因缺少工具
schema 失败；没有工具调用时，SFT 的 `tools` 为 `[]`。因此需要工具训练数据时应保持
默认 `actf` 或使用 `full`，并提供带绝对 `baseUrl` 的 `--model-config`。

### 6.2 Thinking

| 参数 | 当前回复 | 后续请求历史 | ACTF | SFT | raw |
| --- | --- | --- | --- | --- | --- |
| `--keep-thinking`（默认） | 后端返回什么就接收什么 | 保留历史 reasoning | 保存可见 reasoning | 从 ACTF 保存 | 保留原始事件 |
| `--no-keep-thinking` | 当前轮仍可产生 reasoning | 代理转发前剥离历史 reasoning | reasoning 置空 | reasoning 置空 | 仍保留原始事件 |

`--thinking LEVEL` 控制向 OpenClaw/模型请求的逻辑档位，但不能凭空生成可见文本。GPT 原生 effort、开源模型 profile 和旧镜像限制见 [Thinking 配置](openclaw-configuration.md#5-thinkingsystem-prompt-与-wire-trace)。
后端只报告 reasoning token、却不返回 reasoning summary/content 时，ACTF/SFT 没有可保存
的 `reasoning_content`。GPT 的压缩 summary 会原样进入该字段，encrypted reasoning
不会进入 SFT。

`--no-keep-thinking` 即使配合 `--wire-trace off`，也必须启动代理来剥离请求历史，因此
仍要求 `--model-config` 中存在可代理 `baseUrl`。

### 6.3 Cache trace

| 参数 | ACTF/SFT system | `cache_trace.jsonl` |
| --- | --- | --- |
| `--cache-trace`（默认） | 使用捕获到的首个组装后 system prompt | 仅 full 模式最终保留 |
| `--no-cache-trace` | `""` | 不创建 |

如果训练需要复现 Agent 的真实 system prompt，不应关闭 cache trace。

## 7. 成功、失败、重试与 Resume

### 7.1 成功保证

状态为 `ok` 的普通 sample 保证存在：

- `trace.jsonl`；
- `trace_ACTF.json`；
- `trace_SFT.json`；
- `info.json`。

User Simulator sample 还保证根 `conversation.json` 和 Simulator 自己的 raw/ACTF/SFT。
`results/`、cache trace 和 wire trace 始终是条件产物，不属于成功必备文件。

### 7.2 失败 sample

`error` 或 `timeout` 不等于目录为空。结束阶段会尽量回收 raw、system prompt、tools 和
安全输出，然后尝试 ACTF/SFT 转换。因此失败 sample 可以包含完整或部分轨迹，适合诊断，
但不会进入 run 级 `sft.jsonl`。

以下情况会把原本成功的 Harness 结果改为 `error`：

- 找不到 raw session trace；
- ACTF 或 SFT 转换失败；
- task 输出目录复制失败；
- 输出中发现符号链接或特殊文件；
- User Simulator 任一参与方失败或其轨迹转换失败。

### 7.3 Retry

`--retries N` 最多额外执行 N 次完整 rollout。每次 attempt 开始前会清理上一次的运行期
raw/cache/wire 文件，避免混合不同 attempt。Sample `info.json.attempts` 保留每次状态和
耗时，canonical raw/ACTF/SFT 对应最终 attempt。

### 7.4 Resume

`--resume` 只复用旧状态为 `ok` 且满足以下条件的 sample：

- raw、ACTF、SFT 都存在；
- 当前 Task Bundle digest 与旧 provenance 一致；
- Context 没有使用 `capture=none`；
- User Simulator task 还具有 `conversation.json` 和 Simulator raw/ACTF/SFT。

复用结果在本次 run 中标为 `skipped`，仍会写入新的 `sft.jsonl`。缺失任一必需文件或
输入 provenance 变化时，会重新执行整个 sample。

## 8. 推荐配置

### 批量生成文本/工具 SFT

```bash
roller collect \
  --tasks tasks.jsonl \
  --model provider/model \
  --model-config models.json \
  --thinking high \
  --wire-trace actf \
  --output runs
```

保留默认 `--cache-trace` 和 `--keep-thinking`。该配置会产出完整 ACTF/SFT，同时删除体积
较大的诊断中间层。

### 调试 reasoning 或请求历史

```bash
roller collect \
  --tasks tasks.jsonl \
  --model provider/model \
  --model-config models.json \
  --thinking high \
  --wire-trace full \
  --output debug-runs
```

调试结束后优先保留 raw/ACTF/SFT；确认不再需要请求级审计时删除 full wire 文件，减少
重复数据和敏感信息暴露。

## 9. 发布训练集前检查

建议至少检查：

1. Run `info.json.summary.failed` 为 0，或已明确排除失败任务；
2. `sft.jsonl` 行数等于预期的 `ok + skipped` task/model request 数；
3. 每行顶层只有 `messages` 和 `tools`；
4. assistant 都有 `reasoning_content`、`content`、`tool_calls`；
5. tool call ID 与 tool message 一一对应；
6. tools schema 与训练时 chat template 的工具格式兼容；
7. system prompt、用户内容、tool result、reasoning 和 Agent 输出已完成隐私审查；
8. 如果使用 `--no-keep-thinking`，确认 raw/full 诊断层不会被误当成已去除 thinking 的
   发布数据。
