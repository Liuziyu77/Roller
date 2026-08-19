# Task Bundle 与文件存储设计

本文档说明 Roller 如何处理任务消息、输入依赖、系统上下文、Agent 输出和 rollout 归档。快速使用请先阅读项目根目录的 [README](../README.md)，机器可读约束见 [Task JSON Schema](../schemas/task.schema.json)。

## 1. 数据生命周期

一个任务会先被规范化并生成不可变 Task Bundle，再与模型列表展开为多个 rollout：

```text
task JSON/JSONL
  ├─ messages                     -> 同一 session 中的多轮 user 消息
  ├─ files.inputs                 -> hash/blob -> 只读输入树
  ├─ files.context                -> 运行期快照 -> Harness 语义注入
  └─ files.outputs                -> 输出契约
                                      │
                         task × model × harness
                                      │
                          trace + logs + artifacts
```

同一个 task 的所有模型共享相同的 input tree 和本次运行的 context 快照。每个 task/model pair 使用独立容器和 sample 目录，避免输出相互污染。

## 2. Task Bundle v1

顶层格式：

```json
{
  "schema_version": "roller.task/v1",
  "id": "task_000001",
  "file_root": "..",
  "messages": [
    {"role": "user", "content": "第一轮任务"},
    {"role": "user", "content": "第二轮补充要求"}
  ],
  "model": "optional-provider/model",
  "files": {
    "inputs": [],
    "context": [],
    "outputs": []
  },
  "runtime": {
    "env": ["API_KEY_NAME"],
    "setup": ["command run inside the workspace"],
    "thinking": "high"
  }
}
```

默认情况下，`source` 路径相对于任务清单文件所在目录解析，也可以使用绝对宿主机路径。任务声明 `file_root` 后，相对 `source` 改为以该目录为基准；`file_root` 自身仍相对于任务清单目录解析。这样位于 `examples/tasks.jsonl` 的任务可以用 `"file_root":".."` 将基准设为 Roller 根目录，再引用 `examples/task_files/...`。

`target` 和 output `path` 是运行时逻辑相对路径，不受 `file_root` 影响；绝对路径、`..` 逃逸和不允许位置的 glob 会在启动容器前被拒绝。

任务消息有两种写法：

- `messages`：正式的多轮表示。目前每条消息必须是 `user` role；Harness 会在同一 session 中按顺序发送。
- `prompt`：单轮简写。兼容字段 `question` 也会规范化为单轮 prompt。

当命令行同时传入多个 `--model` 时，任务与模型组成笛卡尔积。任务自身的 `model` 仅在命令行没有提供 model 时使用。

## 3. 输入文件

`files.inputs` 接受文件或目录：

```json
"inputs": [
  "brief.md",
  {"source": "./documents", "target": "documents"},
  {
    "source": "dataset.csv",
    "target": "data/dataset.csv",
    "required": true,
    "sha256": "64-character-expected-digest"
  }
]
```

字符串写法等价于 `source` 指向该文件、`target` 使用文件名、`required=true`。目录会递归展开到 target 下；可选输入不存在时记录 `present=false`，required 输入不存在则任务准备失败。

### 快照与去重

Roller 在执行前完成以下操作：

1. 拒绝输入根本身或目录内部的符号链接、特殊文件和路径冲突。
2. 以流式方式计算每个普通文件的 SHA-256、大小和 mode。
3. 将内容保存为 `blobs/sha256/<digest>`。
4. 根据逻辑目录和文件记录计算 input tree digest，并构建 `trees/sha256/<digest>`。
5. 将相同内容和相同输入树在 run 内复用。

为防止源文件在扫描与复制之间被替换，blob 写入后会再次校验 digest 和大小。文件的显式 `sha256` 对比文件 digest；目录的显式 `sha256` 对比由其目录、相对路径、文件 digest、大小和 mode 共同计算的 source digest。

默认单文件上限为 10 GiB，单任务输入总量上限为 50 GiB：

```bash
roller collect ... \
  --max-input-file-bytes 1073741824 \
  --max-task-input-bytes 5368709120
```

### OpenClaw 中的路径

新版 Task Bundle 输入在容器内始终只读：

```text
/roller/task/inputs
/tmp_workspace/inputs
```

OpenClaw agent 的工作目录是 `/tmp_workspace`，提示词可以统一使用相对路径 `inputs/<target>`；需要稳定绝对路径时也可以使用 `/roller/task/inputs/<target>`。

## 4. Context 与系统文件

`files.context` 用于 Harness 需要语义注入的文件，不应与普通只读资料混在 inputs 中：

```json
"context": [
  {"role": "system", "source": "./SYSTEM.md", "capture": "full"},
  {"role": "user", "source": "./USER.md", "capture": "hash"},
  {"role": "memory", "source": "./MEMORY.md", "capture": "none"},
  {
    "role": "skill",
    "source": "./skills/web-research",
    "target": "skills/web-research",
    "capture": "full"
  }
]
```

role 决定省略 `target` 时的默认路径：

| role | 默认 target |
| --- | --- |
| `user` | `USER.md` |
| `memory` | `MEMORY.md` |
| `system` | `SYSTEM.md` |
| `soul` | `SOUL.md` |
| `agents` | `AGENTS.md` |
| `skill` / `skills` | `skills/<source-name>` |
| 其他 role | `<source-name>` |

OpenClaw adapter 先把运行期 context 只读挂载到 `/roller/task/context`，再将同一份内容复制到 `/root/<target>` 和 `/tmp_workspace/<target>`。因此 `USER.md`、`MEMORY.md`、`SYSTEM.md` 以及 skill 目录都能出现在 OpenClaw 常用发现位置。role 负责选择默认 target；显式 target 可以覆盖它。

### capture 隐私策略

`capture` 只控制永久归档方式，不影响本次执行是否注入：

| capture | 永久保存内容 | manifest 保存 SHA-256 | 适用场景 |
| --- | --- | --- | --- |
| `full` | 是，进入 blob store | 是 | 可公开、需完全复现的系统文件 |
| `hash` | 否 | 是 | 不想归档原文，但需要变化检测 |
| `none` | 否 | 否 | 高敏感且不能留下内容指纹 |

`user` 和 `memory` 的默认策略是 `hash`，其他 role 默认 `full`。所有 context 都会为当前 run 创建权限受限的 `.runtime/context` 快照，正常结束后删除。使用 `--keep-container-on-error` 时，失败现场和运行期 context 会保留以便排查。

`capture=none` 无法证明两次运行使用了相同内容，所以只要存在这类文件，任务就会标记为 `resume_safe=false`，`--resume` 不会复用其旧成功结果。

## 5. 输出契约

Agent 应将新输出写到以下任一路径；两者指向同一个目录：

```text
/tmp_workspace/outputs
/roller/task/outputs
```

OpenClaw prompt 中推荐使用相对路径 `outputs/<path>`。`files.outputs[].path` 已经以 outputs 为根，因此声明中只写 `<path>`，不添加 `outputs/` 前缀。

声明示例：

```json
"outputs": [
  {"path": "answer.md", "type": "file", "required": true, "max_bytes": 1048576},
  {"path": "assets", "type": "directory", "required": false},
  {"path": "references/**", "type": "glob", "required": false}
]
```

支持三种类型：

- `file`：路径必须是普通文件。
- `directory`：路径必须是目录；空目录也满足存在性要求。
- `glob`：相对于 outputs 根目录匹配一个或多个路径。

容器退出前，OpenClaw adapter 会回收整个 outputs 目录。统一 artifact collector 会检查 required、类型、每个匹配文件的 `max_bytes`、符号链接和非普通文件，并为普通文件记录 SHA-256、size 和 MIME type。

匹配输出契约的文件复制到 `artifacts/declared`，其他文件复制到 `artifacts/undeclared`。未声明文件不会被静默丢弃；但任何安全或契约错误都会写入 `artifact_validation.json`，并把 rollout 状态改为 error，使其可以进入重试流程。即使校验失败，已取得的 trace、日志和安全的普通输出仍保留。

## 6. 多轮消息

多轮边界只由 `messages` 数组决定，Roller 和 Harness 都不会解析消息正文。以下两种写法等价：

```json
"messages": [
  "第一轮任务",
  "第二轮补充要求"
]
```

```json
"messages": [
  {"role": "user", "content": "第一轮任务"},
  {"role": "user", "content": "第二轮补充要求"}
]
```

每个元素会在同一个 OpenClaw session 中顺序发送一次。即使 message 正文包含 `## 第 1 轮`、`---` 或其他 Markdown，它仍然只是一个完整 message。

`prompt` 和兼容字段 `question` 始终转换为单个 message。若旧数据把多个轮次编码在一个字符串中，应在进入 Roller 前转换成 `messages` 数组；运行时不会猜测或拆分文本。

## 7. 自定义 API 与运行环境

`--model-config` 接收 OpenClaw models JSON，并原样合并到 OpenClaw 配置。字符串中的 `${ENV_NAME}` 在宿主机运行期展开：

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

展开后的配置只用于容器配置，不写入 `run.json`。`provenance.json` 仅记录脱敏后的 API base URL：移除 URL 中的认证信息、query 和 fragment。环境变量只记录名称，不记录值。

OpenClaw 兼容环境变量包括 `OPENROUTER_API_KEY`、`OPENROUTER_BASE_URL`，以及代理和搜索 provider 的常用变量。`OPENROUTER_BASE_URL_OPENCLAW` 会兼容映射为容器内的 `OPENROUTER_BASE_URL`。其他变量可通过以下两种方式声明：

- 命令行 `--pass-env NAME`：如果宿主机存在则传入。
- task `runtime.env`：该任务强制依赖；变量缺失时直接失败。

`runtime.setup` 中的命令会在 context 注入完成后、Agent 启动前，以 `/tmp_workspace` 为工作目录逐条执行。

## 8. Run 存储结构

```text
runs/<run-name>/
├── run.json                         # 本次命令的非敏感配置
├── manifest.jsonl                   # 所有 sample 结果索引
├── summary.json                     # 状态和 usage 汇总
├── blobs/sha256/<digest>            # inputs 与 full context 内容
├── trees/sha256/<tree-digest>/      # 可复用输入树
├── tasks/<index>_<task-id>/
│   ├── task.json                    # 规范化任务与 bundle digest
│   ├── inputs.manifest.json
│   └── context.manifest.json
└── samples/<model>/<index>_<task-id>/
    ├── request.json                 # 此 task/model 的规范化请求
    ├── result.json                  # 最终状态、错误、usage、attempts
    ├── provenance.json              # 版本、镜像、模型、bundle/API 来源
    ├── outputs.manifest.json        # 输出声明、匹配关系、文件 hash
    ├── artifact_validation.json     # 输出契约检查结果
    ├── rollout/
    │   ├── trace.jsonl              # 最后一次 attempt 的原始 OpenClaw trace
    │   ├── trace.attempt-N.jsonl     # 各次 retry 的原始 trace
    │   ├── usage.json
    │   ├── agent.attempt-N.log
    │   └── gateway.attempt-N.log
    └── artifacts/
        ├── declared/
        ├── undeclared/
        └── legacy/                  # 仅 --collect-path
```

`trace.jsonl` 不做消息重写或清洗。`usage.json` 从原始 trace 的 assistant usage 字段聚合 token、cache token、request count 和可用的 cost。编号 trace 和日志用于分析重试；canonical `trace.jsonl` 始终指向最后一次 attempt。

### Resume 判定

`--resume` 只有在以下条件同时满足时才跳过 sample：

1. 旧 `result.json` 状态为 `ok`。
2. canonical trace 文件仍存在。
3. 旧 provenance 中的 Task Bundle digest 与当前任务一致。
4. 当前 bundle 的 `resume_safe=true`。

消息、输入 manifest、context manifest、output contract 或 runtime 声明发生变化，都会改变 bundle digest，从而触发重新采集。

## 9. Legacy 兼容边界

为迁移现有数据，Roller 还保留：

- `question` 作为 `prompt` 的别名。
- `workspace` / `workspace_path`：未声明 `files.inputs` 时，将整个目录作为 legacy input bundle，并复制成可写的 `/tmp_workspace`。
- `persona`：从 legacy workspace persona 目录寻找 `USER.md` 和 `MEMORY.md`。
- `warmup` 作为 `runtime.setup` 的别名。
- `--collect-path CONTAINER_PATH`：额外复制旧任务写到任意容器路径的结果。

`question` 只提供字段名兼容，不提供文本内多轮解析。现有 `Interns2_data` 中使用 Markdown 标题拼接多轮的任务，需要先转换成 Task Bundle `messages`。

Legacy workspace 是整体快照。如果它混入私密 persona 或无关大文件，这些内容也会进入 input blob store。需要隐私控制和精确复现时，应迁移为显式 `files.inputs`、`files.context` 和 `files.outputs`。

转换后的数据可先执行不启动 Docker 的验证：

```bash
roller collect \
  --tasks converted_tasks.jsonl \
  --model my-provider/model \
  --start-index 0 \
  --limit 2 \
  --dry-run
```
