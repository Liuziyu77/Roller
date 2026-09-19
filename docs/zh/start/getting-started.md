# 快速上手：从安装到第一条轨迹

本页以 **OpenClaw** 为例，带你把一条示例 Task 从安装运行到生成轨迹。其他 Harness 也可以沿用准备配置、dry-run、正式采集、验收结果和打开 Viewer 这条主流程。各 Harness 的镜像、API 配置和专属参数并不完全相同，实际使用时请同时参考后面的对应 Harness 指南。

完成后，你会执行一次 dry-run 和正式采集，找到输出文件，并在 Viewer 中打开结果。如果还不熟悉 Task、Harness、rollout、run 或 trace，可以先阅读 [Roller 简介](introduction.md)。

## 1. 准备运行环境

需要：

- Python 3.10+。
- Docker CLI。
- CLI 能连接的 Docker daemon。
- OpenClaw 镜像。
- 支持 OpenAI Responses 或 Chat Completions 的模型 API。

在仓库根目录安装 Roller：

```bash
python3 -m pip install -e .
roller --version
docker version
```

`docker version` 必须同时返回 Client 和 Server 信息。只有 Docker CLI、没有可连接的 daemon 时，Roller 无法启动 rollout。

## 2. 准备 OpenClaw 镜像

OpenClaw 同时支持 OpenAI Responses 和 Chat Completions。你可以在线拉取固定版本镜像，也可以加载已有归档：

```bash
docker pull --platform linux/amd64 ghcr.io/openclaw/openclaw:2026.7.1-2

# 或
docker load -i /path/to/openclaw-2026.7.1-2-linux-amd64.tar.gz
```

如果使用自定义镜像 tag，可以通过 `--docker-image` 传入，或者设置 `ROLLER_OPENCLAW_IMAGE`。

第一次运行可以保留默认 reasoning 配置。Roller 会保存模型服务返回的可见 reasoning 文本。如果服务只返回 reasoning token 数，就没有可以保存的 reasoning 文本。

## 3. 准备 OpenClaw API 配置

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

`examples/env/*.env` 已被 Git 忽略。真实 Key 不应写入 shell 历史、task、模型 JSON 或文档。

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

- `mode: "merge"` 表示把这里声明的 provider 合并进 OpenClaw 现有模型配置，而不是用这份文件替换整个配置集合。
- `--model` 使用 `provider/model-id`，上例对应 `my-openai-proxy/my-model`。
- `${ENV_NAME}` 由 Roller 在宿主机展开，缺失或空值会在 rollout 前报错。
- Responses 服务使用 `openai-responses`。只有 `/v1/chat/completions` 的服务使用 `openai-completions`。
- `baseUrl` 通常写到 `/v1`，不要把 `/chat/completions` 当作 base URL。

仓库模板位于 [`examples/config/openclaw/models.json`](../../../examples/config/openclaw/models.json)。

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

`--model` 可以在命令行统一指定。若未传，则每条 task 必须有自己的 `model` 字段。详细 schema 见 [Task 格式参考](../tasks/task-format.md)。

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

- 读取并规范化 JSON/JSONL task。
- 检查重复 ID、对话模式和 Harness 限制。
- 解析 input、Context 与 output 路径。
- 执行路径冲突、符号链接、文件大小和内容摘要检查。
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

同名目录已存在时，Roller 默认拒绝覆盖。原配置继续运行应增加 `--resume`。新实验应改用新的 `--run-name`。

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

1. run `info.json.status` 是否为 `completed`，成功/失败数量是否符合预期。
2. sample `info.json.status` 是否为 `ok`，`usage` 和版本字段是否存在。
3. `trace_ACTF.json` 中 reasoning、工具调用与结果是否正确配对。
4. `trace_SFT.json` 顶层是否只有 `messages` 和 `tools`（含图片的样本会额外带
   `images`，且 `<image>` 占位符数量与之一致）。
5. 需要文件交付的 task，其 sample `results/` 是否包含预期文件。
6. run `sft.jsonl` 行数是否等于可纳入训练的成功 sample 数。

各文件何时生成、失败时会保留哪些内容，见 [轨迹产出文件指南](../traces/trajectory-outputs.md)。

## 8. 打开 Viewer

```bash
roller view runs/quickstart
```

默认监听 `127.0.0.1:8765`。远程运行时保持本地回环地址，并从本机建立 SSH 端口转发：

```bash
ssh -L 8765:127.0.0.1:8765 your-host
```

Viewer 会展示 ACTF 时间线。Inspector 用于查看 system prompt、tools schema、运行信息和原始内容。详见 [Viewer 指南](../traces/viewer.md)。

## 9. 下一步

- 写自己的数据：[Task 格式参考](../tasks/task-format.md)
- 配置 reasoning：[OpenClaw Harness 指南](../harnesses/openclaw-configuration.md#5-thinkingsystem-prompt-与-wire-trace)
- 理解训练格式：[SFT 数据格式](../traces/sft-format.md)
- 查全部命令行选项：[CLI 参数参考](cli-reference.md)

## 常见的首次运行问题

| 现象 | 检查项 |
| --- | --- |
| Docker preflight 失败 | `docker version` 是否能连接 Server。镜像是否已 pull/load |
| OpenClaw 找不到模型 | `--model` 是否与 JSON 的 `provider/models[].id` 完全对应 |
| 配置提示环境变量缺失 | 是否 `source examples/env/api.env`。占位符名称是否一致 |
| `/responses` 返回 404 | 服务只支持 Chat Completions。把 OpenClaw provider 的 `api` 改为 `openai-completions` |
| 同名 run 已存在 | 原实验使用 `--resume`，新实验更换 `--run-name` |
| Viewer 没有新 task | 确认传入的是 run 根目录，刷新页面并检查 run `info.json` |

OpenClaw Harness 指南还列有更多 OpenClaw 相关错误。
