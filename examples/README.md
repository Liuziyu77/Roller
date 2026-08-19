# Roller Examples

`tasks.jsonl` 提供四个尽量短、便于快速验证的任务：

1. `01_text_only`：纯文本计算。
2. `02_local_markdown`：读取本地 Markdown，并保留一个结果文件。
3. `03_user_memory_context`：注入 `USER.md` 与 `MEMORY.md`。
4. `04_multi_turn`：两个显式 user messages，共用一个 OpenClaw session。

所有宿主机文件路径都统一相对于 Roller 项目根目录。task 使用 `"file_root":".."`，因为这个值从 `examples/tasks.jsonl` 所在目录开始解析。

```text
examples/
├── tasks.jsonl
├── openclaw-models.json
├── api.env.example
└── task_files/
    ├── 02_local_markdown/product-brief.md
    └── 03_user_memory_context/
        ├── USER.md
        └── MEMORY.md
```

## 检查任务

```bash
roller collect \
  --tasks examples/tasks.jsonl \
  --model my-openai-proxy/my-model \
  --parallel 4 \
  --dry-run
```

四个任务的 turn 数应为 `1、1、1、2`。第二个任务声明 `files.output_dir=quick_results` 和 `files.outputs=name.txt`，所以只有它的 sample 会产生 `results/name.txt`。

## 自定义 API 示例

`openclaw-models.json` 使用环境变量设置 OpenAI Responses 兼容 provider：

```bash
export MY_PROXY_BASE_URL=https://api.example.com/v1
export MY_PROXY_API_KEY=your-api-key

roller collect \
  --tasks examples/tasks.jsonl \
  --model my-openai-proxy/my-model \
  --model-config examples/openclaw-models.json \
  --docker-image your-openclaw-image:tag \
  --parallel 4 \
  --output runs \
  --run-name examples_custom_api
```

`--output` 和 `--run-name` 决定宿主机结果目录。API key 只在运行时展开，不会写入 Roller 仓库或 run metadata。

## 路径对应关系

| 用途 | 路径 |
| --- | --- |
| task 的本地 `source` | Roller 根目录相对路径，例如 `examples/task_files/...` |
| Agent 输入 | `inputs/<target>` |
| Agent 自定义输出 | `<files.output_dir>/<files.outputs[].path>` |
| 稳定输出别名 | `/roller/task/outputs/<path>` |
| 系统文件 | `/root/<target>` 和 `/tmp_workspace/<target>` |

完整规则见 [Task 文件与存储设计](../docs/task-bundle-and-storage.md)。
