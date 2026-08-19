# Roller

Roller 是一个开源的 Agentic 轨迹采集框架：输入任务清单和模型列表，即可并行运行 Harness，并以可复现、可审计的格式保存原始轨迹、任务文件和 Agent 输出。

当前支持 **OpenClaw**。核心文件协议与 Harness 解耦，后续可以在同一套 Task Bundle 和归档格式上接入其他 Harness。

## 能做什么

- 批量执行任务 × 模型矩阵，支持并行、重试和断点续跑
- 为每个任务声明输入文件、系统/用户/记忆文件和预期输出
- 对输入做内容寻址快照，确保不同模型获得相同输入
- 将 `USER.md`、`MEMORY.md`、`SYSTEM.md`、skills 等注入 OpenClaw
- 校验 required、文件类型、大小和符号链接，并区分声明/未声明输出
- 保存 OpenClaw 原始 trace、usage、日志、模型和镜像 provenance
- 通过 OpenClaw model config 使用自定义 provider、API base URL 和 API key

## 安装

需要 Python 3.10+、Docker，以及包含 `openclaw` 和 `python3` 的运行镜像。

```bash
python3 -m pip install -e .
export ROLLER_OPENCLAW_IMAGE=your-openclaw-image:tag
```

## 快速开始

任务清单可以是 JSON 或 JSONL。下面的任务会挂载一个输入文件、注入 `SYSTEM.md`，并要求 Agent 生成 `report.md`：

```json
{
  "schema_version": "roller.task/v1",
  "id": "research_001",
  "prompt": "读取 inputs/brief.md，并将报告写入 outputs/report.md。",
  "files": {
    "inputs": [
      {"source": "./brief.md", "target": "brief.md"}
    ],
    "context": [
      {"role": "system", "source": "./SYSTEM.md", "capture": "full"}
    ],
    "outputs": [
      {"path": "report.md", "type": "file", "required": true}
    ]
  }
}
```

先验证任务、文件和执行矩阵，不启动 Docker：

```bash
roller collect \
  --tasks examples/tasks.jsonl \
  --model my-provider/model-a \
  --model my-provider/model-b \
  --parallel 4 \
  --dry-run
```

开始采集：

```bash
roller collect \
  --tasks examples/tasks.jsonl \
  --model my-provider/model-a \
  --docker-image your-openclaw-image:tag \
  --parallel 4 \
  --timeout 1200 \
  --retries 1 \
  --output runs \
  --run-name experiment_001
```

完整示例包含纯文本、本地 Markdown、`USER.md`/`MEMORY.md` 和多轮任务，见 [examples/README.md](examples/README.md)；字段约束见 [Task JSON Schema](schemas/task.schema.json)。

## 多轮任务

新任务应显式使用 `messages`；每条 user message 会在同一个 OpenClaw session 中依次发送：

```json
{
  "schema_version": "roller.task/v1",
  "id": "plan_001",
  "messages": [
    {"role": "user", "content": "先生成项目计划。"},
    {"role": "user", "content": "再补充负责人、风险和里程碑。"}
  ]
}
```

Roller 不解析 message 内的 Markdown 或分隔符。`prompt/question` 始终是一轮；需要多轮时必须提供多个 `messages`，从任务数据本身明确轮次边界。

## 自定义 API

OpenClaw provider 配置通过 `--model-config` 传入。配置支持 `${ENV_NAME}`，因此密钥不需要写进文件：

```bash
export MY_PROXY_BASE_URL=https://api.example.com/v1
export MY_PROXY_API_KEY=your_key

roller collect \
  --tasks examples/tasks.jsonl \
  --model my-openai-proxy/my-model \
  --model-config examples/openclaw-models.json
```

示例配置见 [examples/openclaw-models.json](examples/openclaw-models.json)。展开后的密钥不会写入 run metadata。

## 采集结果

每次 run 包含：

```text
runs/<run-name>/
├── run.json / manifest.jsonl / summary.json
├── blobs/ / trees/ / tasks/               # 输入与上下文快照
└── samples/<model>/<task>/
    ├── request.json / result.json / provenance.json
    ├── rollout/                            # 原始 trace、usage、日志
    ├── artifacts/declared/                 # 满足输出契约的文件
    ├── artifacts/undeclared/               # Agent 额外生成的文件
    └── outputs.manifest.json / artifact_validation.json
```

任务文件如何解析、注入、去重、校验和归档，详见 [Task Bundle 与文件存储设计](docs/task-bundle-and-storage.md)。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## License

Apache-2.0
