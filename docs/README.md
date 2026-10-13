# Roller 文档中心

[项目首页](../README.md) · [快速上手](zh/start/getting-started.md) · [示例](zh/start/examples.md) · [CLI 参数参考](zh/start/cli-reference.md)

文档按主题组织，可按下面的目录查阅。

| 目录 | 内容 |
| --- | --- |
| `en/` | 英文手册（网站默认语言） |
| `zh/` | 中文手册 |
| `en/start/` · `zh/start/` | 快速上手、Examples、CLI 参数 |
| `en/harnesses/` · `zh/harnesses/` | OpenClaw、Codex、Claude Code、Hermes、OpenCode、DeepSeek、Pi Agent |
| `en/tasks/` · `zh/tasks/` | Task 格式、Bundle 与存储、User Simulator |
| `en/traces/` · `zh/traces/` | 轨迹产物、SFT、Viewer |
| `en/benches/` · `zh/benches/` | Bench 接入与各 bench 说明 |

## 阅读顺序

| 阶段 | 阅读内容 | 完成标准 |
| --- | --- | --- |
| 1. 跑通 | [快速上手](zh/start/getting-started.md) | 完成 dry-run、采集并打开 Viewer |
| 2. 选 Harness | [OpenClaw](zh/harnesses/openclaw-configuration.md)、[Codex](zh/harnesses/codex-configuration.md) 或 [Claude Code](zh/harnesses/claude-code-configuration.md)、[Hermes](zh/harnesses/hermes-configuration.md) · [OpenCode](zh/harnesses/opencode-configuration.md) · [DeepSeek](zh/harnesses/deepseek-configuration.md) · [Pi Agent](zh/harnesses/pi-configuration.md) | 镜像、API 协议和 reasoning 参数匹配 |
| 3. 写 Task | [Task 格式参考](zh/tasks/task-format.md) | 能声明 prompt/messages、输入、Context 和输出目录 |
| 4. 验收产物 | [轨迹产出文件指南](zh/traces/trajectory-outputs.md) | 能区分 raw、ACTF、SFT、wire 与 results |
| 5. 批量运行 | [Task Bundle 与结果存储](zh/tasks/task-bundle-and-storage.md) | 理解快照、run lock、retry 与 Resume |

仓库内可运行命令集中在 [Examples](zh/start/examples.md)。

## 在线文档站

本目录中的 Markdown 同时作为 [Sphinx](https://www.sphinx-doc.org/) 站点源文件。本地预览：

```bash
python -m pip install -r docs/requirements-docs.txt
cd docs
make html
python -m http.server -d _build/html 8000
```

浏览器打开 `http://127.0.0.1:8000` 为英文默认站，`http://127.0.0.1:8000/zh/` 为中文。将仓库接入 [Read the Docs](https://readthedocs.org/) 后，会按根目录 `.readthedocs.yaml` 自动构建并发布。

## 按目标查文档

### 配置与运行

| 目标 | 文档 |
| --- | --- |
| 从空环境完成第一次采集 | [快速上手](zh/start/getting-started.md) |
| 配置 Docker 构建的 apt、npm、Python 镜像源与代理 | [Docker 构建与镜像源](zh/start/docker-builds.md) |
| 查某个 CLI 参数、默认值或适用 Harness | [CLI 参数参考](zh/start/cli-reference.md) |
| 连接 Responses、Chat Completions 或开源模型服务 | [OpenClaw Harness 指南](zh/harnesses/openclaw-configuration.md) |
| 使用 Codex CLI 采集轨迹 | [Codex Harness 指南](zh/harnesses/codex-configuration.md) |
| 使用 Anthropic Messages API 与 Claude Code | [Claude Code Harness 指南](zh/harnesses/claude-code-configuration.md) |
| 批量并行、retry 或断点续跑 | [Task Bundle 与结果存储](zh/tasks/task-bundle-and-storage.md#6-run-与-sample) |

### Task 与文件

| 目标 | 文档 |
| --- | --- |
| 编写单轮或预定义多轮 task | [Task 格式参考](zh/tasks/task-format.md) |
| 携带本地文件、Context、skills 或 setup | [Task 格式参考](zh/tasks/task-format.md#4-fileroot) |
| 理解输入快照、工作区和结果回收 | [Task Bundle 与结果存储](zh/tasks/task-bundle-and-storage.md) |
| 让另一个模型动态扮演用户 | [User Simulator](zh/tasks/user-simulator.md) |

### Bench 与评分

| 目标 | 文档 |
| --- | --- |
| 运行 benchmark 并评分 | [Bench 接入与评分](zh/benches/bench-format.md#7-cli) |
| 运行 WildClawBench（上游参数对应、search shim、已知问题） | [WildClawBench](zh/benches/wildclawbench.md) |
| 跑 SkillsBench 的 with-skill / no-skill 对照 | [SkillsBench](zh/benches/skillsbench.md) |
| 跑 PinchBench（自动、混合与 LLM judge） | [PinchBench](zh/benches/pinchbench.md) |
| 跑 QwenClawBench（本地 Qwen、judge API、每题重复运行） | [QwenClawBench](zh/benches/qwenclawbench.md) |
| 接入一个新 bench | [Bench 接入与评分](zh/benches/bench-format.md#6-加一个新-bench) |
| 看懂 `score.json` 与聚合口径 | [Bench 接入与评分](zh/benches/bench-format.md#5-聚合口径) |
| 确保答案不泄漏给 Agent | [Bench 接入与评分](zh/benches/bench-format.md#4-stage答案不能放在-agent-的工作目录里) |

### 轨迹与训练数据

| 目标 | 文档 |
| --- | --- |
| 知道每个产物何时生成、应该看哪个 | [轨迹产出文件指南](zh/traces/trajectory-outputs.md) |
| 直接训练 `messages` + `tools` 数据 | [SFT 数据格式](zh/traces/sft-format.md) |
| 核对模型实际收到的请求 context | [Wire trace 与 context 审计](zh/traces/trajectory-outputs.md#32-session-历史不等于模型请求-context) |
| 在网页中检查 reasoning 和工具步骤 | [Viewer](zh/traces/viewer.md) |

## 按 Harness 选择

| 能力 | OpenClaw | Codex | Claude Code | Hermes | OpenCode | DeepSeek Harness | Pi Agent |
| --- | --- | --- | --- | --- | --- | --- | --- |
| API 协议 | Responses / Chat Completions | Responses | Anthropic Messages | Chat Completions | Chat Completions | DeepSeek Chat Completions | Chat Completions |
| 模型配置入口 | `--model-config` | `--api-base` + `--api-key-env` | `--api-base` + `--api-key-env` | `--hermes-api-base` + `--hermes-api-key-env` | `--opencode-api-base` + `--opencode-api-key-env` | `--deepseek-api-base` + `--deepseek-api-key-env` | `--pi-api-base` + `--pi-api-key-env` |
| 单轮 / 预定义多轮 | 是 / 是 | 是 / 是 | 是 / 是 | 是 / 是 | 是 / 是 | 是 / 是 | 是 / 是 |
| 动态 User Simulator | 是 | 是 | 是 | 是 | 是 | 是 | 是 |
| Wire request capture | 是 | 是 | 是 | 是 | 是 | 是 | 是 |
| Harness 专属指南 | [打开](zh/harnesses/openclaw-configuration.md) | [打开](zh/harnesses/codex-configuration.md) | [打开](zh/harnesses/claude-code-configuration.md) | [打开](zh/harnesses/hermes-configuration.md) | [打开](zh/harnesses/opencode-configuration.md) | [打开](zh/harnesses/deepseek-configuration.md) | [打开](zh/harnesses/pi-configuration.md) |

## Reasoning 采集

默认 `--keep-thinking` 保存服务实际返回的可见 reasoning。`--thinking` 控制原生推理档位，不能保证服务返回完整的内部推理文本。
OpenClaw 各模型的可用档位、配置方式和限制见 [Thinking 档位表](zh/harnesses/openclaw-thinking.md)。

| 模型类型 | ACTF/SFT 保存内容 |
| --- | --- |
| GPT reasoning 系列 | 服务返回的可见 summary/content，不扩写压缩 summary |
| Claude 系列 | 服务实际返回的可见 thinking/summary |
| Qwen、GLM 等开源模型 | 服务实际返回的明文 reasoning |

只返回 reasoning token 数或加密状态时，`reasoning_content` 可以为空。工具参数不会转换成 reasoning。

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

- 仓库根 `README.md`：项目介绍与入门步骤；
- `zh/start/getting-started.md` / `en/start/getting-started.md`：首次运行的完整流程；
- `zh/harnesses/` / `en/harnesses/`：各 Harness 的镜像、API、reasoning、native raw 与限制；
- `zh/tasks/task-format.md` / `en/tasks/task-format.md`：输入字段与校验规则；
- `zh/traces/trajectory-outputs.md` / `en/traces/trajectory-outputs.md`：产物生成条件与记录内容；
- `zh/traces/sft-format.md` / `en/traces/sft-format.md`：SFT 字段定义；
- `zh/start/cli-reference.md` / `en/start/cli-reference.md`：参数适用范围和默认值。

机器可读 Task 约束见 [`schemas/task.schema.json`](../schemas/task.schema.json)。已安装版本的 CLI 参数可通过以下命令查看：

```bash
roller collect --help
roller view --help
```

Roller 需要 Python 3.10+，使用 Task schema `roller.task/v1` 和轨迹格式 `ACTF_v1.0`。项目处于 Alpha 阶段。
