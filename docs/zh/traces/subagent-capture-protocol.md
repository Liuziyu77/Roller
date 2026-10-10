# Subagent 采集协议 v1

目标：不同 harness 使用同一套调用、参与者、关系和完整性规则。Roller
记录执行事实，不在这里定义训练损失或奖励。

## 开启

`roller collect ... --capture-calls` 支持目前七个 harness：Claude Code、
OpenCode、Codex、Hermes、OpenClaw、Pi 和 DeepSeek。旧的
`--claude-capture-calls` 保留。需要显式 API base、host 网络、开启 wire trace。
默认不开启；OpenCode 仅在开启此选项时允许原生 `task` 子任务工具。
采集模式写入 run 配置，resume 不能混用开启前后的样本。

## 三个对象

| 对象 | 协议 | 含义 |
|---|---|---|
| Participant | manifest 的 `participants` | 一个原生执行者；一个执行者可以有多次调用 |
| Call | `roller.call/v1` | 一次实际 HTTP 模型请求及响应 |
| Relation | `roller.relation/v1` | 有证据支持的启动、结果交付和上下文关联 |

`participant_id` 是不透明的引用标识。消费者通过 `role` 判断 root/child，
不要通过标识前缀解释角色。参与者身份在一次采集 attempt 内有效；跨样本或
attempt 引用时必须带 sample/manifest 路径。`call_id` 和新生成的
`relation_id` 包含采集实例的随机作用域，避免不同样本、重试的编号冲突。

Call 的 `context` 保留实际发送的完整请求体；`output` 是重组后的 provider
响应。`protocol` 区分 `anthropic-messages`、`openai-chat-completions`
以及 `openai-responses`，按实际 HTTP 请求端点识别协议，各协议保持自己的消息格式。原始响应文件也保留，不靠原生日志重建请求。
不保存 HTTP 认证头，不用父 agent 的配置覆盖子 agent 的模型和工具配置。

| 状态 | 回答的问题 |
|---|---|
| `execution_status` | 调用/任务是否执行完成？ |
| `response_capture_status` | 响应是否完整落盘？ |
| `identity_status` | 能否确定属于哪个参与者？ |
| `capture_status` | 当前记录是否完整？ |
| `export_status` | 这次调用是否能进入逐调用导出？ |

仅当模型响应正常结束、输入响应完整、身份明确、模型匹配且没有采集错误时，
Call 才可导出。未归属、失败、中断的请求仍保留在 attempt 的调用和原始
证据中。整体采集可以是 partial，同时包含可导出的完整调用。

## 关系语义

| type | 需要的证据 |
|---|---|
| `spawn` | 原生工具调用及子任务身份 |
| `result_delivered` | 父任务收到了子任务的最终工具结果 |
| `child_completed` / `child_failed` | 原生子任务完成/失败证据 |
| `result_in_context` | 后续父模型的实际输入里存在相同工具调用 ID 的结果 |
| `launch_acknowledged` | 后台子任务启动回执 |
| `launch_ack_in_context` | 启动回执进入父模型实际输入 |

每条关系保留原生文件及事件/part ID 引用。父模型调用通过响应 message ID
或其输出中的工具调用 ID 关联；无法唯一关联时标记 unknown。OpenClaw 的
Responses SDK 使用 `call_id|item_id` 作为原生工具 ID；关系保留原生 ID，并用
`model_tool_ref` 同时核对 HTTP 输出中的 call_id 和 item_id，不能只匹配其中一个。结果进入多个
后续调用时，分别记录关联。后台启动回执不能当成任务完成。

## 公共层与适配层

适配层只返回 `capture_protocol.NativeCapture`：参与者、身份映射、原生关系、
可观测输出 ID/数量和能力声明。公共 `finalize_capture` 负责关联 HTTP 调用、
判断状态、生成上下文关系、写文件和 manifest。`model_protocols` 负责
Anthropic/Chat Completions/Responses 响应解码和工具引用提取。

| harness | 身份证据 | 原生输出覆盖检查 |
|---|---|---|
| Claude Code | provider 响应 ID 与原生日志 message ID 匹配 | 逐 message ID |
| OpenCode | `chat.headers` 提供的 session ID 与原生日志匹配 | 各 session 完成的 assistant 数量 |
| Codex | 每次 HTTP 请求原生 `thread-id` 与 rollout session_meta 匹配 | token_usage 更新数量；不是逐响应 ID |
| Hermes | 执行实例自己的 session ID 与原生日志匹配 | provider 响应 ID |
| OpenClaw | 原生 stream wrapper 的 session ID 与 session 文件匹配 | stream context 数量 |
| Pi | SDK session ID | 完成的 assistant 数量 |
| DeepSeek | runner session ID | 完成的 assistant 数量 |

OpenCode 的关联头仅在本地代理使用，代理向模型服务转发前移除。各请求携带
自己的 session ID，因此并行请求不依赖“当前 session”全局变量。Codex 的原生
`thread-id` 继续转发给模型服务，只移除 Roller 新增的 `x-roller-*` 身份头。原生数量
检查比逐 ID 检查弱，manifest 的 `capabilities.native_output_coverage` 明确
记录这一差别；不根据时间、文本或请求次序猜身份。

主 ACTF/SFT 只转换主参与者的消息和请求配置；子任务内部消息留在自己的
Call 中。`calls.jsonl` 是逐调用的协议数据，不能直接当成主轨迹 SFT。

## 文件与边界

每个 `capture/attempt-NNNN/` 顶层只有 `manifest.json`、`calls.jsonl`、
`relations.jsonl` 和 `raw/`。原生日志、原始响应、采集事件及来源映射集中在
`raw/`；事件的 `body_ref` 引用 `calls.jsonl` 中对应调用的 `context`，不重复保存
请求正文。参与者通过 manifest 的 `call_ids` 和 Call 的 `participant_id` 关联，
不再另存 `participants/*/calls.jsonl` 副本。

manifest 使用 `roller.capture/v1`，新布局以 `storage_layout: compact/v1` 标识。
读取和完整性校验兼容旧布局；采集关闭前仍使用工作文件，发布时整理为新布局。
可用 `PYTHONPATH=src python scripts/view_capture.py /path/to/attempt-0001`
查看按代理展开的调用树，不生成额外的数据副本。
关闭 recorder 后再发布文件哈希；文件缺失、修改、错误引用会阻止 resume
复用。即使关闭辅助日志保留，也不会清理 capture 正式证据。

## 七个 harness 的执行边界

- Claude Code / OpenCode：已有原生子任务适配。未完成的后台任务标记 partial。
- Codex：保留 Responses 原生 output（包括加密 reasoning）及所有线程 rollout；
  spawn 返回的是启动回执，wait 返回完成结果。多轮执行通过已确认的根线程 ID resume，
  防止 `resume --last` 误选最近的子线程。根 ACTF 的工具快照只取根线程，完整快照仍作为证据保存。
  `tool_search_output` 动态加载的定义保存在实际 Call.context 和工具快照；这种轨迹的
  主 SFT 暂不导出，避免丢失加载顺序或虚构静态工具列表。逐调用导出不受此边界影响。
- Hermes：在原生 delegate_task 创建子实例时记录父 session ID 和委派工具 ID；
  一个批量工具调用可以对应多个孩子。使用原生有限查询 session 设置等待孩子返回。
- OpenClaw：固定镜像内的原生 stream wrapper 记录各 session 的实际上下文；
  等待原生 registry 中的子任务及完成通知清理结束，再收集文件。启动回执与完成通知分开；
  完成通知只有与父模型实际输入逐字匹配才建立 result_in_context。工具 ID 规范化映射来自
  原生 resolver 的实际输出。支持按实际请求端点解码 Chat Completions、Responses 或 Messages，
  真实测试结果分别注明模型与协议。固定镜像的 hook 不匹配时会报错。
- Pi / DeepSeek：统一记录实际模型 I/O、根参与者和完整性。Roller 内置的 Pi 配置禁用
  扩展，DeepSeek 使用 sdk-minimal profile；二者没有原生委派工具。manifest 明确声明
  `not_available_in_bundled_profile`，子任务测试为 not_applicable，不能当作通过。

共同验收覆盖单子任务、并行、相同提示词、文件共享、工具错误、不同工具配置和普通对话。
跨进程恢复、完整嵌套执行和 compaction 分段重建仍不在此实现范围。

共享验收入口：`scripts/test_subagents_e2e.py --harness HARNESS ...`（七个 harness 均可选）。
测试凭据只从指定环境变量读取。

OpenCode 原生接口依据：[固定版本的插件 hook 定义](https://github.com/anomalyco/opencode/blob/v1.18.30/packages/plugin/src/index.ts)、
[task 工具元数据](https://github.com/anomalyco/opencode/blob/v1.18.30/packages/opencode/src/tool/task.ts)。
