# Pi Agent Harness

Roller 使用官方 [Pi Coding Agent SDK](https://pi.dev/docs/latest/sdk)，固定 `@earendil-works/pi-coding-agent`、`pi-agent-core` 和 `pi-ai` 为 0.85.1。原生 `AgentSession` 负责调用模型和执行工具，Task Agent 与 User Simulator 均可设置 `"harness": "pi"`。

## 构建与运行

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

首版接入流式 **OpenAI Chat Completions** 兼容服务，需要支持工具调用。模型 ID 原样发送，包含 `/` 时也不拆分成 Pi provider 名。宿主仅需 Roller、Python 3.10+ 与 Docker；Node 与 SDK 安装在容器内。默认基础镜像为 `node:22.22.0-bookworm-slim`；`BASE_IMAGE` 可替换为已包含 Node 22.19+ 和 npm 的 Debian/Ubuntu 镜像。

镜像额外预装带 SHA-256 校验的官方 `fd 10.2.0`，供原生 `find` 工具使用；运行时无需下载工具。

## 配置

| 参数 | 默认值与行为 |
| --- | --- |
| `--pi-image` | 优先于 `--docker-image`、`ROLLER_PI_IMAGE`、`roller-pi:0.85.1` |
| `--pi-api-base` | 回退到 `--api-base`、`OPENAI_BASE_URL`、`https://api.openai.com/v1` |
| `--pi-api-key-env` | 回退到 `--api-key-env`、`OPENAI_API_KEY`；填写变量名 |
| `--pi-max-steps` | 每条用户消息最多 12 次模型请求，另受任务/轮次超时约束 |
| `--pi-context-length` | 128000，至少 1024；按实际模型服务配置 |
| `--pi-output-tokens` | 8192，必须为正且小于上下文窗口；发送为 `max_tokens` |
| `--thinking` / `runtime.thinking` | `off/minimal/low/medium/high/xhigh/max`；默认 `off`，`none` 为别名 |
| `--wire-trace` | 各模式均记录原生请求；`full` 额外采集 HTTP 请求，要求显式 API base 和 `--network host` |
| `--no-keep-thinking` | 只移除 ACTF/SFT reasoning，原始事件与续聊历史保持完整 |

Thinking 通过 `reasoning_effort` 发送，`off` 映射为 `none`，其他级别保持同名；服务必须支持选用的值。此接口不自动转换为各厂商独有的 thinking 协议。

专属参数同时适用于 Pi Agent 和 Simulator。混合运行时可为其他 Harness 指定各自的 API、凭据变量和镜像。Run lock 记录版本、镜像摘要、工具、连接配置与执行上限，Resume 按参与者验证产物。

## 会话、Context 与工具

每个参与者拥有独立容器和 `PI_CODING_AGENT_DIR`；同一参与者跨轮复用一个原生 `AgentSession`。对话历史和工作区文件持续保留。Pi 的 Bash 每次启动独立 Shell，`export` 和 `cd` 不会跨工具调用持续生效；需要持续状态时应写文件，或在同一条 Bash 命令中完成操作。

启用七个原生工具：`read`、`bash`、`edit`、`write`、`grep`、`find`、`ls`。输入只读挂载，Context 复制到声明路径；非 skill Context 通过官方 loader 加入原生 system prompt。Skill 文件可按提示读取，不自动注册为 Pi skill。支持 setup、显式环境变量、自定义输出目录、文件回收，以及容器销毁前评分。

自动压缩、会话/模型重试、遥测、远程模型目录更新及自动扩展发现均关闭。Roller 保留 Pi 原生 system prompt，并追加工作区、输出目录和 Context 路径说明。

## 轨迹与支持范围

`trace.jsonl` 使用 `roller.pi-events/v1`，包含原生事件、模型 context、实际请求及 Roller 轮次边界。ACTF/SFT 使用实际 system prompt 和工具 schema，保留文本、reasoning、调用参数和结果；cache token 单独统计。

空响应、截断、API 错误、超出步数，以及未配对工具调用不会成为成功轨迹。转换器拒绝会改变 system prompt/schema 或丢失非文本内容的轨迹。超时停止容器并回收已生成文件；Pi 不要求 OpenClaw cache trace。

Roller 首版支持 Pi 的文本与文件任务。视觉验收、交互登录、其他 Pi provider 协议、自定义扩展/插件和自动压缩暂不支持。

## 可复用验证

```bash
python examples/coverage/suite.py validate --case '*/pi'
python examples/coverage/suite.py run --case '*/pi' \
  --exclude-case 'vision/*' --dry-run
ROLLER_PI_DOCKER_TEST=1 PYTHONPATH=src \
  python -m unittest discover -s tests -p test_pi_docker.py -v
```

Docker 测试使用本地确定性流式 API 和虚构 Key，执行真实 Pi SDK 与工具，不消耗模型额度。公开输入样例 204 条，其中非视觉 200 条；涉及 Pi 的 13 种 Agent × Simulator 配对共 52 条，可用 `--case 'simulators/pair/pi--*' --case 'simulators/pair/*--pi'` 选择。视觉组记录首版尚未支持的视觉输入。

若同时准备了 DeepSeek 镜像，可额外设置 `ROLLER_PI_MIXED_DOCKER_TEST=1`，验证 Pi ↔ DeepSeek 双向配对与 Resume。

完整矩阵见 [Coverage](https://github.com/Liuziyu77/Roller/blob/main/examples/coverage/README.md)。离线校验检查配置；容器测试检查 SDK 和工具执行；外部模型验收检查服务兼容性。
