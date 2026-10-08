# Pi Agent Harness 指南

本页只说明 Pi 专属的镜像、API、原生会话和排错。Roller 使用官方 [Pi Coding Agent SDK](https://pi.dev/docs/latest/sdk)，固定 `@earendil-works/pi-coding-agent`、`pi-agent-core` 和 `pi-ai` 为 **0.85.1**，通过原生 `AgentSession` 执行模型循环和工具。Task 字段见 [Task 格式参考](../tasks/task-format.md)，所有 Harness 共用的产物语义见 [轨迹产出文件指南](../traces/trajectory-outputs.md)。

## 1. 适用范围

Pi Harness 支持：

- 单轮 `prompt` 与预定义多轮 `messages`（同一参与者跨轮复用一个原生 `AgentSession`）。
- Task Agent 与 User Simulator 均可设置 `"harness": "pi"`。
- Task Bundle、只读 inputs、Context、setup、显式环境变量、自定义 `output_dir`、文件回收和容器销毁前评分。
- 流式 **OpenAI Chat Completions**（含工具调用）。
- 七个原生工具：`read`、`bash`、`edit`、`write`、`grep`、`find`、`ls`。
- 模型与服务支持图片输入时，可通过 `read` 打开本地图片。
- ACTF、SFT、reasoning 导出和 usage（cache token 单独统计）。
- `--wire-trace actf|full|off`。
- `--thinking off/minimal/low/medium/high/xhigh/max`（经 `reasoning_effort` 发送）。

当前不支持：

- 任务文件中的直接多模态消息与视频。
- 交互登录。
- 其他 Pi provider 协议。
- 自定义扩展/插件。
- 自动压缩。
- OpenClaw cache trace（Resume 不要求该文件）。

## 2. 镜像准备与首次运行

```bash
docker build -t roller-pi:0.85.1 docker/pi
export OPENAI_API_KEY='your-key'
export OPENAI_BASE_URL='https://api.example.com/v1'

python -m roller collect \
  --tasks examples/coverage/cases/formats/jsonl/pi.jsonl \
  --harness pi --model your-model \
  --thinking off --parallel 2 --timeout 180 --retries 0 \
  --output runs/pi --run-name smoke
```

首版接入流式 **OpenAI Chat Completions** 兼容服务，需要支持工具调用。模型 ID 原样发送，包含 `/` 时也不拆分成 Pi provider 名。宿主仅需 Roller、Python 3.10+ 与 Docker。Node 与 SDK 安装在容器内。默认基础镜像为 `node:22.22.0-bookworm-slim`。`BASE_IMAGE` 可替换为已包含 Node 22.19+ 和 npm 的 Debian/Ubuntu 镜像。

镜像额外预装带 SHA-256 校验的官方 `fd 10.2.0`，供原生 `find` 工具使用。运行时无需下载工具。

Debian 镜像参数 `DEBIAN_MIRROR`、`DEBIAN_SECURITY_MIRROR` 的用法见 [Docker 构建与镜像源](../start/docker-builds.md)。npm 下载可通过 `NPM_REGISTRY` 指定镜像。未设置时沿用 npm 原有配置。

## 3. 环境变量与 API

Pi Harness 连接支持流式响应和工具调用的 OpenAI Chat Completions 服务。第一次运行需要准备 API 地址、API Key 和模型 ID。

| 配置项 | 默认读取 | CLI 覆盖 |
| --- | --- | --- |
| API 地址 | `OPENAI_BASE_URL`，未设置时使用 `https://api.openai.com/v1` | `--pi-api-base`，单一 Harness 运行也可用 `--api-base` |
| API Key | `OPENAI_API_KEY` | `--pi-api-key-env`，单一 Harness 运行也可用 `--api-key-env` |
| 模型 ID | task 的 `model` 或 `--model` | 原样发送给服务 |

`--pi-api-key-env` 和 `--api-key-env` 的值是宿主机环境变量名，不是 Key 本身。Key 只在执行时读取，不写入公开配置。

(pi-cli-flags)=
## 4. Pi 专属参数

下面只列 Pi 专属运行参数。API 地址和 Key 的配置方式见第 3 节。所有 Harness 共用的参数见 [CLI 参数参考](../start/cli-reference.md)。

| 参数 | 默认值与行为 |
| --- | --- |
| `--pi-image` | 优先于 `--docker-image`、`ROLLER_PI_IMAGE`、`roller-pi:0.85.1` |
| `--pi-max-steps` | 每条用户消息最多 12 次模型请求，另受任务/轮次超时约束 |
| `--pi-input-modalities` | 默认 `text,image`；纯文本模型使用 `text`。声明 SDK 输入能力，不会使上游模型获得视觉能力。写入 run lock，resume 时检查一致性。 |
| `--pi-context-length` | 128000，至少 1024。按实际模型服务配置 |
| `--pi-output-tokens` | 8192，必须为正且小于上下文窗口。发送为 `max_tokens` |
| `--thinking` / `runtime.thinking` | `off/minimal/low/medium/high/xhigh/max`。默认 `off`，`none` 为别名 |
| `--wire-trace` | 各模式均记录原生请求。`full` 额外采集 HTTP 请求，要求显式 API base 和 `--network host` |
| `--no-keep-thinking` | 只移除 ACTF/SFT reasoning，原始事件与续聊历史保持完整 |

Thinking 通过 `reasoning_effort` 发送，`off` 映射为 `none`，其他级别保持同名。服务必须支持选用的值。此接口不自动转换为各厂商独有的 thinking 协议。

专属参数同时适用于 Pi Agent 和 Simulator。混合运行时可为其他 Harness 指定各自的 API、凭据变量和镜像。Run lock 记录版本、镜像摘要、工具、连接配置与执行上限，Resume 按参与者验证产物。

## 5. 会话、工作区与工具

每个参与者拥有独立容器和 `PI_CODING_AGENT_DIR`。同一参与者跨轮复用一个原生 `AgentSession`。对话历史和工作区文件持续保留。Pi 的 Bash 每次启动独立 Shell，`export` 和 `cd` 不会跨工具调用持续生效。需要持续状态时应写文件，或在同一条 Bash 命令中完成操作。

启用七个原生工具：`read`、`bash`、`edit`、`write`、`grep`、`find`、`ls`。输入只读挂载。所选模型支持图片时，`read` 的原生图片结果会传给模型，并保留到 raw、ACTF 和多模态 SFT。Context 复制到声明路径。非 skill Context 通过官方 loader 加入原生 system prompt。Skill 文件可按提示读取，不自动注册为 Pi skill。支持 setup、显式环境变量、自定义输出目录、文件回收，以及容器销毁前评分。

自动压缩、会话/模型重试、遥测、远程模型目录更新及自动扩展发现均关闭。Roller 保留 Pi 原生 system prompt，并追加工作区、输出目录和 Context 路径说明。

## 6. 轨迹产物

`trace.jsonl` 使用 `roller.pi-events/v1`，包含原生事件、模型 context、实际请求及 Roller 轮次边界。ACTF/SFT 使用实际 system prompt 和工具 schema，保留文本、reasoning、调用参数和结果。cache token 单独统计。

空响应、截断、API 错误、超出步数，以及未配对工具调用不会成为成功轨迹。转换器拒绝会改变 system prompt/schema 或丢失非文本内容的轨迹。超时停止容器并回收已生成文件。Pi 不要求 OpenClaw cache trace。

Roller 支持 Pi 的文本与文件任务。模型与 OpenAI-compatible 服务支持图片输入时，也支持通过 `read` 打开本地图片。直接 user-message 图片附件、视频、交互登录、其他 Pi provider 协议、自定义扩展或插件和自动压缩暂不支持。

## 7. 验证

```bash
python examples/coverage/suite.py validate --case '*/pi'
python examples/coverage/suite.py run --case '*/pi' --dry-run
ROLLER_PI_DOCKER_TEST=1 PYTHONPATH=src \
  python -m unittest discover -s tests -p test_pi_docker.py -v
```

Docker 测试使用本地确定性流式 API 和虚构 Key，执行真实 Pi SDK 与工具，不消耗模型额度。公开输入样例 204 条，其中包含 4 条视觉任务。涉及 Pi 的 13 种 Agent × Simulator 配对共 52 条，可用 `--case 'simulators/pair/pi--*' --case 'simulators/pair/*--pi'` 选择。外部模型视觉测试还要求模型与服务端确实支持图片输入。

若同时准备了 DeepSeek 镜像，可额外设置 `ROLLER_PI_MIXED_DOCKER_TEST=1`，验证 Pi ↔ DeepSeek 双向配对与 Resume。

完整矩阵见 [Coverage](https://github.com/Liuziyu77/Roller/blob/main/examples/coverage/README.md)。离线配置校验、真实容器测试与外部模型服务验收是不同验证层级。

### Issue #46 回归覆盖

Pi Docker 测试校验真实请求的 Bearer token 是否等于合成凭据，并检查错误凭据失败；通过原生 `read` 读取两张图片，比较请求、ACTF、SFT 中的图片字节与顺序。此外覆盖纯文本输入能力、原生 reasoning、run 级 SFT、resume 和 Viewer 图片数据。Pi 的 Chat Completions 协议会为工具图片插入合成 user 消息；规范比较时将其还原到对应工具结果。

2026-10-08 使用 Pi 0.85.1 对 `kimi-k3`、`glm-5.3-flash` 实测：两个模型均读取两张 64×64 PNG，将 `red, blue` 写入文件并返回相同答案，各调用模型四次。已核验 raw/wire 请求、图片字节与顺序、ACTF/SFT 规范投影、run 级 SFT，以及 resume 后轨迹不变。Kimi 返回三段 reasoning，逐段完整保留；GLM 本轮未返回可见 reasoning。Viewer 图片数据接口及 Chrome 页面视觉验收均已通过：两条真实轨迹的第一步显示红图，第二步显示蓝图，图片位于对应 read 结果中，文本与图片顺序及布局正常。此结果不代表其他模型或网关均支持视觉。

同日补测 `gpt-5.6-sol` 和 `bailian/deepseek-v4.1-flash`，当前凭据均可完成两图任务并回答 `red, blue`。GPT 在该网关的 Chat Completions 接口上以 `high` 搭配工具返回 400，改为 `--thinking off` 后成功（4 次请求）；DeepSeek 使用 `high` 成功（3 次请求，返回并保留 2 段 reasoning）。图片字节、顺序、ACTF/SFT、run 级 SFT 和 Viewer 图片接口均通过检查；DeepSeek 并行读图的请求按工具结果组进行规范比较。这两次补测未重复执行 resume 或浏览器视觉验收。响应模型名分别为 `gpt-5.6-sol-2026-07-09` 和 `deepseek-v4.1-flash`，与请求名不同，可能是网关别名映射，不能据此独立验证后端模型身份。

输入能力写入 run lock。新增配置前的历史 run 可能无法直接 resume，应保留旧证据并使用新目录运行。凭据和本地验收产物不进入版本控制。

## 8. 常见问题

常见错误：

- 轨迹未被判定为成功：空响应、截断、API 错误、超出步数或未配对工具调用都会失败，检查 `trace.jsonl` 定位原因。
- 转换器拒绝轨迹：会改变 system prompt/schema 或丢失非文本内容的轨迹会被拒绝。
- Bash 状态丢失：Pi 的 Bash 每次启动独立 Shell，`export`/`cd` 不跨调用生效。需要持续状态应写文件或在同一条命令内完成。
- 长任务超窗：自动压缩已关闭，按实际模型服务调大 `--pi-context-length`。
- rollout 超时：容器被停止并回收已生成文件，按需调整 `--timeout` 或 `--pi-max-steps`。
- `--wire-trace full` 无法启动：需要显式 API base 和 `--network host`。
