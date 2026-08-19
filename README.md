<div align="center">
  <img src="assets/roller-logo.png" alt="Roller logo" width="132">
  <h1>Roller</h1>
  <p><strong>批量采集、整理和保存 Agentic 轨迹</strong></p>
  <p>
    <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
    <img alt="OpenClaw Harness" src="https://img.shields.io/badge/Harness-OpenClaw-6F42C1">
    <img alt="ACTF v1.0" src="https://img.shields.io/badge/Trace-ACTF__v1.0-00A67E">
    <a href="LICENSE"><img alt="Apache 2.0 License" src="https://img.shields.io/badge/License-Apache--2.0-D22128"></a>
  </p>
</div>

Roller 把 task 清单和模型列表组合成执行任务，并行运行 Agent Harness。每次 rollout 都会保存原始轨迹、ACTF 轨迹和必要的运行信息；如果 task 要求保留文件，结果会单独放进 `results/`。

目前支持 **OpenClaw**。任务格式、文件管理和结果目录不依赖 OpenClaw，后续可以继续接入其他 Harness。

## ✨ 能做什么

| | 能力 | 说明 |
| --- | --- | --- |
| 🧮 | 批量执行 | 一份 task 清单可以跑多个模型，支持并行和失败重试 |
| 📁 | 本地文件 | 每个 task 可以带自己的文件或目录，Agent 通过统一路径读取 |
| 🧠 | Context 注入 | 支持 `USER.md`、`MEMORY.md`、`SYSTEM.md`、`SOUL.md` 和 skills |
| 💬 | 多轮输入 | 使用 `messages` 明确每一轮，不从 prompt 文本中猜测分隔位置 |
| 📤 | 输出管理 | 每个 task 可以指定输出目录，只保存声明过的文件 |
| 🧾 | 两种轨迹 | 同时保留 OpenClaw 原始 JSONL 和 `ACTF_v1.0` |
| 🔌 | 自定义 API | 支持自定义 provider、API base、文本模型和多模态模型 |
| 🔁 | 断点续跑 | 对 task 与文件计算 digest，未发生变化的成功任务可以跳过 |

Roller 适合做模型对比、轨迹数据集采集、Harness 回归测试，以及需要保留 Agent 文件输出的批量任务。

## 🚀 安装

运行环境需要：

- Python 3.10+
- Docker
- 一个包含 `openclaw` 和 `python3` 的 Docker 镜像

在项目目录安装：

```bash
python3 -m pip install -e .
roller --version
```

也可以设置默认镜像：

```bash
export ROLLER_OPENCLAW_IMAGE=your-openclaw-image:tag
```

## 📚 示例目录

模型配置、环境变量和 task 数据分别存放：

```text
examples/
├── json/
│   ├── openclaw-models.json
│   └── openclaw-models-multimodal.json
├── env/
│   └── api.env.example
└── tasks/
    ├── tasks.jsonl
    └── files/
```

四个内置 task 分别演示：

1. 📝 纯文本输入
2. 📄 本地 Markdown 输入与文件输出
3. 👤 `USER.md`、`MEMORY.md` 注入
4. 💬 两轮连续对话

完整说明见 [examples/README.md](examples/README.md)。

## ⚡ 快速开始

### 1. 检查 task

`--dry-run` 会解析 task、检查本地文件并打印执行计划，不会启动 Docker：

```bash
roller collect \
  --tasks examples/tasks/tasks.jsonl \
  --model my-provider/my-model \
  --parallel 4 \
  --dry-run
```

### 2. 准备自定义 API

复制环境变量模板并填写真实值。本地 `examples/env/*.env` 已被 `.gitignore` 排除：

```bash
cp examples/env/api.env.example examples/env/api.env

# 编辑 examples/env/api.env 后加载
set -a
source examples/env/api.env
set +a
```

### 3. 并行采集

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

结果保存在 `runs/quickstart/`。

## 📝 Task 格式

task 清单可以是 `.json` 或 `.jsonl`。最小 task 只需要 `id` 和 `prompt`：

```json
{"schema_version":"roller.task/v1","id":"hello","prompt":"计算 2+3，只回复数字。"}
```

### 常用顶层字段

| 字段 | 是否必需 | 说明 |
| --- | --- | --- |
| `schema_version` | 推荐 | 当前为 `roller.task/v1` |
| `id` | 是 | task 的唯一标识 |
| `prompt` | 二选一 | 单轮 user 输入 |
| `messages` | 二选一 | 多轮 user 输入列表 |
| `file_root` | 否 | 本地 `source` 相对路径的基准目录 |
| `model` | 否 | task 自己指定模型；命令行 `--model` 优先 |
| `files` | 否 | 输入、context、输出目录和输出契约 |
| `runtime` | 否 | 环境变量、setup 命令和 thinking 配置 |

### 💬 多轮输入

多轮任务直接提供多个 user message：

```json
{
  "schema_version": "roller.task/v1",
  "id": "remember_word",
  "messages": [
    {"role": "user", "content": "请记住校验词 ROLLER。"},
    {"role": "user", "content": "刚才的校验词是什么？"}
  ]
}
```

所有 message 会进入同一个 OpenClaw session。Markdown 标题、`---` 或“第 1 轮”之类的文本不会被自动拆分。

### 📁 输入文件

```json
"inputs": [
  "brief.md",
  {"source": "./documents", "target": "documents"},
  {
    "source": "dataset.csv",
    "target": "data/dataset.csv",
    "required": true,
    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }
]
```

- `source` 是宿主机文件或目录。
- `target` 是 Agent 看到的相对路径。
- Agent 可以通过 `inputs/<target>` 或 `/roller/task/inputs/<target>` 读取。
- 输入在容器中只读；目录会递归复制并检查符号链接。

### 🧠 USER.md、MEMORY.md 与系统文件

```json
"context": [
  {"role": "system", "source": "./SYSTEM.md", "capture": "full"},
  {"role": "user", "source": "./USER.md", "capture": "hash"},
  {"role": "memory", "source": "./MEMORY.md", "capture": "hash"},
  {"role": "skill", "source": "./skills/research"}
]
```

| `role` | 默认 target |
| --- | --- |
| `system` | `SYSTEM.md` |
| `user` | `USER.md` |
| `memory` | `MEMORY.md` |
| `soul` | `SOUL.md` |
| `agents` | `AGENTS.md` |
| `skill` / `skills` | `skills/<source-name>` |

OpenClaw adapter 会把 context 放到 `/root/<target>` 和 `/tmp_workspace/<target>`。`capture` 可选 `full`、`hash` 或 `none`；详细差别见 [Task 文件与存储设计](docs/task-bundle-and-storage.md)。

### 📤 输出目录与输出契约

```json
"files": {
  "output_dir": "deliverables",
  "outputs": [
    {"path": "report.md", "type": "file", "required": true},
    {"path": "assets", "type": "directory", "required": false},
    {"path": "references/**", "type": "glob", "required": false}
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `files.output_dir` | Agent 工作区中的可写目录，默认 `outputs` |
| `path` | 相对于 `output_dir` 的路径 |
| `type` | `file`、`directory` 或 `glob` |
| `required` | 缺失时是否将 rollout 标记为失败 |
| `max_bytes` | 每个匹配文件允许的最大字节数 |

Agent 写入 `/tmp_workspace/<output_dir>/`；`/roller/task/outputs` 是同一目录的稳定别名。只有匹配 `files.outputs` 的内容会保存到 sample 的 `results/`。

### ⚙️ 运行时设置

```json
"runtime": {
  "env": ["EXTRA_API_KEY"],
  "setup": ["python3 -m pip install -r inputs/requirements.txt"],
  "thinking": "high"
}
```

- `env`：该 task 强制依赖的宿主机环境变量。
- `setup`：Agent 启动前在 `/tmp_workspace` 执行的命令。
- `thinking`：覆盖本次 task 的 OpenClaw thinking 设置。

完整约束见 [schemas/task.schema.json](schemas/task.schema.json)。

## 🔌 模型、API 与搜索

### 普通模型

[examples/json/openclaw-models.json](examples/json/openclaw-models.json) 保留最小的文本模型配置：

```bash
--model my-openai-proxy/my-model \
--model-config examples/json/openclaw-models.json
```

### 多模态模型

[examples/json/openclaw-models-multimodal.json](examples/json/openclaw-models-multimodal.json) 使用：

```json
"input": ["text", "image"]
```

对应命令：

```bash
--model my-openai-proxy/my-multimodal-model \
--model-config examples/json/openclaw-models-multimodal.json
```

这表示模型可以接收图像用于理解和分析，不等同于文生图。后端本身也必须支持所选 API adapter 的图像输入。

### Brave Search

在环境中设置 `BRAVE_API_KEY`，并增加：

```bash
--search-provider brave
```

Roller 默认会把 `BRAVE_API_KEY` 传给 OpenClaw。没有使用网页搜索的 task 不会消耗该服务。

### API adapter

示例默认使用 `"api":"openai-responses"`。如果自定义服务只实现 `/v1/chat/completions`，请改为 `"openai-completions"`。

> [!IMPORTANT]
> 不要把真实 API key 写入 JSON、task 或 README。`${ENV_NAME}` 在运行时展开，Roller 的 run metadata 只记录环境变量名称和脱敏后的 API base URL。

## 🛠️ 常用命令参数

| 参数 | 说明 |
| --- | --- |
| `--model MODEL` | 指定模型；重复使用可运行多个模型 |
| `--parallel N` | 同时运行的 trajectory 数量 |
| `--retries N` | 失败后的重试次数 |
| `--timeout N` | 每条 trajectory 的总超时秒数 |
| `--start-index N` | 从规范化 task 清单的第 N 条开始 |
| `--limit N` | 最多运行多少条 task |
| `--output DIR` | run 的宿主机根目录 |
| `--run-name NAME` | 本次 run 的目录名 |
| `--resume` | 跳过可以安全复用的成功 sample |
| `--dry-run` | 只检查和打印计划，不启动 Docker |
| `--pass-env NAME` | 向 OpenClaw 额外传递环境变量 |
| `--thinking LEVEL` | 设置默认 thinking 模式 |

完整参数：

```bash
roller collect --help
```

同时跑两个模型：

```bash
roller collect \
  --tasks tasks.jsonl \
  --model provider/model-a \
  --model provider/model-b \
  --parallel 4
```

## 📦 输出格式

```text
<output>/<run-name>/
├── info.json                         # run 配置、状态统计和 usage 汇总
└── samples/<model>/<index>_<task>/
    ├── trace.jsonl                   # OpenClaw 原始 session JSONL
    ├── trace_ACTF.json               # ACTF_v1.0 trajectory
    ├── info.json                     # 状态、usage、耗时、版本和 digest
    └── results/                      # 可选；只保存 task 声明的输出
```

### 文件说明

- 🧾 `trace.jsonl`：原始事实来源，不做消息清洗或改写。
- 🔄 `trace_ACTF.json`：按 assistant step 组织 tool call、observation 和多轮 user message。
- ℹ️ `info.json`：保存状态、错误、token/cost usage、attempt、版本和文件 digest。
- 📂 `results/`：只有声明 `files.outputs` 时才存在。

运行期日志、Task Bundle、retry 中间 trace 和未声明输出不会留在最终 run 中。

## 🔁 Resume 规则

使用 `--resume` 时，Roller 只复用满足以下条件的 sample：

1. 上一次状态为 `ok`。
2. `trace.jsonl` 和 `trace_ACTF.json` 都还在。
3. 当前 Task Bundle digest 与旧结果一致。
4. context 没有使用会禁用安全 resume 的 `capture=none`。

prompt、messages、输入文件、context、输出契约或 runtime 设置变化后，task 会重新运行。

## 🔒 文件与密钥安全

- 输入和 context 会在启动容器前检查路径、大小、符号链接和 hash。
- API key 只通过环境变量或现有外部配置读取，不写入 Roller run。
- `examples/env/*.env` 默认被 Git 忽略，模板文件只放占位值。
- sample 只保存声明过的输出，避免把工作区临时文件带进数据集。
- `capture=none` 不保存 context 内容 hash，同时会关闭该 task 的安全 resume。

## 🧪 开发与测试

- Harness 接口：[src/roller/harnesses/base.py](src/roller/harnesses/base.py)
- OpenClaw adapter：[src/roller/harnesses/openclaw.py](src/roller/harnesses/openclaw.py)
- ACTF 转换：[src/roller/actf.py](src/roller/actf.py)

运行测试：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 📖 更多文档

| 文档 | 内容 |
| --- | --- |
| [Examples](examples/README.md) | 环境变量、普通/多模态模型和四类 task |
| [Task 文件与存储设计](docs/task-bundle-and-storage.md) | 输入快照、context、输出校验、ACTF 和 resume 细节 |
| [Task JSON Schema](schemas/task.schema.json) | `roller.task/v1` 的机器可读约束 |

## 📄 License

[Apache License 2.0](LICENSE)
