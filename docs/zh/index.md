# Roller 文档

Roller 是面向多模型、多 Harness 的 Agent 轨迹采集框架。它把 task 清单与模型列表展开为独立 rollout，在一次性 Docker 容器中运行 Agent，并同时保存：

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

当前范围：Python 3.10+、Task schema `roller.task/v1`、轨迹格式 `ACTF_v1.0`。项目处于 Alpha 阶段，许可证为 Apache 2.0。源码见 [GitHub](https://github.com/Liuziyu77/Roller)。

## 核心用途

- 批量采集单轮、多轮或工具型 Agent 轨迹
- 同一批 task 对比多个模型或 Harness
- 生成顶层严格为 `messages` 和 `tools` 的 SFT JSONL
- 保存 reasoning、工具调用、工具结果与任务文件产物
- 用 run lock、重试和 Resume 构建可恢复的数据生产流程
- 在内置 Viewer 中逐步检查每个 task 的思考与行动轨迹

## 选择 Harness

| Harness | 模型服务协议 | 适合场景 | 关键能力与限制 |
| --- | --- | --- | --- |
| OpenClaw（默认） | OpenAI Responses 或 Chat Completions | 自定义/开源模型、动态 User Simulator、多模态输入 | 支持 `external` / `external_always`；模型通过 JSON 配置 |
| Codex | Responses | Codex CLI 工具轨迹、GPT 与兼容 Responses 服务 | 支持 external thinking；可作为 Task Agent 或 User Simulator |
| Claude Code | Anthropic `/v1/messages` | Claude Code 工具轨迹与 native thinking | 支持实验性 signature recovery；可作为 Task Agent 或 User Simulator |
| Hermes | OpenAI Chat Completions | 原生请求与工具消息 → ACTF/SFT | 容器内 terminal/file；可作为 Agent 或 Simulator |

四者共享相同的 Task、Sample、ACTF、SFT、Viewer、重试和 Resume 边界。镜像、API 和 reasoning 参数见对应 Harness 指南。

## 阅读顺序

| 阶段 | 阅读内容 | 完成标准 |
| --- | --- | --- |
| 1. 跑通 | [快速上手](start/getting-started.md) | 完成 dry-run、采集并打开 Viewer |
| 2. 选 Harness | [OpenClaw](harnesses/openclaw-configuration.md)、[Codex](harnesses/codex-configuration.md) 或 [Claude Code](harnesses/claude-code-configuration.md)、[Hermes](harnesses/hermes-configuration.md) | 镜像、API 协议和 reasoning 参数匹配 |
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
traces/external-thinking
traces/viewer
```

```{toctree}
:maxdepth: 1
:caption: Bench

benches/bench-format
benches/wildclawbench
```
