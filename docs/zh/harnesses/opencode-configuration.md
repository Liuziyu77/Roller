# OpenCode Harness 指南

本页只说明 OpenCode 专属的镜像、API、session 和排错。Roller 在一次性 Docker 容器内运行 [OpenCode](https://opencode.ai/docs/cli/) **1.18.30**，采用原生非交互 CLI、session 续聊和会话导出。Task 字段见 [Task 格式参考](../tasks/task-format.md)，所有 Harness 共用的产物语义见 [轨迹产出文件指南](../traces/trajectory-outputs.md)。

## 1. 适用范围

OpenCode Harness 支持：

- 单轮 `prompt` 与预定义多轮 `messages`（`opencode run --session` 恢复同一原生会话）。
- Task Agent 和 User Simulator 均可设置 `"harness": "opencode"`。
- Task Bundle、只读 inputs、Context、setup、任务环境变量、自定义 `output_dir` 和容器内评分。
- 流式 **OpenAI Chat Completions**（含工具调用）。
- `bash/read/write/edit/apply_patch/glob/grep` 工具权限，实际工具集合由固定版本和模型选择决定。
- 让具备视觉能力的模型接收 OpenCode `read` 工具生成的图片附件。
- ACTF、SFT、reasoning 导出和 usage。
- `--wire-trace actf|full|off`。
- 并行、重试、run lock 和混合 Resume。

当前不支持：

- 任务文件中的直接多模态消息。
- 原生账号/OAuth 登录。
- 自定义 provider SDK、MCP 和长期记忆。子 Agent 需要显式开启 `--capture-calls`。
- 自动压缩（长任务需自行配置足够的上下文窗口，超窗会失败）。

## 2. 镜像准备

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

首版使用 OpenAI-compatible **Chat Completions**，服务需要支持流式响应和工具调用。模型 ID 原样传给 API。适配器在 OpenCode 内部添加 `roller/` provider 前缀，任务中无需添加该前缀。镜像下载固定版本的独立二进制，校验 npm 包 SHA-512。构建时预装原生插件依赖，每个容器使用镜像内依赖和独立配置目录，避免首次启动在线安装。不要求宿主安装 Node.js、Bun 或 OpenCode。

Dockerfile 默认基础镜像为 `python:3.12-slim-bookworm`。可以通过 `--build-arg BASE_IMAGE=...` 使用已有 Linux 镜像，需含 Python 3.10+ 和 `apt-get`。支持 Linux x86_64/aarch64。x86_64 使用 baseline 二进制。`NPM_REGISTRY` 构建参数可指定兼容 npm 元数据和 tarball 的镜像站。

Debian 镜像参数 `DEBIAN_MIRROR`、`DEBIAN_SECURITY_MIRROR` 的用法见 [Docker 构建与镜像源](../start/docker-builds.md)。

## 3. 环境变量与 API

OpenCode Harness 连接支持流式响应和工具调用的 OpenAI Chat Completions 服务。第一次运行需要准备 API 地址、API Key 和模型 ID。

| 配置项 | 默认读取 | CLI 覆盖 |
| --- | --- | --- |
| API 地址 | `OPENAI_BASE_URL`，未设置时使用 `https://api.openai.com/v1` | `--opencode-api-base`，单一 Harness 运行也可用 `--api-base` |
| API Key | `OPENAI_API_KEY` | `--opencode-api-key-env`，单一 Harness 运行也可用 `--api-key-env` |
| 模型 ID | task 的 `model` 或 `--model` | 原样发送给 API，OpenCode 内部会添加 `roller/` provider 前缀 |

`--opencode-api-key-env` 和 `--api-key-env` 的值是宿主机环境变量名，不是 Key 本身。Key 只在执行时读取，不写入公开配置。

(opencode-cli-flags)=
## 4. OpenCode 专属参数

下面只列 OpenCode 专属运行参数。API 地址和 Key 的配置方式见第 3 节。所有 Harness 共用的参数见 [CLI 参数参考](../start/cli-reference.md)。

| 参数 | 默认值与行为 |
| --- | --- |
| `--opencode-image` | 优先于 `--docker-image`。否则使用 `ROLLER_OPENCODE_IMAGE` 或 `roller-opencode:1.18.30` |
| `--opencode-max-steps` | SkillsBench 默认为 100，其他任务默认为 12，另受整个 rollout 超时约束 |
| `--opencode-context-length` | 模型上下文窗口，默认 128000，至少 1024，按服务真实能力设置 |
| `--opencode-output-tokens` | SkillsBench 默认为 32768，其他任务默认为 8192，必须小于上下文窗口 |
| `--opencode-vision` / `--no-opencode-vision` | 是否向 OpenCode 声明模型支持图片输入。默认开启，纯文本模型可显式关闭 |
| `--thinking` / `runtime.thinking` | 支持 `off/minimal/low/medium/high/xhigh`。传给兼容服务的 reasoning effort，`off` 映射为 `none`，仍需服务支持 |
| `--wire-trace` | 默认 `actf`。各模式均通过插件保留原生请求和 schema。`full` 额外保存 HTTP 记录，要求显式 API base 与 host 网络 |
| `--no-keep-thinking` | 仅移除 ACTF/SFT 中的 reasoning。原生日志和续聊历史保持完整 |

专属配置同时适用于 OpenCode Agent 和 Simulator。Run lock 包含镜像摘要、OpenCode 版本、API 地址、Key 变量名、工具、窗口、步骤上限与导出策略。改变配置应使用新的运行目录。密钥在执行时读取，不写入公开配置。

## 5. 多模态能力

OpenCode 可以使用 `bash`、`read`、`write`、`edit`、`apply_patch`、`glob` 和 `grep`。具备视觉能力的模型和服务可以通过 `read` 打开本地图片。使用纯文本模型时，可以通过 `--no-opencode-vision` 关闭图片输入声明。

图片通过 `files.inputs` 进入工作区，并由 `read` 生成图片附件。图片结果会保留到 raw、ACTF 和多模态 SFT。Task 的 `messages[].content` 当前只支持字符串，不能直接写入图片内容块。

## 6. 会话与工作区

每个参与者单独创建容器。输入只读挂载，Context 复制到声明路径并在任务提示中列出，支持 setup、任务环境变量、自定义输出目录和容器内评分。

每轮使用 `opencode run --session` 恢复同一原生会话，共享参与者工作区。不恢复 CLI 进程内的后台句柄。每个参与者使用独立的 HOME/XDG 目录，不会读取宿主机的 OpenCode 配置。

自动分享、更新、模型目录刷新、LSP、formatter、自动压缩及工具结果裁剪均已关闭。默认不启用子 Agent；`--capture-calls` 显式允许原生 `task` 子任务。MCP 和联网工具仍未启用。Context/skill 测试只验证相应文件是否可访问。

## 7. 轨迹与请求记录

本地插件在 provider 的 fetch 边界记录实际序列化请求，不改写请求或响应。`trace.jsonl` 保存这些请求、原生 message/part 事件及会话导出。ACTF 根据原生 ID 合并更新，保留多轮对话、reasoning、工具调用和结果。工具 schema 来自真实请求，不根据参数猜测。内联图片附件会保留为 image block 并导出到 SFT。ACTF details 同时保留有界附件元数据和内容哈希供审计。非图片附件继续只保存有界摘要。

不完整会话、空回复、API 错误、截断回复、缺失或变化的工具 schema、损坏的 JSONL 会触发失败，raw 保留用于排查。超时会停止容器执行并尝试回收已有轨迹和文件。输出继续经过统一的文件安全检查。

OpenCode 不生成 OpenClaw cache trace，混合 Resume 按参与者的实际 Harness 判断。`full` 仍要求 wire trace，`capture: none` 仍不可复用。

## 8. 验证

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

矩阵新增 204 条 OpenCode 输入任务和 36 条 Simulator 配对任务，每组 4 条。非视觉输入共 200 条。涉及 OpenCode 的 9 种配对可用 `--case 'simulators/pair/opencode--*' --case 'simulators/pair/*--opencode'` 选择，重叠组自动去重。完整矩阵为 1368 条任务、20 条隔离负例，见 {download}`Coverage 使用说明 <../../../examples/coverage/README.md>`。

## 9. 常见问题

**(1) 服务不支持流式或工具调用**

首版要求流式 Chat Completions 与工具调用，换用满足要求的服务。

**(2) 轨迹失败**

不完整会话、空回复、API 错误、截断回复、缺失或变化的工具 schema 和损坏的 JSONL 都会触发失败，检查 raw 定位原因。

**(3) 长任务超窗失败**

自动压缩已关闭，按服务真实能力调大 `--opencode-context-length`。

**(4) rollout 超时**

容器被停止并尝试回收已有轨迹和文件，按需调整 `--timeout` 或 `--opencode-max-steps`。

**(5) `--wire-trace full` 无法启动**

需要显式 API base 与 host 网络。

## 10. Subagent 调用采集

`--capture-calls` 使用与 Claude Code 相同的采集协议，需要显式 API base、host
网络和开启 wire trace。成功采集默认保存 `manifest.json`、`calls.jsonl` 和
`relations.jsonl`；失败时或指定 `--capture-debug full` 时保留原始 HTTP 与原生证据。
主 ACTF/SFT 不混入子任务内部消息或工具配置。

OpenCode 使用插件提供的原生 session ID 关联每次请求，本地代理在向服务
转发前移除关联头。开启采集时恢复 Explore 的 write/edit/apply_patch/task/skill
工具限制，并将步骤上限应用到 general/explore 子任务。

Bash 非零退出码可能只在原生工具元数据中，而模型收到 `(no output)`。采集
原样保留实际上下文和原生证据，不向模型输入添加错误标记。后台任务整体标记
partial；尚未实现等待后台任务完成。

字段和完整性规则见 [Subagent 采集协议](../traces/subagent-capture-protocol.md)。
