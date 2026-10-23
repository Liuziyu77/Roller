# 快速上手：从安装到第一条轨迹

本页以 **OpenClaw** 为例，带你把一条示例 Task 从安装运行到生成轨迹。其他 Harness 也可以沿用准备配置、dry-run、正式采集、验收结果和打开 Viewer 这条主流程。各 Harness 的镜像、API 配置和专属参数并不完全相同，实际使用时请同时参考后面的对应 Harness 指南。

完成后，你会执行一次 dry-run 和正式采集，找到输出文件，并在 Viewer 中打开结果。如果还不熟悉 Task、Harness、rollout、run 或 trace，可以先阅读 [Roller 简介](introduction.md)。

## 1. 准备运行环境

需要：

- Python 3.10+。
- Docker（用户自行安装）。
- OpenClaw 镜像。
- 支持 OpenAI Responses 或 Chat Completions 的模型 API。

建议使用独立的 Python 环境。下面以 Conda 和 Python 3.11 为例，在仓库根目录执行：

```bash
conda create -n roller python=3.11 -y
conda activate roller

python -m pip install -e .
roller --version
docker version
```

这里使用 `python -m pip`，确保 pip 属于刚刚激活的 Python 环境。安装并启动 Docker 后，`docker version` 会显示 Client 和 Server 信息。

在 macOS 上使用 Docker Desktop 时，还需要在 **Settings → Resources → Network** 中打开 **Enable host networking**，然后重启 Docker Desktop。Roller 默认通过 host network 让 OpenClaw 访问本地请求代理。

## 2. 准备 OpenClaw 镜像

OpenClaw 同时支持 OpenAI Responses 和 Chat Completions。

在线拉取固定版本镜像：

```bash
docker pull ghcr.io/openclaw/openclaw:2026.7.1-2
```

也可以加载已有归档：

```bash
docker load -i /path/to/openclaw-2026.7.1-2-linux-amd64.tar.gz
```

如果使用自定义镜像 tag，可以通过 `--docker-image` 传入，或者设置 `ROLLER_OPENCLAW_IMAGE`。

第一次运行可以保留默认 reasoning 配置。Roller 会保存模型服务返回的可见 reasoning 文本。如果服务只返回 reasoning token 数，就没有可以保存的 reasoning 文本。

## 3. 准备 OpenClaw API 配置

复制本地环境变量模板：

```bash
cp examples/env/api.env.example examples/env/api.env
```

在 `examples/env/api.env` 中，将 `MY_PROXY_BASE_URL` 和 `MY_PROXY_API_KEY` 改成实际模型服务的地址和 Key。本页不使用其他 Harness 或 Web Search，其他变量暂时留空。

保存后，将变量加载到当前 shell：

```bash
set -a
source examples/env/api.env
set +a
```


打开仓库提供的 [`examples/config/openclaw/models.json`](../../../examples/config/openclaw/models.json)，将其中的 model `id` 改为实际使用的模型名。后续命令中 provider 后面的 model ID 必须与这里的 `id` 完全一致。例如，如果把 `id` 改为 `qwen3.8-flash`，后续命令就要使用 `--model my-openai-proxy/qwen3.8-flash`。

## 4. 认识示例 Task

[`examples/tasks/tasks.jsonl`](../../../examples/tasks/tasks.jsonl) 包含四条由简到繁的 task：

| ID | 展示内容 |
| --- | --- |
| `01_text_only` | 单轮纯文本 |
| `02_local_markdown` | 读取输入文件并写入 `outputs/` |
| `03_user_memory_context` | 注入 `USER.md` 与 `MEMORY.md` |
| `04_multi_turn` | 两条预定义 user message 共用一个 session |

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

Dry-run 用于在正式采集前检查 task 和参数是否能被 Roller 正确读取，并预览即将执行的 rollout。它不会启动容器、调用模型 API 或生成 run 结果。确认没有报错后，再进行正式采集。

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

1. 本次运行目录下的 `info.json` 中，`state` 是否为 `finished`，`summary.failed` 是否为 `0`，`summary.completed` 是否等于预期执行数量。
2. 每个 sample `info.json` 中的 `status` 是否为 `ok`，`usage.request_count` 是否大于 `0`。
3. `trace_ACTF.json` 是否包含完整的用户输入和模型输出。若存在工具调用，调用与结果是否正确配对。
4. `trace_SFT.json` 顶层是否只有 `messages` 和 `tools`（含图片的样本会额外带
   `images`，且 `<image>` 占位符数量与之一致）。
5. 需要文件交付的 task，其 sample `results/` 中是否包含预期文件。
6. run `sft.jsonl` 行数是否等于成功生成 `trace_SFT.json` 的 sample 数。

各文件何时生成、失败时会保留哪些内容，见 [轨迹产出文件指南](../traces/trajectory-outputs.md)。

## 8. 打开 Viewer

```bash
roller view runs/quickstart
```

默认监听 `127.0.0.1:17865`，被占用时自动改用空闲端口。命令启动后，在浏览器中打开终端打印的 URL。

Viewer 会展示 ACTF 时间线。Inspector 用于查看 system prompt、tools schema、运行信息和原始内容。详见 [Viewer 指南](../traces/viewer.md)。

## 9. 下一步

- 写自己的数据：[Task 格式参考](../tasks/task-format.md)
- 配置 reasoning：[OpenClaw Harness 指南](../harnesses/openclaw-configuration.md#6-thinking-配置)
- 理解训练格式：[SFT 数据格式](../traces/sft-format.md)
- 查全部命令行选项：[CLI 参数参考](cli-reference.md)

## 10. 常见问题

**(1) Docker preflight 失败**

检查 `docker version` 是否能连接 Server，以及镜像是否已 pull/load。

**(2) OpenClaw 找不到模型**

检查 `--model` 是否与 JSON 的 `provider/models[].id` 完全对应。

**(3) 配置提示环境变量缺失**

检查是否已执行 `source examples/env/api.env`，以及占位符名称是否一致。

**(4) `/responses` 返回 404**

服务可能只支持 Chat Completions。把 OpenClaw provider 的 `api` 改为 `openai-completions`。

**(5) 同名 run 已存在**

原实验使用 `--resume`，新实验更换 `--run-name`。

**(6) Viewer 没有新 task**

确认传入的是 run 根目录，刷新页面并检查 run `info.json`。

OpenClaw Harness 指南还列有更多 OpenClaw 相关错误。
