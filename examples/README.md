# Roller Examples

示例按用途分成三个目录，避免把模型配置、环境变量和 task 数据混在一起：

```text
examples/
├── json/
│   ├── openclaw-models.json          # 普通文本模型
│   └── openclaw-models-multimodal.json # 多模态模型
├── env/
│   └── api.env.example               # API/Search 环境变量模板
└── tasks/
    ├── tasks.jsonl                   # 四类轻量 task
    └── files/                        # task 的输入与 context 文件
        ├── 02_local_markdown/
        │   └── inputs/
        │       └── product-brief.md
        └── 03_user_memory_context/
            └── context/
                ├── USER.md
                └── MEMORY.md
```

## 1. 准备环境变量

[`env/api.env.example`](env/api.env.example) 包含：

| 变量 | 用途 |
| --- | --- |
| `MY_PROXY_BASE_URL` | OpenAI-compatible API base URL |
| `MY_PROXY_API_KEY` | 模型 API key |
| `BRAVE_API_KEY` | OpenClaw Brave Search API key |

复制为已被 `.gitignore` 排除的本地文件，替换占位值后加载：

```bash
cp examples/env/api.env.example examples/env/api.env

# 编辑 examples/env/api.env 后执行
set -a
source examples/env/api.env
set +a
```

> [!IMPORTANT]
> 不要把真实 API key 写入 `api.env.example`、JSON 配置或 task 文件。Roller 只在运行时读取环境变量。

## 2. 选择普通模型或多模态模型

| 配置文件 | Model ref | 用途 |
| --- | --- | --- |
| [`openclaw-models.json`](json/openclaw-models.json) | `my-openai-proxy/my-model` | 普通文本和工具调用任务 |
| [`openclaw-models-multimodal.json`](json/openclaw-models-multimodal.json) | `my-openai-proxy/my-multimodal-model` | 原生图像输入和图像理解任务 |

OpenClaw 通过 model entry 的 `input` 判断是否可向模型发送图像。这里的 image model 指视觉输入/图像理解模型，不是文生图模型；后者使用 OpenClaw 独立的 image-generation 配置。

示例使用 `"api":"openai-responses"`。如果代理只实现 `/v1/chat/completions`，请改成 `"openai-completions"`。更多字段见 [OpenClaw custom provider 文档](https://docs.openclaw.ai/gateway/config-tools#custom-providers-and-base-urls)。

## 3. 检查四类 task

[`tasks/tasks.jsonl`](tasks/tasks.jsonl) 包含：

1. `01_text_only`：纯文本计算。
2. `02_local_markdown`：读取本地 Markdown，并把生成文件写入 `outputs/`。
3. `03_user_memory_context`：注入 `USER.md` 与 `MEMORY.md`。
4. `04_multi_turn`：两个显式 user messages，共用一个 OpenClaw session。

先执行 dry-run：

```bash
roller collect \
  --tasks examples/tasks/tasks.jsonl \
  --model my-openai-proxy/my-model \
  --model-config examples/json/openclaw-models.json \
  --parallel 4 \
  --dry-run
```

计划中的 turn 数应为 `1、1、1、2`。第二个 task 声明了 `files.output_dir=outputs`；Agent 写入该目录的全部文件都会保存到 sample 的 `results/`。

## 4. 运行示例

### 文本模型 + Brave Search

```bash
roller collect \
  --tasks examples/tasks/tasks.jsonl \
  --model my-openai-proxy/my-model \
  --model-config examples/json/openclaw-models.json \
  --search-provider brave \
  --docker-image your-openclaw-image:tag \
  --parallel 4 \
  --output runs \
  --run-name examples_text
```

`BRAVE_API_KEY` 属于 Roller 默认传入 OpenClaw 的常用环境变量；只有任务实际进行网页搜索时才会使用它。

### 视觉模型

```bash
roller collect \
  --tasks your-image-tasks.jsonl \
  --model my-openai-proxy/my-multimodal-model \
  --model-config examples/json/openclaw-models-multimodal.json \
  --docker-image your-openclaw-image:tag \
  --parallel 2 \
  --output runs \
  --run-name examples_vision
```

视觉 provider 必须真实支持所选 API adapter 的图像输入；`input: ["text", "image"]` 只是向 OpenClaw 声明模型能力，不会为不支持图像的后端增加该能力。

## 路径对应关系

每个带文件的 task 都有独立 `file_root`。`inputs` 和 `output_dir` 都使用该目录下的相对路径，输入进入 Agent 后不会重命名：

| 用途 | 示例路径 |
| --- | --- |
| task 清单 | `examples/tasks/tasks.jsonl` |
| task 的 `file_root` | `examples/tasks/files/02_local_markdown/` |
| task 本地 source | `inputs/product-brief.md` |
| Agent 只读输入 | `inputs/product-brief.md` |
| Agent 自定义输出 | `outputs/name.txt` |
| sample 保留结果 | `results/name.txt` |
| 系统文件 | `/root/<target>` 和 `/tmp_workspace/<target>` |

完整的输入快照、context 注入、输出回收和归档规则见 [Task 文件与存储设计](../docs/task-bundle-and-storage.md)。
