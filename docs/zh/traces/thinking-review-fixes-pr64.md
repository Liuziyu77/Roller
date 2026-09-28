# PR #64 逐项修复说明

本文对应 reviewer yph22 在 PR #64 提出的六项问题，以及与最新 `main` 的合并冲突。
修复由提交 `2015f21` 引入，边界回归测试由 `af681e7` 补充；
在线复测及结果见[检查矩阵](thinking-verification-matrix.md)和
[2026-09-28 复测报告](thinking-verification-results-2026-09-28.md)。

## 1. Claude Code 能力限制可被等价 API URL 绕过

**原因。** Claude Code 运行时会把地址末尾的 `/v1` 或 `/v1/messages` 归一化，
原来的能力预检却用未经处理的 URL 匹配 profile。同一个服务端点写成不同形式时，
预检可能找不到限制并放行不支持的 effort。

**修复。** `thinking_capabilities.py` 对 Claude Code 的 profile 地址和请求地址
统一调用 `normalize_anthropic_base_url()`；`cli.py` 将解析后的
`claude_code_config.api_base` 交给预检，与实际运行配置保持一致。

**验证。** `test_thinking_capabilities.py` 和 `test_cli.py` 覆盖根地址、
`/v1`、`/v1/messages` 三种等价写法。profile 只允许 `low` 时，三者请求
`high` 均在 dry-run 阶段拒绝。

## 2. User simulator 漏掉可见 reasoning 审计

**原因。** 主 Agent 在 Collector 内单独执行可见 reasoning 检查；simulator
归档只调用共享的 `record_training_audits()`。因此 simulator 请求 `off`
但 ACTF 留有 reasoning 时，没有 `thinking_audit` 或 SFT 排除原因。

**修复。** 将可见 reasoning 检查移入 `trajectory_audit.py` 的统一审计入口，
同时在该入口写入 `thinking_effort_mismatch`。Collector 和 simulator 继续调用
同一入口；Collector 中重复的私有检查被移除。simulator 仍按原有规则不计入
主 Agent 的 SFT 汇总。

**验证。** `test_conversation.py` 用 simulator 归档实际路径断言 mismatch 和
排除原因；`test_trajectory_audit.py` 验证统一入口对相同 ACTF 的判定。

## 3. Response 截断证据未参与完成状态判定

**原因。** 原逻辑在 ACTF 存在时只看 ACTF 的 stop reason。若最终响应明确为
`incomplete/max_output_tokens`，但 ACTF 给出 `stop`，截断样本仍可能被视为
可训练。

**修复。** `trajectory_audit.py` 增加 `response_completion_audit`，读取与请求
关联的响应元数据，以最后一个已观测请求判定最终状态。最终响应截断时，
保留 ACTF 原判断到 `actf_completion_audit`，并把有效完成状态置为
`incomplete`、排除原因为 `token_limit`；如果后续请求成功完成，则不因之前
的截断排除。

**验证。** `test_trajectory_audit.py` 同时覆盖“ACTF 为 stop、最终 response
截断”和“早期截断后成功恢复”。这是审计输入的确定性复现，不宣称所有真实
服务都会返回这类组合。

## 4. 默认 `actf` 模式忽略原生请求证据

**原因。** effort 审计原先只读 `wire_trace.jsonl`。Pi、DeepSeek、Hermes、
OpenCode 在默认采集下可能没有 wire trace，但原生 `trace.jsonl` 已保存请求
参数；这些明确证据被误判为不可观察。

**修复。** `trajectory_audit.py` 在没有 wire 请求行时，按已知原生 trace
schema 读取请求快照，使用同一 `_request_effort()` 判定，并记录证据来源
`wire` 或 `native`。明确不符写为 `wire_effort_mismatch` 并阻止 SFT 汇总；
请求只表明“启用推理”但没有精确档位时，仍判 `unverified`。

**验证。** `test_trajectory_audit.py` 分别用四种原生 schema 构造“请求
`high`、任务声明 `low`”，断言默认路径识别 mismatch、记录 `native` 来源并
排除样本。真实服务主矩阵在默认 `actf` 下形成 105 项 native 请求审计。

## 5. Codex `off` / `adaptive` 预检与执行规则不一致

**原因。** CLI 预检使用通用 `normalize_thinking()`，它接受这两个值；
Codex 运行时的 `_effective_effort()` 则拒绝，导致 dry-run 放行、rollout
才失败。

**修复。** `harnesses/codex.py` 抽出 `codex_effort()`，由 CLI 预检和运行时
共同调用。`off` 和 `adaptive` 在启动前拒绝，并返回各自的原因；没有为
Codex 虚构这两个档位的支持。

**验证。** `test_cli.py` 覆盖 dry-run，`test_codex.py` 覆盖运行时规则。
真实服务矩阵中的 Codex `off` 七格均为预检拒绝，没有模型请求。

## 6. Claude `ultra` 逻辑档位与传输值混用

**原因。** Claude Code 将用户选择的 `ultra` 转成出站值 `ultracode`，旧元数据
把传输值记作请求档位。通用审计只认识逻辑档位，于是可能把实际匹配的请求
判为 `unverified`。

**修复。** Claude Code 元数据分别保存 `reasoning_effort=ultra` 和
`transport_effort=ultracode`。出站审计读取传输值后映射回逻辑档位再比较，
报告仍显示用户选择的 `ultra`。这只修正记录和比较语义，不推断上游一定支持
`ultracode`。

**验证。** `test_trajectory_audit.py` 构造 `ultracode` 出站请求，断言报告中
请求档位为 `ultra` 且判为匹配。

## 与最新 `main` 的合并冲突

冲突发生在 `wire_trace.py` 的请求记录异常处理。`main` 的 #66 要在工具捕获
失败时保存 `proxy.recording_error`，本 PR 要保留 `_record(body)` 返回的
`request_seq`，用于将响应元数据关联到原请求。合并后的代码同时保留了两者：
成功记录时保存请求序号；记录失败时保留错误状态，并让 Codex 工具捕获检查
拒绝不完整数据。`test_codex_tools.py` 覆盖记录失败与工具快照缺失场景。

## 本轮在线复测的边界

上述修复针对**检测、资格判定和预检一致性**。真实服务仍出现 OpenClaw
`low` 出站为 `high`、部分 `off` 响应含可见 reasoning、模型不支持某些档位、
服务错误和超时。Roller 已将明确冲突排除、将证据不足标为 `unverified`；
这些上游行为本身并未被本 PR 改写。具体组合、数量及 SFT 对账见复测报告。
