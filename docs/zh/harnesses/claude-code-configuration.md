# Claude Code Harness 指南

本页介绍 Claude Code 专属的镜像、Anthropic Messages API、thinking、native session 和排错。Task 字段见 [Task 格式参考](../tasks/task-format.md)，各 Harness 的产物说明见 [轨迹产出文件指南](../traces/trajectory-outputs.md)。

> 快速定位：支持范围（第 1 节）· 镜像（第 2 节）· API（第 3 节）· thinking（第 4–5 节）· native/ACTF/SFT/wire（第 8–9 节）· 排错（第 10 节）

## 1. 当前支持范围

Claude Code Harness 支持：

- 单轮 `prompt`；
- 预定义多轮 `messages`，后续消息使用 `claude --continue` 延续同一 session；
- Task Bundle、只读 inputs、Context、`runtime.setup` 和完整 `output_dir` 回收；
- 并行、retry、run lock 和 Resume 完整性检查；
- Claude Code 主 session 及 subagent sidechain native JSONL；
- 明文 `thinking/reasoning/analysis`、文本、多个 `tool_use`、`tool_result` 和 usage 转换；
- `ACTF_v1.0`、单条 SFT、run 级 `sft.jsonl` 和 Viewer；
- `--wire-trace actf|full|off`；
- 动态 User Simulator 和 Task Agent；两者可使用不同 model，Simulator 通过
  `user_simulator.harness` 独立选择 Harness；
- Claude Code `settings.json`、`--max-turns`、`MAX_THINKING_TOKENS` 和 permission mode；

当前不支持：

- 只有 OpenAI Chat Completions/Responses、没有 Anthropic `/v1/messages` 的 API；
- 从 `redacted_thinking` 或 `thinking.signature` 恢复明文。密文保留在 raw，不写入 ACTF/SFT reasoning；不发起额外恢复请求。

## 2. 构建固定版本镜像

Roller 使用 [`docker/claude-code/Dockerfile`](../../../docker/claude-code/Dockerfile)，基础镜像为
`node:22-bookworm-slim`，默认固定安装 `@anthropic-ai/claude-code@2.1.222`。Codex 已使用的
Node 22 离线归档可以直接复用，不需要重新下载基础镜像。

在线基础镜像：

```bash
docker pull --platform linux/amd64 node:22-bookworm-slim
docker build \
  --platform linux/amd64 \
  --build-arg CLAUDE_CODE_VERSION=2.1.222 \
  -f docker/claude-code/Dockerfile \
  -t roller-claude-code:2.1.222 \
  .
```

离线基础镜像：

```bash
docker load -i /path/to/node-22-bookworm-slim-linux-amd64.tar.gz
docker build \
  --platform linux/amd64 \
  --build-arg CLAUDE_CODE_VERSION=2.1.222 \
  -f docker/claude-code/Dockerfile \
  -t roller-claude-code:2.1.222 \
  .
```

Debian 镜像参数 `DEBIAN_MIRROR`、`DEBIAN_SECURITY_MIRROR` 的用法见 [Docker 构建与镜像源](../start/docker-builds.md)。npm 下载可通过 `NPM_REGISTRY` 指定镜像；未设置时沿用 npm 原有配置。

默认 Debian 软件源不可达时可以传入镜像源：

```bash
docker build \
  --platform linux/amd64 \
  --build-arg CLAUDE_CODE_VERSION=2.1.222 \
  --build-arg DEBIAN_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian \
  --build-arg DEBIAN_SECURITY_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian-security \
  -f docker/claude-code/Dockerfile \
  -t roller-claude-code:2.1.222 \
  .
```

验证并设置默认镜像：

```bash
docker run --rm roller-claude-code:2.1.222 claude --version
export ROLLER_CLAUDE_CODE_IMAGE=roller-claude-code:2.1.222
```

正式数据集应固定 Claude Code 版本。Native session 字段、permission mode 和 effort 列表会随
CLI 演进；Roller 会把实际 `claude_code_version`、镜像 tag 和镜像 ID 写入 run/sample 信息。

(claude-code-cli-flags)=
## 3. CLI 专属参数、Anthropic API 与 Key

下表集中列出 Claude Code 专属入口。单一 Claude Code 运行可以使用通用形式，混合 Harness 运行应使用 Claude Code 专属形式。

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `--docker-image` / `--claude-code-image` | `ROLLER_CLAUDE_CODE_IMAGE` 或 `roller-claude-code:2.1.222` | 覆盖 Claude Code 镜像 |
| `--api-base` / `--anthropic-base-url` / `--claude-api-base` | `ANTHROPIC_BASE_URL` | Anthropic API 地址或完整 `/v1/messages` endpoint |
| `--api-key-env` / `--claude-api-key-env` | `ANTHROPIC_API_KEY` | 存放 API Key 的宿主机变量名 |
| `--claude-settings` | 无 | 注入隔离容器的原生 `settings.json` |
| `--claude-max-turns` | 无 | 每条预定义 user message 的 agent turn 上限 |
| `--claude-max-thinking-tokens` | 无 | 传给 Claude Code 的 `MAX_THINKING_TOKENS` |
| `--claude-permission-mode` | `bypassPermissions` | Claude Code permission mode |

旧的 `--claude-signature-recovery*` 参数已移除。旧命令应直接删除这些参数。


Claude Code 使用 Anthropic Messages API。推荐把 Key 放在自定义宿主机环境变量中，再由
`--api-key-env` 映射为一次性容器内的 `ANTHROPIC_API_KEY`：

```bash
export ROLLER_ANTHROPIC_KEY='...'

roller collect \
  --harness claude-code \
  --tasks examples/tasks/tasks.jsonl \
  --model claude-sonnet-4-6-thinking \
  --api-base http://your-api.example/v1/messages \
  --api-key-env ROLLER_ANTHROPIC_KEY \
  --docker-image roller-claude-code:2.1.222 \
  --thinking high \
  --output runs \
  --run-name claude-code-demo
```

`--api-base`、`--anthropic-base-url` 是同一个参数的两个名称，接受以下形式：

```text
http://host:port
http://host:port/v1
http://host:port/v1/messages
```

Roller 会将末尾 `/v1` 或 `/v1/messages` 去掉，再写入 `ANTHROPIC_BASE_URL`；Claude Code
随后请求唯一的 `/v1/messages`，避免出现 `/v1/v1/messages`。Claude Code 2.1.222
还会在 SDK 请求上追加 `?beta=true` 和 `anthropic-beta` header；使用 Roller
wire 代理时，这两个 transport-only 标记会在转发给兼容服务前移除，其他 query
参数和 header 保留。如果不传该参数，则读取宿主机
`ANTHROPIC_BASE_URL`。Key 参数默认读取 `ANTHROPIC_API_KEY`。

Key 值不会写入 run、lock、raw 或 provenance；只记录 Key 的环境变量名。API base 会删除
query 和 URL 凭据后再写入元数据。完整 endpoint 可以用以下最小请求预检：

```bash
curl -sS -X POST 'http://host:port/v1/messages' \
  -H 'Content-Type: application/json' \
  -H 'anthropic-version: 2023-06-01' \
  -H "x-api-key: $ROLLER_ANTHROPIC_KEY" \
  -d '{
    "model":"claude-sonnet-4-6-thinking",
    "max_tokens":128,
    "messages":[{"role":"user","content":"Reply OK"}]
  }'
```

模型出现在 `/v1/models` 不保证当前账号存在可用 channel，应以实际 `/v1/messages` 请求为准。

## 4. Thinking 与 effort

`--thinking LEVEL` 由 Claude Code Harness 映射到 `claude --effort LEVEL`：

| Roller 输入 | Claude Code 2.1.222 参数 | 说明 |
| --- | --- | --- |
| 省略 | 不传 `--effort` | 使用 CLI/model 默认行为 |
| `low` | `low` | 低 effort |
| `medium` | `medium` | 中 effort |
| `high` | `high` | 高 effort |
| `xhigh` | `xhigh` | 依赖模型支持 |
| `max` | `max` | 依赖模型支持 |
| `ultra` | `ultracode` | Roller 逻辑名到固定 CLI 的名称映射 |

`off/none`、`minimal` 和 `adaptive` 没有对应的 Claude Code CLI 档位，因此会在容器启动
前拒绝，不会静默降级。Task 的 `runtime.thinking` 优先于命令行默认值。

还可以设置显式 thinking token 上限：

```text
--claude-max-thinking-tokens 4096
```

它通过 `MAX_THINKING_TOKENS` 传给 Claude Code。不同模型可能使用 budget thinking 或 adaptive
thinking；模型/provider 不支持该组合时，应省略此参数并只使用 `--thinking`。

Claude Code native session 中实际存在的 `thinking`、`reasoning` 或 `analysis` block 会进入
ACTF/SFT 的 `reasoning_content`。以下情况不会产生可训练明文：

- 服务只返回 thinking token 数而把 thinking 文本留空；
- Anthropic 返回不可解密的 `redacted_thinking`；
- 模型只在最终 `text` 中展示计算过程，而没有 reasoning block。

Roller 不根据 token 数、最终文本或签名推测隐藏 reasoning。`--no-keep-thinking` 会清空 ACTF/SFT
中的 reasoning，但不修改 raw session，也不修改后续 `--continue` 使用的 Claude Code context。

## 5. Reasoning 的保存边界

Roller 只保存服务实际返回的可见 thinking/summary，不还原签名、不补写 CoT，也不在任务结束后追加模型调用。`--keep-thinking` 默认保留这些文本；`--no-keep-thinking` 只清空 ACTF/SFT reasoning，raw 不变。

## 6. Claude settings 与权限

可以把已有 settings JSON 注入每个一次性容器：

```bash
roller collect ... \
  --claude-settings examples/config/claude-code/settings.json.example
```

文件复制到隔离的 `$CLAUDE_CONFIG_DIR/settings.json`，并通过 `--settings` 显式加载。为避免把
可能含环境变量或 hook 的 settings 内容复制进 run，运行锁只保存一个短 BLAKE2b 内容指纹；
文件改变时 Resume 会拒绝混用旧 sample。仓库示例不包含 Key、hook 或权限放宽，只关闭 commit
co-author 标记；按需复制后再增加自己的 Claude Code 设置。

自动化采集默认使用：

```text
--claude-permission-mode bypassPermissions
IS_SANDBOX=1
```

也可以按固定镜像支持的值选择其他 permission mode。`--claude-max-turns` 限制每次预定义
user message 内 Claude Code 的 agent turns；它不等于 task 的 `messages` 数量。

## 7. Context、工作区与多轮

Claude Code 容器工作目录是 `/roller/workspace`：

- inputs 保持 Task Bundle 中的相对路径并以只读方式挂载；
- Context 文件复制到 workspace；需要 Claude Code 自动加载的项目指令应把 target 设置为
  `CLAUDE.md`，例如 `{"role":"system","source":"context/CLAUDE.md","target":"CLAUDE.md"}`；
- `role=skill` 的 `skills/` 内容还会复制到隔离的 `$CLAUDE_CONFIG_DIR/skills/`；
- `runtime.setup` 在首轮前于 workspace 中执行；
- `files.output_dir` 保持可写并在结束时安全回收到 sample 的 `results/`。

预定义 `messages` 的第一条启动新 session，后续消息使用 `--continue`。每个 rollout 有独立
Docker 容器和 `$CLAUDE_CONFIG_DIR`，不会读取宿主机的 Claude 登录信息、settings 或 session。

## 8. 轨迹产物

默认 sample 结构与其他 Harness 一致：

```text
<sample>/
├── trace.jsonl
├── trace_ACTF.json
├── trace_SFT.json
├── info.json
└── results/                 # 有输出时
```

### `trace.jsonl`

Roller 从 `$CLAUDE_CONFIG_DIR/projects/**/<session-id>.jsonl` 选择本 rollout 的最新主 session，
同时读取 `<session-id>/subagents/*.jsonl`。事件按 timestamp 合并，并按原生 `uuid` 去重后保存为
`trace.jsonl`。事件对象本身不做训练清洗；`info.json.trajectory.raw_schema` 标记为
`claude-code-session-jsonl`。

这是 Claude Code native session 的合并导出，不是：

- CLI `--output-format=stream-json` 的终端展示流；
- provider 的 HTTP 请求或响应；
- 多个源 JSONL 的逐字节拼接副本。

Raw 保留 plaintext thinking、加密 thinking envelope、tool metadata、usage 和 sidechain 私有
字段，结构不保证跨 Claude Code 版本稳定。

### `trace_ACTF.json`

转换器按 Claude `message.id` 把一次推理的 text、reasoning 和多个 `tool_use` 合并为一个
ACTF step，再按 `tool_use_id` 关联后续 `tool_result`。主 session 和 subagent sidechain 都按
时间顺序进入轨迹；sidechain step 在 `extra.is_sidechain` 标记。

Usage 使用同一 `message.id` 的最后一个值，避免 stream 更新重复累计：

- `input_tokens`
- `cache_read_input_tokens`
- `cache_creation_input_tokens`
- `output_tokens`

CLI stream 中每次成功 result 的 `total_cost_usd` 会累计到 sample usage。

### `trace_SFT.json` 与 `sft.jsonl`

Collector 从 ACTF 生成仅含 `messages` 和 `tools` 的训练记录。Assistant 消息格式如下：

```json
{
  "role": "assistant",
  "reasoning_content": "visible thinking",
  "content": "visible answer",
  "tool_calls": []
}
```

Claude 原生 `tool_use` 转为 OpenAI-compatible function call，`tool_result` 转为对应 `role=tool`
消息。如果 wire 捕获了完整 Anthropic tool schema，会把 `input_schema` 标准化为
`function.parameters`；否则根据实际调用参数生成 permissive schema。

## 9. Wire trace

使用自定义 `--api-base` 时，Claude Code 默认的 `--wire-trace actf` 会启动 rollout-local
反向代理，临时捕获首个 system prompt、完整 tools schema 和非消息请求参数。生成 ACTF/SFT
后删除中间 `wire_trace.jsonl`，减少 sample 的存储占用。

需要核对模型每次真实收到的 JSON context 时使用：

```text
--wire-trace full
```

此时 sample 额外保留 `wire_trace.jsonl`，包含每次 Anthropic `/v1/messages` 请求的 system、
messages、tools、thinking/output config 和 sampling 参数。它记录的是转发给服务的请求 body，
不是 provider 响应。多轮请求会重复历史，因此不应直接作为 SFT 数据。
若发生兼容改写，`path` 保留 Claude Code 原始请求目标，`upstream_path`
记录实际转发目标，`dropped_request_headers` 只记录被移除的 header 名，不记录值。

`--wire-trace off` 不启动代理。此时 native session 仍能生成 raw/ACTF/SFT，但顶层
`system_prompt` 为空，tools schema 只能根据实际调用推导。Wire 代理需要容器使用默认
`--network host`。

## 10. 已验证行为与排错

实现阶段已用 Anthropic endpoint 和 Claude Code 2.1.207 做过以下协议探测，正式镜像固定为
2.1.222：

- `/v1/messages` 鉴权和普通文本响应正常；
- `claude-sonnet-4-6-thinking` + `--effort high` 的 native session 保存明文 thinking；
- Bash、Read 工具调用及结果进入 native session；
- 第二条 CLI 调用使用 `--continue` 后复用同一 session；
- 合并后的 ACTF/SFT 保留两条 user message、thinking、三个工具动作和最终回复。

常见错误：

- `Invalid token`：检查 `--api-key-env` 指向的宿主机变量；
- `No available channel for model`：模型虽然出现在列表中，但当前账号/分组没有可用路由；
- URL 出现 `/v1/v1/messages`：不要手工把 `ANTHROPIC_BASE_URL` 传入容器，改用 Roller
  `--api-base` 让它规范化；
- `invalid beta flag`：使用显式 `--api-base` 和默认 `--wire-trace actf/full`，
  Roller 会仅在 upstream 转发时移除 Claude SDK 的 `beta=true` query 和
  `anthropic-beta` header；
- model 拒绝 `budget_tokens` 或要求 adaptive thinking：移除
  `--claude-max-thinking-tokens`，只使用该模型支持的 `--thinking` effort；
- ACTF 有 thinking token 但 `reasoning_content` 为空：服务没有返回明文，不是转换器丢失；
