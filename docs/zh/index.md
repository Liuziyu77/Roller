# Roller 文档

Roller 支持用多种模型和 Harness 采集 Agent 轨迹。每个 task 与模型的组合在独立的一次性 Docker 容器中运行，并保存：

- `trace.jsonl`：Harness 原始轨迹
- `trace_ACTF.json`：统一分析与可视化格式（`ACTF_v1.0`）
- `trace_SFT.json`：单条标准训练样本
- `results/`：Agent 生成的文件
- `info.json`：状态、usage 与运行来源

```text
tasks.jsonl × models
        │
        ▼
Docker 中的 Agent Harness
        │
        ├─ trace.jsonl       Harness 原始轨迹
        ├─ trace_ACTF.json   统一分析与可视化格式
        ├─ trace_SFT.json    单条标准训练样本
        ├─ results/          Agent 生成的文件
        └─ info.json         状态、usage 与运行来源
```

Roller 需要 Python 3.10+，使用 Task schema `roller.task/v1` 和轨迹格式 `ACTF_v1.0`。项目处于 Alpha 阶段，许可证为 Apache 2.0。源码见 [GitHub](https://github.com/Liuziyu77/Roller)。

## 核心用途

- 批量采集单轮、多轮或工具型 Agent 轨迹
- 同一批 task 对比多个模型或 Harness
- 生成顶层严格为 `messages` 和 `tools` 的 SFT JSONL
- 保存 reasoning、工具调用、工具结果与任务文件产物
- 用 run lock 检查运行配置，通过重试和 Resume 继续采集
- 在内置 Viewer 中逐步查看每个 task 的 reasoning 和工具操作

## 选择 Harness

| Harness | 模型服务协议 | 适合场景 | 关键能力与限制 |
| --- | --- | --- | --- |
| OpenClaw（默认） | OpenAI Responses 或 Chat Completions | 自定义/开源模型、动态 User Simulator、多模态输入 | 模型通过 JSON 配置；保留原生 reasoning |
| Codex | Responses | Codex CLI 工具轨迹、GPT 与兼容 Responses 服务 | 保留原生 reasoning；可作为 Task Agent 或 User Simulator |
| Claude Code | Anthropic `/v1/messages` | Claude Code 工具轨迹与 native thinking | 保留原生 thinking；可作为 Task Agent 或 User Simulator |
| Hermes | OpenAI Chat Completions | 原生请求与工具消息 → ACTF/SFT | 容器内 terminal/file；可作为 Agent 或 Simulator |
| OpenCode | OpenAI Chat Completions | CLI session / tools → ACTF/SFT | 固定版本，文本与文件工具；支持 Agent / Simulator |
| DeepSeek Harness | DeepSeek Chat Completions | 原生 SDK / 持久化 Bash → ACTF/SFT | sdk-minimal；支持 Agent / Simulator |
| Pi Agent | OpenAI Chat Completions | 原生 AgentSession / 七种工具 → ACTF/SFT | 固定 SDK 版本；支持 Agent / Simulator |

七种 Harness 使用相同的 Task、Sample、ACTF 和 SFT 格式，也共用 Viewer、重试和 Resume 流程。镜像、API 和 reasoning 参数见对应 Harness 指南。

## 阅读顺序

| 阶段 | 阅读内容 | 完成标准 |
| --- | --- | --- |
| 1. 跑通 | [快速上手](start/getting-started.md) | 完成 dry-run、采集并打开 Viewer |
| 2. 选 Harness | [OpenClaw](harnesses/openclaw-configuration.md)、[Codex](harnesses/codex-configuration.md) 或 [Claude Code](harnesses/claude-code-configuration.md)、[Hermes](harnesses/hermes-configuration.md) · [OpenCode](harnesses/opencode-configuration.md) · [DeepSeek](harnesses/deepseek-configuration.md) · [Pi Agent](harnesses/pi-configuration.md) | 镜像、API 协议和 reasoning 参数匹配 |
| 3. 写 Task | [Task 格式参考](tasks/task-format.md) | 能声明 prompt/messages、输入、Context 和输出目录 |
| 4. 验收产物 | [轨迹产出文件指南](traces/trajectory-outputs.md) | 能区分 raw、ACTF、SFT、wire 与 results |
| 5. 批量运行 | [Task Bundle 与结果存储](tasks/task-bundle-and-storage.md) | 理解快照、run lock、retry 与 Resume |

仓库内可运行命令见 [Examples](start/examples.md)。

```{toctree}
:maxdepth: 1
:caption: 快速上手

start/getting-started
start/examples
start/cli-reference
```

```{toctree}
:maxdepth: 1
:caption: Harness 配置

harnesses/openclaw-configuration
harnesses/codex-configuration
harnesses/claude-code-configuration
harnesses/hermes-configuration
harnesses/opencode-configuration
harnesses/deepseek-configuration
harnesses/pi-configuration
```

```{toctree}
:maxdepth: 1
:caption: Task 与运行

tasks/task-format
tasks/task-bundle-and-storage
tasks/user-simulator
```

```{toctree}
:maxdepth: 1
:caption: 轨迹与训练数据

traces/trajectory-outputs
traces/sft-format
traces/viewer
```

```{toctree}
:maxdepth: 1
:caption: Bench

benches/bench-format
benches/wildclawbench
benches/skillsbench
benches/pinchbench
```
