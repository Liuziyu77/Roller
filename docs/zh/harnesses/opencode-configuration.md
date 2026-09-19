# OpenCode Harness 指南

本页只说明 OpenCode 专属的镜像、API、session 和排错。Roller 在一次性 Docker 容器内运行 [OpenCode](https://opencode.ai/docs/cli/) **1.18.30**，采用原生非交互 CLI、session 续聊和会话导出。Task 字段见 [Task 格式参考](../tasks/task-format.md)，所有 Harness 共用的产物语义见 [轨迹产出文件指南](../traces/trajectory-outputs.md)。

> 快速定位：支持范围（第 1 节）· 镜像与运行（第 2 节）· 配置（第 3 节）· 会话与工作区（第 4 节）· 轨迹与 Resume（第 5 节）· 验证（第 6 节）· 排错（第 7 节）

## 1. 当前支持范围

OpenCode Harness 支持：

- 单轮 `prompt` 与预定义多轮 `messages`（`opencode run --session` 恢复同一原生会话）；
- Task Agent 和 User Simulator 均可设置 `"harness": "opencode"`；
- Task Bundle、只读 inputs、Context、setup、任务环境变量、自定义 `output_dir` 和容器内评分；
- 流式 **OpenAI Chat Completions**（含工具调用）；
- `bash/read/write/edit/apply_patch/glob/grep` 工具权限，实际工具集合由固定版本和模型选择决定；
- 让具备视觉能力的模型接收 OpenCode `read` 工具生成的图片附件；
- ACTF、SFT、reasoning 导出和 usage；
- `--wire-trace actf|full|off`；
- 并行、重试、run lock 和混合 Resume。

当前不支持：

- 任务文件中的直接多模态消息；
- 原生账号/OAuth 登录；
- 自定义 provider SDK、MCP、子 Agent 和长期记忆；
- 自动压缩（长任务需自行配置足够的上下文窗口，超窗会失败）。

## 2. 构建与运行

```bash
docker build -t roller-opencode:1.18.30 docker/opencode
export OPENAI_API_KEY='your-key'
export OPENAI_BASE_URL='https://api.example.com/v1'

python -m roller collect \
  --tasks examples/coverage/cases/formats/jsonl/opencode.jsonl \
  --model your-model-id --harness opencode \
  --parallel 2 --timeout 180 --retries 0 \
  --output runs/opencode --run-name smoke
```

首版使用 OpenAI-compatible **Chat Completions**，服务需要支持流式响应和工具调用。模型 ID 原样传给 API；适配器在 OpenCode 内部添加 `roller/` provider 前缀，任务中无需添加该前缀。镜像下载固定版本的独立二进制，校验 npm 包 SHA-512；构建时预装原生插件依赖，每个容器使用镜像内依赖和独立配置目录，避免首次启动在线安装。不要求宿主安装 Node.js、Bun 或 OpenCode。

Dockerfile 默认基础镜像为 `python:3.12-slim-bookworm`。可以通过 `--build-arg BASE_IMAGE=...` 使用已有 Linux 镜像，需含 Python 3.10+ 和 `apt-get`。支持 Linux x86_64/aarch64；x86_64 使用 baseline 二进制。`NPM_REGISTRY` 构建参数可指定兼容 npm 元数据和 tarball 的镜像站。

Debian 镜像参数 `DEBIAN_MIRROR`、`DEBIAN_SECURITY_MIRROR` 的用法见 [Docker 构建与镜像源](../start/docker-builds.md)。

## 3. 配置

| 参数 | 默认值与行为 |
| --- | --- |
| `--opencode-image` | 优先于 `--docker-image`；否则使用 `ROLLER_OPENCODE_IMAGE` 或 `roller-opencode:1.18.30` |
| `--opencode-api-base` | 回退到 `--api-base`、`OPENAI_BASE_URL`、`https://api.openai.com/v1` |
| `--opencode-api-key-env` | 回退到 `--api-key-env`、`OPENAI_API_KEY`；传变量名，不传密钥值 |
| `--opencode-max-steps` | 每条用户消息最多 12 个原生模型步骤，另受整个 rollout 超时约束 |
| `--opencode-context-length` | 模型上下文窗口，默认 134200，至少 1024；按服务真实能力设置 |
| `--opencode-output-tokens` | 最大输出 tokens，默认 8192，必须小于上下文窗口 |
| `--opencode-vision` / `--no-opencode-vision` | 是否向 OpenCode 声明模型支持图片输入；默认开启，纯文本模型可显式关闭 |
| `--thinking` / `runtime.thinking` | 支持 `off/minimal/low/medium/high/xhigh`；传给兼容服务的 reasoning effort，`off` 映射为 `none`，仍需服务支持 |
| `--wire-trace` | 默认 `actf`；各模式均通过插件保留原生请求和 schema；`full` 额外保存 HTTP 记录，要求显式 API base 与 host 网络 |
| `--no-keep-thinking` | 仅移除 ACTF/SFT 中的 reasoning；原生日志和续聊历史保持完整 |

专属配置同时适用于 OpenCode Agent 和 Simulator。Run lock 包含镜像摘要、OpenCode 版本、API 地址、Key 变量名、工具、窗口、步骤上限与导出策略。改变配置应使用新的运行目录；密钥在执行时读取，不写入公开配置。

## 4. 会话与工作区

每个参与者单独创建容器。输入只读挂载，Context 复制到声明路径并在任务提示中列出，支持 setup、任务环境变量、自定义输出目录和容器内评分。

每轮使用 `opencode run --session` 恢复同一原生会话，共享参与者工作区；不恢复 CLI 进程内的后台句柄。关闭自动分享、更新、模型目录刷新、LSP、formatter、自动压缩及工具结果裁剪。禁用工作区 OpenCode 配置的隐式加载，使用独立的 HOME/XDG 目录。Context/skill 测试验证文件可访问性；首版不启用 skill 工具、MCP、子 Agent 或联网工具。

## 5. 轨迹、错误和 Resume

本地插件在 provider 的 fetch 边界记录实际序列化请求，不改写请求或响应。`trace.jsonl` 保存这些请求、原生 message/part 事件及会话导出；ACTF 根据原生 ID 合并更新，保留多轮对话、reasoning、工具调用和结果。工具 schema 来自真实请求，不根据参数猜测。较大的内联附件 payload 完整保留在原生轨迹中，ACTF/SFT 记录有界元数据和内容哈希。

不完整会话、空回复、API 错误、截断回复、缺失或变化的工具 schema、损坏的 JSONL 会触发失败，raw 保留用于排查。超时会停止容器执行并尝试回收已有轨迹和文件。输出继续经过统一的文件安全检查。

OpenCode 不生成 OpenClaw cache trace，混合 Resume 按参与者的实际 Harness 判断；`full` 仍要求 wire trace，`capture: none` 仍不可复用。

## 6. 可复用验证

```bash
python examples/coverage/suite.py validate --case '*/opencode'
python examples/coverage/suite.py run --case '*/opencode' \
  --exclude-case 'vision/*' --dry-run
python examples/coverage/suite.py run --case data/txt/opencode \
  --opencode-model your-model-id
python examples/coverage/suite.py check --case data/txt/opencode

PYTHONPATH=src python -m unittest discover -s tests
ROLLER_OPENCODE_DOCKER_TEST=1 PYTHONPATH=src \
  python -m unittest discover -s tests -p test_opencode_docker.py -v
```

Docker 集成测试使用本地确定性流式 API 和虚构 Key，实际运行 OpenCode 工具，验证只读输入、Context/setup/env、多轮历史、reasoning、wire、Resume 和超时清理，不产生外部模型费用。

矩阵新增 204 条 OpenCode 输入任务和 36 条 Simulator 配对任务，每组 4 条。非视觉输入共 200 条；涉及 OpenCode 的 9 种配对可用 `--case 'simulators/pair/opencode--*' --case 'simulators/pair/*--opencode'` 选择，重叠组自动去重。完整矩阵为 1368 条任务、20 条隔离负例，见 {download}`Coverage 使用说明 <../../../examples/coverage/README.md>`。

## 7. 排错

常见错误：

- 服务不支持流式或工具调用：首版要求流式 Chat Completions 与工具调用，换用满足要求的服务；
- 轨迹失败：不完整会话、空回复、API 错误、截断回复、缺失或变化的工具 schema和损坏的 JSONL 都会触发失败，检查 raw 定位原因；
- 长任务超窗失败：自动压缩已关闭，按服务真实能力调大 `--opencode-context-length`；
- rollout 超时：容器被停止并尝试回收已有轨迹和文件，按需调整 `--timeout` 或 `--opencode-max-steps`；
- `--wire-trace full` 无法启动：需要显式 API base 与 host 网络。
