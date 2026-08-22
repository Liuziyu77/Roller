# Roller 文档中心

[项目首页](../README.md) · [快速上手](getting-started.md) · [示例](../examples/README.md) · [CLI 参数参考](cli-reference.md)

文档分为快速上手、Harness 配置、Task、轨迹产物和高级功能五类。各专题页独立维护，不要求按目录顺序通读。

## 阅读顺序

| 阶段 | 阅读内容 | 完成标准 |
| --- | --- | --- |
| 1. 跑通 | [快速上手](getting-started.md) | 完成 dry-run、采集并打开 Viewer |
| 2. 选 Harness | [OpenClaw](openclaw-configuration.md)、[Codex](codex-configuration.md) 或 [Claude Code](claude-code-configuration.md) | 镜像、API 协议和 reasoning 参数匹配 |
| 3. 写 Task | [Task 格式参考](task-format.md) | 能声明 prompt/messages、输入、Context 和输出目录 |
| 4. 验收产物 | [轨迹产出文件指南](trajectory-outputs.md) | 能区分 raw、ACTF、SFT、wire 与 results |
| 5. 批量运行 | [Task Bundle 与结果存储](task-bundle-and-storage.md) | 理解快照、run lock、retry 与 Resume |

仓库内可运行命令集中在 [Examples](../examples/README.md)。

## 按目标查文档

### 配置与运行

| 目标 | 文档 |
| --- | --- |
| 从空环境完成第一次采集 | [快速上手](getting-started.md) |
| 查某个 CLI 参数、默认值或适用 Harness | [CLI 参数参考](cli-reference.md) |
| 连接 Responses、Chat Completions 或开源模型服务 | [OpenClaw Harness 指南](openclaw-configuration.md) |
| 使用 Codex CLI 采集轨迹 | [Codex Harness 指南](codex-configuration.md) |
| 使用 Anthropic Messages API 与 Claude Code | [Claude Code Harness 指南](claude-code-configuration.md) |
| 批量并行、retry 或断点续跑 | [Task Bundle 与结果存储](task-bundle-and-storage.md#6-run-与-sample) |

### Task 与文件

| 目标 | 文档 |
| --- | --- |
| 编写单轮或预定义多轮 task | [Task 格式参考](task-format.md) |
| 携带本地文件、Context、skills 或 setup | [Task 格式参考](task-format.md#4-fileroot) |
| 理解输入快照、工作区和结果回收 | [Task Bundle 与结果存储](task-bundle-and-storage.md) |
| 让另一个模型动态扮演用户 | [User Simulator](user-simulator.md) |

### 轨迹与训练数据

| 目标 | 文档 |
| --- | --- |
| 知道每个产物何时生成、应该看哪个 | [轨迹产出文件指南](trajectory-outputs.md) |
| 直接训练 `messages` + `tools` 数据 | [SFT 数据格式](sft-format.md) |
| 核对模型实际收到的请求 context | [Wire trace 与 context 审计](trajectory-outputs.md#32-session-历史不等于模型请求-context) |
| 用工具参数保存显式 CoT | [External Thinking](external-thinking.md) |
| 恢复 Claude signed thinking | [Claude Signature Recovery](claude-code-configuration.md#5-signature-recovery实验性) |
| 在网页中检查 reasoning 和工具步骤 | [Viewer](viewer.md) |

## 按 Harness 选择

| 能力 | OpenClaw | Codex | Claude Code |
| --- | --- | --- | --- |
| API 协议 | Responses / Chat Completions | Responses | Anthropic Messages |
| 模型配置入口 | `--model-config` | `--api-base` + `--api-key-env` | `--api-base` + `--api-key-env` |
| 单轮 / 预定义多轮 | 是 / 是 | 是 / 是 | 是 / 是 |
| 动态 User Simulator | 是 | 否 | 否 |
| External thinking | 是 | 是 | 否 |
| Signature recovery | 否 | 否 | 实验性 |
| Wire request capture | 是 | 是 | 是 |
| Harness 专属指南 | [打开](openclaw-configuration.md) | [打开](codex-configuration.md) | [打开](claude-code-configuration.md) |

## CoT 路径选择

| 场景 | 推荐配置 |
| --- | --- |
| GPT reasoning 系列（如 `gpt-5.6-sol`）需要比压缩 summary 更完整的显式 CoT | OpenClaw/Codex `--external-thinking external|external_always` |
| Claude 系列（如 Claude Opus 4.8，示例 ID `claude-opus-4-8`）存在 signed thinking | Claude Code `--claude-signature-recovery fill|prefer` |
| Qwen、GLM 等开源模型的 API 返回明文 reasoning | 默认 `--keep-thinking`，直接写入 ACTF/SFT `reasoning_content` |

第三种路径要求后端返回明文 `reasoning_content`、`reasoning` 或 `reasoning_text`。Reasoning token 数本身不能转换成 CoT。

## 核心概念

| 名词 | 含义 |
| --- | --- |
| Task | 一条输入数据，声明对话、文件、Context、输出目录和运行要求 |
| Model ref | Harness 使用的模型标识；OpenClaw 通常为 `provider/model` |
| Rollout | 一个 task 与一个任务 Agent 模型的实际执行 |
| Harness | 运行 Agent 并导出 native raw、ACTF 和 usage 的适配器 |
| Task Bundle | 运行前构建的不可变输入与 Context 快照，只在运行期存在 |
| Run | 一次 `roller collect` 的完整目录，包含多个 sample 和聚合 SFT |
| Sample | 一个 rollout 的最终保存目录 |
| ACTF | Roller 的统一分析轨迹 `ACTF_v1.0` |
| SFT | 顶层只有 `messages` 与 `tools` 的训练投影 |
| Wire trace | Roller 客户端实际转发给模型 API 的 request body 记录 |

命令行传入多个 `--model` 时，每个 task 会与每个模型组合成独立 rollout；`--parallel` 控制同时运行的 rollout 数，不是单个 rollout 内的模型调用数。

## 文档职责

- `README.md`：项目定位和最短入门路径；
- `getting-started.md`：首次运行的完整流程；
- Harness 指南：只解释对应镜像、API、reasoning、native raw 与限制；
- `task-format.md`：输入 schema 的权威人工参考；
- `trajectory-outputs.md`：产物条件和信息边界的权威参考；
- `sft-format.md`：训练投影 schema 的权威参考；
- `cli-reference.md`：参数适用范围和默认值。

机器可读 Task 约束见 [`schemas/task.schema.json`](../schemas/task.schema.json)。CLI 的最终事实来源始终是：

```bash
roller collect --help
roller view --help
```

当前范围：Python 3.10+、Task schema `roller.task/v1`、轨迹格式 `ACTF_v1.0`，项目处于 Alpha 阶段。
