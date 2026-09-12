# Roller 功能覆盖测试

[English](README.en.md) · [场景清单](COVERAGE.md) · [合并总 JSON](all_tasks.json) · [逐组预期](manifest.json) · [文件来源](sources.json) · [Examples](../README.md)

可随仓库复用的小任务测试集：**1368 条正常任务，342 组，每组 4 条；另有 20 条预期拒绝的输入**。覆盖 OpenClaw、Codex、Claude Code、Hermes、OpenCode、DeepSeek 六种 Harness，以及全部 36 种 Task Agent × User Simulator 配对。

输入小文件已包含在仓库内，运行和重新生成均不需要原始 `Roller_Data`。公开任务使用模型占位值和相对路径。所有命令从 **Roller 项目根目录** 执行，需要 **Python 3.10+**。

## 1. 离线验证

无需模型凭据、Docker 或额外 Python 依赖：

```bash
python examples/coverage/suite.py list
python examples/coverage/suite.py validate
python examples/coverage/validate_merged.py
python examples/coverage/suite.py run --case '*' --dry-run
```

预期：342 个正常组、1368 条任务、20 条错误输入分别通过校验，40 份来源文件摘要匹配。检查包括清单解析、模型路由、文件打包、Context 的 Resume 策略、对话轮数和内嵌评分代码。`--dry-run` 不执行模型任务。

## 2. 配置模型服务

真实采集前，按 [Getting Started](../../docs/zh/start/getting-started.md) 安装 Roller 并准备所选 Harness 的镜像。不同 Harness 使用不同协议；同一服务是否支持它们，取决于服务自身。

| Harness | 服务协议 | 模型变量 | 连接配置 |
| --- | --- | --- | --- |
| OpenClaw | Responses 或 Chat Completions | `ROLLER_TEST_OPENCLAW_MODEL`，格式为 `provider/model-id` | [openclaw.models.json](config/openclaw.models.json)，地址和 Key 从环境变量展开 |
| Codex | Responses | `ROLLER_TEST_CODEX_MODEL` | `--codex-api-base`、`--codex-api-key-env` |
| Claude Code | Anthropic Messages | `ROLLER_TEST_CLAUDE_CODE_MODEL` | `--claude-api-base`、`--claude-api-key-env` |
| Hermes | Chat Completions | `ROLLER_TEST_HERMES_MODEL` | `--hermes-api-base`、`--hermes-api-key-env` |
| OpenCode | Streaming Chat Completions | `ROLLER_TEST_OPENCODE_MODEL` | `--opencode-api-base`, `--opencode-api-key-env` |
| DeepSeek Harness | DeepSeek Chat Completions | `ROLLER_TEST_DEEPSEEK_MODEL` | `--deepseek-api-base`, `--deepseek-api-key-env` |

```bash
cp examples/coverage/config/.env.example examples/coverage/config/.env
# 编辑 config/.env，填写所选 Harness（包括 Simulator）的实际模型、地址和 Key。
set -a
source examples/coverage/config/.env
set +a
```

`.env` 已被 Git 忽略，Roller 不会自动加载它。OpenClaw 的模型 ID 和协议分别由 `ROLLER_TEST_OPENCLAW_MODEL_ID`、`ROLLER_TEST_OPENCLAW_API` 配置。镜像可用 `ROLLER_OPENCLAW_IMAGE`、`ROLLER_CODEX_IMAGE`、`ROLLER_CLAUDE_CODE_IMAGE`、`ROLLER_HERMES_IMAGE`、`ROLLER_OPENCODE_IMAGE` 覆盖。模板不依赖私人镜像或固定代理端口。

## 3. 先运行一组 4 条任务

以下命令择一使用，只需配置该组涉及的 Harness。默认并发 2、单次超时 180 秒、重试 0；任务通常只做加法、读取小文件或写入一个答案。

```bash
# Codex：4 条加法任务。
python examples/coverage/suite.py run --case formats/jsonl/codex \
  -- --codex-api-base "$ROLLER_CODEX_BASE_URL" \
     --codex-api-key-env ROLLER_CODEX_API_KEY

# OpenClaw：4 条 CSV 读取和文件产物任务。
python examples/coverage/suite.py run --case data/csv/openclaw \
  -- --model-config examples/coverage/config/openclaw.models.json

# Claude Code：4 条 Markdown 文件读取任务。
python examples/coverage/suite.py run --case data/md/claude-code \
  -- --claude-api-base "$ROLLER_ANTHROPIC_BASE_URL" \
     --claude-api-key-env ROLLER_ANTHROPIC_API_KEY

# Hermes：4 条文本文件读取任务。
python examples/coverage/suite.py run --case data/txt/hermes \
  -- --hermes-api-base "$ROLLER_HERMES_BASE_URL" \
     --hermes-api-key-env ROLLER_HERMES_API_KEY

# OpenCode：4 条文本文件读取任务。
python examples/coverage/suite.py run --case data/txt/opencode \
  -- --opencode-api-base "$ROLLER_OPENCODE_BASE_URL" \
     --opencode-api-key-env ROLLER_OPENCODE_API_KEY

# Codex Agent + Claude Code Simulator：4 条短交互，最多两轮。
python examples/coverage/suite.py run --case simulators/pair/codex--claude-code \
  -- --codex-api-base "$ROLLER_CODEX_BASE_URL" \
     --codex-api-key-env ROLLER_CODEX_API_KEY \
     --claude-api-base "$ROLLER_ANTHROPIC_BASE_URL" \
     --claude-api-key-env ROLLER_ANTHROPIC_API_KEY
```

`--case` 支持带引号的 glob，可重复指定；`--exclude-case` 可排除匹配组，例如 `--case '*/hermes' --exclude-case 'vision/*'` 选择 200 条 Hermes 非视觉任务。例如 `--case 'data/*/codex'` 选中 40 条任务，`--case 'simulators/pair/*'` 选中全部 144 条 Simulator 任务。默认输出在 `runs/coverage/<场景名__Harness>/`；可用 `--output` 隔离不同服务的结果。

`suite.py run` 从 `ROLLER_TEST_*_MODEL` 读取模型，也接受 `--openclaw-model`、`--codex-model`、`--claude-code-model`、`--hermes-model`、`--opencode-model`、`--deepseek-model` 覆盖。脚本在临时任务副本中绑定 Agent 和 Simulator 模型，保留模型继承测试，并处理自定义解析参数和环境变量。`--` 后的参数传给 `roller collect`；改变任务数量、路由或模型展开的参数会被拒绝。

验收对应的四条结果：

```bash
python examples/coverage/suite.py check --case formats/jsonl/codex
```

`check` 验证 raw/ACTF/SFT 完整性、轮数、答案、输出文件、Simulator 终止原因、评分和 SFT 汇总顺序；失败时退出码非零。为 `run` 和 `check` 传入相同的 `--output`。同一采集命令在 `--` 后加 `--resume` 可验证重复运行；`context/none` 预期重新执行。

## 4. 使用一个总 JSON 运行混合 Harness

[all_tasks.json](all_tasks.json) 是全部样例的可迁移模板，包含 `tasks`、`negative_cases`、`coverage`、`models`、`environment`。`roller collect` 只读取其中的 `tasks`；错误输入由校验脚本逐条测试。

先生成绑定本地模型的副本。下面选择六种 Harness 各 4 条加法任务，共 24 条；将 `--case 'formats/jsonl/*'` 改成 `--case '*'` 即生成完整 1368 条。

```bash
python examples/coverage/merge.py \
  --case 'formats/jsonl/*' \
  --openclaw-model "$ROLLER_TEST_OPENCLAW_MODEL" \
  --codex-model "$ROLLER_TEST_CODEX_MODEL" \
  --claude-code-model "$ROLLER_TEST_CLAUDE_CODE_MODEL" \
  --hermes-model "$ROLLER_TEST_HERMES_MODEL" \
  --opencode-model "$ROLLER_TEST_OPENCODE_MODEL" \
  --deepseek-model "$ROLLER_TEST_DEEPSEEK_MODEL" \
  --absolute-roots \
  --output runs/coverage/merged/tasks.json

python examples/coverage/validate_merged.py --tasks runs/coverage/merged/tasks.json

python -m roller collect \
  --tasks runs/coverage/merged/tasks.json \
  --model-config examples/coverage/config/openclaw.models.json \
  --codex-api-base "$ROLLER_CODEX_BASE_URL" \
  --codex-api-key-env ROLLER_CODEX_API_KEY \
  --claude-api-base "$ROLLER_ANTHROPIC_BASE_URL" \
  --claude-api-key-env ROLLER_ANTHROPIC_API_KEY \
  --hermes-api-base "$ROLLER_HERMES_BASE_URL" \
  --hermes-api-key-env ROLLER_HERMES_API_KEY \
  --opencode-api-base "$ROLLER_OPENCODE_BASE_URL" \
  --opencode-api-key-env ROLLER_OPENCODE_API_KEY \
  --deepseek-api-base "$ROLLER_DEEPSEEK_BASE_URL" \
  --deepseek-api-key-env ROLLER_DEEPSEEK_API_KEY \
  --parallel 2 --timeout 180 --retries 0 --wire-trace full \
  --output runs/coverage/merged --run-name collection
```

可先在采集命令添加 `--dry-run`。完整采集包含可选视觉任务，需要另外配置视觉能力；仅验证文本和文件管线时，优先选择非视觉组。`environment` 是使用提示，Collector 不会自动导出其中的变量；上面的 `.env` 已设置 `runtime/env` 所需的 `ROLLER_EXAMPLE_TOKEN=ENV_OK`。

`merge.py` 的模型参数必须显式提供，不读取环境，以保证无参数重新生成始终得到同一份公开模板。如果三种协议服务支持同一模型 ID，也可用 `--model MODEL_ID --openclaw-provider PROVIDER` 简写；各 Harness 的独立参数优先。本地模型或任务子集必须写到 `--output` 指定的新文件。

合并会统一 JSON/JSONL 格式、自定义 prompt/id key 和特殊 ID。**原始解析格式的完整覆盖应使用 `suite.py`**。绝对 `file_root` 场景在仓库里保存相对路径，由 `suite.py` 运行时转换；合并本地副本时用 `--absolute-roots` 保留这一验证点。复制整个 Roller 仓库即可迁移样例，单独复制 `all_tasks.json` 仍需保留它引用的 fixtures。

## 5. 生成报告和审计 Resume

下面的检查不请求模型，必须传入当时使用的任务副本：

```bash
python examples/coverage/report.py runs/coverage/merged/collection \
  --tasks runs/coverage/merged/tasks.json \
  --output runs/coverage/merged/report.json --fail-on-check

python examples/coverage/audit_resume.py runs/coverage/merged/collection \
  --tasks runs/coverage/merged/tasks.json \
  --output runs/coverage/merged/resume-audit.json
```

报告区分采集成功 `collected_ok` 和严格验收通过 `passed`；额外解释、错误答案或缺失产物会导致严格验收失败。`--fail-on-check` 适合 CI，也检查运行是否完整及 SFT 汇总。检查脚本读取运行的 trace 配置，按 Agent、Simulator 各自的实际 Harness 判断 cache trace。

Resume 审计调用 Collector 的样本复用判定，不启动容器、不改写结果；它不替代 CLI 的 run lock 校验。真正恢复时，使用相同任务副本和采集参数，在采集命令末尾加 `--resume`。模型、输入或运行配置变化时使用新的输出目录。

## 覆盖范围与限制

| 类别 | 场景 / 配对数 | 任务数 | 验证点 |
| --- | ---: | ---: | --- |
| 清单格式 | 4 | 96 | JSONL、JSON 数组、tasks 对象、BOM 与空行 |
| 对话 | 5 | 120 | question、自定义 key、两种多轮消息、换行 |
| 路由与 ID | 3 | 72 | Harness 别名、CLI model、特殊 ID |
| 文本文件 | 10 | 240 | CSV、HTML、JSON、JSONL、LOG、MD、PY、SVG、TXT、YAML |
| 文件包 | 10 | 240 | SHA-256、多文件、目录、可选缺失、中文空格路径、空文件、根目录、旧 workspace |
| Context | 6 | 144 | full/hash/none、各角色、skill 目录、可选缺失 |
| Runtime | 4 | 96 | setup、env 的字符串/列表形式、low thinking |
| 产物 | 3 | 72 | 自定义目录、多文件、无文件产物 |
| 二进制 | 4 | 96 | PNG/PDF 字节数、WAV 帧数、ZIP 内文本 |
| 可选视觉 | 1 | 24 | 图像查看工具识别纯色 PNG |
| 容器内评分 | 1 | 24 | 输出文件确定性评分 |
| Simulator | 36 | 144 | 六种 Agent × 六种 Simulator |
| 错误输入 | 5 | 20 | 对话、路径、完整性、目录冲突、配置的拒绝行为 |

- Simulator 每组依次验证：一轮纯文本、CSV 两轮并保存答案、Memory 与自定义输出、`max_turns` 截断。同 Harness 第一条省略 Simulator model，验证继承。结束决策也会消耗模型调用。
- 文件输入表示将文件放入工作区；`messages[].content` 目前只支持字符串。二进制读取不代表视觉、PDF 文本理解或语音识别。视觉组需要图像工具、模型和服务协议同时支持；OpenClaw 模型配置还需声明 `"input": ["text", "image"]`。可复制为被忽略的 `config/openclaw.local.json` 后修改。
- Hermes 首版固定启用 `terminal`、`file`，不启用视觉工具；它的视觉组用于记录当前能力边界，不能作为要求全部通过的验收组。构建与限制见 [Hermes 指南](../../docs/zh/harnesses/hermes-configuration.md)。
- DeepSeek 使用官方 `sdk-minimal`，提供持久化 Bash，视觉验收不在首版范围；见 [DeepSeek 指南](../../docs/zh/harnesses/deepseek-configuration.md)。
- OpenCode 首版支持文本与文件工具，视觉组记录暂不支持的验收范围；见 [OpenCode 指南](../../docs/zh/harnesses/opencode-configuration.md)。
- 视觉工具可能产生当前 SFT 导出尚不支持的非文本内容，需结合 raw、ACTF 和错误信息定位。文本模型的视觉失败不能直接作为文件传输失败的证据。
- Context/skill 任务显式要求读取注入文件，测试可访问性和 capture 策略。`context/none` 不可 Resume 是预期行为。
- `bench/file_grader` 使用内嵌 Python 标准库评分，不依赖外部 benchmark；当前没有和 Simulator 组合。输入场景与全部 Simulator 配对未做完整笛卡尔积。

## 目录与维护

```text
coverage/
├── README.md / README.en.md    中英文使用说明
├── COVERAGE.md / manifest.json 场景索引、解析参数和预期
├── all_tasks.json              1368 条任务 + 20 条负例的总模板
├── sources.json               来源相对路径、大小和 SHA-256
├── config/                    环境模板和 OpenClaw 配置
├── cases/                     每组 4 条的原始清单
├── fixtures/                  内置小文件、Context 和媒体
│   └── *-default-root.jsonl    默认根目录测试，特意与输入同目录
├── negative/                  5 组逐条验证的错误输入
├── generate.py / merge.py      生成原始矩阵 / 总 JSON
├── suite.py                   列表、校验、分组运行与验收
├── validate_merged.py          校验总 JSON 或选定子集
└── report.py / audit_resume.py 合并运行的结果和复用检查
```

默认使用已收录的 40 份来源文件重新生成，不写入当前机器路径或环境模型：

```bash
python examples/coverage/generate.py
python examples/coverage/merge.py
python examples/coverage/suite.py validate
python examples/coverage/validate_merged.py
```

要从另一个完整数据集重新选取小文件，传 `generate.py --data-root /path/to/Roller_Data`，然后重新合并和校验。来源相对路径与摘要保存在 `sources.json`；原数据保持只读。新增场景时修改 `generate.py`，为每种情况提供 4 条短任务及预期，再重新生成；不要只修改生成的 JSON。

个人 `.env`、本地模型配置、运行产物与报告不属于公开样例。命令默认把产物写入 Git 忽略的 `runs/`；历史测试记录也保留在该目录，不随测试套件发布。
