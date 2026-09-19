# DeepSeek Harness 指南

本页只说明 DeepSeek Harness 专属的镜像、API、原生会话和排错。Roller 通过官方 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) Python SDK 驱动原生 `dsh` 运行时，固定 SDK 和 runtime 为 **0.1.5rc1**，采用官方 `sdk-minimal` profile。Task 字段见 [Task 格式参考](../tasks/task-format.md)，所有 Harness 共用的产物语义见 [轨迹产出文件指南](../traces/trajectory-outputs.md)。

> 快速定位：支持范围（第 1 节）· 镜像与运行（第 2 节）· 配置（第 3 节）· 原生会话与产物（第 4 节）· 验证（第 5 节）· 排错（第 6 节）

## 1. 当前支持范围

DeepSeek Harness 支持：

- 单轮 `prompt` 与预定义多轮 `messages`（同一会话内复用 SDK/runtime 进程和原生 Bash PTY，多轮间保留对话历史、工作区、shell 变量和工作目录）；
- Task Agent 与 User Simulator 均可设置 `"harness": "deepseek"`；
- Task Bundle、只读 inputs、Context、`runtime.setup`、任务环境变量、自定义 `output_dir`、产物回收及容器内评分；
- 流式 Chat Completions、工具调用和 DeepSeek thinking 字段；
- 官方 `sdk-minimal` profile 的持久化 `bash`；
- ACTF、SFT、provenance 和 usage；
- `--wire-trace actf|full|off`；
- 原生 `--thinking off/low/high/max` 映射。

当前不支持：

- 直接多模态消息与视觉验收；
- 交互登录；
- 任意 profile/插件配置；
- 跨任务的长期记忆；
- OpenClaw cache trace（Resume 不要求该文件）。

## 2. 构建与运行

```bash
docker build -t roller-deepseek:0.1.5rc1 docker/deepseek
export DEEPSEEK_API_KEY='your-key'
# 使用兼容网关时指定，默认连接 https://api.deepseek.com
export DEEPSEEK_BASE_URL='https://api.example.com/v1'

python -m roller collect \
  --tasks examples/coverage/cases/formats/jsonl/deepseek.jsonl \
  --harness deepseek --model deepseek-v4-flash \
  --thinking off --parallel 2 --timeout 180 --retries 0 \
  --output runs/deepseek --run-name smoke
```

需要支持流式响应、工具调用和 DeepSeek thinking 字段的 Chat Completions 服务。模型 ID 原样发送，包含网关前缀时也不拆分。宿主仅需 Roller、Python 3.10+ 与 Docker；SDK 和包含原生运行时的官方 wheel 安装在镜像的独立 Python 环境中。默认基础镜像为 `python:3.12-slim`，可用 `--build-arg BASE_IMAGE=...` 指定含 Python 3.10+ 的 Debian/Ubuntu 镜像。官方 wheel 提供 Linux x86_64/aarch64 版本。

Debian 镜像参数 `DEBIAN_MIRROR`、`DEBIAN_SECURITY_MIRROR` 的用法见 [Docker 构建与镜像源](../start/docker-builds.md)。

## 3. 配置

| 参数 | 默认值与行为 |
| --- | --- |
| `--deepseek-image` | 优先于 `--docker-image`、`ROLLER_DEEPSEEK_IMAGE`、`roller-deepseek:0.1.5rc1` |
| `--deepseek-api-base` | 回退到 `--api-base`、`DEEPSEEK_BASE_URL`、`https://api.deepseek.com` |
| `--deepseek-api-key-env` | 回退到 `--api-key-env`、`DEEPSEEK_API_KEY`；参数是变量名 |
| `--deepseek-max-steps` | 每条用户消息最多 12 次模型请求，另受任务/对话轮次超时约束 |
| `--deepseek-context-length` | 默认 134200，至少 1024；按实际服务设置 |
| `--deepseek-output-tokens` | 默认 8192，必须为正且小于上下文窗口 |
| `--thinking` / `runtime.thinking` | 原生支持 `off/low/high/max`；`none` 是 `off` 别名；省略时由官方适配器决定 |
| `--wire-trace` | 每种模式均保留实际请求和 schema；`full` 额外保存 HTTP 请求，要求显式 API base 和 host 网络 |
| `--no-keep-thinking` | 仅移除 ACTF/SFT reasoning，保留原始事件和续聊历史 |

专属参数同时适用于 Agent 和 Simulator。Run lock 记录镜像摘要、固定版本、profile、工具、API 地址、Key 变量名、步数和 token 上限。密钥通过执行环境读取。

## 4. 原生会话与产物

每个参与者使用独立容器与 `DSH_HOME`。同一会话内复用 SDK/runtime 进程和原生 Bash PTY，多轮之间保留对话历史、工作区、shell 变量和工作目录。输入只读挂载；Context 复制到声明路径，system prompt 列出相关文件；支持 setup、环境变量、自定义输出目录、产物回收及容器评分。

官方 `sdk-minimal` 只向模型提供持久化 `bash`。文件读写、搜索等任务通过它完成。该 profile 没有自动压缩、隐式工作区指令发现、skills、MCP、Web 或子 Agent；Context/skill 样例验证文件可访问性。Roller 的固定 patch 加入请求日志，关闭自动模型重试和官方 API 的 session/plugin 附加上传字段，设置当前模型的窗口与输出上限。

`trace.jsonl` 使用 `roller.deepseek-events/v1`，保存实际序列化请求及 SDK 原生通知。ACTF/SFT 使用实际 system prompt、工具 schema、reasoning、调用参数和工具结果。失败、截断、空回复、未完成工具调用及无法无损表示的非文本内容会被拒绝。超时停止整个容器并回收已有文件，Resume 按参与者分别验证，DeepSeek 不要求 OpenClaw cache trace。

关闭自动压缩的长任务应配置足够窗口；不同模型的 DeepSeek 协议兼容性仍需验证。

## 5. 可复用验证

```bash
python examples/coverage/suite.py validate --case '*/deepseek'
python examples/coverage/suite.py run --case '*/deepseek' \
  --exclude-case 'vision/*' --dry-run
ROLLER_DEEPSEEK_DOCKER_TEST=1 PYTHONPATH=src \
  python -m unittest discover -s tests -p test_deepseek_docker.py -v
```

Docker 测试以本地确定性 API 和虚构 Key 运行真实 SDK、Bash 与文件操作。输入样例共 204 条，其中非视觉 200 条；涉及 DeepSeek 的 13 种配对共 52 条，可使用 `--case 'simulators/pair/deepseek--*' --case 'simulators/pair/*--deepseek'` 选择。完整矩阵与运行方式见 [Coverage](https://github.com/Liuziyu77/Roller/blob/main/examples/coverage/README.md)。

## 6. 排错

常见错误：

- 轨迹被拒绝：失败、截断、空回复、未完成工具调用及无法无损表示的非文本内容都会被拒绝，检查 `trace.jsonl` 定位原因；
- 长任务超窗：自动压缩已关闭，按实际服务调大 `--deepseek-context-length`；
- rollout 超时：整个容器被停止并回收已有文件，按需调整 `--timeout` 或 `--deepseek-max-steps`；
- Resume 被拒绝：run lock 中镜像摘要、固定版本、profile、工具、API、步数或 token 上限发生变化，应使用新的运行目录；
- `--wire-trace full` 无法启动：需要显式 API base 和 host 网络。
