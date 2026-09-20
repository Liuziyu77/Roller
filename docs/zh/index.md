# Roller 文档

Roller 会在一次性的 Docker 容器中运行能够调用工具的 AI Agent，并记录完整过程，包括 prompt、reasoning、工具调用、工具结果、生成文件和运行信息。它适合需要重复采集和评测多种模型、多个 Agent 运行时的场景。

**第一次接触 Roller？** 建议先阅读 [Roller 简介](start/introduction.md)。这部分不要求配置环境，会先解释几个核心概念。然后按照[快速上手](start/getting-started.md)生成第一条轨迹。

## Roller 做什么

你提供一组 Task、一个或多个模型，以及一个 Harness。Roller 会为每个 Task 与模型的组合创建独立 rollout，并把结果写入结构清晰的 run 目录。

~~~text
tasks.jsonl × 模型
        │
        ▼
Docker 中的 Agent Harness
        │
        ├─ trace.jsonl       Harness 原生事件
        ├─ trace_ACTF.json   统一分析轨迹
        ├─ trace_SFT.json    标准训练样本
        ├─ results/          Agent 生成的文件
        └─ info.json         状态、usage 与运行来源
~~~

常见用途包括：

- 采集单轮、多轮和工具型 Agent 轨迹。
- 在同一批 Task 上比较模型或 Harness。
- 检查 reasoning、工具调用、工具结果和生成文件。
- 制作统一的 ACTF 轨迹或 SFT JSONL。
- 继续中断的采集，并在内置 Viewer 中检查结果。

Roller 需要 Python 3.10+。项目目前使用 Task schema <code>roller.task/v1</code> 和轨迹格式 <code>ACTF_v1.0</code>，处于 Alpha 阶段，采用 Apache 2.0 许可证。源码见 [GitHub](https://github.com/Liuziyu77/Roller)。

## 选择 Harness

第一次运行的教程使用 **OpenClaw**。在成功生成第一条轨迹前，你可以一直沿用这条主线。需要特定 Agent 运行时或 API 协议时，再选择其他 Harness。

| Harness | 模型服务协议 | 常见用途 |
| --- | --- | --- |
| OpenClaw（默认） | OpenAI Responses 或 Chat Completions | 自定义/开源模型、多模态输入和动态 User Simulator |
| Codex | Responses | Codex CLI 工具轨迹 |
| Claude Code | Anthropic <code>/v1/messages</code> | Claude Code 工具和 native thinking |
| Hermes | OpenAI Chat Completions | 原生请求与工具消息轨迹 |
| OpenCode | OpenAI Chat Completions | 原生 CLI session 和文件工具 |
| DeepSeek Harness | DeepSeek Chat Completions | 原生 SDK session 和持久 Bash |
| Pi Agent | OpenAI Chat Completions | 原生 AgentSession 和编程工具 |

七种 Harness 使用相同的 Task、Sample、ACTF 和 SFT 格式，也共用 Viewer、重试和 Resume 流程。它们的镜像、API、工具和 reasoning 参数不同。

## 从哪里开始

1. 阅读 [Roller 简介](start/introduction.md)，理解 Task、Harness、rollout、run 和 trace。
2. 按照[快速上手](start/getting-started.md)安装 Roller、执行 dry-run、完成一次采集并打开 Viewer。
3. 准备编写自己的 Task 时，阅读 [Task 格式](tasks/task-format.md)。
4. 需要使用 OpenClaw 以外的运行时时，再阅读对应的 Harness 指南。
5. 需要确认 ACTF、SFT、wire trace 或失败保留规则时，阅读[轨迹产出文件](traces/trajectory-outputs.md)。

~~~{toctree}
:maxdepth: 1
:caption: 开始使用

start/introduction
start/getting-started
start/cli-reference
start/docker-builds
~~~

~~~{toctree}
:maxdepth: 1
:caption: Harness 配置

harnesses/openclaw-configuration
harnesses/codex-configuration
harnesses/claude-code-configuration
harnesses/hermes-configuration
harnesses/opencode-configuration
harnesses/deepseek-configuration
harnesses/pi-configuration
~~~

~~~{toctree}
:maxdepth: 1
:caption: Task 与运行

tasks/task-format
tasks/task-bundle-and-storage
tasks/user-simulator
~~~

~~~{toctree}
:maxdepth: 1
:caption: 轨迹与训练数据

traces/trajectory-outputs
traces/sft-format
traces/viewer
~~~

~~~{toctree}
:maxdepth: 1
:caption: Bench

benches/bench-format
benches/wildclawbench
benches/skillsbench
benches/pinchbench
benches/swebench-pro
~~~
