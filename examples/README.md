# Roller Examples

[项目首页](../README.md) · [快速上手](../docs/zh/start/getting-started.md) · [Task 格式](../docs/zh/tasks/task-format.md) · [产物说明](../docs/zh/traces/trajectory-outputs.md)

本目录包含配置模板、task 和运行命令。安装、字段定义与轨迹语义由对应专题文档维护。

| 使用目的 | 入口 |
| --- | --- |
| 跑通模型服务、Harness 和轨迹采集 | [基础 Task 清单](#基础-task-清单)：4 条文本、文件、Context 和多轮任务 |
| 检查不同输入形式和 Harness 组合的功能 | [Coverage 功能测试](#coverage-功能测试)：按场景选择任务，附离线校验和结果验收 |

以下命令均从 **Roller 项目根目录** 执行。

## 目录内容

```text
examples/
├── coverage/                       功能覆盖测试套件
│   ├── README.md / README.en.md     中英文使用说明
│   ├── COVERAGE.md / manifest.json  场景索引和机器可读预期
│   ├── all_tasks.json              合并任务模板
│   ├── config/                     测试用模型与环境配置模板
│   ├── cases/                      按场景与 Harness 分组的清单
│   ├── fixtures/                   已包含的输入小文件
│   ├── negative/                   预期拒绝的输入
│   └── suite.py                    列表、校验、分组运行与验收
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

## Coverage 功能测试

[完整使用说明](coverage/README.md) · [English quickstart](coverage/README.en.md) · [场景清单](coverage/COVERAGE.md)

Coverage 用于检查清单解析、文件和 Context 输入、Runtime 配置、产物回收、评分，以及多种 Harness 的执行与 Resume 行为。包含 **1624 条正常任务，分为 406 组，每组 4 条**，另有 **20 条预期拒绝的输入**。其中 1428 条覆盖 51 种场景在 OpenClaw、Codex、Claude Code、Hermes、OpenCode、DeepSeek、Pi 上的执行，196 条覆盖全部 49 种 Task Agent × User Simulator 配对。

任务以加法、读取小文件、写入短答案为主，对话最多两轮。输入文件已随仓库提供，下载后无需另行准备 `Roller_Data`。

### 先做离线校验

需要 Python 3.10+，无需 Docker、API Key 或额外 Python 依赖：

```bash
python examples/coverage/suite.py list
python examples/coverage/suite.py validate
python examples/coverage/validate_merged.py

# 检查一组 4 条任务的 CLI 参数、模型路由和文件打包，不请求模型。
python examples/coverage/suite.py run --case formats/jsonl/codex --dry-run
```

将 `--case formats/jsonl/codex` 改成 `--case '*'` 可遍历全部 406 组。错误输入由校验脚本逐条验证，不作为正常任务提交给模型。

### 配置后运行一组任务

按 [Getting Started](../docs/zh/start/getting-started.md) 准备 Roller 和所选 Harness 的镜像，再复制 Coverage 的 [环境配置模板](coverage/config/.env.example)。下面以 Codex 的 4 条加法任务为例：

```bash
cp examples/coverage/config/.env.example examples/coverage/config/.env
# 编辑 .env，填写实际的 ROLLER_TEST_CODEX_MODEL、服务地址和 API Key。
set -a
source examples/coverage/config/.env
set +a

python examples/coverage/suite.py run --case formats/jsonl/codex \
  -- --codex-api-base "$ROLLER_CODEX_BASE_URL" \
     --codex-api-key-env ROLLER_CODEX_API_KEY

python examples/coverage/suite.py check --case formats/jsonl/codex
```

默认并发 2、单次超时 180 秒、重试 0，结果写入 `runs/coverage/formats__jsonl__codex/`。本地 `.env` 和 `runs/` 已被 Git 忽略。其他 Harness、Simulator 配对及结果检查方式见 [Coverage 使用说明](coverage/README.md)。

### 选择任务入口

| 需要验证的内容 | 使用方式 |
| --- | --- |
| 某个输入场景或 Harness | 从 [场景清单](coverage/COVERAGE.md) 选择组名，用 `suite.py run --case ...` 运行、`suite.py check` 验收；保留原始格式、自定义 key 和特殊 ID 的测试 |
| 一次运行多个 Harness | 用 [merge.py](coverage/merge.py) 选择场景并绑定实际模型，生成单个 JSON 后交给 `roller collect`；完整模板见 [all_tasks.json](coverage/all_tasks.json) |
| 检查合并运行的结果 | 用 [report.py](coverage/report.py) 检查采集状态、答案、产物和轨迹；`--fail-on-check` 可用于 CI |
| 检查已有样本能否 Resume | 用 [audit_resume.py](coverage/audit_resume.py) 做只读审计；实际恢复仍使用原采集命令加 `--resume` |

总 JSON 中的模型名是占位值，合并时会统一原始清单格式和 ID；本地模型配置与任务副本的生成命令见 [混合 Harness 运行说明](coverage/README.md#4-使用一个总-json-运行混合-harness)。其中 28 条可选视觉任务需要模型、服务与图像工具支持；Hermes、OpenCode、DeepSeek 和 Pi 各 4 条视觉任务用于记录暂不支持的能力边界；二进制文件读取测试仅验证文件访问能力。

## 基础样例的采集准备

先按照 [Getting Started](../docs/zh/start/getting-started.md#1-准备运行环境) 安装 Roller，并准备至少一个 Harness 镜像。

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

运行前必须把模板中的 base URL、环境变量和值域改成真实服务支持的配置：

- Responses API：`"api": "openai-responses"`；
- Chat Completions：`"api": "openai-completions"`；
- OpenClaw 的 `--model` 必须与 `providers.<name>` 和 `models[].id` 完全对应；
- Codex 和 Claude Code 不读取 OpenClaw models JSON，分别直接使用 Responses 和 Anthropic
  Messages API；二者的 model、endpoint、Key 环境变量和 effort 都通过 CLI 设置；
- `--codex-config` 和 `--claude-settings` 是可选的 Harness 原生配置入口，不是模型注册表。

完整规则见 [OpenClaw Harness 指南](../docs/zh/harnesses/openclaw-configuration.md#2-模型配置文件)。

## 基础 Task 清单

[`tasks/tasks.jsonl`](tasks/tasks.jsonl) 每行一条 task：

| ID | 输入 | 验证点 |
| --- | --- | --- |
| `01_text_only` | 单轮文本 | 最小 prompt 与文本回复 |
| `02_local_markdown` | `inputs/product-brief.md` | 读取文件并生成 `results/name.txt` |
| `03_user_memory_context` | `USER.md` + `MEMORY.md` | Context 注入和 capture |
| `04_multi_turn` | 两条 `messages` | 同一 Agent session 的预定义多轮 |

这四条 task 可由七种 Harness 运行，是最适合做环境验收的一组数据。

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

`--codex-config` 可以省略；示例文件只设置 Roller 没有覆盖的 text verbosity，并保留一个注释掉的
MCP 模板。第四条 task 会通过 `codex exec resume --last` 延续 session。镜像、API 和 reasoning
细节见 [Codex Harness 指南](../docs/zh/harnesses/codex-configuration.md)。

## OpenCode：原生会话与文件工具

按 [OpenCode 指南](../docs/zh/harnesses/opencode-configuration.md) 构建固定版本镜像，配置流式 Chat Completions 服务：

```bash
python examples/coverage/suite.py run --case data/txt/opencode \
  -- --opencode-api-base "$ROLLER_OPENCODE_BASE_URL" \
     --opencode-api-key-env ROLLER_OPENCODE_API_KEY
python examples/coverage/suite.py check --case data/txt/opencode
```

支持 Agent、Simulator、原生 session 续聊、ACTF/SFT 和 Resume。暂不支持视觉验收。

## DeepSeek Harness：原生 SDK 与持久化 Bash

按 [DeepSeek 指南](../docs/zh/harnesses/deepseek-configuration.md) 构建镜像，配置 DeepSeek 兼容服务：

```bash
python examples/coverage/suite.py run --case data/txt/deepseek \
  --deepseek-model deepseek-v4-flash \
  -- --deepseek-api-base "$ROLLER_DEEPSEEK_BASE_URL" \
     --deepseek-api-key-env ROLLER_DEEPSEEK_API_KEY --thinking off
python examples/coverage/suite.py check --case data/txt/deepseek
```

使用 `--case '*/deepseek' --exclude-case 'vision/*'` 选择 200 条非视觉输入任务。13 种相关 Simulator 配对共 52 条，可用 `--case 'simulators/pair/deepseek--*' --case 'simulators/pair/*--deepseek'` 选择。

## Pi Agent Harness：原生会话与文件工具

按 [Pi 指南](../docs/zh/harnesses/pi-configuration.md) 构建镜像，配置支持工具调用的流式 Chat Completions 服务：

```bash
python examples/coverage/suite.py run --case data/txt/pi \
  --pi-model "$ROLLER_TEST_PI_MODEL" \
  -- --pi-api-base "$ROLLER_PI_BASE_URL" \
     --pi-api-key-env ROLLER_PI_API_KEY --thinking off
python examples/coverage/suite.py check --case data/txt/pi
```

使用 `--case '*/pi' --exclude-case 'vision/*'` 选择 200 条非视觉输入任务。涉及 Pi 的 13 种 Simulator 配对共 52 条，可用 `--case 'simulators/pair/pi--*' --case 'simulators/pair/*--pi'` 选择。多轮保留会话历史和文件，Bash 每次启动独立 Shell。

## Hermes：文本、文件与多轮任务

按 [Hermes 指南](../docs/zh/harnesses/hermes-configuration.md) 构建镜像，配置 Chat Completions API 后运行：

```bash
python examples/coverage/suite.py run --case data/txt/hermes \
  --hermes-model your-model-id \
  -- --hermes-api-base "$OPENAI_BASE_URL" --hermes-api-key-env OPENAI_API_KEY
python examples/coverage/suite.py check --case data/txt/hermes
```

Hermes 也可作为 User Simulator。当前固定启用容器内 `terminal`、`file` 工具，支持原生历史恢复、ACTF/SFT 和 Resume；暂不支持视觉工具。

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

`--claude-settings` 可以省略；示例只关闭 commit co-author 标记，不改变 permission mode、thinking
或工具行为。第四条 task 会通过 `claude --continue` 延续 session。镜像、endpoint 和 thinking
配置见 [Claude Code Harness 指南](../docs/zh/harnesses/claude-code-configuration.md)。

## 进阶案例

### 动态 User Simulator

[`tasks/user-simulator.jsonl`](tasks/user-simulator.jsonl) 让另一个 OpenClaw Harness 扮演 HR，根据任务 Agent 当前草稿逐轮补充招聘需求：

```bash
roller collect \
  --tasks examples/tasks/user-simulator.jsonl \
  --model-config examples/config/openclaw/models.json \
  --parallel 1 \
  --timeout 900 \
  --output runs \
  --run-name examples-user-simulator
```

Task 已内置 `model: my-openai-proxy/my-model`。动态对话会额外生成 `conversation.json` 和 `user_simulator/` 轨迹；Task Agent 与 Simulator 可在各自字段中选择 OpenClaw、Codex、Claude Code、Hermes、OpenCode、DeepSeek 或 Pi。详见 [User Simulator](../docs/zh/tasks/user-simulator.md)。

### 多模态输入

使用 [`config/openclaw/models-multimodal.json`](config/openclaw/models-multimodal.json)，并在 task 的 `files.inputs` 中声明本地图像：

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

模型配置声明能力不会让文本后端自动支持图像；服务与模型本身也必须支持对应协议下的视觉输入。

## 验收与查看

以 OpenClaw run 为例：

```bash
roller view runs/examples-openclaw
```

重点检查：

- `02_local_markdown` sample 是否包含 `results/name.txt`；
- `04_multi_turn` 是否包含两条 user message；
- 每个成功 sample 是否有 `trace.jsonl`、`trace_ACTF.json`、`trace_SFT.json` 和 `info.json`；
- run 根目录 `sft.jsonl` 是否按 request 顺序汇总成功样本。

下一步：编写自己的 [Task](../docs/zh/tasks/task-format.md)，或阅读 [轨迹产出文件指南](../docs/zh/traces/trajectory-outputs.md) 选择训练、调试与审计所需的文件。
