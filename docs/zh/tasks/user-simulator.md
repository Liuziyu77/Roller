# User Simulator 与动态对话

[文档中心](../index.md) · [Task 格式](task-format.md) · [示例](../start/examples.md#动态-user-simulator) · [产物说明](../traces/trajectory-outputs.md)

User Simulator 是一个独立运行的 Harness，用模型扮演用户。它不会提前生成完整对话，而是在任务 Agent 完成当前指令后读取截至当前轮的完整上下文，再决定提出新要求或结束任务。

这种模式适合收集需求逐步暴露、反复修改和协作交付的轨迹，例如招聘广告、需求访谈、客服对话、教学辅导和交互式代码评审。

## 1. 与预定义多轮的区别

| 模式 | User message 从哪里来 | 适合场景 |
| --- | --- | --- |
| `messages` | Task 文件中提前写好 | 可复现的固定流程、回归测试 |
| `user_simulator` | Simulator 根据 Agent 当前回复动态生成 | 需求补充、追问、协商与自适应任务 |

预定义多轮无论 Agent 回答什么，下一条 message 都不变。User Simulator 可以检查遗漏、纠正方向，也可以在目标已经完成时提前结束。

两种模式不能同时出现在一条 task 中。单轮和预定义多轮的写法见 [Task 格式参考](task-format.md#3-对话模式)。

## 2. 一条完整 Task

```json
{
  "schema_version": "roller.task/v1",
  "id": "hr_recruitment_ad",
  "harness": "codex",
  "model": "my-openai-proxy/task-agent-model",
  "user_simulator": {
    "harness": "openclaw",
    "model": "my-openai-proxy/simulator-model",
    "max_turns": 6,
    "thinking": "high",
    "instruction": "你是一名正在招聘后端工程师的 HR。通过多轮交流让任务 Agent 完成一份中文招聘广告。先说明基本需求，再根据当前草稿逐步补充职责、技术要求、薪资、远程政策和申请方式；不要一次给出全部信息。最终版本必须保存到 outputs/recruitment-ad.md，确认内容完整且文件已经保存后结束。"
  },
  "files": {
    "output_dir": "outputs"
  }
}
```

| 字段 | 必需 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `instruction` | 是 | 无 | 固定身份、目标、交互策略和完成标准 |
| `harness` | 否 | `openclaw` | Simulator 使用的 Harness：`openclaw`、`codex`、`claude-code`、`hermes`、`opencode` 或 `deepseek` |
| `model` | 否 | 任务 Agent 模型 | Simulator 使用的 model ref |
| `max_turns` | 否 | `6` | 最多发送给任务 Agent 的指令数，范围 `1–100` |
| `thinking` | 否 | CLI 默认值 | 只覆盖 Simulator；使用与 [`--thinking`](../harnesses/openclaw-configuration.md#5-thinkingsystem-prompt-与-wire-trace) 相同的逻辑档位和校验 |

命令行 `--model` 只覆盖任务 Agent 的模型；`user_simulator.model` 仍按 task 配置。省略 Simulator model 时，它跟随本次实际使用的任务 Agent 模型。

## 3. Conversation Manager

动态 rollout 有三个参与者：

| 参与者 | 职责 |
| --- | --- |
| Conversation Manager | 维护上下文、转发消息、执行轮次与超时规则 |
| Task Agent | 执行用户指令、调用工具、读取输入并写出结果 |
| User Simulator | 根据固定 instruction 和完整对话生成下一条用户指令 |

执行顺序如下：

1. Manager 分别启动任务 Agent 和 User Simulator session。
2. Manager 把固定 instruction、空对话和轮次状态发给 Simulator。
3. Simulator 给出第一条 user message，Manager 将其转发给任务 Agent。
4. Agent 完成当前轮，Manager 把 user/assistant 消息加入 transcript。
5. Manager 把固定 instruction 与更新后的完整 transcript 再次交给 Simulator。
6. 重复交替过程，直到 Simulator 结束、达到轮次上限、发生错误或整体超时。

两个 session 在整个 rollout 中持续存在，不会每轮重启。Task Agent 拥有 task 的输入、Context 和 `output_dir`；Simulator 使用独立工作区，只能通过 Manager 提供的对话了解任务进展。Codex 和 Claude Code 也支持该交互接口，并分别使用各自的 native resume/continue session。

## 4. Simulator 决策协议

每次调用时，Manager 都会提供：

- task 中固定不变的 `instruction`；
- `rounds_completed` 与 `max_turns`；
- 从第一轮开始的完整 user/assistant transcript。

Simulator 必须只返回以下两个 JSON 对象之一。

继续对话：

```json
{"action":"message","content":"请在岗位要求中补充 Python 与 PostgreSQL。"}
```

结束对话：

```json
{"action":"finish","reason":"招聘广告内容完整，且已确认写入目标文件。"}
```

Roller 使用 JSON parser 读取决策，不从 Markdown 标题、分隔线或自然语言中的关键字猜测轮次和结束标记。返回 Markdown code fence、额外解释、未知 action 或空 content 都会使本次 attempt 失败；配置了 `--retries` 时可以重新执行整条 rollout。

Simulator 至少需要给 Task Agent 发出一条指令，之后才能结束。

## 5. 如何写好 instruction

Instruction 不是第一轮 user message，而是 Simulator 每一轮都要遵守的固定策略。建议同时写清五类信息：

1. **身份**：Simulator 扮演谁，掌握哪些信息。
2. **目标**：最终要让 Agent 完成什么任务。
3. **披露策略**：信息一次给完，还是根据当前结果逐步补充。
4. **检查方式**：每一轮应检查哪些遗漏、错误或交付物状态。
5. **结束条件**：什么情况下返回 `finish`，是否必须确认目标文件存在。

例如 HR 案例中，合理的交互可能是：

| 阶段 | Simulator 行为 |
| --- | --- |
| 启动 | 给出岗位和候选人的基本要求 |
| 修订 | 检查草稿，补充职责与技术栈 |
| 完善 | 继续补充薪资、工作方式和申请渠道 |
| 验收 | 检查内容完整，并要求写入 `outputs/recruitment-ad.md` |
| 结束 | Agent 明确完成交付后返回 `finish` |

不要把“逐步披露需求”和一份已经包含所有细节的第一轮话术同时写进 instruction，否则动态交互会退化成普通单轮任务。也不要只写宽泛身份而省略结束条件，否则 Simulator 容易无意义地持续追问到 `max_turns`。

仓库中的可运行版本见 [`examples/tasks/user-simulator.jsonl`](../../../examples/tasks/user-simulator.jsonl)。

## 6. 终止与失败

`conversation.json` 的 `termination.reason` 会记录终止类型：

| Reason | 状态 | 含义 |
| --- | --- | --- |
| `simulator_finished` | `ok` | Simulator 主动确认目标完成 |
| `max_turns` | `ok` | 已完成允许的 Agent 轮次，但 Simulator 仍想继续 |
| `timeout` | `timeout` | 两个 Harness 的总执行时间超过 `--timeout` |
| `agent_error` | `error` | Task Agent session 收尾失败 |
| `simulator_error` | `error` | Simulator session 收尾失败 |
| `simulator_trace_error` | `error` | Simulator raw trace 无法归档或转换 |
| `error` | `error` | 决策 JSON、容器或其他调度阶段失败 |

完成第 `max_turns` 个 Agent 回合后，Manager 会再请求一次 Simulator 决策，让它有机会确认完成。如果 Simulator 此时又返回 `message`，这条消息不会再交给 Agent，而是记入 `termination.pending_instruction`，并以 `max_turns` 正常结束。

`--timeout` 是整个动态 rollout 的总时限，包含两个容器启动、所有 Simulator 调用、所有 Task Agent 回合和轨迹收尾。长对话应为它预留比普通单轮更充足的时间。

## 7. 模型、并行与成本

Task Agent 与 Simulator 可以分别设定 Harness 和 model。例如用 Codex 执行任务、用 OpenClaw 或 Claude Code 扮演用户；同一 Harness 下也可以设置不同 model。OpenClaw 的 model ref 仍来自 `--model-config`，Codex/Claude Code/Hermes/OpenCode/DeepSeek 使用各自 API 参数。

```bash
roller collect \
  --tasks examples/tasks/user-simulator.jsonl \
  --model-config examples/config/openclaw/models.json \
  --docker-image your-openclaw-image:tag \
  --parallel 2 \
  --timeout 900 \
  --output runs \
  --run-name user_simulator_hr
```

`--parallel N` 仍表示同时执行 N 条 rollout。每条动态 rollout 会同时占用两个容器，并产生两组模型调用和两份轨迹。端口规划见 [OpenClaw 配置](../harnesses/openclaw-configuration.md#7-并行容器与端口)。

## 8. 保存与使用轨迹

动态 sample 在普通文件之外增加干净对话和 Simulator 子目录：

```text
samples/<task-agent-model>/<index>_<task>/
├── trace.jsonl                    # Task Agent 原始轨迹
├── trace_ACTF.json                # Task Agent ACTF_v1.0
├── trace_SFT.json                 # Task Agent SFT record
├── conversation.json              # 交替对话与终止信息
├── info.json                      # 总 usage 与双方分项信息
├── user_simulator/
│   ├── trace.jsonl                # Simulator 原始轨迹
│   ├── trace_ACTF.json            # Simulator ACTF_v1.0
│   ├── trace_SFT.json             # Simulator SFT record
│   └── info.json                  # Simulator 独立 metadata
└── results/                       # 可选；Task Agent 的 output_dir
```

各文件的用途：

- 训练 Task Agent：使用根目录 `trace_SFT.json` 或 run 级 `sft.jsonl`；分析时使用 `trace.jsonl` 或 `trace_ACTF.json`。
- 研究模拟用户策略：使用 `user_simulator/` 下的轨迹。
- 只分析双方可见对话：使用 `conversation.json`。
- 统计成本：根 `info.json` 的 `usage` 是双方总量；根 `info.json` 的 `conversation.participants` 保存两边各自的 usage 和轨迹位置。
- 检查任务交付：使用 `results/`，这里只回收 Task Agent 的输出。

`conversation.json` 不复制隐藏 reasoning、tool call 或 tool result；这些事件仍留在各自 raw/ACTF/SFT 轨迹中。详细产出条件见 [轨迹产出文件指南](../traces/trajectory-outputs.md)，sample 字段和结果安全规则见 [文件、Task Bundle 与结果存储](task-bundle-and-storage.md#6-run-与-sample)。

## 9. Retry 与 Resume

`--retries N` 会重新执行整条动态 rollout，而不是只重试失败的一轮。这能避免复用与原对话状态不一致的 session。

Instruction、Simulator model、thinking、`max_turns`、Task Agent 输入或 Context 发生变化时，
Task Bundle digest 会变化，run lock 检查会拒绝把新配置混入旧 run。锁一致后，`--resume`
只会复用身份与 canonical 轨迹校验通过、并同时具备以下文件的成功 sample：

- Task Agent 的 raw/ACTF/SFT；
- `conversation.json`；
- Simulator 的 raw/ACTF/SFT；
- 与当前 task 和文件一致的 digest。

`conversation.json` 和双方 participant `info.json` 也必须具有正确 schema 与成功状态。
完整规则见 [文件、Task Bundle 与结果存储](task-bundle-and-storage.md#9-retry-与-resume)。

## 10. 当前边界

- Task Agent harness 可按 task 设置；显式 `--harness` 会统一覆盖所有 Task Agent。Simulator harness 可按 task 独立设置；
- Simulator 只通过 transcript 获取对话信息，不共享 Task Agent 的文件系统与工具状态；
- 当前协议是一次决策对应一条 user message，不支持多个模拟用户同时参与；
- `max_turns` 是交给 Task Agent 的指令数，不包含最终的 `finish` 检查。

后续接入其他 Harness 时，Conversation Manager 的轮次、决策和保存边界可以保持不变，只需由新的 Harness 实现 interactive session 接口。

Hermes 保留同一个容器，每轮启动 Python agent 并恢复原生消息历史；后台进程句柄等进程内状态不会恢复。详见 [Hermes](../harnesses/hermes-configuration.md)。

DeepSeek Harness 为每个参与者保留同一个 SDK/runtime 进程和持久化 Bash，跨轮共享原生会话及 shell 状态。详见 [DeepSeek Harness](../harnesses/deepseek-configuration.md)。
