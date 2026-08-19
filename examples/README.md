# Roller Examples

这个目录提供一组可以直接用于 `roller collect` 的示例，包括四种任务输入方式和一个自定义 OpenClaw API provider 配置。

## 文件结构

```text
examples/
├── tasks.jsonl
├── openclaw-models.json
├── api.env.example
└── task_files/
    ├── 02_local_markdown/
    │   └── product-brief.md
    └── 03_user_memory_context/
        ├── USER.md
        └── MEMORY.md
```

`tasks.jsonl` 中的四个任务分别展示：

1. `01_text_only`：只有文本 prompt，没有本地依赖文件。
2. `02_local_markdown`：将本地 Markdown 文件作为只读 input。
3. `03_user_memory_context`：通过 context 注入 `USER.md` 和 `MEMORY.md`。
4. `04_multi_turn`：使用三个显式 user messages，在同一个 OpenClaw session 中执行三轮。

四条任务都声明了 `"file_root":".."`。`file_root` 自身相对于 `tasks.jsonl`，因此它指向 Roller 项目根目录；所有 `source` 统一写成 `examples/task_files/...` 这种项目根目录相对路径。从其他工作目录启动 Roller 也不会改变解析结果。

## 先做 dry-run

下面的命令会解析四个任务、检查输入文件、构建 Task Bundle 并打印执行计划，但不会启动 Docker：

```bash
roller collect \
  --tasks examples/tasks.jsonl \
  --model my-openai-proxy/my-model \
  --parallel 2 \
  --dry-run
```

计划中的 turn 数应依次为 `1、1、1、3`。

## 自定义 API 示例

[openclaw-models.json](openclaw-models.json) 声明了一个兼容 OpenAI Responses API 的 OpenClaw provider：

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

先根据 [api.env.example](api.env.example) 设置宿主机环境变量：

```bash
export MY_PROXY_BASE_URL=https://api.example.com/v1
export MY_PROXY_API_KEY=your-api-key
```

然后启动四个示例任务：

```bash
roller collect \
  --tasks examples/tasks.jsonl \
  --model my-openai-proxy/my-model \
  --model-config examples/openclaw-models.json \
  --docker-image your-openclaw-image:tag \
  --parallel 2 \
  --output runs \
  --run-name examples_custom_api
```

`${MY_PROXY_BASE_URL}` 和 `${MY_PROXY_API_KEY}` 只在运行时展开。API key 不会写入 run metadata，provenance 只记录脱敏后的 base URL 和环境变量名称。

## 输入输出路径

- 宿主机依赖：相对于 Roller 根目录，例如 `examples/task_files/02_local_markdown/product-brief.md`。
- Agent prompt 中的普通 input：使用 `inputs/<target>`。
- Agent prompt 中的输出：使用 `outputs/<path>`。
- `files.outputs[].path`：相对于 outputs 根目录，因此只写 `answer.md`，不重复添加 `outputs/`。
- `USER.md`、`MEMORY.md`：注入 `/root/` 和 `/tmp_workspace/`。

OpenClaw agent 以 `/tmp_workspace` 为工作目录，因此示例中的 `inputs/...` 和 `outputs/...` 都是有效的相对路径。对应的稳定绝对路径分别是 `/roller/task/inputs/...` 和 `/roller/task/outputs/...`。

详细的快照、注入和归档规则见 [Task Bundle 与文件存储设计](../docs/task-bundle-and-storage.md)。
