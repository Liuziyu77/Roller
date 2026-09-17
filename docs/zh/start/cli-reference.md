# CLI 参数参考

[文档中心](../index.md) · [快速上手](getting-started.md) · [Task 格式](../tasks/task-format.md) · [产物说明](../traces/trajectory-outputs.md)

本页按适用范围列出 Roller CLI 参数。当前安装版本的参数以命令输出为准：

```bash
roller --help
roller collect --help
roller view --help
```

## 1. 命令结构

```text
roller collect [OPTIONS]   # 采集轨迹
roller view RUN_DIR        # 浏览已有 run
roller bench ...           # bench 翻译与评分汇总，见 §11
roller --version
```

最小 collect 命令需要 `--tasks`，并且模型必须来自重复的 `--model` 参数或 task 自身的 `model` 字段：

```bash
roller collect --tasks tasks.jsonl --model provider/model ...
```

## 2. 配置优先级

涉及 task 与全局命令行同时可配置的值时：

| 配置 | 优先级 |
| --- | --- |
| 模型 | 命令行存在任意 `--model` 时使用命令行模型；否则使用每条 task 的 `model` |
| Thinking | task `runtime.thinking` 优先；否则使用 `--thinking`；都省略时交给 Harness/model 默认值 |
| Workspace | task `workspace` 优先；否则使用全局 `--workspace` |
| User Simulator 模型 | `user_simulator.model` 优先；否则跟随当前 task Agent model |

重复的 `--model` 会去重，并与每条 task 做笛卡尔组合。`--start-index` 和 `--limit` 先裁剪规范化后的 task 列表，再展开模型。

## 3. 通用采集参数

以下参数适用于七种 Harness：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--tasks PATH` | 必填 | JSON 或 JSONL task 清单 |
| `--model MODEL` | task `model` | 可重复；为每条 task 运行所有指定模型 |
| `--harness NAME` | task `harness` 或 `openclaw` | `openclaw`、`codex`、`claude-code`、`hermes`、`opencode`、`deepseek` 或 `pi`；显式提供时统一覆盖 Task Agent 的字段 |
| `--docker-image IMAGE` | 对应 Harness 默认镜像 | 统一镜像覆盖；混合参与者可用 `--openclaw-image`、`--codex-image`、`--claude-code-image`、`--hermes-image`、`--opencode-image`、`--deepseek-image`、`--pi-image` 分别指定 |
| `--api-base URL` / `--api-key-env NAME` | 各 Harness 默认值 | 兼容旧用法；混合运行可用 `--codex-api-base`、`--claude-api-base`、`--hermes-api-base`、`--opencode-api-base`、`--deepseek-api-base`、`--pi-api-base` 及对应 `*-api-key-env` 分别指定 |
| `--parallel N` | `1` | 最大并行 rollout 数，必须至少为 1 |
| `--retries N` | `0` | 首次失败后的额外尝试次数 |
| `--timeout SECONDS` | `600` | 每条 rollout 的总超时，必须大于 0 |
| `--task-timeouts` | 关闭 | 改用每条 task 自己的 `timeout_seconds`，缺失时回落到 `--timeout`。bench 跑通常必须开启，见 [Bench 接入与评分](../benches/bench-format.md#7-cli) |
| `--start-index N` | `0` | 从第 N 条规范化 task 开始 |
| `--limit N` | 不限制 | 最多选取 N 条 task |
| `--output DIR` | `runs` | run 根目录的父目录 |
| `--run-name NAME` | UTC 时间名 | 本次 run 目录名 |
| `--resume` | 关闭 | 校验 run lock，并跳过完整的成功 sample |
| `--dry-run` | 关闭 | 校验 task 与文件并打印计划，不启动 Docker |
| `--workspace DIR` | 无 | legacy 全局工作区；新 task 使用 `file_root`/`files` |
| `--prompt-key NAME` | `prompt` | legacy 数据的 prompt 字段名 |
| `--id-key NAME` | `id` | legacy 数据的 ID 字段名 |
| `--log-level LEVEL` | `INFO` | `DEBUG`、`INFO`、`WARNING` 或 `ERROR` |

文件限制：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--max-input-file-bytes` | 10 GiB | 单个 input/context 文件上限 |
| `--max-task-input-bytes` | 50 GiB | 单条 task 全部输入的总上限 |

## 4. 通用 Harness 运行参数

这些参数由七种 Harness 共用，但具体允许值和语义由 Harness 校验：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--docker-image IMAGE` | 对应 Harness 固定镜像 | 覆盖本次运行镜像 |
| `--container-user USER` | `0` | 一次性容器用户；`image` 表示沿用镜像默认用户 |
| `--docker-binary PATH` | `docker` | Docker CLI 名称或路径 |
| `--network MODE` | `host` | 容器网络；本地 wire proxy 功能依赖 host network |
| `--pass-env NAME` | 无 | 可重复，额外透传宿主机非空环境变量 |
| `--thinking LEVEL` | 未设置 | Harness reasoning/thinking 档位 |
| `--keep-thinking` / `--no-keep-thinking` | 开启 | 控制 ACTF/SFT 是否导出 reasoning；OpenClaw 还控制请求历史剥离 |
| `--wire-trace MODE` | `actf` | `off`、`actf` 或 `full` |
| `--keep-container-on-error` | 关闭 | 失败时保留一次性容器便于调试 |

`--thinking` 的解析值为：

```text
off | minimal | low | medium | high | xhigh | adaptive | max | ultra
```

输入 `none` 会先规范化为 `off`。这不表示每个 Harness 或模型都接受所有档位：

| Harness | 当前 Roller 映射 |
| --- | --- |
| OpenClaw 2026.7.1 | 接受至 `max`；所选 model profile 仍会进一步校验 |
| Codex 0.149.0 | `minimal/low/medium/high/xhigh`；省略表示使用默认值 |
| Claude Code 2.1.222 | `low/medium/high/xhigh/max/ultra`，其中 `ultra` 映射为 `ultracode` |

详细映射以各 Harness 指南的 reasoning 章节为准。

### Wire trace

| 模式 | 产物与用途 | DeepSeek Harness |
| --- | --- | --- |
| `off` | 不启动 HTTP 代理采集；OpenCode/Hermes/DeepSeek 仍保留原生请求中的 schema | 原生 reasoning |
| `actf` | 默认；保留 ACTF 需要的 system/tools，删除中间 wire 文件 | 不支持 |
| `full` | 额外保留 `wire_trace.jsonl`，用于审计实际 HTTP 请求 | 不支持 |

`full` 会保存 prompt、reasoning、工具参数与结果等完整请求文本，体积和敏感度都更高。参见 {ref}`轨迹产出文件指南 <wire-trace-jsonl>`。

## 5. OpenClaw 专属参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--model-config PATH` | 无 | OpenClaw providers/models JSON，支持 `${ENV_NAME}` |
| `--gateway-port N` | `18789` | worker 0 的 gateway 起始端口 |
| `--gateway-port-step N` | `10` | worker slot 之间的端口间隔 |
| `--gateway-startup-timeout SEC` | `30` | 等待 gateway 就绪的时间 |
| `--session-id NAME` | `roller` | OpenClaw session ID 前缀 |
| `--search-provider NAME` | 无 | OpenClaw web search provider |
| `--cache-trace` / `--no-cache-trace` | 开启 | 记录每次组装后的 system prompt |
| `--set-image-model` / `--no-set-image-model` | 开启 | 是否同时尝试设置 image model |
| `--collect-path PATH` | 无 | Legacy：额外回收容器路径到 `results/legacy` |

默认镜像优先级：`--docker-image` → `ROLLER_OPENCLAW_IMAGE` → `ghcr.io/openclaw/openclaw:2026.7.1-2`。

## 6. Codex 专属参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--api-base URL` | `OPENAI_BASE_URL` | Responses API base |
| `--api-key-env NAME` | `OPENAI_API_KEY` | 存放 API Key 的宿主机变量名 |
| `--codex-config PATH` | 无 | 复制到隔离 `$CODEX_HOME/config.toml` 的配置 |
| `--reasoning-summary MODE` | Codex 默认 | `auto`、`concise`、`detailed` 或 `none` |
| `--codex-web-search MODE` | `disabled` | `disabled`、`cached` 或 `live` |
| `--codex-websockets` / `--no-codex-websockets` | 关闭 | 自定义 provider 是否允许 Responses WebSocket |

默认镜像优先级：`--docker-image` → `ROLLER_CODEX_IMAGE` → `roller-codex:0.149.0`。

自定义 OpenAI-compatible API 默认使用 Responses HTTP streaming。External thinking 或 full wire trace 要求显式 `--api-base`、默认 host network 和 `--no-codex-websockets`。

## 7. Claude Code 专属参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--api-base URL` / `--anthropic-base-url URL` | `ANTHROPIC_BASE_URL` | Anthropic origin、`/v1` base 或 `/v1/messages` endpoint |
| `--api-key-env NAME` | `ANTHROPIC_API_KEY` | 存放 API Key 的宿主机变量名 |
| `--claude-settings PATH` | 无 | 注入隔离容器的 settings JSON |
| `--claude-max-turns N` | 无 | 每条预定义 user message 的 Claude agent turn 上限 |
| `--claude-max-thinking-tokens N` | 无 | 容器内 `MAX_THINKING_TOKENS` |
| `--claude-permission-mode MODE` | `bypassPermissions` | Claude Code permission mode |
| `--claude-signature-recovery MODE` | `off` | `off`、`fill` 或 `prefer` |
| `--claude-signature-recovery-attempts N` | `1` | 每个 signed block 的最大 replay 次数 |
| `--claude-signature-recovery-max-tokens N` | `8192` | 每次 replay 的最大可见输出 token |

默认镜像优先级：`--docker-image` → `ROLLER_CLAUDE_CODE_IMAGE` → `roller-claude-code:2.1.222`。

Signature recovery 必须同时保持 `--keep-thinking`。它是任务结束后的额外 provider 请求，不是本地解密；详见 [Claude Code 指南](../harnesses/claude-code-configuration.md#5-signature-recovery实验性)。

## 8. External thinking

该选项主要面向 `gpt-5.6-sol` 等 GPT reasoning 模型。Qwen、GLM 等开源模型的 API 返回
明文 reasoning 时，保持默认 `--keep-thinking`，无需使用 external thinking。Claude signed
thinking 使用 `--claude-signature-recovery`，不使用本选项。

```text
--external-thinking off|external|external_always
```

| 模式 | OpenClaw | Codex | Claude Code | Hermes | OpenCode |
| --- | --- | --- | --- | --- | --- |
| `off` | 支持，默认 | 支持，默认 | 唯一支持值 | 唯一支持值 | 唯一支持值 |
| `external` | Responses provider | Responses HTTP | 不支持 | 不支持 | 不支持 |
| `external_always` | Responses provider | Responses HTTP | 不支持 | 不支持 | 不支持 |

启用后，Roller 关闭原生 reasoning，把 `deep_think` 工具参数标准化进 SFT 的 `reasoning_content`。阶段协议、验收和安全边界见 [External Thinking](../traces/external-thinking.md)。

## 9. Resume

```bash
roller collect [原命令] --resume
```

Resume 不是“忽略所有已有文件”。Roller 会先比对 `lock.json` 中影响语义的配置与输入摘要，再验证成功 sample 的必需产物；只有锁一致且完整性通过的 sample 才会跳过。配置改变、输入改变或产物损坏会拒绝混用或触发重跑。

应继续同一个 run 时使用 `--resume`；要改变模型、镜像、thinking、wire 或其他语义配置时，应使用新的 `--run-name`。

## 10. Viewer 参数

```bash
roller view RUN_DIR [--host HOST] [--port PORT]
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | 必填 | 包含 `info.json` 与 `samples/` 的 Roller run |
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `8765` | 监听端口 |

监听 `0.0.0.0` 会向网络暴露轨迹内容。远程访问使用默认地址和 SSH 端口转发。

## 11. Bench 参数

```bash
roller bench list
roller bench export NAME ROOT --output TASKS.jsonl [--category CAT]... \
  [--task-id ID]... [--runs-per-task N]
roller bench report RUN_DIR [--output REPORT.json]
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `NAME` | 必填 | adapter 名，取自 `roller bench list` |
| `ROOT` | 必填 | 已 checkout 的 benchmark 目录 |
| `--output` / `-o` | export 必填 | export 写出的 task JSONL；report 时为可选的报告 JSON |
| `--category CAT` | 不限制 | 只导出该类别，可重复 |
| `--task-id ID` | 不限制 | 只导出该 task，可重复；写错的 id 会报错而不是静默跳过 |
| `--runs-per-task N` | `1` | 每个选中 task 生成 N 个独立 rollout；id 后缀为 `__run_01` 等 |
| `RUN_DIR` | 必填 | 已完成的 run 目录，**位置参数**（不是 `--run`） |

`bench export` 只做翻译，不执行任何任务、不联网、不启容器。
采集时记得加 `--task-timeouts`，并用 `--pass-env` 透传 grader 需要的变量。
`swebench-pro` 是例外的运行期镜像适配：导出仍然离线，但 `collect` 首次处理每个实例时
会拉取官方镜像并构建带 OpenClaw 的本地派生镜像。

字段语义、评分时序、`stage` 防泄漏规则和聚合口径见 [Bench 接入与评分](../benches/bench-format.md)。

## Hermes 专属参数

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `--hermes-image` | `ROLLER_HERMES_IMAGE` 或 `roller-hermes:d15ed4445207` | 覆盖共享 `--docker-image` |
| `--hermes-api-base` | `--api-base`、`OPENAI_BASE_URL`、OpenAI `/v1` | Chat Completions API 地址 |
| `--hermes-api-key-env` | `--api-key-env` 或 `OPENAI_API_KEY` | 宿主机 Key 变量名 |
| `--hermes-context-length` | 由 Hermes 读取模型信息 | 可选窗口覆盖，至少 64000 tokens |
| `--hermes-max-iterations` | `12` | 每条 user message 的模型迭代上限 |

Hermes 接受 `off/minimal/low/medium/high/xhigh` reasoning 提示，最终由原生模型/provider 适配决定；暂不支持 external thinking。所有 wire 模式均保留原生请求中的工具 schema；`full` 额外保留 HTTP 轨迹，要求已配置 API base 和 host 网络。详见 [Hermes 配置](../harnesses/hermes-configuration.md)。

## OpenCode

| Option | Default |
| --- | --- |
| `--opencode-image` | `roller-opencode:1.18.30` |
| `--opencode-api-base` | `--api-base` / `OPENAI_BASE_URL` |
| `--opencode-api-key-env` | `--api-key-env` / `OPENAI_API_KEY` |
| `--opencode-max-steps` | 12 |
| `--opencode-context-length` | 128000 |
| `--opencode-output-tokens` | 8192 |

[原生会话、请求捕获和限制](../harnesses/opencode-configuration.md)。

## DeepSeek Harness

| 参数 | 默认值 |
| --- | --- |
| `--deepseek-image` | `roller-deepseek:0.1.5rc1` |
| `--deepseek-api-base` | `--api-base` / `DEEPSEEK_BASE_URL` / `https://api.deepseek.com` |
| `--deepseek-api-key-env` | `--api-key-env` / `DEEPSEEK_API_KEY` |
| `--deepseek-max-steps` | 12 |
| `--deepseek-context-length` | 128000 |
| `--deepseek-output-tokens` | 8192 |

[DeepSeek Harness](../harnesses/deepseek-configuration.md) 使用 `sdk-minimal`、持久化 Bash 和原生 `off/low/high/max` reasoning。

## Pi Agent Harness

| 参数 | 默认值 |
| --- | --- |
| `--pi-image` | `roller-pi:0.85.1` |
| `--pi-api-base` | `--api-base` / `OPENAI_BASE_URL` / `https://api.openai.com/v1` |
| `--pi-api-key-env` | `--api-key-env` / `OPENAI_API_KEY` |
| `--pi-max-steps` | 12 |
| `--pi-context-length` | 128000 |
| `--pi-output-tokens` | 8192 |

[Pi Agent](../harnesses/pi-configuration.md) 支持 `off/minimal/low/medium/high/xhigh/max` reasoning；默认 `off`。
