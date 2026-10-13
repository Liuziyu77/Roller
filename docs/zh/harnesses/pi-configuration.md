# Pi Agent Harness 指南

Pi Agent Harness 用于通过 Pi Coding Agent 执行需要运行命令、读写文件或查看图片的 task。它连接支持流式响应和工具调用的 OpenAI Chat Completions 服务。本页按照镜像、API、专属参数、会话和轨迹的顺序介绍首次使用所需内容。

Roller 当前固定使用官方 [Pi Coding Agent SDK](https://pi.dev/docs/latest/sdk) 0.85.1。Task 字段见 [Task 格式参考](../tasks/task-format.md)，通用产物见 [轨迹产出文件指南](../traces/trajectory-outputs.md)。

首次使用时，先完成第 1 至第 3 节并运行示例命令。后面的专属参数、会话和轨迹说明可在需要相应功能时再查看。

## 1. 适用范围

Pi Agent Harness 适合以下场景：

- 使用支持流式响应和工具调用的 OpenAI Chat Completions 服务。
- 让 Agent 运行命令、搜索内容和读写文件。
- 使用具备视觉能力的模型查看本地图片。
- 运行单轮、多轮、Task Agent 或 User Simulator task。

当前不支持直接在 task 消息中放入图片或视频，也不启用账号登录、其他 Pi 服务协议、自定义插件和自动压缩。

## 2. 镜像准备

在 Roller 仓库根目录构建 Pi 镜像：

```bash
docker build -t roller-pi:0.85.1 docker/pi
```

镜像包含 Node.js、Pi SDK 和运行工具。宿主机无需另外安装这些依赖。构建完成后，在第 3 节配置 API 地址、API Key 和模型 ID。

默认基础镜像为 `node:22.22.0-bookworm-slim`。需要更换基础镜像、npm 下载源或 Debian 软件源时，参见 [Docker 构建与镜像源](../start/docker-builds.md)。

## 3. 环境变量与 API

Pi Harness 连接支持流式响应和工具调用的 OpenAI Chat Completions 服务。第一次运行需要准备 API 地址、API Key 和模型 ID。

| 配置项 | 默认读取 | CLI 覆盖 |
| --- | --- | --- |
| API 地址 | `OPENAI_BASE_URL`，未设置时使用 `https://api.openai.com/v1` | `--pi-api-base`，单一 Harness 运行也可用 `--api-base` |
| API Key | `OPENAI_API_KEY` | `--pi-api-key-env`，单一 Harness 运行也可用 `--api-key-env` |
| 模型 ID | task 的 `model` 或 `--model` | 原样发送给服务 |

`--pi-api-key-env` 和 `--api-key-env` 的值是宿主机环境变量名，不是 Key 本身。Key 只在执行时读取，不写入公开配置。

将下面的 `...`、API 地址和 `model-id` 替换为模型服务提供的实际值。第一次运行只执行一个 task，便于先确认配置是否正确：

```bash
export ROLLER_PI_KEY='...'

roller collect \
  --harness pi \
  --tasks examples/tasks/tasks.jsonl \
  --model model-id \
  --api-base https://api.example.com/v1 \
  --api-key-env ROLLER_PI_KEY \
  --docker-image roller-pi:0.85.1 \
  --limit 1 \
  --output runs \
  --run-name pi-demo
```

(pi-cli-flags)=
## 4. Pi 专属参数

下面只列 Pi 专属运行参数。API 地址和 Key 的配置方式见第 3 节。所有 Harness 共用的参数见 [CLI 参数参考](../start/cli-reference.md)。

| 参数 | 默认值与行为 |
| --- | --- |
| `--pi-image` | 优先于 `--docker-image`、`ROLLER_PI_IMAGE`、`roller-pi:0.85.1` |
| `--pi-max-steps` | 每条用户消息最多 12 次模型请求，另受任务/轮次超时约束 |
| `--pi-context-length` | 128000，至少 1024。按实际模型服务配置 |
| `--pi-output-tokens` | 8192，必须为正且小于上下文窗口。发送为 `max_tokens` |
| `--thinking` / `runtime.thinking` | `off/minimal/low/medium/high/xhigh/max`。默认 `off`，`none` 为别名 |
| `--wire-trace` | 各模式均记录原生请求。`full` 额外采集 HTTP 请求，要求显式 API 地址和 `--network host` |
| `--no-keep-thinking` | 只移除 ACTF/SFT reasoning，原始事件与续聊历史保持完整 |

Thinking 通过 `reasoning_effort` 发送，`off` 映射为 `none`，其他级别保持同名。服务必须支持选用的值。此接口不自动转换为各厂商独有的 thinking 协议。

Pi 作为 Task Agent 或 User Simulator 时都使用这些参数。修改镜像、API 地址或执行上限后，建议换一个新的 `--run-name`，避免与旧运行配置冲突。

## 5. 多模态能力

Pi 启用 `read`、`bash`、`edit`、`write`、`grep`、`find` 和 `ls` 七个原生工具。具备视觉能力的模型和 OpenAI-compatible 服务可以通过 `read` 打开本地图片。图片结果会保留到原始轨迹、ACTF 和多模态 SFT。

图片通过 `files.inputs` 进入工作区。Task 的 `messages[].content` 当前只支持字符串，不能直接写入图片内容块。纯文本模型仍可处理文本和普通文件，但不能完成需要理解图片内容的任务。视频、其他 Pi 服务协议和自定义扩展或插件暂不支持。

## 6. 会话与工作区

每个 Agent 或 Simulator 都在独立容器中运行。输入文件只读，Context 文件复制到 task 声明的位置，输出文件在结束后回收到 sample 的 `results/`。

同一参与者的多轮消息会复用同一个 Pi 会话，因此对话历史和工作区文件可以继续使用。Pi 的每次 Bash 调用都会启动新的 Shell，`export` 和 `cd` 不会自动延续到下一次调用。需要保留的信息应写入文件，或在同一条 Bash 命令中完成相关操作。

采集时不会启用自动压缩、远程模型更新或自定义插件。Context 和 skill 文件仍可按路径读取。

## 7. 轨迹与请求记录

一次运行会生成三类主要轨迹文件：

| 文件 | 用途 |
| --- | --- |
| `trace.jsonl` | 保存 Pi 原生事件和实际模型请求，主要用于排错 |
| `trace_ACTF.json` | 将对话、reasoning、工具调用和结果整理成统一结构 |
| `trace_SFT.json` | 将成功轨迹整理成训练数据格式 |

空回复、截断、API 错误、超过步数上限或未配对的工具调用不会被标记为成功。超时后会停止容器，并尽量回收已经生成的轨迹和结果文件。

## 8. 首次运行检查

首次运行建议只使用一个简单 task。完成后依次检查：

1. run 目录的 `info.json` 中 `state` 为 `finished`。
2. sample 的 `info.json` 中 `status` 为 `ok`。
3. sample 中存在 `trace.jsonl`、`trace_ACTF.json` 和 `trace_SFT.json`。
4. task 要求生成文件时，`results/` 中存在对应文件。

## 9. 常见问题

**(1) 轨迹未被判定为成功**

空响应、截断、API 错误、超出步数或未配对工具调用都会失败，检查 `trace.jsonl` 定位原因。

**(2) 轨迹文件没有生成**

先查看 sample 的 `info.json` 和 `trace.jsonl`。如果回复被截断或工具调用没有完成，可适当增加 `--pi-max-steps` 或 `--timeout` 后重新运行。

**(3) Bash 状态丢失**

Pi 的 Bash 每次启动独立 Shell，`export`/`cd` 不跨调用生效。需要持续状态应写文件或在同一条命令内完成。

**(4) 长任务超窗**

自动压缩已关闭，按实际模型服务调大 `--pi-context-length`。

**(5) task 执行超时**

容器被停止并回收已生成文件，按需调整 `--timeout` 或 `--pi-max-steps`。

**(6) `--wire-trace full` 无法启动**

设置显式 API 地址，并保留 `--network host`。
