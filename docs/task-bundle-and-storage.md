# Task 文件与存储设计

本文档描述 Roller 如何解析 task 的消息、输入文件、系统文件和输出文件，以及最终 run 的存储格式。快速使用见项目根目录 [README](../README.md)，机器可读约束见 [Task JSON Schema](../schemas/task.schema.json)。

## 1. 两类输出路径

Roller 区分宿主机归档路径和 Agent 工作区路径：

- `roller collect --output <dir> --run-name <name>`：run 保存到宿主机 `<dir>/<name>/`。
- task 的 `files.output_dir`：`file_root` 工作区下的可写子目录，默认值是 `outputs`。
- sample 的 `results/`：自动保存 `output_dir` 中的全部普通文件和目录。

例如：

```json
"files": {
  "output_dir": "deliverables/final"
}
```

Agent 可以在 `/tmp_workspace/deliverables/final/` 中生成任意文件；Roller 会保持相对目录结构，将它们全部保存到 sample 的 `results/`。`/roller/task/outputs` 始终是当前 task 输出目录的稳定别名。

`file_root/output_dir` 表示 task 内的逻辑路径关系。为避免并行模型相互覆盖，Roller 不会直接写回宿主机的 `file_root`，最终文件统一进入对应 sample 的 `results/`。

## 2. Task 格式与路径解析

`file_root` 自身相对于 task 清单。每个 `files.inputs[].source` 必须是 `file_root` 下的相对路径，并在 Agent 工作区中保持相同路径；输入不再支持重命名或 `target`。

`output_dir` 同样是 `file_root` 工作区下的 POSIX 风格相对路径。绝对路径、`..` 逃逸，以及输入与输出目录重叠会在启动容器前被拒绝。Context 的 `target` 只控制 Harness 注入位置，不适用于普通输入。

### 单轮和多轮

- `prompt`：一轮 user message；兼容字段 `question` 行为相同。
- `messages`：每个数组元素是一轮 user message，在同一个 OpenClaw session 中顺序发送。
- `user_simulator`：由另一个 live Harness 根据完整上下文动态生成下一轮 user message。

三种模式只能选择一种。`messages` 的内容在运行前已经确定；`user_simulator` 的消息则由 Conversation Manager 在任务 Agent 每次回复后生成。动态模式的完整设计见 [User Simulator 与动态对话](user-simulator.md)。

下面是一条包含两轮对话、输入文件、Context 和文件输出的完整 task：

```json
{
  "schema_version": "roller.task/v1",
  "id": "multi_turn_review",
  "file_root": "files/multi_turn_review",
  "model": "my-openai-proxy/my-model",
  "messages": [
    {
      "role": "user",
      "content": "阅读 inputs/product-brief.md，先概括产品名称和核心特点。"
    },
    {
      "role": "user",
      "content": "根据刚才的分析生成最终报告，并保存到 outputs/report.md。"
    }
  ],
  "files": {
    "output_dir": "outputs",
    "inputs": [
      {
        "source": "inputs/product-brief.md",
        "required": true
      }
    ],
    "context": [
      {"role": "user", "source": "context/USER.md", "capture": "hash"},
      {"role": "memory", "source": "context/MEMORY.md", "capture": "hash"}
    ]
  },
  "runtime": {
    "thinking": "high"
  }
}
```

Roller 不使用正则表达式拆分 prompt。即使一条 message 中包含 `## Round 1`、`---` 等文本，它仍然只是一轮。

动态对话同样不使用文本分隔符。Simulator 通过严格 JSON 告诉 Manager 继续发送消息或结束，Manager 保存双方独立轨迹和一份干净的 `conversation.json`。

## 3. 输入依赖

`files.inputs` 支持文件和目录：

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

运行前，Roller 会拒绝符号链接和特殊文件，计算 SHA-256，检查可选的预期 hash，并为同一 run 内的模型构建一致的只读输入快照。OpenClaw 中可通过以下位置读取：

```text
/tmp_workspace/<source>
/roller/task/inputs/<source>
```

Task Bundle 只作为运行期材料存在，任务结束后删除；最终 sample 的 `info.json` 保存 bundle、input manifest 等 digest，而不会复制整套输入依赖，从而保持 run 精简。重新执行 `--resume` 时会从 task 清单重新构建并比较 digest。

## 4. USER.md、MEMORY.md 与系统文件

`files.context` 表示需要由 Harness 语义注入的文件：

```json
"context": [
  {"role": "system", "source": "context/SYSTEM.md", "capture": "full"},
  {"role": "user", "source": "context/USER.md", "capture": "hash"},
  {"role": "memory", "source": "context/MEMORY.md", "capture": "hash"},
  {
    "role": "skill",
    "source": "context/skills/web-research",
    "target": "skills/web-research",
    "capture": "full"
  }
]
```

省略 `target` 时的默认映射：

| role | 默认 target |
| --- | --- |
| `user` | `USER.md` |
| `memory` | `MEMORY.md` |
| `system` | `SYSTEM.md` |
| `soul` | `SOUL.md` |
| `agents` | `AGENTS.md` |
| `skill` / `skills` | `skills/<source-name>` |

OpenClaw adapter 将 context 注入 `/root/<target>` 和 `/tmp_workspace/<target>`，因此 OpenClaw 能按常用位置发现这些文件。

`capture` 控制运行期 manifest 的内容识别方式：

- `full`：内容寻址快照并记录 hash。
- `hash`：记录 hash，但不进入内容寻址 blob。
- `none`：不记录内容 hash，并将 task 标为不可安全 resume。

三种方式都会为当前执行复制内容，但紧凑 run 不永久保存 context 原文。`user` 和 `memory` 默认是 `hash`，其他 role 默认是 `full`。

## 5. Agent 输出和 results

声明示例：

```json
"files": {
  "output_dir": "generated"
}
```

Agent 可以在 `/tmp_workspace/generated/` 下创建任意层级的文件和目录。任务结束时，Roller 会遍历整个目录，并把全部普通文件和空目录复制到 sample 的 `results/`。

Roller 拒绝符号链接和特殊文件；发现不安全输出时 raw trace 仍保留，但 sample 状态为 `error`，可进入重试。每个安全文件的 hash、大小和 MIME 会写入 sample 的 `info.json`。目录为空时不创建 `results/`。

兼容选项 `--collect-path` 会把指定容器路径放在 `results/legacy/`，新 task 应优先把需要保留的内容写入 `output_dir`。

## 6. 自定义 API

`--model-config` 接收 OpenClaw models JSON。任何字符串都可以引用 `${ENV_NAME}`：

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

环境变量只在运行时展开和传入容器。Roller 不把 API key 或环境变量值写进 run；`info.json` 仅记录环境变量名称和移除认证信息、query、fragment 后的 API base URL。

常用 OpenRouter、代理和搜索变量会自动转发。其他变量可用 `--pass-env NAME`；task 的 `runtime.env` 表示强制依赖，缺失时该 task 直接失败。

## 7. Run 存储格式

```text
<output>/<run-name>/
├── info.json                         # 配置、汇总 usage、sample 索引
└── samples/<model>/<index>_<task>/
    ├── trace.jsonl                   # 原始 OpenClaw session JSONL
    ├── trace_ACTF.json               # ACTF_v1.0 trajectory object
    ├── info.json                     # 状态、usage、attempt、版本、digest、输出清单
    ├── conversation.json             # 可选；User Simulator 动态对话
    ├── user_simulator/               # 可选；Simulator 原始/ACTF 轨迹和 info
    └── results/                      # 可选；output_dir 中的全部文件
```

运行期日志、retry trace、请求副本、Task Bundle 和临时输出不会留在最终 run 中。失败时，如果 OpenClaw 已生成原始 trace，`trace.jsonl` 和可转换的 `trace_ACTF.json` 仍会保留。

### `trace_ACTF.json`

ACTF 文件直接保存 trajectory object，而不是额外套 benchmark result envelope：

```json
{
  "schema_version": "ACTF_v1.0",
  "steps": [],
  "started_at": "2026-08-19 10:00:00.000000",
  "finished_at": "2026-08-19 10:00:01.000000"
}
```

每个 assistant message 对应一个 step；紧随其后的 tool results 放入该 step 的 `observation`。后续 user message 写到下一个 step 的 `user_content`。字段布局与 `model_trace/open/pinchbench/.../details` 中的 ACTF_v1.0 保持一致。

## 8. Resume

`--resume` 复用 sample 需要同时满足：

1. `info.json` 中旧状态为 `ok`。
2. `trace.jsonl` 与 `trace_ACTF.json` 都存在。
3. 当前重新构建的 Task Bundle digest 与旧 `info.json` 一致。
4. context 没有导致 `resume_safe=false` 的 `capture=none` 文件。
5. User Simulator task 的 `conversation.json` 和双方 raw/ACTF 轨迹都完整存在。

消息、User Simulator 配置、输入、context、`output_dir` 或 runtime 声明变化都会改变 digest 并重新运行。

## 9. Legacy 输入

Roller 仍兼容 `question`、`workspace/workspace_path`、`persona` 和 `warmup`。legacy workspace 会整体进入运行期输入快照；新任务建议显式声明 `files.inputs`、`files.context` 和 `files.output_dir`。
