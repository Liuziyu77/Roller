# OpenCode Harness 指南

OpenCode Harness 用于通过 OpenCode Agent 执行需要读写文件、运行命令或查看图片的 task。它连接支持流式响应和工具调用的 OpenAI Chat Completions 服务。本页按照镜像、API、专属参数、会话和轨迹的顺序介绍首次使用所需内容。

Roller 当前固定使用 [OpenCode](https://opencode.ai/docs/cli/) 1.18.30。Task 字段见 [Task 格式参考](../tasks/task-format.md)，通用产物见 [轨迹产出文件指南](../traces/trajectory-outputs.md)。

首次使用时，先完成第 1 至第 3 节并运行示例命令。后面的专属参数、会话和轨迹说明可在需要相应功能时再查看。

## 1. 适用范围

OpenCode Harness 适合以下场景：

- 使用支持流式响应和工具调用的 OpenAI Chat Completions 服务。
- 让 Agent 运行命令、搜索内容和读写文件。
- 使用具备视觉能力的模型查看本地图片。
- 运行单轮、多轮、Task Agent 或 User Simulator task。

当前不支持直接在 task 消息中放入图片，也不启用账号登录、MCP、子 Agent、长期记忆和自动压缩。长任务需要为模型配置足够的上下文窗口。

## 2. 镜像准备

在 Roller 仓库根目录构建 OpenCode 镜像：

```bash
docker build -t roller-opencode:1.18.30 docker/opencode
```

镜像包含固定版本的 OpenCode 及其运行依赖。宿主机无需安装 Node.js、Bun 或 OpenCode。构建完成后，在第 3 节配置 API 地址、API Key 和模型 ID。

默认基础镜像为 `python:3.12-slim-bookworm`，支持 Linux x86_64 和 aarch64。需要更换基础镜像、npm 下载源或 Debian 软件源时，参见 [Docker 构建与镜像源](../start/docker-builds.md)。

## 3. 环境变量与 API

OpenCode Harness 连接支持流式响应和工具调用的 OpenAI Chat Completions 服务。第一次运行需要准备 API 地址、API Key 和模型 ID。

| 配置项 | 默认读取 | CLI 覆盖 |
| --- | --- | --- |
| API 地址 | `OPENAI_BASE_URL`，未设置时使用 `https://api.openai.com/v1` | `--opencode-api-base`，单一 Harness 运行也可用 `--api-base` |
| API Key | `OPENAI_API_KEY` | `--opencode-api-key-env`，单一 Harness 运行也可用 `--api-key-env` |
| 模型 ID | task 的 `model` 或 `--model` | 原样发送给 API，OpenCode 内部会添加 `roller/` 名称前缀 |

`--opencode-api-key-env` 和 `--api-key-env` 的值是宿主机环境变量名，不是 Key 本身。Key 只在执行时读取，不写入公开配置。

将下面的 `...`、API 地址和 `model-id` 替换为模型服务提供的实际值。第一次运行只执行一个 task，便于先确认配置是否正确：

```bash
export ROLLER_OPENCODE_KEY='...'

roller collect \
  --harness opencode \
  --tasks examples/tasks/tasks.jsonl \
  --model model-id \
  --api-base https://api.example.com/v1 \
  --api-key-env ROLLER_OPENCODE_KEY \
  --docker-image roller-opencode:1.18.30 \
  --limit 1 \
  --output runs \
  --run-name opencode-demo
```

(opencode-cli-flags)=
## 4. OpenCode 专属参数

下面只列 OpenCode 专属运行参数。API 地址和 Key 的配置方式见第 3 节。所有 Harness 共用的参数见 [CLI 参数参考](../start/cli-reference.md)。

| 参数 | 默认值与行为 |
| --- | --- |
| `--opencode-image` | 优先于 `--docker-image`。否则使用 `ROLLER_OPENCODE_IMAGE` 或 `roller-opencode:1.18.30` |
| `--opencode-max-steps` | SkillsBench 默认为 100，其他任务默认为 12，另受整次 task 执行的超时限制 |
| `--opencode-context-length` | 模型上下文窗口，默认 128000，至少 1024，按服务真实能力设置 |
| `--opencode-output-tokens` | SkillsBench 默认为 32768，其他任务默认为 8192，必须小于上下文窗口 |
| `--opencode-vision` / `--no-opencode-vision` | 是否向 OpenCode 声明模型支持图片输入。默认开启，纯文本模型可显式关闭 |
| `--thinking` / `runtime.thinking` | 支持 `off/minimal/low/medium/high/xhigh`。传给兼容服务的 reasoning effort，`off` 映射为 `none`，仍需服务支持 |
| `--wire-trace` | 默认 `actf`。各模式均通过插件保留原生请求和工具定义。`full` 额外保存 HTTP 记录，要求显式 API 地址和 `--network host` |
| `--no-keep-thinking` | 仅移除 ACTF/SFT 中的 reasoning。原生日志和续聊历史保持完整 |

OpenCode 作为 Task Agent 或 User Simulator 时都使用这些参数。修改镜像、API 地址、上下文窗口或步骤上限后，建议换一个新的 `--run-name`，避免与旧运行配置冲突。

## 5. 多模态能力

OpenCode 可以使用 `bash`、`read`、`write`、`edit`、`apply_patch`、`glob` 和 `grep`。具备视觉能力的模型和服务可以通过 `read` 打开本地图片。使用纯文本模型时，可以通过 `--no-opencode-vision` 关闭图片输入声明。

图片通过 `files.inputs` 进入工作区，并由 `read` 生成图片附件。图片结果会保留到原始轨迹、ACTF 和多模态 SFT。Task 的 `messages[].content` 当前只支持字符串，不能直接写入图片内容块。

## 6. 会话与工作区

每个 Agent 或 Simulator 都在独立容器中运行，不会读取宿主机的 OpenCode 配置。输入文件只读，Context 文件复制到 task 声明的位置，输出文件在结束后回收到 sample 的 `results/`。

同一参与者的多轮消息会恢复同一个 OpenCode 会话，因此对话历史和工作区文件可以继续使用。每一轮都会启动新的 CLI 进程，后台进程句柄不会保留。

采集时不会自动分享会话、更新 OpenCode 或启用 MCP、子 Agent 和联网工具。Context 和 skill 文件仍可按路径读取。

## 7. 轨迹与请求记录

一次运行会生成三类主要轨迹文件：

| 文件 | 用途 |
| --- | --- |
| `trace.jsonl` | 保存 OpenCode 原生会话和实际模型请求，主要用于排错 |
| `trace_ACTF.json` | 将对话、reasoning、工具调用和结果整理成统一结构 |
| `trace_SFT.json` | 将成功轨迹整理成训练数据格式 |

图片附件会保留到 ACTF 和多模态 SFT。工具定义来自实际模型请求，而不会根据调用参数猜测。

不完整会话、空回复、API 错误、截断回复、损坏的轨迹或前后不一致的工具定义不会被标记为成功。超时后会停止容器，并尽量回收已经生成的轨迹和文件。

## 8. 首次运行检查

首次运行建议只使用一个简单 task。完成后依次检查：

1. run 目录的 `info.json` 中 `state` 为 `finished`。
2. sample 的 `info.json` 中 `status` 为 `ok`。
3. sample 中存在 `trace.jsonl`、`trace_ACTF.json` 和 `trace_SFT.json`。
4. task 要求生成文件时，`results/` 中存在对应文件。

## 9. 常见问题

**(1) 服务不支持流式或工具调用**

OpenCode Harness 要求模型服务支持流式 Chat Completions 和工具调用。请换用满足要求的模型服务。

**(2) 轨迹失败**

不完整会话、空回复、API 错误、截断回复、缺失或变化的工具定义和损坏的 JSONL 都会触发失败，检查 `trace.jsonl` 定位原因。

**(3) 长任务超窗失败**

自动压缩已关闭，按服务真实能力调大 `--opencode-context-length`。

**(4) task 执行超时**

容器被停止并尝试回收已有轨迹和文件，按需调整 `--timeout` 或 `--opencode-max-steps`。

**(5) `--wire-trace full` 无法启动**

需要显式 API 地址和 `--network host`。
