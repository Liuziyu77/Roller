# 轨迹产出文件指南

本文说明 `roller collect` 会产出哪些文件、每个文件何时生成和保留、它们之间的
信息差异，以及训练、分析、调试时应选择哪一层。

## 1. 先看结论

| 需求 | 首选文件 | 原因 |
| --- | --- | --- |
| 直接训练 Task Agent | Run 级 `sft.jsonl` | 每行一个最小 SFT record，可直接流式读取 |
| 查看单条训练样本 | Sample 级 `trace_SFT.json` | 与 `sft.jsonl` 中对应 record 完全相同 |
| 轨迹分析、打分、统计 token | `trace_ACTF.json` | 有 step、reasoning、observation、metric 和时间信息 |
| 追查 Harness session 原始事件 | `trace.jsonl` | OpenClaw transcript 或 Codex native session，Roller 不做训练清洗 |
| 核对首个捕获到的组装后 system prompt | `trace_ACTF.json` 顶层 | 默认从 cache trace 提取并固化一次 |
| 核对每次 system prompt 是否变化 | `--wire-trace full` 的 `cache_trace.jsonl` | 保留 OpenClaw 每次 LLM call 的诊断记录 |
| 核对模型实际收到的请求历史和 tools | `--wire-trace full` 的 `wire_trace.jsonl` | 保存代理实际转发的每次 JSON 请求体 |
| 查看 Task Agent 交付文件 | `results/` | 只回收 task 声明的输出目录和额外 collect path |
| 查看动态双方可见对话 | `conversation.json` | 不包含隐藏 reasoning、tool call 和 tool result |

训练使用 SFT，分析使用 ACTF，排查适配器、context 组装或上游行为时再看 session raw
与 full wire。本文中的 **session raw** 专指 `trace.jsonl`；`wire_trace.jsonl` 是请求侧
诊断记录，不要把两者统称为同一种 raw。也不要把 wire trace 当成训练集：多轮请求会
重复历史，体积接近 O(N²)。

## 2. 目录结构

### 2.1 默认普通 rollout

OpenClaw 默认参数是 `--cache-trace --wire-trace actf --keep-thinking`；Codex 默认关闭 web
search，并保留 native session 中实际可见的 reasoning content 或 summary。两者成功 run 的
主要结构相同：

```text
<output>/<run-name>/
├── info.json
├── lock.json
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

OpenClaw 默认的 `actf` wire 模式会临时捕获 tools schema，写入 ACTF/SFT 后删除中间
`wire_trace.jsonl` 和 `cache_trace.jsonl`，因此最终 sample 保持紧凑。Codex 的默认 `actf`
直接从 native session 生成 permissive tool schema，不启动请求代理，也不产生中间 wire 文件。

### 2.2 `--wire-trace full`

`full` 模式额外保留诊断层：

```text
<index>_<task>/
├── trace.jsonl
├── trace_ACTF.json
├── trace_SFT.json
├── cache_trace.jsonl              # 仅 OpenClaw
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
Harness session store ────────────────> trace.jsonl
  ├─ OpenClaw transcript SDK
  └─ Codex $CODEX_HOME/sessions native rollout
                              │
                              ├─ Harness-native ACTF converter
                              └─ Harness request diagnostics
                                  ├─ OpenClaw cache + wire
                                  └─ Codex wire
                                                │
                                                v
trace.jsonl + Harness 可取得的 system/tools ──> trace_ACTF.json
trace_ACTF.json ────────────────────────────────> trace_SFT.json
全部成功/Resume sample 的 trace_SFT.json ───────> run/sft.jsonl
```

`trace.jsonl` 是当前 Harness session 语义事件的事实来源。ACTF 是该 session 的分析
投影，SFT 是进一步删除训练无关字段后的训练投影。SFT 不反向参与 ACTF 或 raw 的生成。

### 3.1 三种文件回答的是三个不同问题

| 文件 | 捕获位置 | 能回答 | 不能据此断言 |
| --- | --- | --- | --- |
| `trace.jsonl` | Harness session 导出层 | Agent 记录了哪些 user/assistant/tool 事件、顺序与 usage | 某次请求完整携带了哪些历史消息；上游 HTTP 的逐字请求/响应 |
| `cache_trace.jsonl` | OpenClaw context 组装诊断点 | 每次 LLM call 组装出的 system prompt 及相关诊断阶段 | 完整 messages/input 和最终 provider 请求参数 |
| `wire_trace.jsonl`（full） | Harness provider adapter 之后、真实模型 API 之前 | Roller 代理实际转发的 JSON body，包括 messages/input、tools 和采样参数 | 模型服务收到请求后内部如何 tokenize、截断或路由 |

### 3.2 Session 历史不等于模型请求 context

`trace.jsonl` 中“存在某条消息”只说明它进入了 Harness 的 session 记录，不说明它在
后续每次模型调用中仍被逐字发送。OpenClaw 会从 session 构建受 context window 限制的
运行时 context；其官方说明明确区分持久化 transcript 与模型 context：compaction 会用
摘要替代较旧历史并保留近期消息，pruning 可以只从内存 prompt 中移除旧 tool result，
而不重写磁盘 transcript。插件 context engine 和运行时 hook 也可以参与 context 组装。
参见 [OpenClaw Context](https://docs.openclaw.ai/concepts/context) 与
[Session Management and Compaction](https://docs.openclaw.ai/reference/session-management-compaction)。
具体压缩、裁剪和持久化实现取决于镜像内的 OpenClaw 版本、模型 profile 与所选 context
engine；Roller 不从 session 文件猜测这些变换，而是在最终 API 请求边界采集 full wire。
Codex 同样会在 native session 与 Responses input 之间加入 base/developer instructions、
environment context、tool namespace、resume history 和压缩结果；这些最终形态也只能由 full
wire 确认。

因此两个方向的差异都存在：

- session 中有、模型请求中没有：旧 tool result 被裁剪、历史被压缩成摘要、消息因窗口
  或 provider 规则被过滤；
- session 中没有逐条对应事件、模型请求中却有：组装后的 system prompt、tools schema、
  workspace/bootstrap 注入、每轮 runtime context 或 hook 增补内容；
- 内容仍在但表示不同：Harness/provider adapter 可以把内部 message/block 转成
  Chat Completions `messages` 或 Responses `input` 的最终协议形态。

所以 `trace.jsonl` 适合复盘 Agent 逻辑轨迹，`wire_trace full` 才是判断“该次 HTTP 模型
调用看到了什么 JSON context”的依据。ACTF/SFT 都从 session raw 投影，不能反向恢复
被压缩、裁剪或临时注入后的逐请求 context。

## 4. 核心轨迹文件的区别

### 4.1 `trace.jsonl`：Harness 原始轨迹

OpenClaw 内容包括 transcript 中的 user、assistant、thinking、tool call、tool result、usage
等原始 message event。默认镜像中，Roller 通过公开的
`openclaw/plugin-sdk/session-transcript-runtime` 按精确 session identity 读取事件，再逐条
序列化为 JSONL；它不为了训练而删除、合并或重排合法事件。这里的“原始”是相对 Roller
的 ACTF/SFT 转换而言，不是底层 SQLite/JSONL 文件的字节副本，也不是网络层原始请求或
响应。SDK 只返回 OpenClaw 认可的 transcript 事件，因此底层存储中的损坏行或非事件记录
不会出现在导出文件中。legacy OpenClaw 不提供该 SDK 时，Roller 才直接复制 session JSONL。

Codex 内容来自 rollout 容器 `$CODEX_HOME/sessions` 的 native JSONL，包括 `session_meta`、
`turn_context`、`response_item`、`event_msg`、压缩事件和 token usage。Codex raw 同样属于
Harness 私有 schema；它不是 `codex exec --json` 的终端展示流，也不是 HTTP wire trace。
Sample `info.json.trajectory.raw_schema` 会分别标记 `openclaw-session-jsonl` 或
`codex-session-jsonl`。

生成条件：

- Harness 容器已经启动，结束阶段能导出非空 native session；OpenClaw legacy 路径要求
  找到指定 session JSONL 或最新 fallback，Codex 路径要求找到最新 native rollout JSONL；
- Harness 成功返回但不能取得非空 raw trace 时，sample 会改为 `error`；
- Harness 本身为 `error` 或 `timeout` 时，只要仍能导出 transcript，raw 就会保留。

用途与边界：

- 适合追查上游字段、事件顺序和转换问题；
- 结构属于 Harness，不作为稳定训练 schema；
- 不能单独用于验证 context compaction、tool-result pruning 或 provider 适配后的真实
  请求；OpenClaw 和 Codex 都应使用 `--wire-trace full`；
- Chat Completions 返回的 `reasoning`/`reasoning_text` 在进入 OpenClaw 前可能被等价规范化
  为 `reasoning_content`，以保证后续轮次重放；thinking 文本不变，但字段签名和上游 HTTP
  响应不再是逐字对应关系；
- `--no-keep-thinking` 不修改 raw，因此 raw 仍可包含当前轮产生的 thinking；该参数影响
  ACTF/SFT，不是 raw 脱敏开关。OpenClaw 还可在 wire 层剥离后续请求历史；Codex 当前只
  控制导出，不改写 `resume` 上下文。
- external thinking 的完整 thoughts 是显式模型输出，不受 `--no-keep-thinking` 剥离。
  OpenClaw raw 直接保存 `deep_think` tool call arguments；Codex raw 保存外层 `exec`
  custom call，并在 `item_completed/McpToolCall` 或 `mcp_tool_call_end` 的 arguments 中保存
  真实 thoughts。ACTF 将两者统一为 canonical `deep_think` call，SFT 再折叠进随后 action assistant 的
  `reasoning_content`。`info.json.runtime.external_thinking` 保存模式、协议校验状态、think
  次数与 THINK/ACT 配对数。

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

- Harness 已取得 `trace.jsonl`，且对应 adapter 能生成 canonical ACTF；
- OpenClaw system/tools 分别来自 cache/wire 捕获；缺失时 system 为 `""`、tools 为 `null`；
- Codex system 来自 native base instructions、developer/system message 和首轮前的
  `<environment_context>`；task 用户消息按内容匹配到对应轮次。native session 不含完整工具
  schema，因此根据实际调用名和参数字段生成 permissive schema。external MCP 的外层
  programmatic `exec` 会根据 native MCP completion event 还原为 canonical `deep_think`，
  避免训练层暴露 Harness transport 包装。

OpenClaw ACTF 只把 cache trace 中首个有效的组装后 system prompt 和 wire trace 中首个
非空 tools schema 固化在顶层；Codex ACTF 则使用 native system 事件和实际调用推导的
schema。两者都没有逐 step 保存每次请求的完整 messages/input，也不会表达 system/tools
在调用间的变化；需要检查这些变化时使用 full wire，OpenClaw 还可结合 full cache trace。

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

SFT 表达的是从 session 事件重建的逻辑对话，不是每次 provider 请求的快照。它适合
训练，但不能用于逐请求重放或证明某个较早 tool result 在后续模型调用中仍然可见。

external thinking 还有一层训练专用投影：`deep_think` call、固定 placeholder result、
工具 schema 和 system prompt 工具清单项会被删除，thoughts 合并到同一逻辑动作的 assistant
`reasoning_content`，业务工具调用与结果保持不变。raw/ACTF 仍保留实际执行结构，因此
审计协议时不要用 SFT 代替它们。

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

### 5.1 Run `lock.json`

`lock.json` 在采集前生成，用于保证一个 run 内不会混合不同解析配置的样本。它固定 request
顺序、Task Bundle digest、模型配置、OpenClaw image ID，以及 thinking、wire/cache trace
等会影响轨迹含义的选项。Resume 时逐字段比较；不一致会打印有限的差异路径并在启动
Harness 前停止。

锁不会保存 credential：敏感 key 的值替换为 `<redacted>`；URL 的 userinfo 和 fragment
被移除，敏感 query value 被替换，`api-version` 等非敏感 query 则保留参与比较。
credential 或 pass-through env 的值变化不会让锁失配；如果它们会改变实际模型行为，应
另建 run。配置对象直接比较，task/input/context 则复用 Task Bundle 已有的 digest。

### 5.2 Run `info.json`

CLI 在正式采集前写入初始 run 信息；Collector 在首条结果、之后每累计 25 条结果时更新，
如果两次完成事件相隔至少 30 秒也会立即更新，并在中断和收尾阶段强制 checkpoint。它
保存：

- Roller 版本和采集配置；
- 当前 `state`：`initializing`、`running`、`finalizing`、`finished`、`interrupted` 或
  `finalization_error`；
- `total/processed/pending` 实时进度；
- task/model request 数量；
- `ok`、`error`、`timeout`、`skipped` 汇总；
- token、cache、cost、请求数汇总；
- 每个 sample 的状态、错误、路径与 usage。

可捕获的中断会写 `state=interrupted`，并尽力用已完成 sample 生成一份部分
`sft.jsonl`。`SIGKILL`、断电或宿主机故障无法执行最后一次写入，但原子写和周期 checkpoint
可以保证 `info.json` 是上一个完整快照；Viewer 还会扫描已 finalize 的 sample。

### 5.3 Sample `info.json`

每个完成 finalize 的 sample 都会写入，包括失败 sample。它保存 task/model、Harness、
最终状态、错误、attempt 历史、usage、耗时、raw/ACTF/SFT 相对路径、输出回收信息、
runtime 和输入 provenance。训练数据刻意删除的 metadata 在这里保留。

### 5.4 `cache_trace.jsonl`

OpenClaw 的 `diagnostics.cacheTrace` 记录每次 LLM call 组装后的 system prompt。默认启用，
Roller 复制后按敏感 key 递归脱敏，再用它填充 ACTF 顶层 `system_prompt`。

- 默认 `--wire-trace actf`：用于转换后删除；
- `--wire-trace full`：最终保留；
- `--no-cache-trace`：不创建，ACTF/SFT 的 system content 为 `""`。

ACTF 顶层只使用首个 `stream:context.system`，没有该阶段时回退到首个
`session:loaded.system`。如果 system prompt 会随调用变化，只有 full 模式保留的原始
cache trace 能展示每次差异；ACTF 顶层值不能代表所有 step。

脱敏基于 JSON key，不会扫描 system prompt 自由文本中的秘密；该文件和下游 ACTF/SFT
仍应按敏感数据管理。

### 5.5 `wire_trace.jsonl`

本地反向代理在请求发往模型服务前记录 wire 数据：

- OpenClaw `actf` 只记录 model、非消息参数、消息数量以及首次非空 tools schema，不记录
  完整 messages/input；该临时文件在转换后删除；
- Codex `actf` 不启动代理，直接从 native session 生成 ACTF；
- `full` 保存每次完整请求体并最终保留；
- HTTP Authorization 等 header 不写入文件，但完整 request body 可能包含 system prompt、
  用户数据、tool result 和历史 reasoning；`full` 不对这些自由内容做自动脱敏。

对可解析的 JSON 请求，代理先执行当前 rollout 需要的语义改写，再记录并转发同一个 body。
OpenClaw `--no-keep-thinking` 会先剥离历史 reasoning；external thinking 会关闭 native
reasoning 并设置 THINK/ACT 的 instructions、tools 和 tool choice。Codex full wire 中
`input[].type="additional_tools"` 是 programmatic tool namespace；external THINK 阶段只保留
`functions.exec`，ACT 阶段恢复原始 namespace。因此 full wire 展示的是 Roller 客户端实际
交给模型 API 的语义 JSON，而不是改写前的 Harness 草稿。

边界与限制：

- 这是请求侧 JSON 记录，不是 TCP/HTTP 字节级抓包；JSON 空白、字段顺序等序列化细节
  不属于审计语义；
- 不记录请求 headers、上游响应 body 或流式 chunk；响应内容仍应从 session raw/ACTF
  查看，但它已经是 Harness 解析后的事件；
- 只能证明 Roller 代理转发了什么，不能证明模型服务内部是否再次截断、tokenize、改写
  或路由；
- wire 写入是 best effort：单条记录失败不会阻断模型调用。强审计场景应检查 `seq` 是否
  连续，并结合 sample usage/request count 与运行日志确认完整性；
- 多轮请求会重复历史，文件体积接近 O(N²)，只适合审计和调试。

### 5.6 如何审计 context 压缩或改写

1. 使用 `--wire-trace full` 运行最小可复现 task；OpenClaw 同时保留默认
   `--cache-trace`。不要用默认 `actf` 代替 full，因为它不保存 messages/input。
2. 按 `wire_trace.jsonl` 的 `seq` 顺序比较各条 `body.messages`（Chat Completions）或
   `body.input`（Responses），同时检查 `body.tools` 与采样参数。
3. 将差异与 `trace.jsonl` 的 session 事件对照。不要按行一一对应：一次 Agent turn 可以
   包含多次模型请求，每次 wire body 又会重复当时保留的历史。
4. OpenClaw 可查看 `cache_trace.jsonl` 中每个 `stream:context` 的 system prompt；Codex 则
   直接比较 full wire 各请求的 `instructions`、developer input 和 environment context。
5. 若 OpenClaw 使用 `--no-keep-thinking`，以 full wire 为模型可见历史的依据；session raw
   保留的 reasoning 只用于诊断，不代表它被送入了后续请求。Codex 的该参数只控制导出。

审计完成后应单独保护或删除 cache/wire 文件。它们可能包含重复的用户输入、system
prompt、tool result、reasoning、文件内容和运行时注入信息。

### 5.7 `results/`

只有 task 输出目录中存在普通文件或目录时才创建。Roller：

- 复制所有安全的普通文件并保留相对路径；
- 在 sample `info.json` 中记录 path、大小、MIME 和 SHA-256；
- 拒绝符号链接和特殊文件；
- 即使存在不安全文件，安全文件仍会复制，但 sample 最终状态变为 `error`；
- `--collect-path` 的兼容输出放入 `results/legacy/`。

`results/` 是 Agent 交付物，不属于对话训练数据，也不会写入 `sft.jsonl`。

### 5.8 `conversation.json`

仅 User Simulator task 生成。它保存双方可见的 user/assistant 轮次、终止原因、待发送但
未交付的 instruction、参与模型和时间，不复制双方隐藏 reasoning、tool call 或 tool
result。需要训练或分析任一参与方时，应使用各自的 raw/ACTF/SFT。

## 6. 参数组合如何影响轨迹

### 6.1 Wire trace

| Harness / 模式 | 代理要求 | ACTF tools | 最终辅助文件 | 适用场景 |
| --- | --- | --- | --- | --- |
| OpenClaw `actf`（默认） | 有可代理 `baseUrl` 时启用；缺少时降级 | 捕获成功时为完整 schema，否则 `null` | 不保留 cache/wire | 常规批量采集 |
| Codex `actf`（默认） | 不启动代理 | 根据 native 实际调用生成 permissive schema | 不保留 wire | 常规批量采集 |
| OpenClaw `full` | provider 必须有绝对 `baseUrl` | 完整 schema | `wire_trace`，启用 cache 时还有 `cache_trace` | 请求级审计 |
| Codex `full` | 必须显式 `--api-base --network host` | native permissive schema | `wire_trace` | 请求级审计 |
| `off` | 不为记录启动代理 | OpenClaw 为 `null`；Codex 仍从 native 推导 | 不保留 | 最小诊断关闭模式 |

OpenClaw 的 `off` 或 `actf` 降级后，如果轨迹实际调用了工具，SFT 校验会因缺少工具 schema
失败；没有工具调用时，SFT 的 `tools` 为 `[]`。Codex 会从 native 实际调用推导 schema，
所以不依赖 wire 来满足这项结构校验，但该 schema 不是完整权威接口定义。

### 6.2 Thinking

| 参数 | 当前回复 | 后续请求历史 | ACTF | SFT | raw |
| --- | --- | --- | --- | --- | --- |
| `--keep-thinking`（默认） | 后端返回什么就接收什么 | 保留 Harness 正常历史 | 保存可见 reasoning | 从 ACTF 保存 | 保留原始事件 |
| `--no-keep-thinking` | 当前轮仍可产生 reasoning | OpenClaw 剥离后续 wire history；Codex resume 不改写 | reasoning 置空 | reasoning 置空 | 仍保留原始事件 |

`--thinking LEVEL` 控制向 OpenClaw/模型请求的逻辑档位，但不能凭空生成可见文本。GPT 原生 effort、开源模型 profile 和旧镜像限制见 [Thinking 配置](openclaw-configuration.md#5-thinkingsystem-prompt-与-wire-trace)。
后端只报告 reasoning token、却不返回 reasoning summary/content 时，ACTF/SFT 没有可保存
的 `reasoning_content`。GPT 的压缩 summary 会原样进入该字段，encrypted reasoning
不会进入 SFT。

`--no-keep-thinking` 即使配合 `--wire-trace off`，也必须启动代理来剥离请求历史，因此
OpenClaw 仍要求 `--model-config` 中存在可代理 `baseUrl`；Codex 不做该 history 剥离。

`--external-thinking external|external_always` 是另一条路径：native reasoning 固定关闭，
显式 thoughts 在 raw/ACTF 作为工具调用保存，并在 SFT 中折叠回
`reasoning_content`。`--no-keep-thinking` 不删除这些显式 thoughts。

### 6.3 Cache trace（OpenClaw）

| 参数 | ACTF/SFT system | `cache_trace.jsonl` |
| --- | --- | --- |
| `--cache-trace`（默认） | 使用捕获到的首个组装后 system prompt | 仅 full 模式最终保留 |
| `--no-cache-trace` | `""` | 不创建 |

如果训练需要保留 Agent 首次捕获到的组装后 system prompt，不应关闭 cache trace。若需要
复现每一次模型调用的 context，仅有 ACTF/SFT 不足，必须另外保存 full cache/wire。

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

Resume 先要求当前 run lock 与旧 `lock.json` 一致；task/model 顺序、Task Bundle、模型配置、
Docker image ID、timeout 或轨迹语义选项变化时直接停止，避免不同采集条件混批。旧 run
没有锁时会写入当前锁并把旧 sample 全部重跑一次；持久迁移标记会一直禁止复用，直到
整次重采和 run 级 SFT 汇总成功，即使中途被强杀也不会误信遗留 sample。

锁一致后，`--resume` 只复用旧状态为 `ok` 且满足以下条件的 sample：

- sample identity 与当前 request 一致；
- raw 含有效 message，ACTF schema/连续 step 合法；
- SFT 与 ACTF 重新生成的 canonical 投影完全一致；
- `full` 模式要求 wire trace 存在，启用 cache trace 时也要求 cache trace 存在；
- 当前 Task Bundle digest 与旧 provenance 一致；
- Context 没有使用 `capture=none`；
- 输出文件路径安全、文件存在且大小未改变；
- User Simulator task 还具有合法的 `conversation.json`、participant info 和 Simulator
  raw/ACTF/SFT。

复用结果在本次 run 中标为 `skipped`，仍会写入新的 `sft.jsonl`。缺失任一必需文件或
轨迹校验不通过时，会重新执行整个 sample。输出清单保存的 SHA-256 不会在 Resume 时
重新计算，以免为了常规断点续跑再次读取大型产物；需要内容级审计时可单独核对该 hash。

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

### 调试 reasoning、compaction 或请求历史

```bash
roller collect \
  --tasks tasks.jsonl \
  --model provider/model \
  --model-config models.json \
  --thinking high \
  --wire-trace full \
  --output debug-runs
```

该配置用于确认 OpenClaw 经过 context 组装、压缩、裁剪和 provider 适配后，实际交给
模型 API 的 messages/input。调试结束后优先保留 raw/ACTF/SFT；确认不再需要请求级审计
时删除 full cache/wire，减少重复数据和敏感信息暴露。

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
   发布数据；
9. 如果数据声明要复现模型可见 context，确认采集使用 `--wire-trace full`、wire `seq`
   连续且请求数符合预期；不要用 ACTF/SFT 代替逐请求证据。
