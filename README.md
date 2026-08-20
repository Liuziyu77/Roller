<div align="center">
  <img src="assets/roller-logo.png" alt="Roller logo" width="132">
  <h1>Roller</h1>
  <p><strong>面向多模型与多 Harness 的 Agent 轨迹采集框架</strong></p>
  <p>
    <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
    <img alt="OpenClaw Harness" src="https://img.shields.io/badge/Harness-OpenClaw-6F42C1">
    <img alt="ACTF v1.0" src="https://img.shields.io/badge/Trace-ACTF__v1.0-00A67E">
    <a href="LICENSE"><img alt="Apache 2.0 License" src="https://img.shields.io/badge/License-Apache--2.0-D22128"></a>
  </p>
</div>

Roller 把 task 清单与模型列表展开成独立 rollout，在隔离的 Docker 环境中并行运行 Agent Harness，并把每次执行整理成可复用的轨迹样本。每个样本保留 Harness 原始轨迹、`ACTF_v1.0`、运行信息，以及 Agent 明确写入输出目录的文件。

当前支持 **OpenClaw**。Task、文件管理、Conversation Manager 与结果格式不依赖具体 benchmark，后续可以继续接入其他 Agent Harness。

## ✨ 核心能力

| 能力 | 说明 |
| --- | --- |
| 批量 rollout | 一份 task 清单可以运行一个或多个模型，支持并行、重试和断点续跑 |
| 多种对话方式 | 支持单轮 prompt、预定义 `messages`，以及动态 User Simulator |
| 独立文件包 | 每个 task 可以携带自己的输入文件、目录和 Context |
| 系统文件注入 | 支持 `USER.md`、`MEMORY.md`、`SYSTEM.md`、`SOUL.md`、`AGENTS.md` 和 skills |
| 自动回收结果 | Agent 写入 `output_dir` 的全部安全文件会按原目录结构保存 |
| 双轨迹格式 | 同时保存 OpenClaw 原始 JSONL 和便于训练、分析的 `ACTF_v1.0` |
| 自定义模型服务 | 支持自定义 provider、API base、文本模型和多模态模型 |

Roller 适合构建训练轨迹数据集、比较不同模型的 Agent 行为、回归测试 Harness，以及批量收集带文件产物的长程任务。

## 🚀 快速开始

### 1. 安装

运行环境需要 Python 3.10+、Docker，以及一个包含 `openclaw` 和 `python3` 的 Docker 镜像。

```bash
python3 -m pip install -e .
roller --version
```

也可以通过环境变量设置默认镜像：

```bash
export ROLLER_OPENCLAW_IMAGE=your-openclaw-image:tag
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

采集 reasoning 模型时可增加 `--thinking high`。`--keep-thinking` 默认开启，会同时保留 ACTF 中的压缩 thinking 和后续轮次请求上文；通过 Responses API 返回 reasoning summary 的 GPT 模型应使用 `openai-responses`。

完整配置说明见 [OpenClaw、模型与 API](docs/openclaw-configuration.md)。

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
└── samples/<model>/<index>_<task>/
    ├── trace.jsonl
    ├── trace_ACTF.json
    ├── info.json
    └── results/                 # 可选
```

- `trace.jsonl`：Harness 原始轨迹，不清洗、不改写。
- `trace_ACTF.json`：转换后的 `ACTF_v1.0` 轨迹，顶层包含实际 system prompt 和完整 tools schema，并按 step 保存 reasoning。
- `info.json`：状态、错误、usage、版本、耗时和输入 digest。
- `results/`：可选，保存 task `output_dir` 中的文件。

默认 `--wire-trace actf` 只把 tools schema 嵌入 ACTF，不保留中间诊断文件；`--wire-trace full` 会额外保存脱敏后的 `cache_trace.jsonl` 和完整请求体 `wire_trace.jsonl`。

User Simulator rollout 还会增加 `conversation.json` 和 `user_simulator/`，分别保存干净的交替对话和 Simulator 自己的 raw/ACTF 轨迹。

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
| `--resume` | 跳过满足完整性与 digest 条件的成功 sample |
| `--dry-run` | 只校验并打印计划 |
| `--thinking LEVEL` | 设置 `off` / `minimal` / `low` / `medium` / `high` reasoning 等级 |
| `--keep-thinking` / `--no-keep-thinking` | 保留或双向剥离轨迹与请求历史中的 thinking |
| `--cache-trace` / `--no-cache-trace` | 是否记录实际组装的 system prompt |
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
| [OpenClaw、模型与 API](docs/openclaw-configuration.md) | Docker、provider、API base、代理、搜索和常见问题 |
| [User Simulator 与动态对话](docs/user-simulator.md) | Conversation Manager、终止协议、资源占用和双轨迹 |

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
- Conversation Manager：[src/roller/conversation.py](src/roller/conversation.py)
- ACTF 转换：[src/roller/actf.py](src/roller/actf.py)

运行测试：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 📄 License

[Apache License 2.0](LICENSE)
