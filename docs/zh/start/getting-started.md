# 快速上手：从安装到第一条轨迹

[文档中心](../index.md) · [示例目录](examples.md) · [CLI 参数](cli-reference.md) · [产物说明](../traces/trajectory-outputs.md)

本页覆盖环境准备、API 配置、dry-run、正式采集、结果验收和 Viewer。最终 run 包含 native raw、ACTF、SFT 与运行信息。

## 1. 准备运行环境

需要：

- Python 3.10+；
- Docker CLI；
- CLI 能连接的 Docker daemon；
- 至少一个 Harness 镜像；
- 与该 Harness 协议匹配的模型 API。

在仓库根目录安装 Roller：

```bash
python3 -m pip install -e .
roller --version
docker version
```

`docker version` 必须同时返回 Client 和 Server 信息。只有 Docker CLI、没有可连接的 daemon 时，Roller 无法启动 rollout。

## 2. 选择 Harness 与镜像

OpenClaw 同时兼容 Responses 与 Chat Completions，适合作为首次环境验证。动态 User Simulator 可与 OpenClaw、Codex 或 Claude Code 任意组合。

| Harness | API 要求 | 默认镜像 | 准备方式 |
| --- | --- | --- | --- |
| OpenClaw | OpenAI Responses 或 Chat Completions | `ghcr.io/openclaw/openclaw:2026.7.1-2` | 直接 pull/load |
| Codex | Responses streaming | `roller-codex:0.149.0` | 用仓库 Dockerfile 构建 |
| Claude Code | Anthropic `/v1/messages` | `roller-claude-code:2.1.222` | 用仓库 Dockerfile 构建 |

CoT 配置按模型返回形式选择：

- GPT reasoning 系列（如 `gpt-5.6-sol`）：使用 OpenClaw/Codex 的 `external` 或 `external_always` 获取显式 CoT；
- Claude 系列（如 Claude Opus 4.8，示例 ID `claude-opus-4-8`）：使用 Claude Code signature recovery；
- Qwen、GLM 等开源模型：保持默认 `--keep-thinking`。服务返回的明文 reasoning 会进入 ACTF/SFT `reasoning_content`，无需 external thinking。

如果服务只返回 reasoning token 数而没有明文 reasoning，第三种方式不会产生 CoT 文本。

### OpenClaw

在线拉取和离线加载二选一：

```bash
docker pull --platform linux/amd64 ghcr.io/openclaw/openclaw:2026.7.1-2

# 或
docker load -i /path/to/openclaw-2026.7.1-2-linux-amd64.tar.gz
```

### Codex

```bash
docker pull --platform linux/amd64 node:22-bookworm-slim
docker build -f docker/codex/Dockerfile -t roller-codex:0.149.0 .
docker run --rm roller-codex:0.149.0 codex --version
```

已有 Node 22 离线归档时，先执行：

```bash
docker load -i /path/to/node-22-bookworm-slim-linux-amd64.tar.gz
```

完整构建参数和跨机器导出方法见 [Codex Harness 指南](../harnesses/codex-configuration.md#2-构建固定版本镜像)。

### Claude Code

Claude Code 可以复用与 Codex 相同的 Node 22 基础镜像：

```bash
docker build -f docker/claude-code/Dockerfile -t roller-claude-code:2.1.222 .
docker run --rm roller-claude-code:2.1.222 claude --version
```

完整构建参数见 [Claude Code Harness 指南](../harnesses/claude-code-configuration.md#2-构建固定版本镜像)。

如果使用自定义 tag，可通过 `--docker-image` 指定，或设置 `ROLLER_OPENCLAW_IMAGE`、`ROLLER_CODEX_IMAGE`、`ROLLER_CLAUDE_CODE_IMAGE` 中对应的环境变量。

## 3. 准备 API 配置

复制本地环境变量模板：

```bash
cp examples/env/api.env.example examples/env/api.env
```

编辑后加载到当前 shell：

```bash
set -a
source examples/env/api.env
set +a
```

`examples/env/*.env` 已被 Git 忽略，但仍不要在 shell 历史、task、模型 JSON 或文档中直接写真实 Key。

### OpenClaw 配置

OpenClaw 通过 `--model-config` 读取 provider 与 model：

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

- `mode: "merge"` 表示把这里声明的 provider 合并进 OpenClaw 现有模型配置，而不是用这份文件替换整个配置集合；
- `--model` 使用 `provider/model-id`，上例对应 `my-openai-proxy/my-model`；
- `${ENV_NAME}` 由 Roller 在宿主机展开，缺失或空值会在 rollout 前报错；
- Responses 服务使用 `openai-responses`；只有 `/v1/chat/completions` 的服务使用 `openai-completions`；
- `baseUrl` 通常写到 `/v1`，不要把 `/chat/completions` 当作 base URL。

仓库模板位于 [`examples/config/openclaw/models.json`](../../../examples/config/openclaw/models.json)。Reasoning、多模态和 Harness 原生配置模板见 [Examples](examples.md#harness-配置模板)。

### Codex 配置

Codex 不读取 OpenClaw model JSON。它直接使用：

```bash
--api-base "$ROLLER_CODEX_BASE_URL" \
--api-key-env ROLLER_CODEX_API_KEY
```

API 必须支持 Responses streaming、reasoning 与 tool call/result。Chat Completions 成功不代表 Codex 可以使用该服务。

### Claude Code 配置

Claude Code 也不读取 OpenClaw model JSON。它直接使用：

```bash
--api-base "$ROLLER_ANTHROPIC_BASE_URL" \
--api-key-env ROLLER_ANTHROPIC_API_KEY
```

`--api-base` 可以是 origin、末尾带 `/v1` 的 base，或完整 `/v1/messages` endpoint；Roller 会规范化后交给 Claude Code。

## 4. 认识示例 Task

[`examples/tasks/tasks.jsonl`](../../../examples/tasks/tasks.jsonl) 包含四条由简到繁的 task：

| ID | 展示内容 |
| --- | --- |
| `01_text_only` | 单轮纯文本 |
| `02_local_markdown` | 读取输入文件并写入 `outputs/` |
| `03_user_memory_context` | 注入 `USER.md` 与 `MEMORY.md` |
| `04_multi_turn` | 两条预定义 user message 共用一个 session |

最小 task 只有三个字段：

```json
{"schema_version":"roller.task/v1","id":"01_text_only","prompt":"计算 2+3，只回复数字。"}
```

`--model` 可以在命令行统一指定；若未传，则每条 task 必须有自己的 `model` 字段。详细 schema 见 [Task 格式参考](../tasks/task-format.md)。

## 5. Dry-run

以 OpenClaw 为例：

```bash
roller collect \
  --tasks examples/tasks/tasks.jsonl \
  --model my-openai-proxy/my-model \
  --model-config examples/config/openclaw/models.json \
  --parallel 4 \
  --dry-run
```

Dry-run 会：

- 读取并规范化 JSON/JSONL task；
- 检查重复 ID、对话模式和 Harness 限制；
- 解析 input、Context 与 output 路径；
- 执行路径冲突、符号链接、文件大小和内容摘要检查；
- 打印 task × model 展开的 rollout 计划。

它不会检查 Docker daemon、镜像是否存在或模型 API 是否可用，也不会创建 run 目录。

## 6. 正式采集

```bash
roller collect \
  --tasks examples/tasks/tasks.jsonl \
  --model my-openai-proxy/my-model \
  --model-config examples/config/openclaw/models.json \
  --parallel 4 \
  --retries 1 \
  --timeout 600 \
  --output runs \
  --run-name quickstart
```

参数含义：

| 参数 | 含义 |
| --- | --- |
| `--parallel 4` | 同时运行最多 4 个 rollout |
| `--retries 1` | 首次失败后最多再尝试 1 次 |
| `--timeout 600` | 每个 rollout 的总超时为 600 秒 |
| `--output runs` | 所有 run 的宿主机根目录 |
| `--run-name quickstart` | 本次 run 保存为 `runs/quickstart` |

同名目录已存在时，Roller 默认拒绝覆盖。原配置继续运行应增加 `--resume`；新实验应改用新的 `--run-name`。

## 7. 验收结果

成功后先检查 run 根目录：

```text
runs/quickstart/
├── info.json
├── lock.json
├── sft.jsonl
└── samples/
    └── <model>/
        └── <index>_<task-id>/
```

每个成功 sample 至少应有：

```text
trace.jsonl
trace_ACTF.json
trace_SFT.json
info.json
```

验收顺序：

1. run `info.json.status` 是否为 `completed`，成功/失败数量是否符合预期；
2. sample `info.json.status` 是否为 `ok`，`usage` 和版本字段是否存在；
3. `trace_ACTF.json` 中 reasoning、工具调用与结果是否正确配对；
4. `trace_SFT.json` 顶层是否只有 `messages` 和 `tools`；
5. 需要文件交付的 task，其 sample `results/` 是否包含预期文件；
6. run `sft.jsonl` 行数是否等于可纳入训练的成功 sample 数。

各文件的生成条件和失败语义见 [轨迹产出文件指南](../traces/trajectory-outputs.md)。

## 8. 打开 Viewer

```bash
roller view runs/quickstart
```

默认监听 `127.0.0.1:8765`。远程运行时保持 loopback，并从本机建立 SSH 端口转发：

```bash
ssh -L 8765:127.0.0.1:8765 your-host
```

Viewer 会展示 ACTF 时间线；Inspector 用于查看 system prompt、tools schema、运行信息和原始内容。详见 [Viewer 指南](../traces/viewer.md)。

## 9. 切换到 Codex 或 Claude Code

### Codex

```bash
roller collect \
  --harness codex \
  --tasks examples/tasks/tasks.jsonl \
  --model gpt-5.6-sol \
  --api-base "$ROLLER_CODEX_BASE_URL" \
  --api-key-env ROLLER_CODEX_API_KEY \
  --thinking high \
  --reasoning-summary detailed \
  --parallel 2 \
  --timeout 900 \
  --output runs \
  --run-name codex-quickstart
```

### Claude Code

```bash
roller collect \
  --harness claude-code \
  --tasks examples/tasks/tasks.jsonl \
  --model claude-sonnet-4-6-thinking \
  --api-base "$ROLLER_ANTHROPIC_BASE_URL" \
  --api-key-env ROLLER_ANTHROPIC_API_KEY \
  --thinking high \
  --parallel 2 \
  --timeout 900 \
  --output runs \
  --run-name claude-code-quickstart
```

不要把 OpenClaw 的 `--model-config` 传给不含 OpenClaw participant 的运行。Codex 与 Claude Code 同样支持 `prompt`、预定义 `messages` 以及动态 `user_simulator`；Simulator 的 Harness/model 可在 task 的 `user_simulator` 对象中独立指定。

## 10. 下一步

- 复制更多命令：[Examples](examples.md)
- 写自己的数据：[Task 格式参考](../tasks/task-format.md)
- 配置 reasoning：[OpenClaw](../harnesses/openclaw-configuration.md#5-thinkingsystem-prompt-与-wire-trace) · [Codex](../harnesses/codex-configuration.md#4-reasoning-配置) · [Claude Code](../harnesses/claude-code-configuration.md#4-thinking-与-effort)
- 采集显式 CoT：[External Thinking](../traces/external-thinking.md)
- 理解训练格式：[SFT 数据格式](../traces/sft-format.md)
- 查全部命令行选项：[CLI 参数参考](cli-reference.md)

## 常见的首次运行问题

| 现象 | 检查项 |
| --- | --- |
| Docker preflight 失败 | `docker version` 是否能连接 Server；镜像是否已 pull/load |
| OpenClaw 找不到模型 | `--model` 是否与 JSON 的 `provider/models[].id` 完全对应 |
| 配置提示环境变量缺失 | 是否 `source examples/env/api.env`；占位符名称是否一致 |
| `/responses` 返回 404 | 服务只支持 Chat Completions；改用 OpenClaw `openai-completions`，或更换 Codex API |
| Claude URL 重复 `/v1` | 统一通过 `--api-base` 传 origin、`/v1` 或 `/v1/messages` |
| 同名 run 已存在 | 原实验使用 `--resume`，新实验更换 `--run-name` |
| Viewer 没有新 task | 确认传入的是 run 根目录，刷新页面并检查 run `info.json` |

更具体的错误分别列在三个 Harness 指南末尾。
