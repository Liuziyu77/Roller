# Roller 文档

这里集中说明 Roller 的 Task、文件生命周期、OpenClaw/Codex 配置和动态对话机制。想先跑通项目，请从根目录 [README](../README.md#-快速开始) 或 [Examples](../examples/README.md) 开始。

## 阅读顺序

第一次使用建议按以下顺序阅读：

1. [Examples](../examples/README.md)：准备环境变量，执行四个基础 task。
2. [Task 格式参考](task-format.md)：编写自己的单轮、多轮或 User Simulator task。
3. [文件、Task Bundle 与结果存储](task-bundle-and-storage.md)：理解输入、Context、输出和最终 run。
4. [OpenClaw、模型与 API](openclaw-configuration.md)：连接自定义 API、选择 adapter、配置 Docker 与搜索。
5. 使用 Codex 时改读 [Codex Harness 指南](codex-configuration.md)。
6. [轨迹可视化 Viewer](viewer.md)：按 task 浏览 reasoning、工具调用、结果和运行信息。

需要动态生成 user message 时，再阅读 [User Simulator 与动态对话](user-simulator.md)。

## 文档索引

| 文档 | 主要内容 |
| --- | --- |
| [Task 格式参考](task-format.md) | JSON/JSONL、顶层字段、三种对话模式、文件声明、runtime、完整案例 |
| [文件、Task Bundle 与结果存储](task-bundle-and-storage.md) | 路径模型、输入快照、Context capture、results、ACTF、重试和 Resume |
| [轨迹产出文件指南](trajectory-outputs.md) | raw、ACTF、SFT、wire 的生成条件、区别和推荐用法 |
| [SFT 数据格式](sft-format.md) | 最小 `messages`/`tools` 训练格式、转换规则和完整性校验 |
| [轨迹可视化 Viewer](viewer.md) | 本地只读 Web UI、数据来源、懒加载、远程访问和安全边界 |
| [OpenClaw、模型与 API](openclaw-configuration.md) | Docker 镜像、provider JSON、API base、环境变量、代理、搜索和排错 |
| [Codex Harness 指南](codex-configuration.md) | Codex 镜像、API、effort、native session、ACTF/SFT 和当前限制 |
| [External Thinking](external-thinking.md) | 关闭原生 reasoning，以 `deep_think` 工具实现 external/external_always 显式 CoT |
| [User Simulator 与动态对话](user-simulator.md) | Conversation Manager、双 Harness、JSON 决策、终止条件和双方轨迹 |
| [Task JSON Schema](../schemas/task.schema.json) | `roller.task/v1` 的机器可读约束 |
| [Examples](../examples/README.md) | 仓库内可运行的配置与 task |

## 核心名词

| 名词 | 含义 |
| --- | --- |
| Task | 一条输入数据，声明对话方式、依赖文件、Context、输出目录和运行要求 |
| Model ref | Harness 使用的模型标识，例如 `provider/model` |
| Rollout | 一个 task 与一个任务 Agent 模型的实际执行 |
| Harness | 驱动 Agent 运行并返回原始轨迹与 canonical ACTF 的适配器；当前支持 OpenClaw 和 Codex |
| Task Bundle | 运行前构建的不可变输入与 Context 快照，只在运行期存在 |
| Sample | 一个 rollout 的最终保存目录 |
| User Simulator | 另一个 live Harness，读取当前对话并动态生成下一条 user message |

命令行传入多个 `--model` 时，Roller 会把每个 task 与这些模型组合，分别产生 rollout。`--parallel` 控制同时运行多少个 rollout，而不是一个 rollout 内的模型调用数。

## 当前范围

- Task schema：`roller.task/v1`
- 标准化轨迹：`ACTF_v1.0`
- 训练数据：每个 sample 的 `trace_SFT.json` 与 run 级 `sft.jsonl`
- 已实现 Harness：OpenClaw、Codex CLI
- 支持 Python：3.10+

项目仍处于 Alpha 阶段。扩展 Harness 时应保留 Task、Sample 与 ACTF 的稳定边界，Harness 私有运行细节放在 adapter 内部。
