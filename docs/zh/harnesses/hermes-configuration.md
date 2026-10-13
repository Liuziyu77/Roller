# Hermes Harness 指南

Hermes Harness 用于通过 Hermes Agent 执行需要终端、文件或图片工具的 task。它连接支持工具调用的 OpenAI Chat Completions 服务，不需要单独部署 Hermes gateway。本页按照镜像、API、专属参数、会话和轨迹的顺序介绍首次使用所需内容。

Roller 使用 [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) 的 Python 接口，当前固定为 0.21.1。Task 字段见 [Task 格式参考](../tasks/task-format.md)，通用产物见 [轨迹产出文件指南](../traces/trajectory-outputs.md)。

首次使用时，先完成第 1 至第 3 节并运行示例命令。后面的专属参数、会话和轨迹说明可在需要相应功能时再查看。

## 1. 适用范围

Hermes Harness 适合以下场景：

- 使用支持工具调用的 OpenAI Chat Completions 模型服务。
- 让 Agent 操作终端和文件，或通过 `vision_analyze` 查看本地图片。
- 运行单轮、多轮、Task Agent 或 User Simulator task。
- 需要导出 ACTF、SFT、token 使用量和结果文件。

当前不适合直接在 task 消息中放入图片或视频，也不支持远程终端、持久 Memory 和自动学习 skill。

## 2. 镜像准备

在 Roller 仓库根目录构建 Hermes 镜像：

```bash
docker build -t roller-hermes:d15ed4445207 docker/hermes
```

镜像包含 Hermes 及其 Python 依赖。宿主机无需另外安装 Hermes，也不需要启动 Hermes gateway。构建完成后，在第 3 节配置 API 地址、API Key 和模型 ID。

该版本要求模型上下文窗口至少为 64,000 tokens。如果服务报告的窗口大小不正确，可用 `--hermes-context-length` 填写模型实际支持的值。

默认基础镜像使用 Python 3.11。需要更换基础镜像或软件源时，参见 [Docker 构建与镜像源](../start/docker-builds.md)。`PIP_INDEX_URL` 和 `PYPI_DOWNLOAD_BASE` 仅用于网络受限时替换依赖下载源。

## 3. 环境变量与 API

Hermes Harness 连接支持工具调用的 OpenAI Chat Completions 服务，不需要单独部署 Hermes gateway。第一次运行需要准备 API 地址、API Key 和模型 ID。

| 配置项 | 默认读取 | CLI 覆盖 |
| --- | --- | --- |
| API 地址 | `OPENAI_BASE_URL`，未设置时使用 `https://api.openai.com/v1` | `--hermes-api-base`，单一 Harness 运行也可用 `--api-base` |
| API Key | `OPENAI_API_KEY` | `--hermes-api-key-env`，单一 Harness 运行也可用 `--api-key-env` |
| 模型 ID | task 的 `model` 或 `--model` | 原样发送给服务 |

`--hermes-api-key-env` 和 `--api-key-env` 的值是宿主机环境变量名，不是 Key 本身。Key 只在执行时读取，不写入公开样例或运行配置。

将下面的 `...`、API 地址和 `model-id` 替换为模型服务提供的实际值。第一次运行只执行一个 task，便于先确认配置是否正确：

```bash
export ROLLER_HERMES_KEY='...'

roller collect \
  --harness hermes \
  --tasks examples/tasks/tasks.jsonl \
  --model model-id \
  --api-base https://api.example.com/v1 \
  --api-key-env ROLLER_HERMES_KEY \
  --docker-image roller-hermes:d15ed4445207 \
  --limit 1 \
  --output runs \
  --run-name hermes-demo
```

(hermes-cli-flags)=
## 4. Hermes 专属参数

下面只列 Hermes 专属运行参数。API 地址和 Key 的配置方式见第 3 节。所有 Harness 共用的参数见 [CLI 参数参考](../start/cli-reference.md)。

| 参数 | 默认值与行为 |
| --- | --- |
| `--hermes-image` | 优先于共享 `--docker-image`。否则使用 `ROLLER_HERMES_IMAGE` 或 `roller-hermes:d15ed4445207` |
| `--hermes-context-length` | 可选上下文窗口覆盖，至少 64000 tokens |
| `--hermes-max-iterations` | 每条 user message 最多 12 次模型迭代。仍受整次 task 执行的超时限制 |
| `--hermes-vision` / `--no-hermes-vision` | 启用 `vision_analyze`，并声明所选模型支持图片输入。默认开启 |
| `--thinking` / `runtime.thinking` | 可选原生 reasoning 提示：`off/minimal/low/medium/high/xhigh`。实际支持及归一化由 Hermes、模型和服务共同决定 |
| `--wire-trace` | 所有模式均保留原生请求中的工具定义和 system prompt。`full` 额外保存 HTTP 轨迹，要求显式 API 地址和 `--network host` |
| `--no-keep-thinking` | 只从 ACTF/SFT 导出中移除 reasoning，保留原始轨迹与原生续聊历史 |

Hermes 作为 Task Agent 或 User Simulator 时都使用这些参数。修改镜像、API 地址或迭代上限后，建议换一个新的 `--run-name`，避免与旧运行配置冲突。

## 5. 多模态能力

Hermes 提供终端、文件和可选的图片工具。模型和 API 服务都支持图片输入时，可以通过 `vision_analyze` 打开本地图片。使用纯文本模型时，可以通过 `--no-hermes-vision` 关闭图片工具。

图片通过 `files.inputs` 进入工作区，图片工具结果会保留到原始轨迹、ACTF 和多模态 SFT。Task 的 `messages[].content` 当前只支持字符串，不能直接写入图片内容块。视频和远程终端暂不支持。

## 6. 会话与工作区

每个 Agent 或 Simulator 都在独立的一次性容器中运行。输入文件只读，Context 文件复制到 task 声明的位置，输出文件在结束后回收到 sample 的 `results/`。

同一参与者的多轮消息会保留对话历史和工作区文件。每一轮都会重新启动 Python agent，因此后台进程句柄等内存状态不会保留。需要跨轮使用的信息应写入工作区文件。

采集时不会启用 Hermes 的持久记忆或自动学习 skill。Context 和 skill 文件仍可由 task 按路径读取。

## 7. 轨迹与请求记录

一次运行会生成三类主要轨迹文件：

| 文件 | 用途 |
| --- | --- |
| `trace.jsonl` | 保存 Hermes 实际请求、模型回复、工具调用和 token 使用量，主要用于排错 |
| `trace_ACTF.json` | 将不同 Harness 的事件整理成统一结构，便于查看和比较 |
| `trace_SFT.json` | 将成功轨迹整理成训练数据格式 |

图片工具的结果会保留在轨迹中。部分兼容服务不接受图片出现在 `tool` 消息里，此时 Roller 会调整发送给服务的消息位置，同时在 `trace.jsonl` 中保留原始工具结果。

未完成的轮次、空回复、损坏的工具参数或无效 JSONL 不会被标记为成功。超时后会停止执行，并尽量保留已经生成的轨迹和结果文件。

## 8. 首次运行检查

首次运行建议只使用一个简单 task。完成后依次检查：

1. run 目录的 `info.json` 中 `state` 为 `finished`。
2. sample 的 `info.json` 中 `status` 为 `ok`。
3. sample 中存在 `trace.jsonl`、`trace_ACTF.json` 和 `trace_SFT.json`。
4. task 要求生成文件时，`results/` 中存在对应文件。

## 9. 常见问题

**(1) 服务报告上下文窗口错误**

该版本要求至少 64,000 tokens，用 `--hermes-context-length` 指定模型真实支持的窗口。

**(2) 轨迹未被判定为成功**

未完成轮次、空回复、损坏的工具参数、工具定义或 JSONL 都会失败，检查 `trace.jsonl` 定位原因。

**(3) task 执行超时**

执行被停止并回收已有轨迹，按需调整 `--timeout` 或 `--hermes-max-iterations`。

**(4) 无法继续旧的 run**

镜像、API 地址或运行参数发生变化后，旧 run 不能安全续跑。请更换 `--run-name`。

**(5) `--wire-trace full` 无法启动**

需要显式 API 地址和 `--network host`。
