# Claude Code Harness 指南

Claude Code Harness 用于通过 Claude Code 执行需要终端、文件或图片工具的 task。它连接 Anthropic Messages 兼容服务。本页按照镜像、API、专属参数、会话和轨迹的顺序介绍首次使用所需内容。

Roller 当前固定使用 Claude Code 2.1.222。Task 字段见 [Task 格式参考](../tasks/task-format.md)，通用产物见 [轨迹产出文件指南](../traces/trajectory-outputs.md)。

首次使用时，先完成第 1 至第 3 节并运行示例命令。后面的专属参数、Thinking 和轨迹说明可在需要相应功能时再查看。

## 1. 适用范围

Claude Code Harness 适合以下场景：

- 使用实现 Anthropic `/v1/messages` 的模型服务。
- 让 Agent 运行命令、读写文件或通过 `Read` 工具查看图片。
- 运行单轮、多轮、Task Agent 或 User Simulator task。
- 需要保留 Claude Code 原生会话，并导出 ACTF、SFT 和 token 使用量。

只支持 OpenAI Chat Completions 或 Responses 的服务不能直接使用本 Harness。Task 消息目前也不能直接包含图片内容块。Roller 只能保存 API 实际返回的可见 thinking 或 summary，不能恢复隐藏的推理内容。

## 2. 镜像准备

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
docker load -i node-22-bookworm-slim-linux-amd64.tar.gz
docker build \
  --platform linux/amd64 \
  --build-arg CLAUDE_CODE_VERSION=2.1.222 \
  -f docker/claude-code/Dockerfile \
  -t roller-claude-code:2.1.222 \
  .
```

Debian 镜像参数 `DEBIAN_MIRROR`、`DEBIAN_SECURITY_MIRROR` 的用法见 [Docker 构建与镜像源](../start/docker-builds.md)。npm 下载可通过 `NPM_REGISTRY` 指定镜像。未设置时沿用 npm 原有配置。

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
CLI 演进。Roller 会把实际 `claude_code_version`、镜像 tag 和镜像 ID 写入 run/sample 信息。

## 3. 环境变量与 API

Claude Code Harness 需要实现 Anthropic Messages API 的模型服务。首次运行需要准备 API 地址、API Key 和模型 ID。

| 配置项 | 默认读取 | CLI 覆盖 |
| --- | --- | --- |
| API 地址 | `ANTHROPIC_BASE_URL` | `--claude-api-base`，单一 Harness 运行也可用 `--api-base` |
| API Key | `ANTHROPIC_API_KEY` | `--claude-api-key-env`，单一 Harness 运行也可用 `--api-key-env` |
| 模型 ID | task 的 `model` 或 `--model` | 原样交给 Claude Code |

推荐把 Key 放入一个宿主机环境变量，再把变量名传给 Roller：

```bash
export ROLLER_ANTHROPIC_KEY='...'

roller collect \
  --harness claude-code \
  --tasks examples/tasks/tasks.jsonl \
  --model model-id \
  --api-base https://api.example.com/v1/messages \
  --api-key-env ROLLER_ANTHROPIC_KEY \
  --docker-image roller-claude-code:2.1.222 \
  --limit 1 \
  --output runs \
  --run-name claude-code-demo
```

`--api-key-env` 接收的是环境变量名，不是 Key 本身。API 地址可以写到服务域名、`/v1` 或完整的 `/v1/messages`，Roller 会自动整理路径，避免重复追加 `/v1/messages`。

如果模型能被列出但运行仍然失败，可直接向 `/v1/messages` 发送一个最小请求，确认当前账号能够调用该模型。只有 `/v1/messages` 请求成功，才能证明该服务可供 Claude Code 使用。

(claude-code-cli-flags)=
## 4. Claude Code 专属参数

下面只列 Claude Code 专属运行参数。API 地址和 Key 的配置方式见第 3 节。所有 Harness 共用的参数见 [CLI 参数参考](../start/cli-reference.md)。

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `--docker-image` / `--claude-code-image` | `ROLLER_CLAUDE_CODE_IMAGE` 或 `roller-claude-code:2.1.222` | 覆盖 Claude Code 镜像 |
| `--claude-settings` | 无 | 注入隔离容器的原生 `settings.json` |
| `--claude-max-turns` | 无 | 每条预定义 user message 的 agent turn 上限 |
| `--claude-max-thinking-tokens` | 无 | 传给 Claude Code 的 `MAX_THINKING_TOKENS` |
| `--claude-permission-mode` | `bypassPermissions` | Claude Code permission mode |

单一 Claude Code 运行可以使用通用参数形式。混合 Harness 运行应使用 `--claude-code-image`、`--claude-api-base` 和 `--claude-api-key-env` 等专属形式，避免把设置应用到其他参与者。

## 5. 多模态能力

Claude Code Harness 同时支持纯文本模型和具备视觉能力的模型。图片通过 `files.inputs` 进入工作区，Claude Code 可以使用原生 `Read` 工具打开本地图片。图片工具结果会保留到原始轨迹、ACTF 和多模态 SFT，其中 SFT 使用 `<image>` 占位符和顶层 `images` 保存图片。

视觉任务还要求所选模型和 Anthropic-compatible API 服务支持图片内容块。Task 的 `messages[].content` 当前只支持字符串，不能直接写入图片内容块。纯文本模型可以继续处理文本和普通文件，但不能完成需要理解图片内容的任务。

## 6. Thinking 配置

`--thinking` 用来设置 Claude Code 的推理强度。省略时使用 Claude Code 和模型的默认值。

| Roller 输入 | Claude Code 参数 | 说明 |
| --- | --- | --- |
| 省略 | 不传 `--effort` | 使用默认行为 |
| `low` | `low` | 较低推理强度 |
| `medium` | `medium` | 中等推理强度 |
| `high` | `high` | 较高推理强度 |
| `xhigh` | `xhigh` | 仅在模型支持时使用 |
| `max` | `max` | 仅在模型支持时使用 |
| `ultra` | `ultracode` | Roller 名称到 Claude Code 名称的映射 |

`off`、`none`、`minimal` 和 `adaptive` 没有对应的 Claude Code 档位，因此会在启动前报错。首次使用某个模型时，建议省略 `--thinking` 或先选择服务明确支持的档位。

需要限制 thinking token 数时，可以额外使用：

```text
--claude-max-thinking-tokens 4096
```

不同模型对 token 上限的支持方式不同。如果服务拒绝该设置，应移除此参数，只保留 `--thinking`。

Claude Code 返回的可见 thinking、reasoning 或 analysis 会写入 ACTF/SFT 的 `reasoning_content`。服务只返回 token 数或加密内容时，该字段可能为空。`--no-keep-thinking` 只清除 ACTF/SFT 中的推理文本，不修改原始会话。

## 7. Claude settings 与权限

需要使用 Claude Code 原生设置时，可以提供一份 settings 文件：

```bash
roller collect ... \
  --claude-settings examples/config/claude-code/settings.json.example
```

Roller 会把该文件复制到当前容器并显式加载。修改 settings 后，不应继续复用旧 run 目录。示例文件不包含 API Key、hook 或额外权限，按需复制后再修改。

自动化采集默认使用 `bypassPermissions`，避免任务执行停在交互确认界面。也可以通过 `--claude-permission-mode` 选择当前 Claude Code 版本支持的其他模式。`--claude-max-turns` 限制的是每条用户消息内的 Agent 执行轮数，不是 task 的消息数量。

## 8. 会话与工作区

每个 Agent 或 Simulator 都在独立的一次性容器中运行，不会读取宿主机的 Claude 登录信息、settings 或历史会话。

- 输入文件只读，并保持 Task Bundle 中的相对路径。
- Context 文件复制到 task 声明的位置。需要 Claude Code 自动读取项目说明时，可将目标文件命名为 `CLAUDE.md`。
- `runtime.setup` 在第一轮开始前执行。
- 输出目录保持可写，结束后回收到 sample 的 `results/`。

预定义多轮消息会继续使用同一个 Claude Code 会话，因此对话历史和工作区文件可以在后续轮次中使用。

## 9. 轨迹与请求记录

一次运行会生成以下主要文件：

| 文件 | 用途 |
| --- | --- |
| `trace.jsonl` | 合并 Claude Code 主会话和 subagent 的原生事件，主要用于排错 |
| `trace_ACTF.json` | 将文本、thinking、工具调用和结果整理成统一结构 |
| `trace_SFT.json` | 将成功轨迹整理成单条训练数据 |
| `sft.jsonl` | 汇总当前 run 中可用于训练的成功 sample |

### 9.1 `trace.jsonl`

该文件保留 Claude Code 原生会话事件，包括可见 thinking、工具信息和 token 使用量。它不是终端输出，也不是模型服务收到的完整 HTTP 请求，文件格式可能随 Claude Code 版本变化。

### 9.2 `trace_ACTF.json`

ACTF 会把同一次回复中的文本、推理和多个工具调用整理到一起，并将每个工具结果与对应调用配对。主会话和 subagent 事件按时间顺序保存，usage 会去除流式更新造成的重复计数。

### 9.3 `trace_SFT.json` 与 `sft.jsonl`

SFT 使用统一的 `messages` 和 `tools` 结构。Claude 原生工具调用会转换成可训练的 function call，图片样本会额外包含 `images`。

### 9.4 Wire trace

默认 `--wire-trace actf` 只为 ACTF/SFT 捕获 system prompt 和工具定义，不保留完整请求。需要核对模型实际收到的内容时，可以临时使用：

```text
--wire-trace full
```

此时 sample 会额外保留 `wire_trace.jsonl`。该文件可能包含完整消息、工具参数和 thinking 配置，只适合排错或审计。`--wire-trace off` 不记录这些请求信息，仍会保留 Claude Code 原生会话。

## 10. 首次运行检查

首次运行建议只使用一个简单 task。完成后依次检查：

1. run 目录的 `info.json` 中 `state` 为 `finished`。
2. sample 的 `info.json` 中 `status` 为 `ok`。
3. sample 中存在 `trace.jsonl`、`trace_ACTF.json` 和 `trace_SFT.json`。
4. 多轮 task 的轨迹中保留了全部用户消息和对应回复。
5. task 要求生成文件时，`results/` 中存在对应文件。

## 11. 常见问题

**(1) `Invalid token`**

检查 `--api-key-env` 指向的宿主机变量。

**(2) `No available channel for model`**

模型虽然出现在列表中，但当前账号/分组没有可用路由。

**(3) URL 出现 `/v1/v1/messages`**

不要手工把 `ANTHROPIC_BASE_URL` 传入容器，改用 Roller `--api-base` 让它规范化。

**(4) `invalid beta flag`**

使用显式 `--api-base`，并保留默认的 `--wire-trace actf`。Roller 会在转发请求时移除不兼容的 beta 参数。

**(5) model 拒绝 `budget_tokens` 或要求 adaptive thinking**

移除 `--claude-max-thinking-tokens`，只使用该模型支持的 `--thinking` effort。

**(6) ACTF 有 thinking token 但 `reasoning_content` 为空**

服务没有返回明文，不是转换器丢失。
