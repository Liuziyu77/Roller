# OpenClaw Harness 指南

本页介绍 OpenClaw 专属的镜像、模型 provider、API adapter、thinking、搜索、gateway 和排错。Task 字段见 [Task 格式参考](../tasks/task-format.md)，各 Harness 的产物说明见 [轨迹产出文件指南](../traces/trajectory-outputs.md)。

## 1. 适用范围

OpenClaw Harness 适合使用 OpenClaw Agent 运行 task。它支持 OpenAI Responses 和 Chat Completions 兼容模型，也支持文本与图片输入、thinking、多轮会话和 Web Search。

第一次使用可以先按照[快速上手](../start/getting-started.md)完成一次 OpenClaw 采集。本页用于配置自定义模型、环境变量和 OpenClaw 专属功能。

## 2. 镜像准备

采集前需要准备：

- Python 3.10+。
- 可用的 Docker daemon。
- OpenClaw Docker 镜像。
- 模型服务所需的 API 地址与凭证。

Roller 默认使用固定版本的官方稳定镜像 `ghcr.io/openclaw/openclaw:2026.7.1-2`，其中包含
OpenClaw、Node.js 与 Python 3。Roller 直接运行该镜像，无需修改 OpenClaw 源码或构建
派生镜像。可以在线拉取，也可以离线加载：

```bash
docker pull --platform linux/amd64 ghcr.io/openclaw/openclaw:2026.7.1-2

# docker load 可以直接读取 gzip 压缩的 docker save 归档
docker load -i /path/to/openclaw-2026.7.1-2-linux-amd64.tar.gz
```

其他镜像仍可通过参数或环境变量指定：

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

使用官方 OpenClaw 镜像时，无需额外设置容器用户。Roller 会在一次性容器中自动准备可写工作区和本地 Gateway 配置，这些设置不会改变宿主机权限。只有自定义镜像已经为默认用户准备好可写工作区，并希望使用该用户运行时，才需要传入 `--container-user image`。

## 3. 环境变量与 API

OpenClaw 通过模型配置文件读取模型名称、API 协议、服务地址和凭证。配置文件中的敏感值使用环境变量占位，运行前再从当前 shell 加载。

### 模型配置

`--model-config` 接受一份 OpenClaw models JSON。基础文本模板是
[`examples/config/openclaw/models.json`](../../../examples/config/openclaw/models.json)，GPT 与
Chat Completions reasoning 模板是
[`examples/config/openclaw/models-reasoning.json`](../../../examples/config/openclaw/models-reasoning.json)：

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

命令中的 `--model` 使用 `provider/model-id` 格式。`provider` 对应 `providers` 下的名称，`model-id` 对应 `models[].id`。上例应使用：

```text
my-openai-proxy/my-model
```

配置中的模型 ID 应改为模型服务实际使用的名称，命令中的值必须与配置完全一致。`${ENV_NAME}` 从当前 shell 读取，引用的环境变量缺失或为空时，Roller 会在启动 rollout 前报错。

### API 协议

`api` 用于选择模型服务支持的协议：

| `api` | 适用服务 |
| --- | --- |
| `openai-responses` | 实现 OpenAI Responses API 的服务 |
| `openai-completions` | 只实现 Chat Completions 兼容接口的服务 |

`apiKey` 必须是非空值。服务无需鉴权时，可以填写 `"apiKey":"not-required"`。

### 加载环境变量

模型配置中的 API 地址和凭证通过 `${ENV_NAME}` 从当前 shell 读取。可以先复制仓库提供的环境变量模板：

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

(openclaw-cli-flags)=
## 4. OpenClaw 专属参数

本节只列 OpenClaw 运行时需要特别关注的参数。所有 Harness 共用的任务、并行、重试和输出参数见 [CLI 参数参考](../start/cli-reference.md)。

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `--docker-image` / `--openclaw-image` | `ROLLER_OPENCLAW_IMAGE` 或内置值 | 覆盖 OpenClaw 镜像，混合 Harness 运行使用专属形式 |
| `--container-user` | `0` | 一次性 task 容器用户。`image` 使用镜像默认用户 |
| `--docker-binary` | `docker` | Docker CLI 名称或路径 |
| `--model-config` | 无 | 自定义 provider/model JSON |
| `--network` | `host` | 容器网络模式 |
| `--gateway-port` | `18789` | gateway 起始端口 |
| `--gateway-port-step` | `10` | worker 之间的端口间隔 |
| `--gateway-startup-timeout` | `30` | 等待 gateway 就绪的秒数 |
| `--session-id` | `roller` | OpenClaw session ID 前缀 |
| `--thinking` | 无 | 设置模型的 reasoning 强度，常用档位和保存规则见第 6 节 |
| `--keep-thinking` / `--no-keep-thinking` | 开启 | 是否保留轨迹和请求历史中的 thinking |
| `--cache-trace` / `--no-cache-trace` | 开启 | 是否记录每次组装的 system prompt。ACTF 固化首个有效值 |
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

## 5. 多模态能力

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

完整模板见 [`examples/config/openclaw/models-multimodal.json`](../../../examples/config/openclaw/models-multimodal.json)。`input` 只是告诉 OpenClaw 如何使用模型。实际 API 后端仍必须支持对应 adapter 下的图像输入。

图片通过 `files.inputs` 进入任务工作区，再由 OpenClaw 的图像查看工具打开。Task 的 `messages[].content` 当前只支持字符串，不能直接写入图片内容块。纯文本模型仍可处理文本和普通文件，但不能完成需要理解图片内容的任务。

默认情况下，Roller 也会尝试把 task model 配置为 OpenClaw 的 image model。旧版 OpenClaw 不支持该设置时只记录 warning。可以用 `--no-set-image-model` 关闭。

## 6. Thinking 配置

OpenClaw 常用的 thinking 档位是 `off`、`low`、`medium` 和 `high`。例如：

```bash
roller collect ... --thinking high
```

`none` 在 OpenClaw 中会被视为 `off`。其他档位是否可用，取决于 OpenClaw 版本、模型和 API 服务。

各模型可用的档位、`compat` 配置示例和验收方法见 [OpenClaw thinking 档位](openclaw-thinking.md)。

显式开启 thinking 且模型配置没有 `reasoning` 能力字段时，Roller 会为了兼容旧配置在本次 rollout 的副本中补充 `reasoning: true`；显式的 `reasoning: false` 会原样保留。旧配置中的 `tags: ["reasoning"]` 会自动转换为当前 OpenClaw 接受的布尔字段。

> [!IMPORTANT]
> legacy `wildclawbench-ubuntu:v1.3` 内含 OpenClaw `2026.3.11`，只可靠支持到 `high`；Roller 会拒绝其 `xhigh/adaptive/max/ultra`。只更新 Roller 仓库不会替换 Docker 镜像，运行时版本以 sample `info.json` 的 `runtime.openclaw_version` 为准。

轨迹能否保存 reasoning 文本，取决于模型服务实际返回的内容。如果服务只返回 reasoning token 数量，没有返回可见文本，`reasoning_content` 会为空，Roller 不会根据 token 反推内容。

`--keep-thinking` 默认开启，会保留模型返回的可见 reasoning。使用 `--no-keep-thinking` 可以从轨迹和后续请求历史中移除这些内容。

## 7. 轨迹与请求记录

本节说明 Roller 会记录哪些 OpenClaw 运行信息，以及如何在需要排错时保存完整模型请求。

默认开启的 `--cache-trace` 会把 OpenClaw 组装的 system prompt 写入 `trace_ACTF.json`。`--wire-trace` 决定是否继续记录工具 schema 或完整请求：

| `--wire-trace` | 记录内容 | 适用场景 |
| --- | --- | --- |
| `actf`（默认） | 将 tools schema 写入 `trace_ACTF.json`，不保存完整请求 | 日常采集 |
| `full` | 额外保存完整请求到 `wire_trace.jsonl`，并生成脱敏后的 `cache_trace.jsonl` | 排错或审计模型实际收到的内容 |
| `off` | 不记录 wire 数据，ACTF 中的 `tools` 为 `null` | 不需要请求信息时 |

`trace.jsonl` 记录的是 OpenClaw session 事件，不一定等于实际发送给模型的完整请求。需要检查模型实际收到的 messages、tools 和参数时，使用 `--wire-trace full`。

`full` 会产生更大的文件，其中可能包含 prompt、reasoning、工具参数和结果等敏感内容，不适合在大批量采集中长期开启。使用 `actf` 或 `full` 时建议保留默认的 `--network host`，并确保模型配置提供可访问的绝对 `baseUrl`。详细文件说明见[轨迹产出文件指南](../traces/trajectory-outputs.md#32-session-历史不等于模型请求-context)。

## 8. Web Search

Web Search 让 OpenClaw Agent 在执行 task 时查询网页信息。只有 task 需要联网搜索时才需要配置。处理本地文件、已有上下文或不依赖网页信息的 task，可以跳过本节。

使用 Web Search 时，需要同时选择搜索服务并提供对应的 API Key。下面以 Brave Search 为例。

先在 `examples/env/api.env` 中填写：

```bash
BRAVE_API_KEY=实际的-Brave-API-Key
```

保存后，将环境变量加载到当前 shell：

```bash
set -a
source examples/env/api.env
set +a
```

运行任务时指定 Brave：

```bash
roller collect \
  ... \
  --search-provider brave
```

## 9. 常见问题

**(1) Docker preflight 失败**

先运行 `docker version`。如果 Docker 使用远程 daemon，确认当前 shell 的 Docker context、`DOCKER_HOST` 和证书配置能够正常连接。

**(2) `model config references missing environment variable`**

模型 JSON 中的 `${ENV_NAME}` 没有在当前 shell 中设置，或值为空。加载本地 env 文件后重新运行。

**(3) 模型请求返回 404 或协议错误**

通常是 `baseUrl` 末尾路径或 `api` adapter 不匹配。确认服务端实现的是 Responses API 还是 Chat Completions API。

**(4) gateway 端口冲突**

如果出现端口已被占用的错误，可以更换 OpenClaw gateway 的起始端口：

```bash
roller collect ... --gateway-port 22000
```

**(5) 任务成功回复但 sample 失败**

Roller 要求成功 sample 至少具有 OpenClaw raw trace。通常还要求可转换的 ACTF 和标准化 SFT。少数原生附件或多模态内容无法无损投影时，`runtime.trajectory_conversion` 会结构化记录该情况，而不会污染 benchmark score。还应检查输出目录里是否存在被拒绝的符号链接或特殊文件。具体产出条件见 [轨迹产出文件指南](../traces/trajectory-outputs.md)，结果安全规则见 [结果存储](../tasks/task-bundle-and-storage.md#5-输出回收)。
