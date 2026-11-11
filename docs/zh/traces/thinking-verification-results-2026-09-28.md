# Issue #51 thinking 复测结果（2026-09-28）

检查方案见 [thinking 回归检查矩阵](thinking-verification-matrix.md)。本次使用
PR #64 的 `af681e7` 代码和所提供的兼容 API 服务；在线任务每格采样一次、
超时 180 秒、Roller 重试次数 0。主矩阵使用默认 `actf` 采集；对照组针对
Qwen 模型使用 `full` 采集。七个模型分别为 `qwen3.8-flash`、
`glm-5.3-flash`、`kimi-k2.7-code-highspeed`、`deepseek-v4-flash`、
`gpt-5.6-luna`、`claude-opus-4-8` 和 `gemini-3.8-flash`。
四个主矩阵批次并发，每批次两个 worker。

| 阶段 | 已计入 / 预期 | PASS | S | U | T | R | I | H | X | SFT 汇总 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 连通 smoke | 7 / 7 | 5 | 0 | 2 | 0 | 0 | 0 | 0 | 0 | 7 / 7 |
| 默认主矩阵 | 196 / 196 | 82 | 8 | 23 | 1 | 3 | 21 | 28 | 30 | 106 / 106 |
| `full` 对照 | 28 / 28 | 20 | 1 | 2 | 0 | 0 | 0 | 4 | 1 | 22 / 22 |

表中 S 为明确 thinking 冲突，U 为请求档位无法核实，T 为任务结果不符，
R 为 SFT 表示不支持，I 为超时，H 为预检拒绝，X 为执行错误。
三个阶段的 SFT 汇总行数均与可导出 sample 数一致；扫描采集产物未发现本轮
API 凭据明文。

## 需要关注的结果

1. 主矩阵有 8 个明确 thinking 冲突，均未进入 SFT。其中 7 个是请求
   `off` 后 ACTF 仍有可见 reasoning：OpenCode/Claude、DeepSeek/GLM、Pi/Claude，
   以及 OpenClaw 的 Qwen、GLM、Kimi、Claude。另一个是 OpenClaw 2026.7.1
   对 `deepseek-v4-flash` 请求 `low`，三次出站记录均为 `high`；Roller 记录
   `wire_effort_mismatch` 并排除该 sample。Roller 传给 OpenClaw 的配置和命令均为
   `low`，具体在哪一环节被改写，尚需在 OpenClaw/服务路由链路中继续定位。
2. 默认采集下 23 格为 U：Hermes 14 格、OpenClaw 9 格。`full` 对照
   的 OpenClaw/Qwen 仍有两格 U，表明即使保留完整请求，部分出站字段也
   只能证明已启用 reasoning，无法证明精确档位；此类结果不得计为 PASS。
3. 28 格 H 来自 Hermes/OpenCode 的 `max`、Codex/Claude Code 的
   `off`，四个门禁各覆盖七个模型，均未发出模型请求。另有 OpenClaw 的
   `max` 在运行时报告不支持；网关连接警告之后的 embedded fallback
   同样给出明确的档位错误。
4. Codex/`gpt-5.6-luna` 的 `low/high/max` 三格任务与请求审计通过，
   但响应包含 `additional_tools`，当前 SFT 格式无法无损表示。Roller
   保留原始请求并记录 `trajectory_conversion=unavailable`；统一训练策略
   将这三格以 `conversion_unavailable` 排除。
5. Claude Code/`claude-opus-4-8`/`max` 正确写入并读回目标文件，但最终
   回答多出一句说明文字，因此判为 T。响应模型名带 `global.anthropic.` 前缀；
   共 11 格记录了名称差异，该差异仅表明返回名称与请求名称不一致。
6. 21 格超时集中在较慢的模型和多个 Harness；30 格 X 主要源于服务
   HTTP 400（例如 GLM/Kimi 不支持关闭推理、Claude Bedrock 适配错误）、
   Codex 上游高负载、OpenClaw `max` 不支持，以及 Gemini 内容过滤。
   这些格子没有可判定的完整 rollout，不能据此证明请求档位正确。

`full` 对照的 20 个 PASS 均有出站请求证据；其余 8 格包括相同的四个
门禁、一项 OpenClaw `off` 可见 reasoning 冲突、两项档位未验证和一项运行错误。
确定性回归检查另有 39 项通过（`test_cli.py`、`test_conversation.py`、
`test_trajectory_audit.py`）。

逐格 JSON、manifest、ACTF、原始日志和 SFT 保存在本地 Git 忽略目录
`runs/issue51-review-matrix-20260928/`。该目录未提交到 PR；上表及异常
sample 的证据状态由 `scripts/verify_thinking_matrix.py` 根据这些产物重新计算。
