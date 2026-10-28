# Roller 简介

Roller 是一个命令行工具，用来在隔离环境中运行能够调用工具的 AI Agent，并完整保存运行过程。你提供 Task、模型访问方式和 Harness。Roller 会为每个 Task 与模型的组合启动一次性的 Docker 容器，运行结束后把轨迹和指定文件带回宿主机。

这样，同一批 Agent 任务就可以反复运行、比较和检查。你可以用 Roller 对比多个模型，查看工具调用，制作训练数据，或者复现一次失败的运行。

## 1. Roller 解决什么问题

一次 Agent 运行不只有最终回答，它通常还取决于：

- prompt、输入文件和工作区说明。
- 模型及其 API 协议。
- Agent 运行时提供的工具。
- 模型返回的 reasoning。
- 工具调用、工具结果以及任务过程中写出的文件。
- timeout、retry 和版本配置。

只保存最终回答，会丢掉大部分上下文。Roller 会把 Task 定义、运行配置、原生事件、统一轨迹和文件产物放在同一个 run 目录中。

## 2. 一次运行如何完成

1. **描述任务。** 用 JSON 或 JSONL 编写一个或多个 Task。Task 可以包含 prompt、输入文件、Context、setup 命令和输出目录。
2. **选择运行方式。** 选择 Harness 和模型。Harness 负责启动 Agent 运行时，并连接工具和模型 API。
3. **检查执行计划。** Dry-run 会校验 Task，并显示 Roller 将创建哪些 rollout。
4. **隔离运行。** 每个 Task 与模型的组合都在独立 Docker 容器中执行。
5. **检查或复用结果。** 在 Viewer 中查看 run、比较轨迹，或者汇总生成的 SFT 数据。

~~~text
Task + 模型 + Harness
          │
          ▼
      隔离的 rollout
          │
          ├─ 原生轨迹
          ├─ 统一 ACTF 轨迹
          ├─ SFT 样本
          └─ 生成文件与运行信息
~~~

## 3. Handbook 中的常见词

| 术语 | 含义 |
| --- | --- |
| **Task** | 交给 Agent 的工作，以及相关的输入、Context、setup 和预期输出位置。 |
| **Harness** | 启动 Agent、提供工具并连接模型服务的运行时集成。 |
| **Rollout** | 使用一个模型执行一个 Task 的一次尝试。 |
| **Run** | 一批有名称的 rollout。 |
| **Trace** | 一次 rollout 中记录的对话、reasoning、工具调用和工具结果。 |
| **Viewer** | Roller 内置的只读网页界面，用于查看一个 run 中的 Task、轨迹时间线、工具调用和运行信息。 |

第一次运行前不需要记住所有文件格式。[快速上手](getting-started.md)会使用 OpenClaw 生成一条轨迹，并在 Viewer 中打开它。

## 4. Roller 会保存什么

一次成功的 rollout 通常包含：

| 产物 | 用途 |
| --- | --- |
| <code>trace.jsonl</code> | 保存 Harness 原生事件，便于排错和以后重新转换。 |
| <code>trace_ACTF.json</code> | 把不同 Harness 的轨迹整理为统一的分析与可视化格式。 |
| <code>trace_SFT.json</code> | 可转换时保存一条线性 SFT 记录。 |
| <code>sft_segments/</code> | 保存压缩后的上下文分段，路径列在 sample 的 <code>info.json</code> 中。 |
| <code>results/</code> | 保存 Task 要求 Agent 生成的文件。 |
| <code>info.json</code> | 记录状态、耗时、usage、版本和运行来源。 |

run 目录只把符合条件的线性样本汇总到 <code>sft.jsonl</code>；压缩分段保存在各 sample 目录。需要了解准确的保留规则和失败行为时，请阅读[轨迹产出文件](../traces/trajectory-outputs.md)。

## 5. 如何选择 Harness

Harness 决定使用哪一种 Agent 运行时和工具来完成任务。Roller 目前支持 OpenClaw、Codex、Claude Code、Hermes、OpenCode、DeepSeek Harness 和 Pi Agent。

如果你正在第一次了解 Roller，并且没有指定必须使用的 Agent 运行时，可以先选 **OpenClaw**。快速上手使用它，是因为它同时支持 OpenAI Responses 和 Chat Completions 风格的服务。以后需要其他运行时时，再查看对应的 Harness 指南。

## 6. 接下来读什么

- 第一次运行 Roller：[快速上手](getting-started.md)
- 编写自己的输入和 prompt：[Task 格式](../tasks/task-format.md)
- 选择或配置 Agent 运行时：[Harness 配置](../harnesses/openclaw-configuration.md)
- 理解每一种产出文件：[轨迹产出文件](../traces/trajectory-outputs.md)
