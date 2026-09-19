# Examples

仓库的 [`examples/`](https://github.com/Liuziyu77/Roller/tree/main/examples) 目录包含配置模板、task 和运行命令。安装步骤、task 字段和轨迹格式见对应指南。

## 目录内容

```text
examples/
├── env/
│   └── api.env.example
├── config/
│   ├── openclaw/
│   │   ├── models.json
│   │   ├── models-reasoning.json
│   │   └── models-multimodal.json
│   ├── codex/
│   │   └── config.toml.example
│   └── claude-code/
│       └── settings.json.example
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

## 一次性准备

先按照 [快速上手](getting-started.md#1-准备运行环境) 安装 Roller，并准备至少一个 Harness 镜像。

复制并编辑本地 API 环境文件：

```bash
cp examples/env/api.env.example examples/env/api.env

set -a
source examples/env/api.env
set +a
```

`examples/env/api.env` 已被 Git 忽略。不要把真实 Key 写进 JSON、task 或命令行。

## Harness 配置模板

| Harness | 文件 | 用途 |
| --- | --- | --- |
| OpenClaw | `config/openclaw/models.json` | 最小文本模型；model ref 为 `my-openai-proxy/my-model` |
| OpenClaw | `config/openclaw/models-reasoning.json` | GPT Responses 与 Chat Completions reasoning 模型 |
| OpenClaw | `config/openclaw/models-multimodal.json` | 声明 `text` + `image` 输入能力 |
| Codex | `config/codex/config.toml.example` | 可选的原生 `config.toml`；展示 verbosity 和自定义 MCP 写法 |
| Claude Code | `config/claude-code/settings.json.example` | 可选的原生 `settings.json`，不包含 Key、权限放宽或 hook |

运行前，按模型服务的要求填写模板中的 base URL、环境变量和参数值：

- Responses API：`"api": "openai-responses"`
- Chat Completions：`"api": "openai-completions"`
- OpenClaw 的 `--model` 必须与 `providers.<name>` 和 `models[].id` 完全对应
- Codex 和 Claude Code 不读取 OpenClaw models JSON，分别直接使用 Responses 和 Anthropic Messages API；二者的 model、endpoint、Key 环境变量和 effort 都通过 CLI 设置
- `--codex-config` 和 `--claude-settings` 是可选的 Harness 原生配置入口，不是模型注册表

完整规则见 [OpenClaw Harness 指南](../harnesses/openclaw-configuration.md#2-模型配置文件)。

## 基础 Task 清单

[`tasks/tasks.jsonl`](https://github.com/Liuziyu77/Roller/blob/main/examples/tasks/tasks.jsonl) 每行一条 task：

| ID | 输入 | 验证点 |
| --- | --- | --- |
| `01_text_only` | 单轮文本 | 最小 prompt 与文本回复 |
| `02_local_markdown` | `inputs/product-brief.md` | 读取文件并生成 `results/name.txt` |
| `03_user_memory_context` | `USER.md` + `MEMORY.md` | Context 注入和 capture |
| `04_multi_turn` | 两条 `messages` | 同一 Agent session 的预定义多轮 |

这四条 task 可由七种 Harness 运行，可用于检查运行环境。

## OpenClaw：基础采集

### 1. Dry-run

```bash
roller collect \
  --tasks examples/tasks/tasks.jsonl \
  --model my-openai-proxy/my-model \
  --model-config examples/config/openclaw/models.json \
  --parallel 4 \
  --dry-run
```

计划中的 turn 数应依次为 `1、1、1、2`。

### 2. 正式运行

```bash
roller collect \
  --tasks examples/tasks/tasks.jsonl \
  --model my-openai-proxy/my-model \
  --model-config examples/config/openclaw/models.json \
  --parallel 4 \
  --timeout 600 \
  --output runs \
  --run-name examples-openclaw
```

## Codex：运行相同 Task

服务必须支持 Responses protocol，且本地已构建 `roller-codex:0.149.0`：

```bash
roller collect \
  --harness codex \
  --tasks examples/tasks/tasks.jsonl \
  --model gpt-5.6-sol \
  --api-base "$ROLLER_CODEX_BASE_URL" \
  --api-key-env ROLLER_CODEX_API_KEY \
  --codex-config examples/config/codex/config.toml.example \
  --thinking high \
  --reasoning-summary detailed \
  --parallel 2 \
  --timeout 900 \
  --output runs \
  --run-name examples-codex
```

`--codex-config` 可以省略；示例文件只设置 Roller 没有覆盖的 text verbosity，并保留一个注释掉的 MCP 模板。第四条 task 会通过 `codex exec resume --last` 延续 session。镜像、API 和 reasoning 细节见 [Codex Harness 指南](../harnesses/codex-configuration.md)。

## Claude Code：运行相同 Task

服务必须支持 Anthropic `/v1/messages`，且本地已构建 `roller-claude-code:2.1.222`：

```bash
roller collect \
  --harness claude-code \
  --tasks examples/tasks/tasks.jsonl \
  --model claude-sonnet-4-6-thinking \
  --api-base "$ROLLER_ANTHROPIC_BASE_URL" \
  --api-key-env ROLLER_ANTHROPIC_API_KEY \
  --claude-settings examples/config/claude-code/settings.json.example \
  --thinking high \
  --parallel 2 \
  --timeout 900 \
  --output runs \
  --run-name examples-claude-code
```

`--claude-settings` 可以省略；示例只关闭 commit co-author 标记，不改变 permission mode、thinking 或工具行为。第四条 task 会通过 `claude --continue` 延续 session。镜像、endpoint、thinking 见 [Claude Code Harness 指南](../harnesses/claude-code-configuration.md)。

## 进阶案例

### 动态 User Simulator

[`tasks/user-simulator.jsonl`](https://github.com/Liuziyu77/Roller/blob/main/examples/tasks/user-simulator.jsonl) 让另一个 OpenClaw Harness 扮演 HR，根据任务 Agent 当前草稿逐轮补充招聘需求：

```bash
roller collect \
  --tasks examples/tasks/user-simulator.jsonl \
  --model-config examples/config/openclaw/models.json \
  --parallel 1 \
  --timeout 900 \
  --output runs \
  --run-name examples-user-simulator
```

Task 已内置 `model: my-openai-proxy/my-model`。动态对话会额外生成 `conversation.json` 和 `user_simulator/` 轨迹；Task Agent 与 Simulator 可在各自字段中选择 OpenClaw、Codex、Claude Code、Hermes、OpenCode、DeepSeek 或 Pi。详见 [User Simulator](../tasks/user-simulator.md)。

### 多模态输入

使用 [`config/openclaw/models-multimodal.json`](https://github.com/Liuziyu77/Roller/blob/main/examples/config/openclaw/models-multimodal.json)，并在 task 的 `files.inputs` 中声明本地图像：

```json
{
  "schema_version": "roller.task/v1",
  "id": "inspect_image",
  "file_root": "files/inspect_image",
  "prompt": "查看 inputs/example.png，把描述保存到 outputs/description.md。",
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
  --model-config examples/config/openclaw/models-multimodal.json \
  --parallel 2 \
  --output runs \
  --run-name examples-vision
```

模型配置用于声明图像能力；服务与模型也必须支持对应协议下的视觉输入。

## 验收与查看

以 OpenClaw run 为例：

```bash
roller view runs/examples-openclaw
```

重点检查：

- `02_local_markdown` sample 是否包含 `results/name.txt`
- `04_multi_turn` 是否包含两条 user message
- 每个成功 sample 是否有 `trace.jsonl`、`trace_ACTF.json`、`trace_SFT.json` 和 `info.json`
- run 根目录 `sft.jsonl` 是否按 request 顺序汇总成功样本

编写任务参见 [Task](../tasks/task-format.md)；选择训练、调试与审计所需的文件参见 [轨迹产出文件指南](../traces/trajectory-outputs.md)。
