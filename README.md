<div align="center">
  <img src="assets/roller-logo.png" alt="Roller logo" width="132">
  <h1>Roller</h1>
  <p><strong>面向多模型与多 Harness 的 Agent 轨迹采集框架</strong></p>
  <p>
    <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
    <img alt="OpenClaw and Codex Harnesses" src="https://img.shields.io/badge/Harness-OpenClaw%20%7C%20Codex-6F42C1">
    <img alt="ACTF v1.0" src="https://img.shields.io/badge/Trace-ACTF__v1.0-00A67E">
    <a href="LICENSE"><img alt="Apache 2.0 License" src="https://img.shields.io/badge/License-Apache--2.0-D22128"></a>
  </p>
</div>

Roller 把 task 清单与模型列表展开成独立 rollout，在隔离的 Docker 环境中并行运行 Agent Harness，并把每次执行整理成可复用的轨迹样本。每个样本保留 Harness raw、`ACTF_v1.0`、标准化 SFT、运行信息，以及 Agent 明确写入输出目录的文件。

当前支持 **OpenClaw** 和 **Codex CLI**。Task、文件管理与结果格式不依赖具体 benchmark；动态 User Simulator 当前使用 OpenClaw。

## ✨ 核心能力

| 能力 | 说明 |
| --- | --- |
| 批量 rollout | 一份 task 清单可以运行一个或多个模型，支持并行、重试和断点续跑 |
| 多种对话方式 | 支持单轮 prompt、预定义 `messages`，以及动态 User Simulator |
| 独立文件包 | 每个 task 可以携带自己的输入文件、目录和 Context |
| 系统文件注入 | 支持 `USER.md`、`MEMORY.md`、`SYSTEM.md`、`SOUL.md`、`AGENTS.md` 和 skills |
| 自动回收结果 | Agent 写入 `output_dir` 的全部安全文件会按原目录结构保存 |
| 三层轨迹格式 | 同时保存 Harness-native raw、便于分析的 `ACTF_v1.0` 和最小 SFT 数据 |
| 可靠断点续跑 | 运行锁阻止配置混用，周期 checkpoint 保存进度，复用前校验轨迹完整性 |
| 轨迹可视化 | 内置只读 Web Viewer，按 task 展示 reasoning、工具调用、结果和最终回复 |
| 自定义模型服务 | 支持自定义 provider、API base、文本模型和多模态模型 |

Roller 适合构建训练轨迹数据集、比较不同模型的 Agent 行为、回归测试 Harness，以及批量收集带文件产物的长程任务。

## 🚀 快速开始

### 1. 安装

运行环境需要 Python 3.10+ 和可用的 Docker daemon。OpenClaw 默认镜像固定为
`ghcr.io/openclaw/openclaw:2026.7.1-2`；它已经包含 OpenClaw、Node.js 和 Python 3，
无需修改镜像内源码或另做兼容镜像。

```bash
python3 -m pip install -e .
roller --version

# 二选一：在线拉取，或加载离线归档
docker pull --platform linux/amd64 ghcr.io/openclaw/openclaw:2026.7.1-2
docker load -i /path/to/openclaw-2026.7.1-2-linux-amd64.tar.gz
```

也可以通过环境变量设置默认镜像：

```bash
export ROLLER_OPENCLAW_IMAGE=your-openclaw-image:tag
```

Codex 使用仓库内 [`docker/codex/Dockerfile`](docker/codex/Dockerfile) 构建的固定版本镜像：

```bash
docker load -i /path/to/node-22-bookworm-slim-linux-amd64.tar.gz
docker build -f docker/codex/Dockerfile -t roller-codex:0.149.0 .
docker run --rm roller-codex:0.149.0 codex --version

export ROLLER_CODEX_IMAGE=roller-codex:0.149.0
```

### 2. 配置模型 API

复制示例环境文件并填入自己的 API 地址和 Key。`examples/env/*.env` 已被 Git 忽略：

```bash
cp examples/env/api.env.example examples/env/api.env

# 编辑 examples/env/api.env 后加载
set -a
source examples/env/api.env
set +a
```

模型配置通过 `${ENV_NAME}` 引用环境变量，不需要把真实 Key 写进 JSON：

```json
{
  "mode": "merge",
  "providers": {
    "my-openai-proxy": {
      "baseUrl": "${MY_PROXY_BASE_URL}",
      "apiKey": "${MY_PROXY_API_KEY}",
      "api": "openai-responses",
      "models": [{"id": "my-model", "name": "My model"}]
    }
  }
}
```

采集 reasoning 模型时可增加 `--thinking high`。Roller 接受逻辑档位
`off/minimal/low/medium/high/xhigh/adaptive/max/ultra`，也接受 GPT 风格的 `none` 并将它
规范化为 `off`；默认 OpenClaw `2026.7.1` 实际支持到 `max`，不接受 `ultra`。
`--keep-thinking` 默认开启：除写入 ACTF/SFT 外，Chat Completions 返回的
`reasoning`/`reasoning_text` 会规范化为 `reasoning_content`，使其真实进入后续请求历史；
通过 Responses API 返回压缩 reasoning summary 的 GPT 模型应使用 `openai-responses`。

如果希望关闭 GPT 的原生 reasoning，改用完整可见、可训练的工具参数作为显式 CoT，可以
启用 `deep_think` 协议：

```bash
roller collect ... --external-thinking external_always
```

`external` 只强制第一次 `deep_think`，之后允许模型按需调用；`external_always` 在每个
业务动作或最终回复前强制一个独立 THINK。OpenClaw 要求 `openai-responses` provider；
Codex 要求显式 `--api-base` 并使用 Responses HTTP。两者都会把原生 thinking 强制设为
`off`。ACTF 保留 canonical 工具协议，SFT 会把 thoughts 标准化进随后 assistant 的
`reasoning_content`。详见
[External Thinking 指南](docs/external-thinking.md)。

完整配置说明见 [OpenClaw、模型与 API](docs/openclaw-configuration.md)。

Codex CLI 直接使用 API base 和 Key 环境变量，不读取 OpenClaw model JSON：

```bash
export ROLLER_CODEX_KEY='...'

roller collect \
  --harness codex \
  --tasks examples/tasks/tasks.jsonl \
  --model gpt-5.6-sol \
  --api-base http://your-api.example/v1 \
  --api-key-env ROLLER_CODEX_KEY \
  --thinking high \
  --reasoning-summary detailed \
  --parallel 2 \
  --output runs \
  --run-name codex-demo
```

该 API 必须支持 Codex CLI 使用的 Responses 请求；只实现 `/chat/completions` 的服务不能
直接用于这个 Harness。自定义 `--api-base` 默认使用 HTTP Responses streaming；只有
服务明确支持 Responses WebSocket 时才传 `--codex-websockets`。Codex 同样支持
`--external-thinking external|external_always`；启用后保持默认 host network 且不要开启
WebSocket，Roller 会自动注入本地 MCP `deep_think`，无需修改镜像或 Codex config。镜像、
effort、reasoning 保存和原生 session 说明见
[Codex Harness 指南](docs/codex-configuration.md)。

最小 external-always 验收可以在上述命令中去掉 `--thinking/--reasoning-summary`，并增加：

```text
--external-thinking external_always --wire-trace full
```

运行后使用 `roller view <run-dir>` 查看 ACTF 的 THINK/ACT 时间线；训练投影请检查
`trace_SFT.json` 中的 `reasoning_content`。完整验收项和常见错误见
[External Thinking 指南](docs/external-thinking.md#7-如何查看和验收)。

### 3. 检查 task

`--dry-run` 会解析 task、校验文件并打印执行计划，不启动 Docker：

```bash
roller collect \
  --tasks examples/tasks/tasks.jsonl \
  --model my-openai-proxy/my-model \
  --model-config examples/json/openclaw-models.json \
  --parallel 4 \
  --dry-run
```

### 4. 并行采集

```bash
roller collect \
  --tasks examples/tasks/tasks.jsonl \
  --model my-openai-proxy/my-model \
  --model-config examples/json/openclaw-models.json \
  --docker-image your-openclaw-image:tag \
  --parallel 4 \
  --timeout 600 \
  --output runs \
  --run-name quickstart
```

结果保存在 `runs/quickstart/`。可直接运行的模型、环境与 task 示例见 [Examples](examples/README.md)。

### 5. 查看轨迹

采集完成后可以直接启动内置 Viewer，不需要额外安装前端或 Web 框架：

```bash
roller view runs/quickstart
```

打开终端打印的 `http://127.0.0.1:8765`。Viewer 按 task 聚合同一任务的多个模型
rollout；选中 rollout 后可逐步展开 user、reasoning、tool call、tool result 和最终
assistant content，并在 Inspector 中查看 system prompt、tools schema、运行信息和 User
Simulator conversation。

远程机器建议保持默认 loopback 并使用 SSH 端口转发。确实需要直接对外监听时使用
`--host 0.0.0.0`，但页面会暴露 prompt、reasoning 和工具结果等敏感内容。完整说明见
[轨迹可视化 Viewer](docs/viewer.md)。

## 📚 内置案例

基础示例位于 [`examples/tasks/tasks.jsonl`](examples/tasks/tasks.jsonl)：

| Task | 展示内容 |
| --- | --- |
| `01_text_only` | 纯文本单轮输入 |
| `02_local_markdown` | 读取本地 Markdown，并生成文件结果 |
| `03_user_memory_context` | 注入 `USER.md` 与 `MEMORY.md` |
| `04_multi_turn` | 两条预定义 message 共用同一 Agent session |

[`examples/tasks/user-simulator.jsonl`](examples/tasks/user-simulator.jsonl) 是独立的动态对话案例：User Simulator 扮演 HR，根据任务 Agent 当前给出的招聘广告逐轮补充要求，最后要求 Agent 把完整广告保存到 `outputs/recruitment-ad.md`。

```bash
roller collect \
  --tasks examples/tasks/user-simulator.jsonl \
  --model-config examples/json/openclaw-models.json \
  --docker-image your-openclaw-image:tag \
  --parallel 1 \
  --timeout 900 \
  --output runs \
  --run-name user_simulator_hr
```

动态对话会运行两个独立 Harness，详细调度与保存规则见 [User Simulator 与动态对话](docs/user-simulator.md)。

## 📝 Task 一览

Task 清单支持 `.jsonl` 和 `.json`。JSONL 每行是一条 task；JSON 可以直接使用 task 数组或 `{"tasks": [...]}`。

### 1. 单轮 Task

下面是一条带本地输入、Context 和文件输出的单轮 task：

```json
{
  "schema_version": "roller.task/v1",
  "id": "product_summary",
  "file_root": "files/product_summary",
  "model": "my-openai-proxy/my-model",
  "prompt": "阅读 inputs/product-brief.md，撰写摘要并保存到 outputs/summary.md。",
  "files": {
    "inputs": ["inputs/product-brief.md"],
    "context": [
      {"role": "user", "source": "context/USER.md", "capture": "hash"},
      {"role": "memory", "source": "context/MEMORY.md", "capture": "hash"}
    ],
    "output_dir": "outputs"
  }
}
```

对应文件包建议把输入和输出分开：

```text
files/product_summary/
├── inputs/
│   └── product-brief.md
├── context/
│   ├── USER.md
│   └── MEMORY.md
└── outputs/                   # 逻辑输出目录，可不预先创建
```

`outputs/` 不需要预先创建。Roller 会在 Agent 工作区中准备可写目录，并把其中的文件保存到 sample 的 `results/`。

### 2. 对话模式

| 字段 | 用途 |
| --- | --- |
| `prompt` / `question` | 单轮输入；`question` 是兼容字段 |
| `messages` | 预定义多轮输入，按数组顺序发送到同一个 session |
| `user_simulator` | 由另一个 Harness 根据完整上下文动态生成下一轮指令 |

每条 task 只能选择一种对话模式。完整字段、路径约束和多轮示例见 [Task 格式参考](docs/task-format.md)。

### 3. 文件约定

- `file_root`：该 task 的文件包根目录，相对于 task 清单所在目录。
- `files.inputs`：只读输入，进入 Agent 后保持相对于 `file_root` 的路径。
- `files.context`：由 Harness 按语义注入的系统文件或目录。
- `files.output_dir`：Agent 可写目录，默认是 `outputs`；其中全部安全文件会被回收。

不要混淆两个输出位置：task 的 `files.output_dir` 决定 Agent 在工作区写到哪里，CLI 的 `--output` 决定整个 run 在宿主机保存到哪里。

输入快照、Context capture、输出安全检查和路径映射见 [文件、Task Bundle 与结果存储](docs/task-bundle-and-storage.md)。机器可读约束见 [Task JSON Schema](schemas/task.schema.json)。

## 📦 结果格式

普通 rollout 只保留必要文件：

```text
<output>/<run-name>/
├── info.json
├── lock.json                   # 脱敏后的可复现 Resume 运行锁
├── sft.jsonl                    # 全部成功 task Agent 的训练记录
└── samples/<model>/<index>_<task>/
    ├── trace.jsonl
    ├── trace_ACTF.json
    ├── trace_SFT.json
    ├── info.json
    └── results/                 # 可选
```

- `trace.jsonl`：Harness-native session 事件。OpenClaw 通过 transcript SDK 导出；Codex
  保存 `$CODEX_HOME/sessions` 中的原生 rollout JSONL。Roller 不做训练清洗，但它不是模型
  请求体或上游响应的逐字副本。
- `trace_ACTF.json`：转换后的 `ACTF_v1.0` 轨迹，顶层固化捕获到的首个组装后 system prompt 和 tools schema，并按 step 保存 reasoning。
- `trace_SFT.json`：只包含 `messages` 和 `tools` 的标准化训练样本；assistant 固定包含 `reasoning_content`、`content` 和 `tool_calls`。
- `sft.jsonl`：按 request 顺序汇总所有成功 task Agent 的 `trace_SFT.json`，每行一个样本。
- Run `info.json`：当前运行阶段、进度、usage 汇总和 sample 索引；采集中会周期更新。
- `lock.json`：固定 task/input/context digest、模型配置、镜像 ID 和轨迹语义选项；不保存
  API Key，`--resume` 配置不一致时拒绝混用旧样本。
- Sample `info.json`：状态、错误、usage、版本、耗时和输入 digest。
- `results/`：可选，保存 task `output_dir` 中的文件。

各层轨迹的生成条件、保留策略、参数组合和失败/Resume 行为见
[轨迹产出文件指南](docs/trajectory-outputs.md)。

默认 `--wire-trace actf` 只把 tools schema 嵌入 ACTF，不保留完整消息历史；
`--wire-trace full` 会额外保存每次发往模型 API 的 JSON 请求体
`wire_trace.jsonl`；OpenClaw 还会保留 `cache_trace.jsonl`。Harness 可能在 session 与模型
调用之间进行 context 组装、压缩、裁剪或格式适配，因此需要核对“模型当时真正看到了
什么”时必须使用 `full`。捕获点、隐私边界和审计方法见
[轨迹产出文件指南](docs/trajectory-outputs.md#32-session-历史不等于模型请求-context)。

User Simulator rollout 还会增加 `conversation.json` 和 `user_simulator/`，分别保存干净的交替对话和 Simulator 自己的 raw/ACTF/SFT 轨迹。

运行期日志、Task Bundle、retry 中间文件和未声明的工作区临时文件不会进入最终 run。字段说明和 Resume 规则见 [文件、Task Bundle 与结果存储](docs/task-bundle-and-storage.md)。

## 🛠️ 常用参数

| 参数 | 说明 |
| --- | --- |
| `--model MODEL` | 指定任务 Agent 模型；可重复使用以运行多个模型 |
| `--parallel N` | 并行 rollout 数量 |
| `--retries N` | 失败后的重试次数 |
| `--timeout N` | 每条 rollout 的总超时秒数 |
| `--output DIR` | run 的宿主机根目录 |
| `--run-name NAME` | 本次 run 的目录名 |
| `--resume` | 在运行锁一致时，校验并跳过完整的成功 sample；损坏 sample 自动重跑 |
| `--dry-run` | 只校验并打印计划 |
| `--harness NAME` | `openclaw`（默认）或 `codex` |
| `--docker-image IMAGE` | Harness 镜像；默认分别使用 OpenClaw 官方镜像或 `roller-codex:0.149.0` |
| `--container-user USER` | 一次性 task 容器用户；默认 `0`，`image` 表示沿用镜像默认用户 |
| `--thinking LEVEL` | OpenClaw thinking 或 Codex `model_reasoning_effort`；Harness 会校验不适用的档位 |
| `--external-thinking MODE` | `off`（默认）、`external` 或 `external_always`；使用 `deep_think` 工具构造显式 CoT |
| `--keep-thinking` / `--no-keep-thinking` | 控制 ACTF/SFT 是否导出 thinking；OpenClaw 的关闭模式还会剥离后续请求历史，Codex raw/resume 不受影响 |
| `--cache-trace` / `--no-cache-trace` | 是否记录每次组装的 system prompt；ACTF 固化首个有效值 |
| `--wire-trace MODE` | `actf`（默认）、`full` 或 `off`；控制 tools schema 与请求体记录 |

查看全部参数：

```bash
roller collect --help
```

## 📖 文档

建议从 [Docs 首页](docs/README.md) 开始。

| 文档 | 内容 |
| --- | --- |
| [Examples](examples/README.md) | 直接运行基础 task、User Simulator 和多模态配置 |
| [Task 格式参考](docs/task-format.md) | 字段、对话模式、文件声明、runtime 与完整案例 |
| [文件、Task Bundle 与结果存储](docs/task-bundle-and-storage.md) | 输入快照、Context、results、ACTF、重试与 Resume |
| [轨迹产出文件指南](docs/trajectory-outputs.md) | raw、ACTF、SFT、wire、产出条件和使用区别 |
| [SFT 数据格式](docs/sft-format.md) | `messages`/`tools` schema、ACTF 映射和严格校验 |
| [轨迹可视化 Viewer](docs/viewer.md) | 启动方式、页面结构、懒加载、安全边界和缺失字段行为 |
| [OpenClaw、模型与 API](docs/openclaw-configuration.md) | Docker、provider、API base、代理、搜索和常见问题 |
| [Codex Harness 指南](docs/codex-configuration.md) | 镜像构建、API、reasoning、native raw、ACTF/SFT 和限制 |
| [User Simulator 与动态对话](docs/user-simulator.md) | Conversation Manager、终止协议、资源占用和双方轨迹 |

## 🔒 安全边界

- 输入和 Context 在启动容器前检查路径、大小、符号链接和 hash。
- API Key 只从运行环境或外部配置读取，不写入 run metadata。
- 只回收 `output_dir` 中的普通文件和目录；符号链接及特殊文件会被拒绝。
- Context capture 策略会影响内容留存方式和安全 Resume。

详细规则见 [文件、Task Bundle 与结果存储](docs/task-bundle-and-storage.md#8-安全与隐私边界)。

## 🧪 开发

主要扩展点：

- Harness 接口：[src/roller/harnesses/base.py](src/roller/harnesses/base.py)
- OpenClaw adapter：[src/roller/harnesses/openclaw.py](src/roller/harnesses/openclaw.py)
- Codex adapter：[src/roller/harnesses/codex.py](src/roller/harnesses/codex.py)
- Conversation Manager：[src/roller/conversation.py](src/roller/conversation.py)
- ACTF 转换：[src/roller/actf.py](src/roller/actf.py)、[src/roller/codex_actf.py](src/roller/codex_actf.py)

运行测试：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 📄 License

[Apache License 2.0](LICENSE)
