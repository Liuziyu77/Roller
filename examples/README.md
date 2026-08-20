# Roller Examples

这个目录提供一套可以直接 dry-run、替换模型后即可采集的最小案例。更完整的字段和运行机制放在 [Docs](../docs/README.md)，这里专注于“准备什么”和“执行什么”。

## 1. 目录结构

```text
examples/
├── env/
│   └── api.env.example
├── json/
│   ├── openclaw-models.json
│   └── openclaw-models-multimodal.json
└── tasks/
    ├── tasks.jsonl
    ├── user-simulator.jsonl
    └── files/
        ├── 02_local_markdown/
        │   └── inputs/product-brief.md
        └── 03_user_memory_context/
            └── context/
                ├── USER.md
                └── MEMORY.md
```

- `env/`：只放环境变量模板，不提交真实 Key。
- `json/`：OpenClaw provider 与模型定义。
- `tasks/`：Task 清单，以及它们各自依赖的文件包。

## 2. 准备 API

复制模板，填入自己的 OpenAI-compatible API base 与 Key：

```bash
cp examples/env/api.env.example examples/env/api.env
```

```dotenv
MY_PROXY_BASE_URL=https://api.example.com/v1
MY_PROXY_API_KEY=replace-with-your-api-key
BRAVE_API_KEY=replace-with-your-brave-api-key
```

加载到当前 shell：

```bash
set -a
source examples/env/api.env
set +a
```

`examples/env/api.env` 已被 Git 忽略。模型 JSON 通过 `${MY_PROXY_BASE_URL}` 和 `${MY_PROXY_API_KEY}` 读取值，真实凭证不需要进入仓库。

自定义 API adapter、搜索变量与远程 Docker 设置见 [OpenClaw、模型与 API 配置](../docs/openclaw-configuration.md)。

## 3. 配置模型

文本模型模板 [`json/openclaw-models.json`](json/openclaw-models.json) 定义：

```text
my-openai-proxy/my-model
```

在第一次运行前，把 JSON 中的 model `id`、`name` 和 `api` 改成服务端真实支持的值。示例默认使用 `openai-responses`；只支持 Chat Completions 的后端应改为 `openai-completions`。

多模态模板 [`json/openclaw-models-multimodal.json`](json/openclaw-models-multimodal.json) 另外声明：

```json
{"input": ["text", "image"]}
```

它代表视觉输入/图像理解模型，不是文生图模型。后端本身也必须支持图像输入。

## 4. 四个基础案例

[`tasks/tasks.jsonl`](tasks/tasks.jsonl) 每行一条 task：

| Task ID | 输入方式 | 预期行为 |
| --- | --- | --- |
| `01_text_only` | 单轮文本 | 计算 `2+3`，只回复结果 |
| `02_local_markdown` | 本地 Markdown | 读取项目名称，写入 `outputs/name.txt` |
| `03_user_memory_context` | `USER.md` + `MEMORY.md` | 根据注入的偏好与记忆回答问题 |
| `04_multi_turn` | 两条 `messages` | 在同一 session 中记住并复述校验词 |

文件案例的对应关系：

```text
tasks.jsonl
  └─ file_root: files/02_local_markdown
       ├─ inputs/product-brief.md     # Agent 只读
       └─ outputs/                    # 运行时创建，Agent 可写
```

输入不会被重命名；Agent 生成的 `outputs/name.txt` 会出现在该 sample 的 `results/name.txt`。宿主机 `file_root` 不会被 Agent 回写。

### 4.1 先检查

在仓库根目录执行：

```bash
roller collect \
  --tasks examples/tasks/tasks.jsonl \
  --model my-openai-proxy/my-model \
  --model-config examples/json/openclaw-models.json \
  --parallel 4 \
  --dry-run
```

Dry-run 会解析四条 task、检查所有依赖文件和路径，但不会启动 Docker，也不会创建 run。计划中的 turn 数应为 `1、1、1、2`。

### 4.2 并行执行

```bash
roller collect \
  --tasks examples/tasks/tasks.jsonl \
  --model my-openai-proxy/my-model \
  --model-config examples/json/openclaw-models.json \
  --docker-image your-openclaw-image:tag \
  --parallel 4 \
  --timeout 600 \
  --output runs \
  --run-name examples_text
```

运行完成后检查：

```text
runs/examples_text/
├── info.json
└── samples/<model-safe-name>/
    ├── 00000000_01_text_only/
    ├── 00000001_02_local_markdown/
    │   └── results/name.txt
    ├── 00000002_03_user_memory_context/
    └── 00000003_04_multi_turn/
```

Roller 会把 model ref 转成可用于文件系统的安全目录名；包含 `/` 等字符时会附加短 digest，因此程序应从 run `info.json` 的 sample 索引定位目录。每个 sample 的核心文件是 `trace.jsonl`、`trace_ACTF.json` 和 `info.json`。结果目录的完整结构见 [文件、Task Bundle 与结果存储](../docs/task-bundle-and-storage.md#6-run-与-sample)。

## 5. 动态 User Simulator 案例

[`tasks/user-simulator.jsonl`](tasks/user-simulator.jsonl) 中，另一个 OpenClaw Harness 扮演 HR。它不会使用预定义 `messages`，而是根据任务 Agent 当前版本的招聘广告逐轮补充要求，最终要求文件写入 `outputs/recruitment-ad.md`。

先做检查：

```bash
roller collect \
  --tasks examples/tasks/user-simulator.jsonl \
  --model-config examples/json/openclaw-models.json \
  --parallel 1 \
  --dry-run
```

实际执行：

```bash
roller collect \
  --tasks examples/tasks/user-simulator.jsonl \
  --model-config examples/json/openclaw-models.json \
  --docker-image your-openclaw-image:tag \
  --parallel 1 \
  --timeout 900 \
  --output runs \
  --run-name example_user_simulator
```

示例省略了 `user_simulator.model`，所以 Simulator 跟随任务 Agent 使用 `my-openai-proxy/my-model`。需要模型分工时，在 `user_simulator` 中指定同一 models JSON 中的另一个 model ref。

这个案例会多保存 `conversation.json` 和 `user_simulator/` 轨迹。调度协议、instruction 写法、终止原因和成本统计见 [User Simulator 与动态对话](../docs/user-simulator.md)。

## 6. 使用多模态模型

准备一条包含本地图像输入的 task，然后使用多模态配置：

```json
{
  "schema_version": "roller.task/v1",
  "id": "inspect_image",
  "file_root": "files/inspect_image",
  "prompt": "查看 inputs/example.png，描述画面并把结果保存到 outputs/description.md。",
  "files": {
    "inputs": ["inputs/example.png"],
    "output_dir": "outputs"
  }
}
```

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

Task 中只声明本地文件；模型是否实际读取图像取决于 OpenClaw 版本、模型能力和 provider adapter。Roller 不会把普通文本模型自动变成多模态模型。

## 7. 下一步

- 写自己的 task：[Task 格式参考](../docs/task-format.md)
- 理解输入、Context 和 `results/`：[文件与存储](../docs/task-bundle-and-storage.md)
- 连接自定义 API：[OpenClaw 配置](../docs/openclaw-configuration.md)
- 设计动态用户：[User Simulator](../docs/user-simulator.md)
