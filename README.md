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

Roller 是一个开源的 Agent 轨迹采集框架。提供任务和模型配置后，Roller 可以批量、并行地启动Docker和运行 Agent，并把完整轨迹整理成结构统一、便于分析和复用的轨迹数据。

项目目前支持 **OpenClaw** Harness，并用统一的任务格式管理单轮任务、预定义多轮对话、动态用户模拟、本地输入、系统文件、模型配置和文件输出。Harness 接口与数据格式彼此独立，后续可以在不改变任务数据的情况下接入更多 Agent Harness。

## ✨ 能做什么

| 能力 | 说明 |
| --- | --- |
| 批量执行 | 一份 task 清单可以跑多个模型，支持并行和失败重试 |
| 本地文件 | 每个 task 可以带自己的文件或目录，Agent 通过统一路径读取 |
| Context 注入 | 支持 `USER.md`、`MEMORY.md`、`SYSTEM.md`、`SOUL.md` 和 skills |
| 多轮输入 | 使用 `messages` 明确每一轮，不从 prompt 文本中猜测分隔位置 |
| User Simulator | 独立 Harness 根据当前对话动态给出下一条指令，适合交互式 rollout |
| 输出管理 | 每个 task 指定一个可写目录，任务结束后自动回收其中的全部文件 |
| 两种轨迹 | 同时保留 OpenClaw 原始 JSONL 和 `ACTF_v1.0` |
| 自定义 API | 支持自定义 provider、API base、文本模型和多模态模型 |
| 断点续跑 | 对 task 与文件计算 digest，未发生变化的成功任务可以跳过 |

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
    ├── user-simulator.jsonl
    └── files/
```

四个内置 task 分别演示：

1. 纯文本输入
2. 本地 Markdown 输入与文件输出
3. `USER.md`、`MEMORY.md` 注入
4. 两轮连续对话

独立的 `user-simulator.jsonl` 演示由 HR Simulator 与任务 Agent 动态协作完成招聘广告，不会拖慢四个基础示例的快速测试。

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

task 清单可以使用 `.jsonl` 或 `.json`：JSONL 每行是一条 task，JSON 则使用 task 数组或 `{"tasks": [...]}`。每条 task 描述一次 rollout 需要的对话、输入文件、Context、输出目录和运行参数。

### 1. 完整 Task 示例

下面是一条包含常用配置的 task。为了便于阅读，这里将 JSONL 中的一行展开显示：

```json
{
  "schema_version": "roller.task/v1",
  "id": "product_review",
  "file_root": "files/product_review",
  "model": "my-openai-proxy/my-model",
  "prompt": "阅读 inputs/product-brief.md，结合已注入的用户信息撰写产品摘要，并保存到 outputs/report.md。报告需要包含产品名称和三条核心特点。",
  "files": {
    "inputs": [
      {
        "source": "inputs/product-brief.md",
        "required": true
      }
    ],
    "context": [
      {"role": "system", "source": "context/SYSTEM.md", "capture": "full"},
      {"role": "user", "source": "context/USER.md", "capture": "hash"},
      {"role": "memory", "source": "context/MEMORY.md", "capture": "hash"}
    ],
    "output_dir": "outputs"
  },
  "runtime": {
    "thinking": "high"
  }
}
```

在这个例子中，`file_root` 指向 task 所属的文件包，宿主机输入放在其中的 `inputs/`，系统文件放在 `context/`。运行时，Agent 看到的输入与输出分属两个目录：

```text
/tmp_workspace/
├── inputs/product-brief.md     # 只读输入
└── outputs/report.md           # Agent 生成的输出
```

`outputs/` 不需要提前创建；Roller 会准备可写目录，并把其中生成的全部文件带回宿主机。下面逐项说明这些配置。

### 2. 常用顶层字段

| 字段 | 是否必需 | 说明 |
| --- | --- | --- |
| `schema_version` | 是 | 当前为 `roller.task/v1` |
| `id` | 是 | task 的唯一标识 |
| `prompt` / `question` / `messages` / `user_simulator` | 选择一种输入模式 | `prompt` 用于单轮，`question` 是兼容字段，`messages` 是预定义多轮，`user_simulator` 动态生成每轮指令 |
| `file_root` | 否 | 输入、context source 和 `output_dir` 共用的 task 工作区根目录 |
| `model` | 否 | task 自己指定模型；命令行 `--model` 优先 |
| `files` | 否 | 输入、context 和输出目录 |
| `runtime` | 否 | 环境变量、setup 命令和 thinking 配置 |

不需要文件或特殊运行设置时，可以只写一个最小 task：

```json
{"schema_version":"roller.task/v1","id":"hello","prompt":"计算 2+3，只回复数字。"}
```

多轮任务使用 `messages`，所有 message 会进入同一个 Agent session。完整示例和执行语义见 [单轮和多轮任务](docs/task-bundle-and-storage.md#单轮和多轮)。

### 3. User Simulator 动态对话

```json
{
  "schema_version": "roller.task/v1",
  "id": "hr_recruitment_ad",
  "model": "my-openai-proxy/task-agent-model",
  "user_simulator": {
    "harness": "openclaw",
    "model": "my-openai-proxy/simulator-model",
    "max_turns": 6,
    "instruction": "你是一名 HR。通过多轮交流让 Agent 完成招聘广告；检查每版草稿，逐步补充要求，确认最终文件保存后结束。"
  },
  "files": {
    "output_dir": "outputs"
  }
}
```

User Simulator 使用独立模型、容器和 OpenClaw session。Conversation Manager 每轮向它提供固定 `instruction` 与完整对话；Simulator 决定发出下一条指令或结束。任务 Agent 也在同一个 session 中连续回复，因此可以保留对话状态并持续修改工作区文件。

`user_simulator` 不能和 `prompt`、`question`、`messages` 同时出现。`model` 省略时 Simulator 跟随任务 Agent 模型；`max_turns` 默认是 `6`。完整示例见 [examples/tasks/user-simulator.jsonl](examples/tasks/user-simulator.jsonl)，调度、终止协议、资源占用和双轨迹格式见 [User Simulator 文档](docs/user-simulator.md)。

### 4. 输入文件

```json
"inputs": [
  "inputs/brief.md",
  {"source": "inputs/documents", "required": true},
  {
    "source": "inputs/dataset.csv",
    "required": true,
    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }
]
```

- `source` 是 `file_root` 下的文件或目录相对路径，不允许绝对路径或 `..`。
- Roller 不重命名输入；`inputs/dataset.csv` 在 Agent 工作区中仍是 `inputs/dataset.csv`。
- Agent 可以通过 `/tmp_workspace/<source>` 或 `/roller/task/inputs/<source>` 读取。
- 输入在容器中只读；目录会递归复制并检查符号链接。

### 5. USER.md、MEMORY.md 与系统文件

```json
"context": [
  {"role": "system", "source": "context/SYSTEM.md", "capture": "full"},
  {"role": "user", "source": "context/USER.md", "capture": "hash"},
  {"role": "memory", "source": "context/MEMORY.md", "capture": "hash"},
  {"role": "skill", "source": "context/skills/research"}
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

### 6. 输出目录与自动回收

```json
"files": {
  "output_dir": "outputs"
}
```

`output_dir` 是 `file_root` 工作区下的相对目录，默认值为 `outputs`。Roller 在容器中创建 `/tmp_workspace/<output_dir>/`；`/roller/task/outputs` 是同一目录的稳定别名。

任务结束后，目录中的全部普通文件和空目录都会保存到 sample 的 `results/`，不需要预先声明文件名。Roller 会拒绝符号链接和特殊文件，并在 `info.json` 中记录每个结果文件的大小、MIME 和 SHA-256。`output_dir` 为空时不会创建 `results/`。

### 7. 运行时设置

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
    ├── conversation.json             # 可选；动态对话和终止原因
    ├── user_simulator/               # 可选；Simulator raw/ACTF/info
    └── results/                      # 可选；保存 output_dir 中的全部文件
```

### 文件说明

- `trace.jsonl`：原始事实来源，不做消息清洗或改写。
- `trace_ACTF.json`：按 assistant step 组织 tool call、observation 和多轮 user message。
- `info.json`：保存状态、错误、token/cost usage、attempt、版本和文件 digest。
- `conversation.json`：User Simulator 模式下保存清晰的交替消息、轮次和终止原因。
- `user_simulator/`：User Simulator 模式下保存 Simulator 自己的原始轨迹、ACTF 和 usage。
- `results/`：`output_dir` 非空时存在，并保留其中的完整目录结构。

运行期日志、Task Bundle、retry 中间 trace，以及 `output_dir` 之外的工作区文件不会留在最终 run 中。

## 🔁 Resume 规则

使用 `--resume` 时，Roller 只复用满足以下条件的 sample：

1. 上一次状态为 `ok`。
2. `trace.jsonl` 和 `trace_ACTF.json` 都还在。
3. 当前 Task Bundle digest 与旧结果一致。
4. context 没有使用会禁用安全 resume 的 `capture=none`。
5. 动态任务的 `conversation.json` 与 User Simulator raw/ACTF 轨迹都存在。

prompt、messages、User Simulator instruction/model/轮次上限、输入文件、context、`output_dir` 或 runtime 设置变化后，task 会重新运行。

## 🔒 文件与密钥安全

- 输入和 context 会在启动容器前检查路径、大小、符号链接和 hash。
- API key 只通过环境变量或现有外部配置读取，不写入 Roller run。
- `examples/env/*.env` 默认被 Git 忽略，模板文件只放占位值。
- sample 只回收 `output_dir`，不会把工作区其他临时文件带进数据集。
- `capture=none` 不保存 context 内容 hash，同时会关闭该 task 的安全 resume。

## 🧪 开发与测试

- Harness 接口：[src/roller/harnesses/base.py](src/roller/harnesses/base.py)
- 对话管理器：[src/roller/conversation.py](src/roller/conversation.py)
- OpenClaw adapter：[src/roller/harnesses/openclaw.py](src/roller/harnesses/openclaw.py)
- ACTF 转换：[src/roller/actf.py](src/roller/actf.py)

运行测试：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 📖 更多文档

| 文档 | 内容 |
| --- | --- |
| [Examples](examples/README.md) | 环境变量、普通/多模态模型、四类基础 task 和 User Simulator 示例 |
| [User Simulator](docs/user-simulator.md) | 动态对话调度、JSON 决策协议、资源占用和双轨迹目录 |
| [Task 文件与存储设计](docs/task-bundle-and-storage.md) | 输入快照、context、输出回收、ACTF 和 resume 细节 |
| [Task JSON Schema](schemas/task.schema.json) | `roller.task/v1` 的机器可读约束 |

## 📄 License

[Apache License 2.0](LICENSE)
