# OpenClaw Harness 指南

[文档中心](README.md) · [快速上手](getting-started.md) · [Codex](codex-configuration.md) · [Claude Code](claude-code-configuration.md) · [CLI 参数](cli-reference.md)

本页只说明 OpenClaw 专属的镜像、模型 provider、API adapter、thinking、搜索、gateway 和排错。Task 字段见 [Task 格式参考](task-format.md)，所有 Harness 共用的产物语义见 [轨迹产出文件指南](trajectory-outputs.md)。

> 快速定位：镜像与 Docker（第 1 节）· provider JSON（第 2–4 节）· reasoning/wire（第 5 节）· 并行端口（第 7 节）· 参数速查与排错（第 8–10 节）

## 1. 运行要求

采集前需要准备：

- Python 3.10+；
- 可用的 Docker daemon；
- OpenClaw Docker 镜像；
- 模型服务所需的 API 地址与凭证。

Roller 默认固定使用官方稳定镜像 `ghcr.io/openclaw/openclaw:2026.7.1-2`。该镜像已包含
OpenClaw、Node.js 与 Python 3；Roller 直接运行它，不修改镜像内 OpenClaw 源码，也不要求
派生镜像。在线与离线准备方式二选一：

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

官方镜像默认用户是 `node`，而 Roller task 需要创建 `/tmp_workspace` 并执行用户提供的 setup
命令。为保持旧 benchmark 镜像的行为，Roller 默认用 `--container-user 0` 在**一次性 task
容器内部**运行；这不会赋予宿主机 root 权限。自定义镜像已经为默认用户准备好可写工作区时，
可以传 `--container-user image`。Roller 还会为每个 rollout 生成仅存在于容器进程环境中的
临时 Gateway token，并固定 `gateway.mode=local`、`gateway.bind=loopback`；token 不写入 run、
task、轨迹或模型配置。

OpenClaw `2026.7.1` 的 session registry 位于
`$OPENCLAW_STATE_DIR/state/openclaw.sqlite`。Roller 不直接查询数据库，而是通过官方公开的
`openclaw/plugin-sdk/session-transcript-runtime` 按 `{agentId, sessionKey, sessionId}` 导出
事件。legacy `2026.3.11` 不识别 `OPENCLAW_STATE_DIR`，Roller 会自动回退到
`/root/.openclaw` 和 session JSONL。

## 2. 模型配置文件

`--model-config` 接受一份 OpenClaw models JSON。基础文本模板是
[`examples/config/openclaw/models.json`](../examples/config/openclaw/models.json)，GPT 与
Chat Completions reasoning 模板是
[`examples/config/openclaw/models-reasoning.json`](../examples/config/openclaw/models-reasoning.json)：

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

完整模板见 [`examples/config/openclaw/models-multimodal.json`](../examples/config/openclaw/models-multimodal.json)。`input` 只是告诉 OpenClaw 如何使用模型；实际 API 后端仍必须支持对应 adapter 下的图像输入。

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
| `ultra` | `ultra` | 不是原生值 | 默认 OpenClaw `2026.7.1` CLI 不接受；只有所选新版本明确列出时才能使用 |

对 `gpt-5.6-sol` 的实际 Responses 请求已逐项验证：`none/low/medium/high/xhigh/max` 成功；`minimal/adaptive/ultra` 作为原生 effort 会返回 400。`off` 必须由 OpenClaw 转成 `none`，不能直接发给 GPT API。官方支持矩阵以 [OpenAI GPT-5.6 model guidance](https://developers.openai.com/api/docs/guides/model-guidance?model=gpt-5.6) 为准。

开源模型不在 Roller 中维护一张容易过期的硬编码表。OpenClaw `2026.7.1` 的自定义
model 使用顶层 `thinkingLevelMap` 把逻辑档位映射为服务值；Chat Completions 服务通过
`compat.supportsReasoningEffort: true` 声明接受 `reasoning_effort`：

```json
{
  "id": "my-reasoning-model",
  "name": "My reasoning model",
  "reasoning": true,
  "thinkingLevelMap": {
    "off": "none",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max"
  },
  "compat": {"supportsReasoningEffort": true}
}
```

`thinkingLevelMap` 属于 model 顶层，不要放进 `compat`。此稳定版的用户配置 schema 不接受
自定义 `compat.supportedReasoningEfforts` 或 `compat.reasoningEffortMap`；写入这些字段会导致
配置校验失败。使用 `enable_thinking` 的 Qwen-compatible 服务可以把
`compat.thinkingFormat` 设为 `qwen`；实际字段和档位仍以服务实现为准。

> [!IMPORTANT]
> 默认官方镜像内的 OpenClaw `2026.7.1` 支持
> `off/minimal/low/medium/high/xhigh/adaptive/max`，Roller 会拒绝该版本的 `ultra`。
> legacy `wildclawbench-ubuntu:v1.3` 内含 `2026.3.11`，只可靠支持到 `high`；Roller 会拒绝
> 其 `xhigh/adaptive/max/ultra`。只更新 Roller 仓库不会替换 Docker 镜像，运行时版本以
> sample `info.json` 的 `runtime.openclaw_version` 为准。

### Reasoning 内容与轨迹

使用原生 thinking 时，Roller 会在本次 rollout 注入的模型配置副本中设置
`reasoning: true`。Chat Completions adapter 会发送 `reasoning_effort`；Responses adapter
会发送 reasoning 配置。旧配置中的 `tags: ["reasoning"]` 会自动转换为当前 OpenClaw
接受的布尔字段。启用下一节的 external thinking 时行为相反：所选 model 副本会设置
`reasoning: false`，OpenClaw thinking 与 wire `reasoning.effort` 都固定为 `off`/`none`。

后端必须真实返回 reasoning 内容。Chat Completions 通常通过流式 `reasoning_content`（也兼容 `reasoning` 和 `reasoning_text`），Responses API 通过 reasoning items。仅在 usage 中报告 `reasoning_tokens`、但不返回 summary/content 时，没有可写入轨迹的文本。

对 Qwen、GLM 等能够返回明文 reasoning 的开源模型，默认 `--keep-thinking` 会把该文本保留到
ACTF/SFT `reasoning_content`，无需启用 external thinking。External thinking 主要用于
`gpt-5.6-sol` 等只暴露压缩 reasoning summary 的 GPT reasoning 模型。

对于通过 Responses API 暴露压缩 reasoning summary 的 GPT reasoning 模型，应使用：

```json
{
  "api": "openai-responses",
  "models": [{"id": "my-gpt-model", "name": "My GPT model", "reasoning": true}]
}
```

同一模型走 Chat Completions 时可以产生 reasoning token，却不一定返回可见的 `reasoning_content`；Roller 不会从隐藏 token 反推 thinking。

### External Thinking：用工具参数保存显式 CoT

GPT Responses 的 reasoning item 对客户端通常只暴露 summary，而不是生成过程的完整文本。
Roller 可关闭原生 reasoning，并为一次性 OpenClaw 容器加载本地 `deep_think(thoughts)`
工具，把模型主动写入 `thoughts` 的文本作为显式 scratchpad：

```bash
roller collect \
  --tasks tasks.jsonl \
  --model my-gpt-proxy/gpt-5.6-sol \
  --model-config examples/config/openclaw/models-reasoning.json \
  --external-thinking external_always \
  --timeout 1200
```

| 模式 | 请求协议 | 保证 |
| --- | --- | --- |
| `off` | 原生 OpenClaw 行为 | 默认值；`--thinking` 决定 provider reasoning |
| `external` | 首次请求强制 `deep_think`，后续请求保留该工具并允许并行业务工具 | raw 校验首次 response 恰好包含一个非空 think；后续按 instructions 自主调用 |
| `external_always` | 每个逻辑步骤拆成 THINK 与 ACT；THINK 强制且只允许 `deep_think`，ACT 从 tools 中删除它 | raw 校验严格 THINK/ACT 交替、非空 thoughts、无孤立 THINK |

两个 external 模式都要求所选 provider 的 `api` 精确为 `openai-responses` 且提供可代理的
绝对 `baseUrl`。Roller 会覆盖 task/CLI 的 `--thinking`，在三个位置关闭原生 reasoning：

1. rollout 内所选 model 设置 `reasoning: false`；
2. OpenClaw 默认值及每次 `openclaw agent` 调用使用 `--thinking off`；
3. 代理实际转发的 Responses body 固定 `reasoning.effort: "none"`。

插件由 Roller 从自身 package data 复制到一次性容器，通过 `plugins.load.paths` 加载，并在
Gateway 启动前修正为容器主进程 uid/gid；镜像内源码和宿主机 OpenClaw 安装均不修改。
`wire_trace.jsonl` 在 full 模式为每个请求增加 `external_thinking_phase`，便于核对
`think`、`act` 或 `optional`。

`deep_think` 在实际推理协议中是普通 function tool，因此 raw 和 ACTF 中的 thoughts 位于
tool-call arguments，对应 tool result 固定为 `------`。专用 SFT 投影会删除这组内部
call/result 和顶层 `deep_think` schema，并把 thoughts 合并到随后实际 action 对应 assistant
的 `reasoning_content`；业务工具保持不变。这个字段是训练格式标准化结果，不表示 provider
原生返回了同名字段。`--no-keep-thinking` 只剥离原生 thinking，不删除显式 external CoT；
若不希望保存它，应使用 `--external-thinking off`。

完整的安全边界、SFT 形态、与 claw-eval 参考实现的差异见
[External Thinking 指南](external-thinking.md)。

### 保留与剥离

`--keep-thinking` 默认开启，同时控制：

1. `trace_ACTF.json` 中 `steps[*].assistant_content.reasoning_content`；
2. 后续 Chat Completions `messages` 或 Responses `input` 中的历史 thinking。

OpenAI-compatible 服务并不统一使用同一个返回字段。对于 Chat Completions，Roller 的本地
传输代理会在响应进入 OpenClaw 前把非空 `reasoning`/`reasoning_text` 规范化为等价的
`reasoning_content`。这是因为 OpenClaw `2026.7.1` 能记录前两者，却只为 generic custom
provider 重放 `reasoning_content`。规范化不生成或总结文本，后续请求携带的内容与模型返回
文本一致。已用官方 `2026.7.1` + GLM-5.2 工具任务验证：首个 assistant raw thinking 与
第二次 `/chat/completions` 请求的 `reasoning_content` 完全相同。

`--no-keep-thinking` 会把 ACTF reasoning 置空，并在本地代理转发前移除 `reasoning_content`、`reasoning`、`reasoning_text`、thinking content block 和 Responses reasoning item。OpenClaw 本身没有关闭历史 thinking 的配置，因此该模式必须使用 wire 层代理。

### ACTF、cache trace 与 wire trace

默认 `--cache-trace` 记录 OpenClaw 每次 LLM call 组装的 system prompt；Roller 将首个有效
值写入 `trace_ACTF.json` 顶层。默认 `--wire-trace actf` 通过本地反向代理捕获完整 tools
schema，写入 ACTF 顶层后删除中间文件。

| `--wire-trace` | 行为 |
| --- | --- |
| `actf`（默认） | 捕获 tools schema 并写入 ACTF，不记录完整 messages/input；缺少可代理 `baseUrl` 时降级为 `tools=null` |
| `full` | 要求可代理 `baseUrl`，额外保留每次完整 JSON 请求体 `wire_trace.jsonl` 和按敏感 key 脱敏后的 `cache_trace.jsonl` |
| `off` | 不保留 wire 数据，ACTF 的 `tools` 为 `null`；有可代理 model config 时仍可在内存中启用代理做 thinking 规范化/剥离 |

OpenClaw session 文件与模型请求 context 不是同一层：session 保存持久化事件，而模型调用
前还可能发生 system/runtime 注入、compaction、tool-result pruning、context-engine/hook
处理以及 provider 协议适配。因此 `trace.jsonl` 中存在的消息不保证逐字进入每次请求；
ACTF/SFT 又是从该 session 文件投影，也不能证明模型当时看到了哪些完整历史。

需要审计真实请求 context 时使用 `full`。代理位于 OpenClaw provider adapter 和真实模型
API 之间；对合法 JSON，它在可选的历史 thinking 剥离之后记录与转发同一 request body。
`wire_trace.jsonl` 因而保存 Roller 客户端实际交给上游 API 的 messages/input、tools 和
参数。它不记录 headers、响应 body 或模型服务内部的二次处理，也不是字节级抓包。

响应侧 `reasoning`/`reasoning_text` 规范化发生在上游响应返回 OpenClaw 的方向，因此不会
出现在 request-only 的 `wire_trace.jsonl`。它会影响 OpenClaw raw 中 thinking block 的
`thinkingSignature` 字段名，但不改变 thinking 文本；`trace.jsonl` 也因此不是上游 HTTP
响应的逐字副本。

多轮 full wire 会重复历史，体积接近 O(N²)，适合审计，不适合大批量常开。
`cache_trace.jsonl` 保存前会递归脱敏 API key、token、authorization、password 和 secret
等 JSON 字段；`wire_trace.jsonl` 不扫描 system/user/tool/reasoning 自由文本，必须按敏感
数据管理。完整捕获边界、对照步骤和失败/重试语义见
[轨迹产出文件指南](trajectory-outputs.md#32-session-历史不等于模型请求-context)。

本地代理绑定宿主机 `127.0.0.1`，因此 `actf`、`full`、Chat Completions thinking 规范化和
`--no-keep-thinking` 当前要求保留默认 `--network host`，且所选 provider 必须提供绝对
`baseUrl`。

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
| `--container-user` | `0` | 一次性 task 容器用户；`image` 使用镜像默认用户 |
| `--external-thinking` | `off` | `external` 或 `external_always` 启用显式 `deep_think` CoT |
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
