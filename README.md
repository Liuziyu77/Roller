# Roller

Roller 是一个面向 Agentic 数据的批量轨迹采集框架。用户提供版本化任务清单和模型列表，Roller 负责输入快照、系统文件注入、并行执行、输出校验，以及 Harness 原生 rollout 的归档。

当前首个 Harness 是 **OpenClaw**。实现基于 `Interns2_data` 中的 runner 与并发采集脚本重构，同时保留 `question`、`workspace`、`persona` 和“第 N 轮”格式兼容。

## 核心能力

- `roller.task/v1` 统一任务协议和机器可读 [JSON Schema](schemas/task.schema.json)
- 任务级 `inputs`、`context`、`outputs` 文件生命周期
- 输入 SHA-256、内容寻址 blob、跨模型输入树复用
- `USER.md`、`MEMORY.md`、`SYSTEM.md`、`SOUL.md`、`AGENTS.md`、skills 语义注入
- context `full/hash/none` 隐私归档策略
- 声明输出与未声明输出分区、required/type/size/symlink 校验
- 多任务 × 多模型并行、失败重试和 bundle-aware resume
- 原始 OpenClaw trace、usage、日志、版本及镜像 provenance
- 自定义 OpenClaw provider、API base URL 和环境变量凭据

## 安装

要求 Python 3.10+、Docker，以及包含 `openclaw` 和 `python3` 的 Docker 镜像。

```bash
python3 -m pip install -e .
export ROLLER_OPENCLAW_IMAGE=your-openclaw-image:tag
```

默认镜像 `wildclawbench-ubuntu:v1.3` 用于兼容现有 `Interns2_data` 环境。

## Task Bundle v1

任务清单支持 JSON 或 JSONL。文件路径中的 `source` 相对于任务清单所在目录解析，`target/path` 必须是安全的相对路径。

```json
{
  "schema_version": "roller.task/v1",
  "id": "task_000001",
  "messages": [
    {
      "role": "user",
      "content": "读取 /tmp_workspace/inputs/report.pdf，并将结论写入 /tmp_workspace/outputs/answer.md"
    }
  ],
  "files": {
    "inputs": [
      {
        "source": "./task_000001/report.pdf",
        "target": "report.pdf",
        "required": true
      }
    ],
    "context": [
      {
        "role": "system",
        "source": "./prompts/research-system.md",
        "capture": "full"
      },
      {
        "role": "user",
        "source": "./personas/alice/USER.md",
        "capture": "hash"
      },
      {
        "role": "memory",
        "source": "./personas/alice/MEMORY.md",
        "capture": "none",
        "required": false
      }
    ],
    "outputs": [
      {
        "path": "answer.md",
        "type": "file",
        "required": true,
        "max_bytes": 1048576
      },
      {
        "path": "references/**",
        "type": "glob",
        "required": false
      }
    ]
  },
  "runtime": {
    "env": ["BRAVE_API_KEY"],
    "setup": [],
    "thinking": "high"
  }
}
```

### Inputs

`files.inputs` 可以引用文件或目录：

```json
"inputs": [
  "brief.md",
  {"source": "./documents", "target": "documents"},
  {
    "source": "dataset.csv",
    "target": "data/dataset.csv",
    "sha256": "expected-64-character-sha256"
  }
]
```

执行前 Roller 会：

1. 拒绝路径逃逸、目录内符号链接和非普通文件。
2. 流式计算 SHA-256、大小和文件 mode。
3. 将内容写入 `blobs/sha256/<digest>`。
4. 用文件集合 digest 构建只读输入树。
5. 让同一任务在不同模型间复用完全相同的输入树。

默认限制为单文件 10 GiB、单任务输入总计 50 GiB，可通过 `--max-input-file-bytes` 和 `--max-task-input-bytes` 调整。

容器内稳定路径：

```text
/roller/task/inputs
/tmp_workspace/inputs -> /roller/task/inputs
```

### Context 和系统文件

`files.context` 使用语义 role，由 Harness adapter 映射到实际运行环境：

| role | 默认 target | OpenClaw 注入位置 |
| --- | --- | --- |
| `user` | `USER.md` | `/root/USER.md` + OpenClaw workspace `USER.md` |
| `memory` | `MEMORY.md` | `/root/MEMORY.md` + workspace `MEMORY.md` |
| `system` | `SYSTEM.md` | `/root/SYSTEM.md` + workspace `SYSTEM.md` |
| `soul` | `SOUL.md` | `/root/SOUL.md` + workspace `SOUL.md` |
| `agents` | `AGENTS.md` | `/root/AGENTS.md` + workspace `AGENTS.md` |
| `skill` / `skills` | `skills/<source-name>` | `/root/skills/<source-name>` + workspace skills |
| 自定义 role | source 文件名或显式 `target` | `/root/<target>` + workspace `<target>` |

`capture` 控制运行完成后的归档：

- `full`：保存内容 blob、hash、大小和 mode。
- `hash`：manifest 保存 hash，但不永久保存内容；适合 `USER.md`、`MEMORY.md`。
- `none`：只保存逻辑路径、大小和 mode；不保存内容或 hash。

无论 capture 策略如何，同一次运行中的所有模型都会使用同一份运行期 context 快照。正常结束后 `.runtime` 会删除。使用 `--keep-container-on-error` 时，为便于调试会保留运行期 context。由于 `capture=none` 不允许保存内容 hash，Roller 会对包含此策略的任务禁用 resume，避免错误复用已经变化的私密上下文。

### Outputs

Agent 应将新任务输出写到：

```text
/roller/task/outputs
/tmp_workspace/outputs -> /roller/task/outputs
```

输出类型：

- `file`：精确文件。
- `directory`：目录及其所有普通文件，空目录也视为存在。
- `glob`：相对 glob，例如 `assets/**`。

Roller 在删除容器前回收整个 outputs 目录，并执行：

- required 存在性校验
- file/directory 类型校验
- 单文件 `max_bytes` 校验
- 符号链接和非普通文件拒绝
- SHA-256、size 和 MIME 记录
- declared / undeclared 文件分区

校验失败会将 rollout 标记为失败，因此可以进入重试；原始 trace 仍然保留。

## 运行

先做完整文件解析、hash 和任务矩阵验证，不启动 Docker：

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
export OPENROUTER_API_KEY=your_key

roller collect \
  --tasks examples/tasks.jsonl \
  --model openrouter/model-a \
  --model openrouter/model-b \
  --docker-image your-openclaw-image:tag \
  --parallel 4 \
  --timeout 1200 \
  --retries 1 \
  --output runs \
  --run-name experiment_001
```

使用 `--start-index N --limit M` 可以做 smoke test 或任务分片。使用同一 run name 加 `--resume` 时，只有成功 trace 存在且 Task Bundle digest 未变化的样本才会跳过。

## 自定义 API

参考 [examples/openclaw-models.json](examples/openclaw-models.json)：

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

```bash
export MY_PROXY_BASE_URL=https://api.example.com/v1
export MY_PROXY_API_KEY=your_key

roller collect \
  --tasks examples/tasks.jsonl \
  --model my-openai-proxy/my-model \
  --model-config examples/openclaw-models.json
```

`${ENV_NAME}` 只在运行期展开，展开后的 key 不写入 run metadata。provenance 中的 base URL 会移除认证信息、query 和 fragment。`OPENROUTER_BASE_URL` 及 `OPENROUTER_BASE_URL_OPENCLAW` 也受到支持。

## 输出结构

```text
runs/experiment_001/
├── run.json
├── manifest.jsonl
├── summary.json
├── blobs/sha256/                         # 内容寻址输入/full context
├── trees/sha256/                         # 可复用只读输入树
├── tasks/
│   └── <source-index>_<task-id>/
│       ├── task.json
│       ├── inputs.manifest.json
│       └── context.manifest.json
└── samples/
    └── <model>/<source-index>_<task-id>/
        ├── request.json
        ├── result.json
        ├── provenance.json
        ├── outputs.manifest.json
        ├── artifact_validation.json
        ├── rollout/
        │   ├── trace.jsonl
        │   ├── trace.attempt-1.jsonl
        │   ├── usage.json
        │   ├── agent.attempt-1.log
        │   └── gateway.attempt-1.log
        └── artifacts/
            ├── declared/
            ├── undeclared/
            └── legacy/                   # 仅 --collect-path
```

`trace.jsonl` 是未经清洗的 OpenClaw 原始轨迹。`provenance.json` 包含 bundle digest、OpenClaw 版本、Docker image/digest、模型、脱敏 API base URL 和环境变量名称。

## 旧任务兼容

- `question` 自动转换为 user prompt。
- `workspace` 在未声明 `files.inputs` 时转为 legacy input bundle，并复制到 `/tmp_workspace` 根目录。
- `persona` 自动寻找 workspace persona 目录下的 `USER.md` 和 `MEMORY.md`。
- `warmup` 转成 `runtime.setup`。
- `## 第 N 轮` prompt 默认在同一 session 中逐轮发送。
- `--collect-path` 仍可回收旧任务写到任意容器路径的文件，但新任务应使用 output contract。

Legacy `workspace` 会作为一个整体输入快照；如果其中混有私密 persona 文件，它们也会进入 input blob。需要严格执行 context 隐私策略时，应改用显式 `files.inputs` 与 `files.context`，不要使用整目录 legacy workspace。

直接验证现有 `Interns2_data`：

```bash
roller collect \
  --tasks ../Interns2_data/prompt_pools/task_categories_multi_turn.jsonl \
  --prompt-key question \
  --model my-provider/model \
  --start-index 0 \
  --limit 2 \
  --dry-run
```

## 测试

运行时没有第三方 Python 依赖，单元测试不需要 Docker 或 API key：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 扩展 Harness

文件解析、快照、输出校验与归档属于 Roller 核心。新 Harness 只需实现：

1. 将 `TaskBundle.inputs_root` 只读提供给 Agent。
2. 根据 context role/target 注入系统文件。
3. 提供独立 outputs 目录。
4. 调用统一 artifact collector。
5. 返回原生 trace 和 `HarnessResult`。

## License

Apache-2.0
