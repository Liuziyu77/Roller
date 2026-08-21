# OpenClaw、模型与 API 配置

本文说明 Roller 如何启动 OpenClaw、连接自定义模型服务，以及传递搜索与代理配置。Task 和文件声明分别见 [Task 格式参考](task-format.md) 与 [文件、Task Bundle 与结果存储](task-bundle-and-storage.md)。

## 1. 运行要求

采集前需要准备：

- Python 3.10+；
- 可用的 Docker daemon；
- 一个同时包含 `openclaw` 和 `python3` 的 Docker 镜像；
- 模型服务所需的 API 地址与凭证。

镜像可以通过参数或环境变量指定：

```bash
roller collect ... --docker-image your-openclaw-image:tag

# 或作为默认值
export ROLLER_OPENCLAW_IMAGE=your-openclaw-image:tag
```

如果 Docker daemon 在另一台机器上，可以使用 Docker CLI 自带的 `DOCKER_HOST` 等配置。Roller 调用的是本机 `docker` 命令，因此 `docker version` 能连接成功时，Roller 通常也能使用同一个 daemon。

```bash
docker version
docker image inspect your-openclaw-image:tag
```

## 2. 模型配置文件

`--model-config` 接受一份 OpenClaw models JSON。仓库中的文本模型模板是 [`examples/json/openclaw-models.json`](../examples/json/openclaw-models.json)：

```json
{
  "mode": "merge",
  "providers": {
    "my-openai-proxy": {
      "baseUrl": "${MY_PROXY_BASE_URL}",
      "apiKey": "${MY_PROXY_API_KEY}",
      "api": "openai-responses",
      "models": [
        {"id": "my-model", "name": "My model"}
      ]
    }
  }
}
```

运行时的 model ref 是 `provider/model`，对应上例中的：

```text
my-openai-proxy/my-model
```

Roller 在宿主机展开 `${ENV_NAME}`，把合并后的配置临时注入容器。引用的环境变量缺失或为空时，会在启动 rollout 前报错。临时文件使用后立即删除，最终 run 不保存展开后的 JSON。

### API base 与 adapter

`baseUrl` 用来设置自定义 API base，例如：

```text
https://api.example.com/v1
```

具体请求路径由 OpenClaw 根据 `api` adapter 生成：

| `api` | 典型后端 |
| --- | --- |
| `openai-responses` | 实现 OpenAI Responses API 的服务 |
| `openai-completions` | 只实现 Chat Completions 兼容接口的服务 |

如果服务端只提供 `/v1/chat/completions`，应选择 `openai-completions`，而不是把完整 endpoint 当成 `baseUrl`。模型 ID、adapter 和服务端能力必须一致；Roller 不会替后端转换不受支持的协议。

命令行的 `provider/model` 必须与 JSON 完全对应：`providers.my-provider` 配合 `--model my-provider/...`，`models[].id = "my-model"` 配合 `--model .../my-model`。`baseUrl` 通常包含 `/v1`，具体以服务端文档为准。

无鉴权的自建服务也需要非空 `apiKey` 以通过 OpenAI client 的配置校验。如果服务端忽略 Authorization header，可以使用 `"apiKey":"not-required"`。

## 3. 环境变量与密钥

推荐从示例创建一个不会提交到 Git 的本地文件：

```bash
cp examples/env/api.env.example examples/env/api.env

# 编辑后加载到当前 shell
set -a
source examples/env/api.env
set +a
```

以下常用变量会在宿主机存在且非空时自动传入 OpenClaw 容器：

```text
OPENROUTER_API_KEY        OPENROUTER_BASE_URL
BRAVE_API_KEY             TAVILY_API_KEY
HTTP_PROXY                HTTPS_PROXY               NO_PROXY
http_proxy                https_proxy               no_proxy
```

还有两种传入方式：

- `runtime.env`：该 task 的硬依赖；变量缺失时直接失败；
- `--pass-env NAME`：为本次 run 增加一个可选透传变量。

例如，某个 task 必须使用内部服务：

```json
{
  "runtime": {
    "env": ["INTERNAL_SERVICE_TOKEN"]
  }
}
```

或者对所有 task 透传：

```bash
roller collect ... --pass-env INTERNAL_SERVICE_TOKEN
```

> [!IMPORTANT]
> Task 与模型 JSON 只写环境变量名或 `${ENV_NAME}` 占位符。不要把真实 API Key 写进仓库、task、命令行参数或 README。

为兼容 Interns2_data，宿主机的 `OPENROUTER_BASE_URL_OPENCLAW` 在没有设置 `OPENROUTER_BASE_URL` 时会映射为后者。新配置建议直接使用标准变量名。

## 4. 文本模型与多模态模型

普通文本模型使用：

```json
{"id":"my-model","name":"My model"}
```

视觉输入模型需要声明 image 能力：

```json
{
  "id": "my-multimodal-model",
  "name": "My Multimodal Model",
  "input": ["text", "image"]
}
```

完整模板见 [`examples/json/openclaw-models-multimodal.json`](../examples/json/openclaw-models-multimodal.json)。`input` 只是告诉 OpenClaw 如何使用模型；实际 API 后端仍必须支持对应 adapter 下的图像输入。

默认情况下，Roller 也会尝试把 task model 配置为 OpenClaw 的 image model。旧版 OpenClaw 不支持该设置时只记录 warning；可以用 `--no-set-image-model` 关闭。

## 5. Thinking、system prompt 与 wire trace

### 逻辑档位与 provider effort

Roller 的 `--thinking` 使用 OpenClaw 的**逻辑档位**，而不是把字符串直接写入任意模型 API：

```text
off | minimal | low | medium | high | xhigh | adaptive | max | ultra
```

另外接受 `none` 作为输入别名，并在进入 task、配置和轨迹元数据前统一规范化为 `off`。CLI 全局值、task 的 `runtime.thinking`、User Simulator 的 `thinking` 都使用同一套校验。task 值优先于 CLI 全局值；未设置时 Roller 不注入 thinking 默认值，也不向 `openclaw agent` 增加 `--thinking`。

需要模型生成 reasoning 时显式传入：

```bash
roller collect ... --thinking high
```

档位先由 Roller 原样交给 OpenClaw，再由 OpenClaw 当前 model profile 校验并映射成 provider 参数。因此 Roller 接受某个逻辑档位，不代表每个模型都支持它：

| Roller 输入 | OpenClaw 规范值 | GPT-5.6 Sol 的 Responses API | 说明 |
| --- | --- | --- | --- |
| `none` / `off` | `off` | `none` | `none` 只是 Roller 的输入别名；原生 GPT API 不接受 `off` |
| `minimal` | `minimal` | 不支持 | 只用于声明支持它的其他模型 |
| `low` | `low` | `low` | 原样映射 |
| `medium` | `medium` | `medium` | 原样映射；GPT-5.6 Sol 未传 effort 时的 API 默认值也是 `medium` |
| `high` | `high` | `high` | 原样映射 |
| `xhigh` | `xhigh` | `xhigh` | 需要 OpenClaw profile 声明支持 |
| `adaptive` | `adaptive` | 不是原生值 | 由支持自适应 thinking 的 OpenClaw profile 决定实际档位 |
| `max` | `max` | `max` | 需要新版 OpenClaw 和对应 model profile |
| `ultra` | `ultra` | 不是原生值 | 新版 OpenClaw 的编排档位；对支持的 GPT runtime 通常以 `max` 加额外编排实现 |

对 `gpt-5.6-sol` 的实际 Responses 请求已逐项验证：`none/low/medium/high/xhigh/max` 成功；`minimal/adaptive/ultra` 作为原生 effort 会返回 400。`off` 必须由 OpenClaw 转成 `none`，不能直接发给 GPT API。官方支持矩阵以 [OpenAI GPT-5.6 model guidance](https://developers.openai.com/api/docs/guides/model-guidance?model=gpt-5.6) 为准。

开源模型不在 Roller 中维护一张容易过期的硬编码表。常见 OpenAI-compatible 后端通常支持 `low/medium/high`，而 `minimal/xhigh/adaptive/max` 是否存在以及怎样映射由模型服务和 OpenClaw provider profile 决定。新版 OpenClaw 的自定义 provider 可通过 model `compat.supportedReasoningEfforts` 和 `compat.reasoningEffortMap` 描述服务能力；字段和具体模型档位以 [OpenClaw Thinking Levels](https://docs.openclaw.ai/tools/thinking) 及 [OpenClaw 配置文档](https://docs.openclaw.ai/gateway/config-tools) 为准。

> [!IMPORTANT]
> 目前测试到的默认镜像 `wildclawbench-ubuntu:v1.3` 内含 OpenClaw `2026.3.11`。该版本 CLI 只承诺 `off/minimal/low/medium/high`，并会把部分高级值静默降档，不能用于验证 GPT-5.6 Sol 的 `xhigh/max` 或开源模型的 `adaptive/max`。Roller 会对这个已知版本的 `xhigh/adaptive/max/ultra` 直接报错，避免产生档位标错的轨迹。使用这些档位时请通过 `--docker-image` 或 `ROLLER_OPENCLAW_IMAGE` 选择包含新版 OpenClaw 的镜像；只更新外部 Roller 仓库不会替换镜像内的 OpenClaw。

### Reasoning 内容与轨迹

Roller 会在本次 rollout 注入的模型配置副本中设置 `reasoning: true`。Chat Completions adapter 会发送 `reasoning_effort`；Responses adapter 会发送 reasoning 配置。旧配置中的 `tags: ["reasoning"]` 会自动转换为当前 OpenClaw 接受的布尔字段。

后端必须真实返回 reasoning 内容。Chat Completions 通常通过流式 `reasoning_content`（也兼容 `reasoning` 和 `reasoning_text`），Responses API 通过 reasoning items。仅在 usage 中报告 `reasoning_tokens`、但不返回 summary/content 时，没有可写入轨迹的文本。

对于通过 Responses API 暴露压缩 reasoning summary 的 GPT reasoning 模型，应使用：

```json
{
  "api": "openai-responses",
  "models": [{"id": "my-gpt-model", "name": "My GPT model", "reasoning": true}]
}
```

同一模型走 Chat Completions 时可以产生 reasoning token，却不一定返回可见的 `reasoning_content`；Roller 不会从隐藏 token 反推 thinking。

### 保留与剥离

`--keep-thinking` 默认开启，同时控制：

1. `trace_ACTF.json` 中 `steps[*].assistant_content.reasoning_content`；
2. 后续 Chat Completions messages 或 Responses input 中的历史 thinking。

`--no-keep-thinking` 会把 ACTF reasoning 置空，并在本地代理转发前移除 `reasoning_content`、`reasoning`、`reasoning_text`、thinking content block 和 Responses reasoning item。OpenClaw 本身没有关闭历史 thinking 的配置，因此该模式必须使用 wire 层代理。

### ACTF、cache trace 与 wire trace

默认 `--cache-trace` 记录 OpenClaw 每次 LLM call 组装的 system prompt；Roller 将首个有效
值写入 `trace_ACTF.json` 顶层。默认 `--wire-trace actf` 通过本地反向代理捕获完整 tools
schema，写入 ACTF 顶层后删除中间文件。

| `--wire-trace` | 行为 |
| --- | --- |
| `actf`（默认） | 捕获 tools schema 并写入 ACTF，不记录完整 messages/input；缺少可代理 `baseUrl` 时降级为 `tools=null` |
| `full` | 要求可代理 `baseUrl`，额外保留每次完整 JSON 请求体 `wire_trace.jsonl` 和按敏感 key 脱敏后的 `cache_trace.jsonl` |
| `off` | 不记录 wire 数据，ACTF 的 `tools` 为 `null`；但 `--no-keep-thinking` 仍会启用代理做剥离 |

OpenClaw session 文件与模型请求 context 不是同一层：session 保存持久化事件，而模型调用
前还可能发生 system/runtime 注入、compaction、tool-result pruning、context-engine/hook
处理以及 provider 协议适配。因此 `trace.jsonl` 中存在的消息不保证逐字进入每次请求；
ACTF/SFT 又是从该 session 文件投影，也不能证明模型当时看到了哪些完整历史。

需要审计真实请求 context 时使用 `full`。代理位于 OpenClaw provider adapter 和真实模型
API 之间；对合法 JSON，它在可选的历史 thinking 剥离之后记录与转发同一 request body。
`wire_trace.jsonl` 因而保存 Roller 客户端实际交给上游 API 的 messages/input、tools 和
参数。它不记录 headers、响应 body 或模型服务内部的二次处理，也不是字节级抓包。

多轮 full wire 会重复历史，体积接近 O(N²)，适合审计，不适合大批量常开。
`cache_trace.jsonl` 保存前会递归脱敏 API key、token、authorization、password 和 secret
等 JSON 字段；`wire_trace.jsonl` 不扫描 system/user/tool/reasoning 自由文本，必须按敏感
数据管理。完整捕获边界、对照步骤和失败/重试语义见
[轨迹产出文件指南](trajectory-outputs.md#32-session-历史不等于模型请求-context)。

本地代理绑定宿主机 `127.0.0.1`，因此 `actf`、`full` 和 `--no-keep-thinking` 当前要求保留默认 `--network host`，且所选 provider 必须提供绝对 `baseUrl`。

## 6. Web Search

例如使用 Brave Search：

```bash
export BRAVE_API_KEY=your-key

roller collect \
  ... \
  --search-provider brave
```

`--search-provider` 配置 OpenClaw 的搜索 provider，API Key 仍通过环境变量传入。即使 task 不搜索，某些预构建镜像也可能在启动时校验已有的搜索配置；遇到 unresolved secret 一类错误时，应检查镜像中的 OpenClaw 配置或提供对应变量。

## 7. 并行、容器与端口

普通 rollout 使用一个独立容器；User Simulator rollout 同时使用任务 Agent 和 Simulator 两个容器。一个 session 会在多轮期间保持运行，不会每一轮重新创建容器。

OpenClaw gateway 端口按 worker slot 计算：

```text
gateway_port = --gateway-port + worker_slot × --gateway-port-step
```

默认起始端口是 `18789`，步长是 `10`。User Simulator 使用从 `--parallel` 开始的另一组 slot。因此 `--parallel 4` 的动态对话最多会同时占用 8 个容器和 8 个 gateway 端口。

使用默认 `--network host` 时，要确保端口范围在 Docker host 上空闲。并行 run 共用同一 Docker daemon 时，建议给它们设置不同的起始端口：

```bash
roller collect ... --gateway-port 22000 --gateway-port-step 10
```

## 8. 常用 OpenClaw 参数

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `--docker-image` | `ROLLER_OPENCLAW_IMAGE` 或内置值 | OpenClaw 镜像 |
| `--docker-binary` | `docker` | Docker CLI 名称或路径 |
| `--model-config` | 无 | 自定义 provider/model JSON |
| `--network` | `host` | 容器网络模式 |
| `--gateway-port` | `18789` | gateway 起始端口 |
| `--gateway-port-step` | `10` | worker 之间的端口间隔 |
| `--gateway-startup-timeout` | `30` | 等待 gateway 就绪的秒数 |
| `--session-id` | `roller` | OpenClaw session ID 前缀 |
| `--thinking` | 无 | 全局 OpenClaw 逻辑档位；完整值、模型映射和版本限制见第 5 节 |
| `--keep-thinking` / `--no-keep-thinking` | 开启 | 是否保留轨迹和请求历史中的 thinking |
| `--cache-trace` / `--no-cache-trace` | 开启 | 是否记录每次组装的 system prompt；ACTF 固化首个有效值 |
| `--wire-trace` | `actf` | tools schema / 完整请求体记录模式 |
| `--search-provider` | 无 | OpenClaw Web Search provider |
| `--pass-env NAME` | 无 | 增加一个环境变量名，可重复 |
| `--set-image-model` / `--no-set-image-model` | 开启 | 是否设置 OpenClaw image model |
| `--keep-container-on-error` | 关闭 | 出错后保留容器以便排查 |
| `--collect-path` | 无 | Legacy：额外回收容器绝对路径 |

全部参数以当前安装版本为准：

```bash
roller collect --help
```

## 9. 运行元数据与安全

Sample provenance 会记录 OpenClaw 版本、镜像及 digest、model ref、使用过的环境变量名称，以及脱敏后的 API base。API base 中的用户名、密码、query 和 fragment 不会进入结果；环境变量值和展开后的 API Key 也不会保存。

`--keep-container-on-error` 适合本地排错，但保留的容器可能含有临时配置、输入或运行环境变量。检查完成后应自行删除容器，不要在共享 Docker host 上长期保留。

## 10. 常见问题

### Docker preflight 失败

先运行 `docker version`。如果 Docker 使用远程 daemon，确认当前 shell 的 Docker context、`DOCKER_HOST` 和证书配置能够正常连接。

### `model config references missing environment variable`

模型 JSON 中的 `${ENV_NAME}` 没有在当前 shell 中设置，或值为空。加载本地 env 文件后重新运行。

### 模型请求返回 404 或协议错误

通常是 `baseUrl` 末尾路径或 `api` adapter 不匹配。确认服务端实现的是 Responses API 还是 Chat Completions API。

### gateway 端口冲突

调整 `--gateway-port`，并确保按照 `parallel × 参与者数量` 预留足够端口。

### 任务成功回复但 sample 失败

Roller 要求成功 sample 同时具有 OpenClaw raw trace、可转换的 ACTF 和标准化 SFT。还应检查输出目录里是否存在被拒绝的符号链接或特殊文件。具体产出条件见 [轨迹产出文件指南](trajectory-outputs.md)，结果安全规则见 [结果存储](task-bundle-and-storage.md#5-输出回收)。
