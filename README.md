<div align="center">
  <img src="assets/roller-logo.png" alt="Roller logo" width="132">
  <h1>Roller</h1>
  <p><strong>面向多模型、多 Harness 的 Agent 轨迹采集框架</strong></p>
  <p>
    <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
    <img alt="OpenClaw, Codex, Claude Code, Hermes, OpenCode, DeepSeek, and Pi Harnesses" src="https://img.shields.io/badge/Harness-OpenClaw%20%7C%20Codex%20%7C%20Claude_Code%20%7C%20Hermes%20%7C%20OpenCode%20%7C%20DeepSeek-6F42C1">
    <img alt="ACTF v1.0" src="https://img.shields.io/badge/Trace-ACTF__v1.0-00A67E">
    <a href="LICENSE"><img alt="Apache 2.0 License" src="https://img.shields.io/badge/License-Apache--2.0-D22128"></a>
  </p>
</div>

Roller 为每个 task 与模型的组合创建独立 rollout，在一次性 Docker 容器中运行 Agent。每次运行保存 Harness 原始轨迹、ACTF 分析轨迹、SFT 训练数据和任务生成的文件。

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

## 核心用途

- 批量采集单轮、多轮或工具型 Agent 轨迹；
- 同一批 task 对比多个模型或 Harness；
- 生成顶层为 `messages` 和 `tools`（含图片时追加 `images`）的 SFT JSONL；
- 保存 reasoning、工具调用、工具结果与任务文件产物；
- 用 run lock 检查运行配置，通过重试和 Resume 继续采集；
- 在内置 Viewer 中逐步查看每个 task 的 reasoning 和工具操作。

## 选择 Harness

| Harness | 模型服务协议 | 适合场景 | 关键能力与限制 |
| --- | --- | --- | --- |
| OpenClaw（默认） | OpenAI Responses 或 Chat Completions | 自定义/开源模型、动态 User Simulator、多模态输入 | 模型通过 JSON 配置；保留原生 reasoning |
| Codex | Responses | Codex CLI 工具轨迹、GPT 与兼容 Responses 服务 | 保留原生 reasoning；可作为 Task Agent 或 User Simulator |
| Claude Code | Anthropic `/v1/messages` | Claude Code 工具轨迹与 native thinking | 保留原生 thinking；可作为 Task Agent 或 User Simulator |
| Hermes | OpenAI Chat Completions | 原生请求与工具消息 → ACTF/SFT | 固定版本，容器内 terminal/file；可作为 Task Agent 或 User Simulator |
| OpenCode | OpenAI Chat Completions | CLI session / tools → ACTF/SFT | 固定版本，文本与文件工具；支持 Agent / Simulator |
| DeepSeek Harness | DeepSeek Chat Completions | 原生 SDK / 持久化 Bash → ACTF/SFT | 固定 sdk-minimal profile；支持 Agent / Simulator |
| Pi Agent | OpenAI Chat Completions | 原生 AgentSession / 七种工具 → ACTF/SFT | 固定 SDK 版本；支持 Agent / Simulator |

七种 Harness 使用相同的 Task、Sample、ACTF 和 SFT 格式，也共用 Viewer、重试和 Resume 流程。镜像、API 和 reasoning 参数不同，详见对应的 [Harness 指南](docs/README.md#按-harness-选择)。

## Reasoning 采集

默认 `--keep-thinking` 保存服务实际返回的可见 reasoning。`--thinking` 控制原生推理档位，不能保证服务返回完整的内部推理文本。

| 模型类型 | ACTF/SFT 保存内容 |
| --- | --- |
| GPT reasoning 系列 | 服务返回的可见 summary/content，不扩写压缩 summary |
| Claude 系列 | 服务实际返回的可见 thinking/summary |
| Qwen、GLM 等开源模型 | 服务实际返回的明文 reasoning |

只返回 reasoning token 数或加密状态时，`reasoning_content` 可以为空。工具参数不会转换成 reasoning。


## 最小运行流程

以下流程使用 OpenClaw 完成一次基础采集。镜像构建、Codex/Claude Code 配置和结果验收见 [快速上手](docs/zh/start/getting-started.md)。

### 1. 安装 Roller 与镜像

需要 Python 3.10+、Docker CLI 和可用的 Docker daemon。下面使用 Conda 创建独立环境。

```bash
conda create -n roller python=3.11 -y
conda activate roller

python --version
python -m pip install -e .
roller --version
docker version

docker pull --platform linux/amd64 ghcr.io/openclaw/openclaw:2026.7.1-2
```

已有 `docker save` 归档时可离线加载：

```bash
docker load -i /path/to/openclaw-2026.7.1-2-linux-amd64.tar.gz
```

### 2. 配置 API

```bash
cp examples/env/api.env.example examples/env/api.env
```

编辑 `examples/env/api.env` 中的 `MY_PROXY_BASE_URL` 和 `MY_PROXY_API_KEY`，然后加载：

```bash
set -a
source examples/env/api.env
set +a
```

再把 [`examples/config/openclaw/models.json`](examples/config/openclaw/models.json) 中的 model `id` 改成服务端真实模型名。运行时 model ref 由 provider 名与 model id 组成：

```text
my-openai-proxy/my-model
```

只支持 `/chat/completions` 的服务应把配置中的 `api` 从 `openai-responses` 改为 `openai-completions`。API Key 使用环境变量占位符，不要写入 task、命令或仓库文件。

### 3. 先检查，再采集

```bash
roller collect \
  --tasks examples/tasks/tasks.jsonl \
  --model my-openai-proxy/my-model \
  --model-config examples/config/openclaw/models.json \
  --parallel 4 \
  --dry-run
```

Dry-run 只解析 task、检查输入文件并打印执行计划，不启动 Docker。确认无误后移除 `--dry-run`：

```bash
roller collect \
  --tasks examples/tasks/tasks.jsonl \
  --model my-openai-proxy/my-model \
  --model-config examples/config/openclaw/models.json \
  --parallel 4 \
  --timeout 600 \
  --output runs \
  --run-name quickstart
```

### 4. 查看轨迹

```bash
roller view runs/quickstart
```

打开终端打印的 `http://127.0.0.1:8765`，按 task 查看 user、reasoning、工具调用、工具结果和最终回复。

## 输入与输出

最小 task 是一行 JSON：

```json
{"schema_version":"roller.task/v1","id":"hello","prompt":"计算 2+3，只回复数字。"}
```

Task 清单支持 `.jsonl`、JSON 数组或 `{"tasks": [...]}`。每条 task 还可以设置：

- `messages`：预定义多轮 user message；
- `file_root` 与 `files.inputs`：需要复制进工作区的本地输入；
- `files.context`：`AGENTS.md`、`USER.md`、`MEMORY.md`、skills 等 Context；
- `files.output_dir`：Agent 可写且会被完整回收的目录；
- `runtime`：环境变量、setup 命令和 task 级 thinking；
- `harness`（或 `agent_harness` / `task_agent.harness`）：Task Agent 使用的 Harness；也可由 `--harness` 统一覆盖；
- `user_simulator`：由独立 Harness 动态生成后续 user message，可单独设置 `harness` 和 `model`。

完整字段见 [Task 格式参考](docs/zh/tasks/task-format.md)。

每个成功 sample 至少包含：

| 文件 | 用途 |
| --- | --- |
| `trace.jsonl` | 保留 Harness-native session，便于追溯与重新转换 |
| `trace_ACTF.json` | 使用 `ACTF_v1.0` 统一 reasoning、工具调用和工具结果 |
| `trace_SFT.json` | 单条最小训练样本，顶层为 `messages` 与 `tools`，含图片时追加 `images` |
| `info.json` | 状态、耗时、usage、版本和输入来源 |
| `results/` | task `output_dir` 中生成了文件时存在 |

Run 根目录的 `sft.jsonl` 按 request 顺序汇总全部成功样本。需要审计 Harness 是否压缩、裁剪或改写了模型请求 context 时，使用 `--wire-trace full`；native session 不等同于模型实际收到的请求。详见 [轨迹产出文件指南](docs/zh/traces/trajectory-outputs.md)。

## 文档导航

阅读顺序：

1. [快速上手](docs/zh/start/getting-started.md)：从安装到采集、验收和查看轨迹。
2. [Examples](examples/README.md)：OpenClaw、Codex、Claude Code、Hermes、OpenCode 和进阶案例命令。
3. [Task 格式参考](docs/zh/tasks/task-format.md)：开始编写自己的 task。
4. [轨迹产出文件指南](docs/zh/traces/trajectory-outputs.md)：理解 raw、ACTF、SFT、wire 和产出条件。

按需查阅：

| 目标 | 文档 |
| --- | --- |
| 选择并配置 Harness | [OpenClaw](docs/zh/harnesses/openclaw-configuration.md) · [Codex](docs/zh/harnesses/codex-configuration.md) · [Claude Code](docs/zh/harnesses/claude-code-configuration.md) · [Hermes](docs/zh/harnesses/hermes-configuration.md) · [OpenCode](docs/zh/harnesses/opencode-configuration.md) · [DeepSeek](docs/zh/harnesses/deepseek-configuration.md) · [Pi Agent](docs/zh/harnesses/pi-configuration.md) |
| 查命令行参数与适用范围 | [CLI 参数参考](docs/zh/start/cli-reference.md) |
| 管理输入、Context、输出和 Resume | [Task Bundle 与结果存储](docs/zh/tasks/task-bundle-and-storage.md) |
| 跑评测 bench 或接入新 bench | [Bench 接入与评分](docs/zh/benches/bench-format.md) |
| 制作训练数据 | [SFT 数据格式](docs/zh/traces/sft-format.md) |
| 动态生成 user message | [User Simulator](docs/zh/tasks/user-simulator.md) |
| 使用可视化页面 | [Viewer](docs/zh/traces/viewer.md) |
| 浏览全部资料 | [Docs 首页](docs/README.md) |
| 本地构建文档站 | `cd docs && make html`，见 [Docs 首页](docs/README.md#在线文档站) |

## 安全边界

- 输入和 Context 在启动容器前校验路径、大小、符号链接和内容摘要；
- API Key 只从宿主机环境或外部配置读取，不写入 run metadata；
- 只回收 task `output_dir` 中的普通文件和目录；
- `wire_trace.jsonl` 会保存完整请求文本，可能包含敏感 prompt、reasoning 和工具结果，应按敏感数据管理；
- Viewer 默认只监听 `127.0.0.1`，远程访问建议使用 SSH 端口转发。

完整规则见 [Task Bundle 与结果存储](docs/zh/tasks/task-bundle-and-storage.md#8-安全与隐私边界)。

## 开发

主要扩展点位于 `src/roller/harnesses/`、`src/roller/*_actf.py`、`src/roller/sft.py` 和 `src/roller/viewer/`。

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

项目当前处于 Alpha 阶段，Task schema 为 `roller.task/v1`，统一轨迹格式为 `ACTF_v1.0`。许可证为 [Apache License 2.0](LICENSE)。
