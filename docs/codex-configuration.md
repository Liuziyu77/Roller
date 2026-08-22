# Codex Harness 指南

本文说明如何构建 Roller 的 Codex CLI 镜像、连接 API、控制 reasoning effort，以及理解
Codex native raw、ACTF 和 SFT 之间的信息边界。

## 1. 当前支持范围

Codex Harness 支持：

- 单轮 `prompt`；
- 预定义多轮 `messages`，后续消息使用 `codex exec resume --last` 延续同一 session；
- Task Bundle、只读 inputs、Context、`runtime.setup` 和完整 `output_dir` 回收；
- 并行、retry、run lock、Resume 完整性检查；
- Codex native session JSONL、`ACTF_v1.0`、单条 SFT 和 run 级 `sft.jsonl`；
- `external` / `external_always` 显式 CoT，以及 request-side `--wire-trace full`；
- Viewer 展示可见 reasoning、工具调用、工具结果和最终回复。

当前不支持：

- 以 Codex 作为动态 User Simulator 或动态任务 Agent；包含 `user_simulator` 的 task 会在
  启动 Docker 前被拒绝；
- OpenClaw 专属的 cache trace；
- 从 native session 恢复未公开或加密的完整内部 reasoning；
- 仅提供 Chat Completions、没有 Codex 所需 Responses 接口的模型服务。

## 2. 构建固定版本镜像

Roller 使用 [`docker/codex/Dockerfile`](../docker/codex/Dockerfile)，基础镜像为
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
演进；Roller 会把实际 `codex_version`、镜像 tag 和镜像 ID 写入 run/sample 信息及运行锁。

## 3. API 与 Key

推荐把 Key 放在自定义宿主机环境变量中，再用 `--api-key-env` 映射为容器内短期存在的
`OPENAI_API_KEY`：

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

`--api-base` 未提供时读取宿主机 `OPENAI_BASE_URL`。Key 值不会写入 `info.json` 或
`lock.json`；运行锁只记录 Key 的环境变量名。API base 会删除 query 和凭据后写入
provenance。

提供 `--api-base` 时，Roller 会创建一个隔离的 Codex custom provider，固定使用
`wire_api="responses"`，并默认设置 `supports_websockets=false`。这可兼容只实现
Responses HTTP streaming、没有 WebSocket 握手的 OpenAI-compatible 服务。只有确认服务
支持 Responses WebSocket 时才增加 `--codex-websockets`。`--external-thinking` 和
`--wire-trace full` 使用 rollout-local HTTP 代理，因此必须显式提供 `--api-base`、保持
`--no-codex-websockets` 与默认 `--network host`。未设置 `--api-base` 时，Codex 使用自己的
内置 OpenAI provider 和 transport 策略。

Codex CLI 使用 Responses 风格的 agentic API。`curl /v1/chat/completions` 成功并不能证明
服务可供 Codex 使用；服务还必须正确处理 Codex 发出的 Responses streaming、reasoning、
tool call 和 tool result。若需要特殊 model provider、MCP 或其他 Codex 配置，可以提供：

```bash
roller collect ... --codex-config /path/to/config.toml
```

该文件原样复制到 rollout 容器的 `$CODEX_HOME/config.toml`。Resume 锁保存配置内容的
SHA-256，而不把可能含密钥的 TOML 内容复制进 run；显式 CLI 的 API base、model、effort、
summary 和 web-search 设置仍覆盖对应运行项。

## 4. Reasoning 配置

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
`minimal/low/medium/high/xhigh` 交给 Codex，再由模型和 API 服务验证；其中 `xhigh` 取决于
模型是否支持。Codex CLI 0.149.0 不接受 `max`/`ultra`，Roller 会提前报错；OpenClaw 专用的
`adaptive` 也会提前拒绝。`off`/`none` 同样会拒绝，因为 Codex Harness 没有
一个对所有 reasoning 模型都忠实等价的“关闭 reasoning”开关。若要使用模型/CLI 默认值，
直接省略 `--thinking`。

`--reasoning-summary` 可选值：

- `auto`
- `concise`
- `detailed`
- `none`

Codex native session 可能保存明文 `response_item.reasoning.content`、可见的
`response_item.reasoning.summary`，以及不可公开的加密 reasoning。Roller 优先保留实际存在的
明文 content；没有明文时保留 summary；不会解密、猜测或伪造内部 CoT。OpenAI reasoning
模型通常只暴露 summary，开源 Responses 服务也可能直接返回明文 reasoning content。
选择 `none` 时没有 summary 是预期行为，但若服务另行返回明文 content，Roller 仍会按
`--keep-thinking` 保存它。

`--no-keep-thinking` 会在 native raw→ACTF 时清空 `reasoning_content`。Codex native
`trace.jsonl` 仍保持原样，而且该选项目前不改写 Codex 自己用于后续 `resume` 的上下文；
因此它是导出控制，不是 raw 脱敏或双向 context 剥离开关。

### External thinking

如果希望绕过只暴露压缩 summary 的 native reasoning，可让模型把显式中间推理写入
Roller 的本地 `deep_think` MCP 工具：

```bash
export ROLLER_CODEX_KEY='...'

roller collect \
  --harness codex \
  --tasks tasks.jsonl \
  --model gpt-5.6-sol \
  --api-base http://your-api.example/v1 \
  --api-key-env ROLLER_CODEX_KEY \
  --external-thinking external_always \
  --wire-trace full \
  --timeout 1200
```

- `external`：首次请求强制一次成功的 deep-think MCP 调用，之后按需；
- `external_always`：每个业务动作或 final answer 前都要求一个独立 THINK response；
- 两种模式都忽略 CLI/task 的 reasoning effort，最终请求固定
  `reasoning.effort="none"`，`--reasoning-summary` 也不会下发；
- Roller 自动把无权限的 stdio MCP server 复制到一次性容器并用 CLI override 注册，不需要
  修改 Codex 镜像或用户的 `config.toml`；
- ACTF 保留 canonical `deep_think` call/result，SFT 删除工具协议并把 thoughts 合并进下一条
  assistant 的 `reasoning_content`。

详细的阶段协议、raw 事件形态和校验规则见
[External Thinking 指南](external-thinking.md)。

建议先用一条短 task 配合 `--wire-trace full` 验收。成功的 `external_always` 在 sample
`info.json` 中应为 `validated=true`，wire phase 应严格按 `think/act` 交替；Viewer 展示
ACTF 中的 canonical `deep_think` 工具步骤，折叠后的训练 reasoning 则位于
`trace_SFT.json`。可复制的检查命令见
[如何查看和验收](external-thinking.md#7-如何查看和验收)。

## 5. Web search

为了减少不可重放的外部依赖，Codex Harness 默认设置：

```text
--codex-web-search disabled
```

可以显式改成 `cached` 或 `live`。Native session 中存在 `web_search_end` 时，Roller 会将
query/action 和记录到的 results 转成一个 `web_search` tool call/result，供 ACTF、SFT 和
Viewer 使用。

## 6. Context 与工作区

Codex 容器工作目录为 `/roller/workspace`：

- inputs 保持 Task Bundle 中的相对路径并以只读方式挂载；
- Context 文件复制到 workspace，`AGENTS.md` 会按 Codex 的正常发现规则生效；
- `role=skill` 的 `skills/` 内容还会复制到 `$HOME/.agents/skills/`；
- `runtime.setup` 在 workspace 中、首个 Codex turn 之前执行；
- `files.output_dir` 在 workspace 内保持可写，结束时安全回收到 sample 的 `results/`。

预定义 `messages` 的第一条调用 `codex exec`，后续消息调用 `codex exec resume --last`，共用
同一个隔离的 `$CODEX_HOME`。每个 rollout 使用独立容器和 Codex home，不会读取宿主机
`~/.codex` 的账号、session 或全局配置。

## 7. 轨迹产物

Codex sample 的默认结构与 OpenClaw 相同：

```text
<sample>/
├── trace.jsonl
├── trace_ACTF.json
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
message，以及 Codex 在首轮前注入的 `<environment_context>`；真正的 task 用户消息按内容
匹配，不会被环境事件挤占或串到错误轮次。

Codex native session 不提供每个工具的完整原始 JSON Schema。为了产出严格 SFT，Roller 会
基于实际调用名和参数字段生成 permissive function schema；它足以保持 tool call 的名称、
参数和结果，但不能当作 Codex 内置工具接口的完整权威定义。

external thinking 在 native raw 中保持 Codex 的真实两层 transport：外层是模型生成的
`custom_tool_call(name="exec")`，内层成功调用由
`event_msg.item_completed` 的 `McpToolCall` item 记录，也可能由另一条 native writer 路径
写成 `event_msg.mcp_tool_call_end`。ACTF 转换器兼容两者，用 native event 的 server、tool
和 arguments 将其规范化为直接的 `deep_think` call；不会依赖或猜测 JavaScript 字符串中
的 thoughts。

### `trace_SFT.json` 与 `sft.jsonl`

和 OpenClaw 一样，单条 SFT 顶层严格只有 `messages` 和 `tools`。Assistant 固定包含
`reasoning_content`、`content`、`tool_calls`；工具结果转成 `role=tool`。Run 级 `sft.jsonl`
只汇总成功或可信 Resume 的样本。

## 8. Native raw 不是 wire trace

Codex 可能在 native session 与真实模型请求之间加入 instruction、压缩历史、选择性重放
tool result，或把内部事件转换为 Responses input。`trace.jsonl` 和由它生成的 ACTF/SFT 只能
证明 Codex session 记录了什么，不能证明每次模型调用最终收到的完整 JSON context。

Codex Harness 支持 `--wire-trace full`：Roller 把 custom provider 的 API base 临时指向
rollout-local 代理，在转发给真实 `--api-base` 前保存脱敏后的完整 JSON request body。
Codex 请求中的工具定义位于 `input` 的 `additional_tools` item；external THINK 阶段只保留
programmatic `functions.exec`，ACT 阶段恢复原始工具。该文件是 request-side 证据，不保存
上游 response body，也不能说明模型服务内部如何再次截断、路由或 tokenize。

默认 `--wire-trace actf` 不保留额外 wire 文件；Codex ACTF 的工具 schema 来自 native 实际
调用的 permissive 投影，而不是完整 request schema。需要核对“模型某次实际看到了什么”时
必须显式使用 `--wire-trace full`，并配合足够严格的日志访问权限。

## 9. 常见错误

### `Codex requires a non-empty API key`

`--api-key-env` 指向的宿主机变量不存在或为空。检查变量名，不要把 Key 直接写进 task。

### `/responses` 返回 404 或协议错误

API 服务只有 Chat Completions，或 Responses streaming/tool protocol 不兼容。换用支持
Responses 的服务；OpenClaw Harness 可继续用于只支持 Chat Completions 的模型服务。

### `Codex native trace produced no assistant steps`

CLI 在产生有效模型事件前退出，或所用 Codex 版本的 native event schema 已变化。先查看
sample 保留的错误和 `--keep-container-on-error` 容器，再核对 `codex_version`；升级 CLI 时要
同步更新转换器测试。

### User Simulator 被拒绝

这是当前显式限制。先用 `prompt`/`messages` 采集 Codex；动态双方对话继续使用 OpenClaw。

### external thinking 报 programmatic custom exec 缺失

所用 Codex 版本没有暴露 Roller 已验证的 programmatic custom `exec`，或启动参数关闭了
相关能力。优先使用固定的 `roller-codex:0.149.0`；升级 Codex 后应重新跑阶段控制与 native
事件转换测试，不要静默退化为未受控的普通工具调用。
