# Hermes Harness

Roller 通过 [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) 的 Python `AIAgent` 接口运行 Hermes，固定 commit 为 `d15ed4445207dda418b984e8bda0f68f48b8c6f3`（0.21.1）。Task Agent 和 User Simulator 均可声明 `"harness": "hermes"`。

## 构建与运行

在 Roller 仓库根目录执行：

```bash
docker build -t roller-hermes:d15ed4445207 docker/hermes
export OPENAI_API_KEY='your-key'
export OPENAI_BASE_URL='https://api.example.com/v1'

python -m roller collect \
  --tasks examples/coverage/cases/formats/jsonl/hermes.jsonl \
  --model your-model-id --harness hermes \
  --timeout 180 --parallel 2 --hermes-max-iterations 12 \
  --output runs/hermes --run-name smoke
```

模型 ID 原样交给服务，无需加 OpenClaw 的 provider 前缀。服务需要支持带工具调用的 **OpenAI Chat Completions**。运行不需要单独部署 Hermes gateway。该版本要求模型上下文至少 **64,000 tokens**；服务报告错误时，可用 `--hermes-context-length` 指定模型真实支持的窗口大小。

默认基础镜像为 Python 3.11。可通过 `--build-arg BASE_IMAGE=...` 使用已有 Linux 镜像，需含 Python 3.11–3.13、`apt-get` 和 `pip`。依赖按 Hermes 的 `uv.lock` 冻结安装。每次执行前检查源码版本及桥接依赖的 hook；升级 Hermes 需要同步评审和验证 runner。

## 配置

| 参数 | 默认值与行为 |
| --- | --- |
| `--hermes-image` | 优先于共享 `--docker-image`；否则使用 `ROLLER_HERMES_IMAGE` 或 `roller-hermes:d15ed4445207` |
| `--hermes-api-base` | 依次回退到 `--api-base`、`OPENAI_BASE_URL`、`https://api.openai.com/v1` |
| `--hermes-api-key-env` | 依次回退到 `--api-key-env`、`OPENAI_API_KEY`；参数值是变量名，不是 Key |
| `--hermes-context-length` | 可选上下文窗口覆盖，至少 64000 tokens |
| `--hermes-max-iterations` | 每条 user message 最多 12 次模型迭代；仍受整个 rollout 的 timeout 约束 |
| `--thinking` / `runtime.thinking` | 可选原生 reasoning 提示：`off/minimal/low/medium/high/xhigh`；实际支持及归一化由 Hermes、模型和服务共同决定 |
| `--wire-trace` | 所有模式均保留原生请求中的 schema/system prompt；`full` 额外保存 HTTP 轨迹，要求显式 API base 和 host 网络 |
| `--no-keep-thinking` | 只从 ACTF/SFT 导出中移除 reasoning，保留 raw 与原生续聊历史 |

Hermes 的专属 API/镜像参数同时适用于 Agent 和 Simulator。Run lock 包含版本、镜像摘要、API 地址、Key 变量名、迭代上限、工具集和导出设置。API Key 在执行时读取，不进入公开样例或运行配置。

## 会话、文件与轨迹

每个参与者使用独立的一次性容器。Hermes 的 `local` terminal backend 在这个容器内执行，只启用 `terminal`、`file` 工具集。输入只读挂载、Context 文件、setup、任务环境变量、输出回收和容器内评分使用 Roller 的统一工作区。

所有 Context 角色的文件会复制到声明路径，并在任务 system prompt 中列出，任务可显式读取。桥接关闭隐式 profile/context 搜索、持久 Memory、SOUL identity、checkpoint、后台 review 和 rollout 期间的自动依赖安装。因此可以验证 Context/skill 文件可访问性；不会启用 Hermes 自动发现或学习 skill 的服务。

每轮启动 Python agent，在同一个容器里恢复原生消息历史。对话与文件会保留，Python 进程内状态（例如后台进程句柄）不会恢复，终端操作应在本轮完成。Agent、Simulator、并行任务和重试之间相互隔离。

`rollout/trace.jsonl` 是追加式桥接日志，记录实际请求、完整工具 schema、模型响应和 usage，以及原生 persistence hook 在压缩前观察到的工具结果。Raw 保留完整请求快照；ACTF/SFT 展示实际执行的逻辑对话，后续压缩摘要不会覆盖先前工具步骤。此格式不是 Hermes 自带 ShareGPT 导出，也不是 OpenClaw raw。

仍输出 ACTF、SFT、结果文件、provenance 和 usage。未完成轮次、空回复、损坏的工具参数/schema 或 JSONL 不会算作成功。超时会停止执行并回收已有轨迹。Hermes 不生成 OpenClaw cache trace，Resume 不要求该文件；`full` 模式仍要求 wire trace，`capture: none` 仍禁止复用。

## 验证与范围

测试矩阵、模型配置、验收和 Resume 审计命令见 [Coverage 使用说明](../../../examples/coverage/README.md)。

```bash
python examples/coverage/suite.py validate --case '*/hermes'
python examples/coverage/suite.py run --case '*/hermes' --exclude-case 'vision/*' --dry-run
python examples/coverage/suite.py run --case data/txt/hermes \
  --hermes-model your-model-id
python examples/coverage/suite.py check --case data/txt/hermes
```

矩阵新增 204 条 Hermes 输入任务和 28 条 Simulator 配对任务，覆盖文本、文件、Context、setup/env、二进制检查、产物、评分和短对话。完整矩阵为 880 条正向任务、20 条隔离负例。

首版支持 Chat Completions 与文本/文件工具执行。视觉工具、直接多模态消息、external thinking、厂商专属认证、Hermes gateway、远程 terminal backend、持久 Memory 和 skill learning 暂不支持。Hermes 可选视觉组用于记录能力边界，不纳入要求全部通过的验收范围；二进制文件传输与媒体理解分开验证。

网络受限时可设置构建参数 `PIP_INDEX_URL` 和 `PYPI_DOWNLOAD_BASE`。后者仅替换 `uv.lock` 下载地址的域名前缀，保留版本与 SHA-256 校验，镜像站需兼容 `/packages/` 布局。

## 可复用的适配器测试

先运行无需 Docker/凭据的常规测试，再显式启用真实容器集成测试：

```bash
PYTHONPATH=src python -m unittest discover -s tests
ROLLER_HERMES_DOCKER_TEST=1 PYTHONPATH=src \
  python -m unittest discover -s tests -p test_hermes_docker.py -v
```

集成测试使用本地固定响应 HTTP 服务和虚构 Key，真实执行 Hermes 工具，覆盖输入只读、Context/setup/env、多轮历史、full/off wire、reasoning 导出、Resume/run lock，以及超时回收和清理。它不调用外部模型，不产生模型费用；需要 Docker host 网络。

可用 `--case '*/hermes' --exclude-case 'vision/*'` 选中 200 条非视觉输入任务；追加 `--case 'simulators/pair/*'` 则包含全部 64 条 Agent/Simulator 任务，共 264 条，需要配置四种 Harness。`suite.py` 和 `merge.py` 均支持这些筛选参数。
