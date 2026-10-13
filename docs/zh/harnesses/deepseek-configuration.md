# DeepSeek Harness 指南

DeepSeek Harness 用于通过官方 DeepSeek Harness 运行文本和文件类 task。它连接支持流式响应、工具调用和 DeepSeek thinking 字段的 Chat Completions 服务。本页按照镜像、API、专属参数、会话和轨迹的顺序介绍首次使用所需内容。

Roller 当前固定使用官方 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 0.1.5rc1，并使用只提供 Bash 的 `sdk-minimal` 工具配置。Task 字段见 [Task 格式参考](../tasks/task-format.md)，通用产物见 [轨迹产出文件指南](../traces/trajectory-outputs.md)。

首次使用时，先完成第 1 至第 3 节并运行示例命令。后面的专属参数、工具限制和轨迹说明可在需要相应功能时再查看。

## 1. 适用范围

DeepSeek Harness 适合以下场景：

- 使用兼容 DeepSeek thinking 字段的 Chat Completions 服务。
- 通过一个持续可用的 Bash 环境处理文本和普通文件。
- 运行单轮、多轮、Task Agent 或 User Simulator task。
- 需要导出 ACTF、SFT、token 使用量和结果文件。

当前工具配置没有图片查看工具，因此不适合需要理解图片内容的 task。交互登录、自定义插件和跨 task 长期记忆也暂不支持。

## 2. 镜像准备

在 Roller 仓库根目录构建 DeepSeek 镜像：

```bash
docker build -t roller-deepseek:0.1.5rc1 docker/deepseek
```

镜像包含官方 SDK 和运行时。宿主机无需另外安装 DeepSeek Harness。构建完成后，在第 3 节配置 API 地址、API Key 和模型 ID。

默认基础镜像为 `python:3.12-slim`，支持 Linux x86_64 和 aarch64。需要更换基础镜像或 Debian 软件源时，参见 [Docker 构建与镜像源](../start/docker-builds.md)。

## 3. 环境变量与 API

DeepSeek Harness 连接支持流式响应、工具调用和 DeepSeek thinking 字段的 Chat Completions 服务。第一次运行需要准备 API 地址、API Key 和模型 ID。

| 配置项 | 默认读取 | CLI 覆盖 |
| --- | --- | --- |
| API 地址 | `DEEPSEEK_BASE_URL`，未设置时使用 `https://api.deepseek.com` | `--deepseek-api-base`，单一 Harness 运行也可用 `--api-base` |
| API Key | `DEEPSEEK_API_KEY` | `--deepseek-api-key-env`，单一 Harness 运行也可用 `--api-key-env` |
| 模型 ID | task 的 `model` 或 `--model` | 原样发送给服务 |

`--deepseek-api-key-env` 和 `--api-key-env` 的值是宿主机环境变量名，不是 Key 本身。Key 只在执行时读取，不写入 task、run 配置或公开产物。

将下面的 `...`、API 地址和 `model-id` 替换为模型服务提供的实际值。第一次运行只执行一个 task，便于先确认配置是否正确：

```bash
export ROLLER_DEEPSEEK_KEY='...'

roller collect \
  --harness deepseek \
  --tasks examples/tasks/tasks.jsonl \
  --model model-id \
  --api-base https://api.example.com/v1 \
  --api-key-env ROLLER_DEEPSEEK_KEY \
  --docker-image roller-deepseek:0.1.5rc1 \
  --limit 1 \
  --output runs \
  --run-name deepseek-demo
```

(deepseek-cli-flags)=
## 4. DeepSeek 专属参数

下面只列 DeepSeek 专属运行参数。API 地址和 Key 的配置方式见第 3 节。所有 Harness 共用的参数见 [CLI 参数参考](../start/cli-reference.md)。

| 参数 | 默认值与行为 |
| --- | --- |
| `--deepseek-image` | 优先于 `--docker-image`、`ROLLER_DEEPSEEK_IMAGE`、`roller-deepseek:0.1.5rc1` |
| `--deepseek-max-steps` | 每条用户消息最多 12 次模型请求，另受任务/对话轮次超时约束 |
| `--deepseek-context-length` | 默认 128000，至少 1024，按实际服务设置 |
| `--deepseek-output-tokens` | 默认 8192，必须为正且小于上下文窗口 |
| `--thinking` / `runtime.thinking` | 原生支持 `off/low/high/max`。`none` 是 `off` 别名。省略时使用 DeepSeek Harness 的默认行为 |
| `--wire-trace` | 控制请求记录方式。`full` 额外保存完整 HTTP 请求，需要显式 API 地址和 `--network host` |
| `--no-keep-thinking` | 仅移除 ACTF/SFT reasoning，保留原始事件和续聊历史 |

DeepSeek 作为 Task Agent 或 User Simulator 时都使用这些参数。修改镜像、API 地址、步数或 token 上限后，建议换一个新的 `--run-name`，避免与旧运行配置冲突。

## 5. 多模态能力

DeepSeek Harness 当前固定使用官方最小工具配置 `sdk-minimal`，只向模型提供持久化 `bash`，没有图片查看工具。因此当前适合文本和普通文件任务，不支持需要模型理解图片内容的任务。

图片文件可以通过 `files.inputs` 放入工作区，但只会作为普通文件存在，不会作为图片内容发送给模型。Task 的 `messages[].content` 当前也只支持字符串，不能直接写入图片内容块。视觉任务应选择支持图片工具的其他 Harness。

## 6. 会话与工作区

每个 Agent 或 Simulator 都在独立容器中运行。输入文件只读，Context 文件复制到 task 声明的位置，输出文件在结束后回收到 sample 的 `results/`。

同一参与者的多轮消息会复用同一个 DeepSeek 会话和 Bash 环境。对话历史、工作区文件、shell 变量和当前工作目录都会保留，因此后续轮次可以继续使用前面的状态。

## 7. 工具与能力限制

当前 `sdk-minimal` 工具配置只提供一个持续可用的 `bash` 工具。读写文件和搜索内容也通过 Bash 命令完成，不提供单独的图片、Web、MCP 或子 Agent 工具。

自动压缩已关闭。长 task 需要通过 `--deepseek-context-length` 设置足够的上下文窗口，并确保所选模型服务支持 DeepSeek 的请求格式和 thinking 字段。

## 8. 轨迹与请求记录

一次运行会生成三类主要轨迹文件：

| 文件 | 用途 |
| --- | --- |
| `trace.jsonl` | 保存 DeepSeek SDK 的原生事件和实际模型请求，主要用于排错 |
| `trace_ACTF.json` | 将对话、reasoning、Bash 调用和结果整理成统一结构 |
| `trace_SFT.json` | 将成功轨迹整理成训练数据格式 |

失败、截断、空回复、未完成工具调用或无法完整保存的内容不会被标记为成功。超时后会停止容器，并尽量回收已经生成的轨迹和结果文件。

## 9. 首次运行检查

首次运行建议只使用一个简单 task。完成后依次检查：

1. run 目录的 `info.json` 中 `state` 为 `finished`。
2. sample 的 `info.json` 中 `status` 为 `ok`。
3. sample 中存在 `trace.jsonl`、`trace_ACTF.json` 和 `trace_SFT.json`。
4. task 要求生成文件时，`results/` 中存在对应文件。

## 10. 常见问题

**(1) 轨迹被拒绝**

失败、截断、空回复、未完成工具调用及无法无损表示的非文本内容都会被拒绝，检查 `trace.jsonl` 定位原因。

**(2) 长任务超窗**

自动压缩已关闭，按实际服务调大 `--deepseek-context-length`。

**(3) task 执行超时**

整个容器被停止并回收已有文件，按需调整 `--timeout` 或 `--deepseek-max-steps`。

**(4) 无法继续旧的 run**

镜像、API 地址或运行参数发生变化后，旧 run 不能安全续跑。请更换 `--run-name`。

**(5) `--wire-trace full` 无法启动**

需要显式 API 地址和 `--network host`。
