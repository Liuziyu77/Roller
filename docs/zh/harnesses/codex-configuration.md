# Codex Harness 指南

本页介绍 Codex 专属的镜像、Responses API、reasoning、native session 和排错。Task 字段见 [Task 格式参考](../tasks/task-format.md)，各 Harness 的产物说明见 [轨迹产出文件指南](../traces/trajectory-outputs.md)。

## 1. 适用范围

Codex Harness 支持：

- 单轮 `prompt`。
- 预定义多轮 `messages`，后续消息使用 `codex exec resume --last` 延续同一 session。
- Task Bundle、只读 inputs、Context、`runtime.setup` 和完整 `output_dir` 回收。
- 并行、retry、run lock、Resume 完整性检查。
- Codex native session JSONL、`ACTF_v1.0`、单条 SFT 和 run 级 `sft.jsonl`。
- 默认 `--wire-trace actf` 捕获原始请求工具定义，`--wire-trace full` 额外保留完整请求。
- 动态 User Simulator 和 Task Agent。Simulator 可通过 `user_simulator.harness`
  独立选择并使用不同 model。
- Viewer 展示可见 reasoning、工具调用、工具结果和最终回复。

当前不支持：

- OpenClaw 专属的 cache trace。
- API 未以明文或可见 summary 返回的内部 reasoning。
- 仅提供 Chat Completions、没有 Codex 所需 Responses 接口的模型服务。

## 2. 镜像准备

Roller 使用 [`docker/codex/Dockerfile`](../../../docker/codex/Dockerfile)，基础镜像为
`node:22-bookworm-slim`，默认固定安装 `@openai/codex@0.149.0`。镜像还包含 Git、ripgrep、
Python、curl、jq、SSH client 等常用任务工具。

在线环境：

```bash
docker pull --platform linux/amd64 node:22-bookworm-slim
docker build \
  --platform linux/amd64 \
  --build-arg CODEX_VERSION=0.149.0 \
  -f docker/codex/Dockerfile \
  -t roller-codex:0.149.0 \
  .
```

离线基础镜像：

```bash
docker load -i /path/to/node-22-bookworm-slim-linux-amd64.tar.gz
docker build \
  --platform linux/amd64 \
  --build-arg CODEX_VERSION=0.149.0 \
  -f docker/codex/Dockerfile \
  -t roller-codex:0.149.0 \
  .
```

Debian 镜像参数 `DEBIAN_MIRROR`、`DEBIAN_SECURITY_MIRROR` 的用法见 [Docker 构建与镜像源](../start/docker-builds.md)。npm 下载可通过 `NPM_REGISTRY` 指定镜像。未设置时沿用 npm 原有配置。

如果默认 Debian 软件源在当前网络较慢，可在构建时覆盖镜像地址，而无需修改
Dockerfile：

```bash
docker build \
  --platform linux/amd64 \
  --build-arg CODEX_VERSION=0.149.0 \
  --build-arg DEBIAN_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian \
  --build-arg DEBIAN_SECURITY_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian-security \
  -f docker/codex/Dockerfile \
  -t roller-codex:0.149.0 \
  .
```

验证：

```bash
docker run --rm roller-codex:0.149.0 codex --version
```

若构建机与采集机不是同一个 Docker daemon，可以导出后在目标 worker 加载：

```bash
docker save -o roller-codex-0.149.0-linux-amd64.tar roller-codex:0.149.0
docker load -i roller-codex-0.149.0-linux-amd64.tar
```

可以通过 CLI 或环境变量选择其他已构建镜像：

```bash
export ROLLER_CODEX_IMAGE=roller-codex:0.149.0
# 或 roller collect ... --docker-image roller-codex:0.149.0
```

不要在正式数据集中使用未锁定的 `@openai/codex@latest`。Codex native event 会随 CLI
演进。Roller 会把实际 `codex_version`、镜像 tag 和镜像 ID 写入 run/sample 信息及运行锁。

## 3. 环境变量与 API

Codex Harness 使用 Responses 风格的 agentic API。服务需要正确处理 Responses streaming、reasoning、tool call 和 tool result。`/v1/chat/completions` 请求成功不能证明该服务可供 Codex 使用。

| 配置项 | 默认读取 | CLI 覆盖 |
| --- | --- | --- |
| API 地址 | `OPENAI_BASE_URL` | `--codex-api-base`，单一 Harness 运行也可用 `--api-base` |
| API Key | `OPENAI_API_KEY` | `--codex-api-key-env`，单一 Harness 运行也可用 `--api-key-env` |
| 模型 ID | task 的 `model` 或 `--model` | 原样交给 Codex |
| API transport | Codex 内置策略 | 自定义 API 地址默认使用 Responses HTTP streaming |

推荐把 Key 放在自定义宿主机环境变量中，再把变量名传给 Roller：

```bash
export ROLLER_CODEX_KEY='...'

roller collect \
  --harness codex \
  --tasks examples/tasks/tasks.jsonl \
  --model gpt-5.6-sol \
  --api-base http://your-api.example/v1 \
  --api-key-env ROLLER_CODEX_KEY \
  --docker-image roller-codex:0.149.0 \
  --output runs \
  --run-name codex-gpt
```

`--api-base` 未提供时读取宿主机 `OPENAI_BASE_URL`。`--api-key-env` 的值是环境变量名，不是 Key 本身。Key 不会写入 `info.json` 或 `lock.json`。运行锁只记录变量名，provenance 中的 API 地址会移除 query 和凭据。

提供自定义 API 地址时，Roller 会创建隔离的 Codex provider，固定使用 `wire_api="responses"`，并默认设置 `supports_websockets=false`。这适用于只实现 Responses HTTP streaming 的兼容服务。只有确认服务支持 Responses WebSocket 时才使用 `--codex-websockets`。

默认 `--wire-trace actf` 和 `full` 在配置 API 地址时通过当前 rollout 的 HTTP 代理捕获工具，需要 `--network host` 和默认的 `--no-codex-websockets`。未配置 API 地址时仍使用 Codex 内置 provider，可保留 native raw/ACTF，但因无法获取原始工具定义而不导出 SFT。`full` 会拒绝缺少 API 地址或启用 WebSocket 的配置。

(codex-cli-flags)=
## 4. Codex 专属参数

下面只列 Codex 专属运行参数。API 地址和 Key 的配置方式见第 3 节。所有 Harness 共用的参数见 [CLI 参数参考](../start/cli-reference.md)。

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `--docker-image` / `--codex-image` | `ROLLER_CODEX_IMAGE` 或 `roller-codex:0.149.0` | 覆盖 Codex 镜像 |
| `--codex-config` | 无 | 注入隔离容器的原生 `config.toml` |
| `--reasoning-summary` | Codex 默认值 | `auto`、`concise`、`detailed` 或 `none` |
| `--codex-web-search` | `disabled` | `disabled`、`cached` 或 `live` |
| `--codex-websockets` / `--no-codex-websockets` | 关闭 | 自定义 provider 是否使用 Responses WebSocket |

单一 Codex 运行可以使用通用参数形式。混合 Harness 运行应使用 `--codex-image`、`--codex-api-base` 和 `--codex-api-key-env` 等专属形式，避免把设置应用到其他参与者。

若需要特殊 model provider、MCP 或其他 Codex 配置，可以提供：

```bash
roller collect ... \
  --codex-config examples/config/codex/config.toml.example
```

该文件原样复制到 rollout 容器的 `$CODEX_HOME/config.toml`。Resume 锁保存配置内容的 SHA-256，不把可能含密钥的 TOML 内容复制进 run。显式 CLI 的 API 地址、model、effort、summary 和 web search 设置仍覆盖对应运行项。示例文件包含 Roller 未覆盖的 `model_verbosity`，并提供默认注释掉的 MCP server 模板。不要在其中保存 API Key。

## 5. Reasoning 配置

`--thinking LEVEL` 在 Codex Harness 中映射为：

```toml
model_reasoning_effort = "LEVEL"
```

例如：

```bash
roller collect ... \
  --harness codex \
  --thinking xhigh \
  --reasoning-summary detailed
```

Task 的 `runtime.thinking` 优先于 CLI。Roller 会把
`minimal/low/medium/high/xhigh` 交给 Codex，再由模型和 API 服务验证。其中 `xhigh` 取决于
模型是否支持。Codex CLI 0.149.0 不接受 `max`/`ultra`，Roller 会提前报错。OpenClaw 专用的
`adaptive` 也会提前拒绝。`off`/`none` 同样会拒绝，因为 Codex Harness 没有
一个能对所有 reasoning 模型关闭推理的通用开关。若要使用模型/CLI 默认值，
直接省略 `--thinking`。

`--reasoning-summary` 可选值：

- `auto`
- `concise`
- `detailed`
- `none`

Codex native session 可能保存明文 `response_item.reasoning.content`、可见的
`response_item.reasoning.summary`，以及不可公开的加密 reasoning。Roller 优先保留实际存在的
明文 content，没有明文时保留 summary。不可公开的内容只保留在 native raw 中。OpenAI reasoning
模型通常只暴露 summary，开源 Responses 服务也可能直接返回明文 reasoning content。
选择 `none` 时没有 summary 是预期行为，但若服务另行返回明文 content，Roller 仍会按
`--keep-thinking` 保存它。

开源 Responses 模型如果返回明文 reasoning content，默认 `--keep-thinking` 会直接将其投影到
ACTF/SFT `reasoning_content`。

`--no-keep-thinking` 会在 native raw→ACTF 时清空 `reasoning_content`。Codex native
`trace.jsonl` 仍保持原样，而且该选项目前不改写 Codex 自己用于后续 `resume` 的上下文。
因此它是导出控制，不是 raw 脱敏或双向 context 剥离开关。

## 6. Web Search

为了减少不可重放的外部依赖，Codex Harness 默认设置：

```text
--codex-web-search disabled
```

可以显式改成 `cached` 或 `live`。Native session 中存在 `web_search_end` 时，Roller 会将
query/action 和记录到的 results 转成一个 `web_search` tool call/result，供 ACTF、SFT 和
Viewer 使用。

## 7. Context 与工作区

Codex 容器工作目录为 `/roller/workspace`：

- inputs 保持 Task Bundle 中的相对路径并以只读方式挂载。
- Context 文件复制到 workspace，`AGENTS.md` 会按 Codex 的正常发现规则生效。
- `role=skill` 的 `skills/` 内容还会复制到 `$HOME/.agents/skills/`。
- `runtime.setup` 在 workspace 中、首个 Codex turn 之前执行。
- `files.output_dir` 在 workspace 内保持可写，结束时安全回收到 sample 的 `results/`。

预定义 `messages` 的第一条调用 `codex exec`，后续消息调用 `codex exec resume --last`，共用
同一个隔离的 `$CODEX_HOME`。每个 rollout 使用独立容器和 Codex home，不会读取宿主机
`~/.codex` 的账号、session 或全局配置。

## 8. 轨迹产物

Codex sample 的默认结构与 OpenClaw 相同：

```text
<sample>/
├── trace.jsonl
├── trace_ACTF.json
├── tools_capture.jsonl      # 每次请求的原始工具定义
├── trace_SFT.json
├── info.json
└── results/                 # 有输出时
```

### `trace.jsonl`

这是 `$CODEX_HOME/sessions/<date>/rollout-*.jsonl` 中最新的 native session 文件，保持
Codex 事件结构，包括 `session_meta`、`turn_context`、`response_item`、`event_msg`、压缩事件
和 token usage。它属于 Codex 私有 schema，不承诺跨 CLI 版本稳定，也不是逐请求 HTTP wire
trace。

### `trace_ACTF.json`

转换器使用 `token_count` 作为一次模型请求的边界，把分离的 reasoning、assistant message、
多个 function/custom tool call 组合成一个 ACTF step，再按 `call_id` 配对稍后到达的 tool
output。顶层 system prompt 来自 `session_meta.base_instructions`、native developer/system
message，以及 Codex 在首轮前注入的 `<environment_context>`。Task 中的用户消息按内容
匹配，避免把环境事件当作用户消息或分配到错误轮次。

工具定义来自实际发出的 Responses 请求，包括模型未调用的工具，不再根据调用参数推导 schema。ACTF 保存 `tools_format: "responses"`、原样的 `tools` 列表，以及含每次请求快照的 `tools_capture` 来源信息。description、required、enum、custom grammar、namespace 和原始顺序均保留。

`tools_capture.jsonl` 在常规清理后仍保留，仅保存工具相关字段：`tools`、`tool_choice`、`parallel_tool_calls` 和类型为 `additional_tools` 的 input item，不包含普通消息和 HTTP header。schema 按原始 JSON 值保存，不经过通用敏感字段名脱敏，以免破坏名为 `token` 的合法参数。description 和 default 等内容也属于原始 harness 数据。

### `trace_SFT.json` 与 `sft.jsonl`

纯文本 SFT 顶层包含 `messages` 和 `tools`，图片样本额外包含 `images`。Codex 的 `tools` 保持原始 Responses 格式，包括 namespace、custom 和内置工具，不统一改写成 Chat Completions 的嵌套 function。custom call 使用 `type: "custom"` 并保留原始字符串 `input`，namespace 字段也原样保留。Assistant 固定包含
`reasoning_content`、`content`、`tool_calls`。工具结果转成 `role=tool`。Run 级 `sft.jsonl`
只汇总成功或可信 Resume 的样本。

捕获缺失或损坏、各请求间工具定义变化、存在 programmatic `additional_tools` input item 时，SFT 会明确标记为不可用，保留 raw 与完整请求快照。单个顶层 `tools` 无法忠实表达这些变化或其他定义形式。即使模型没有调用工具，也必须捕获到请求才能确认工具列表为空；`--wire-trace off` 因此不产出 Codex SFT。

## 9. Native raw 与 wire trace

Codex 可能在 native session 与真实模型请求之间加入 instruction、压缩历史、选择性重放
tool result，或把内部事件转换为 Responses input。`trace.jsonl` 和由它生成的 ACTF/SFT 只能
证明 Codex session 记录了什么，不能证明每次模型调用最终收到的完整 JSON context。

Codex Harness 支持 `--wire-trace full`：Roller 把 custom provider 的 API base 临时指向
rollout-local 代理，在转发给真实 `--api-base` 前保存脱敏后的完整 JSON request body。
Codex 请求中的工具定义可位于 `input` 的 `additional_tools` item。该文件是 request-side 证据，不保存
上游 response body，也不能说明模型服务内部如何再次截断、路由或 tokenize。

默认 `--wire-trace actf` 保留 `tools_capture.jsonl`，转换后删除临时 wire 文件，在不记录完整消息正文的情况下捕获原始工具定义。检查完整请求上下文需使用 `--wire-trace full` 并设置合适的日志访问权限。捕获证明的是发往 API 的请求，不能验证上游服务内部的再次改写。

## 10. 常见问题

### `Codex requires a non-empty API key`

`--api-key-env` 指向的宿主机变量不存在或为空。检查变量名，不要把 Key 直接写进 task。

### `/responses` 返回 404 或协议错误

API 服务只有 Chat Completions，或 Responses streaming/tool protocol 不兼容。换用支持
Responses 的服务。OpenClaw Harness 可继续用于只支持 Chat Completions 的模型服务。

### `Codex native trace produced no assistant steps`

CLI 在产生有效模型事件前退出，或所用 Codex 版本的 native event schema 已变化。先查看
sample 保留的错误和 `--keep-container-on-error` 容器，再核对 `codex_version`。升级 CLI 时要
同步更新转换器测试。

### User Simulator

Codex 可作为 Task Agent 或 User Simulator。Task Agent 在 task 顶层设置
`"harness": "codex"`，Simulator 则在 `user_simulator.harness` 设置。两者的
`model` 也可以分别指定。动态 session 使用 Codex 的 `resume --last` 保持上下文。
