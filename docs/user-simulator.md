# User Simulator 与动态对话

User Simulator 用另一个正在运行的 Harness 扮演用户。它不会提前生成整段对话，而是在任务 Agent 完成当前指令后读取完整上下文，再决定下一条指令或结束对话。

这适合收集需求逐步暴露、反复修改和协作完成交付物的轨迹，例如招聘广告、需求访谈、客服对话和交互式代码评审。

## 1. Task 示例

```json
{
  "schema_version": "roller.task/v1",
  "id": "hr_recruitment_ad",
  "model": "my-openai-proxy/task-agent-model",
  "user_simulator": {
    "harness": "openclaw",
    "model": "my-openai-proxy/simulator-model",
    "max_turns": 6,
    "instruction": "你是一名 HR。通过多轮交流让 Agent 完成招聘广告；检查每版草稿并逐步补充要求，确认最终文件已经保存后结束。"
  },
  "files": {
    "output_dir": "outputs"
  }
}
```

`user_simulator` 与 `prompt`、`question`、`messages` 不能同时出现。前者由 Simulator 动态决定每一轮 user message，后者的 user message 在运行前已经确定。

字段说明：

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `instruction` | 是 | 每次调用 Simulator 都会提供的固定身份、目标和完成标准 |
| `harness` | 否 | Simulator Harness；目前为 `openclaw` |
| `model` | 否 | Simulator 模型；省略时使用任务 Agent 的模型 |
| `max_turns` | 否 | 最多向任务 Agent 发送多少条指令，默认 `6`，范围 `1–100` |
| `thinking` | 否 | 只覆盖 Simulator 的 OpenClaw thinking 设置 |

完整示例见 [`examples/tasks/user-simulator.jsonl`](../examples/tasks/user-simulator.jsonl)。

## 2. 对话如何运行

每条动态 rollout 由一个 Conversation Manager 和两个独立 session 组成：

1. 启动任务 Agent Harness。
2. 启动 User Simulator Harness。
3. Manager 把固定 `instruction`、完整对话和当前轮次交给 Simulator。
4. Simulator 决定发送下一条 user message，Manager 将它转交给任务 Agent。
5. 任务 Agent 完成这一轮，Manager 把回复加入上下文。
6. 重复步骤 3–5，直到 Simulator 结束、达到 `max_turns`、发生错误或整体超时。

任务 Agent 和 Simulator 各自在一个连续的 OpenClaw session 中运行，不会每轮重启。任务 Agent 可以读取 task 输入与 Context，并写入 `output_dir`；Simulator 使用独立工作区，只通过 Manager 提供的对话上下文了解任务进展。

`--timeout` 是整条 rollout 的总时间，包括两个容器的启动时间和所有对话轮次。`--parallel N` 仍表示并行 rollout 数；每条 User Simulator rollout 会占用两个容器。Roller 为 Simulator 使用另一组 gateway port，避免与同一 worker 的任务 Agent 冲突。

## 3. Manager 与 Simulator 的协议

每一次调用都重复传入固定 instruction，并附上从第一轮开始的完整 transcript。Manager 要求 Simulator 返回以下两种 JSON 之一：

```json
{"action":"message","content":"下一条给任务 Agent 的指令"}
```

```json
{"action":"finish","reason":"结束原因"}
```

Roller 使用 JSON parser 读取决策，不从自然语言中通过正则表达式猜测轮次或结束标记。Simulator 返回 Markdown code fence、普通文本或缺少必要字段时，本次 attempt 会标记为 `error`，并可由 `--retries` 重试。

Simulator 至少要发出一条指令后才能结束。完成 `max_turns` 轮后，Manager 仍允许 Simulator 根据最新回复判断是否结束；如果它继续提出新指令，Roller 会以 `max_turns` 正常终止，并在 `conversation.json` 中记录未转交的 `pending_instruction`。

## 4. 模型和 API 配置

任务 Agent 与 Simulator 共用 `--model-config` 中的 provider 列表，可以选择相同或不同模型：

```bash
roller collect \
  --tasks examples/tasks/user-simulator.jsonl \
  --model-config examples/json/openclaw-models.json \
  --docker-image your-openclaw-image:tag \
  --parallel 2 \
  --timeout 900 \
  --output runs \
  --run-name user_simulator_hr
```

命令行 `--model` 只覆盖任务 Agent 模型；`user_simulator.model` 仍由 task 指定。省略 `user_simulator.model` 时，它会跟随本次任务 Agent 模型。

## 5. 保存内容

动态对话 sample 在普通三件套之外增加 Manager transcript 和 Simulator 轨迹：

```text
samples/<model>/<index>_<task>/
├── trace.jsonl                    # 任务 Agent 原始轨迹
├── trace_ACTF.json                # 任务 Agent ACTF_v1.0
├── conversation.json              # 干净的交替对话与终止原因
├── info.json                      # 双方模型、usage、轮次和状态
├── user_simulator/
│   ├── trace.jsonl                # Simulator 原始轨迹
│   ├── trace_ACTF.json            # Simulator ACTF_v1.0
│   └── info.json                  # Simulator 状态、usage 和运行信息
└── results/                       # 可选；任务 Agent 的 output_dir
```

根目录 `info.json` 的 `usage` 是任务 Agent 与 Simulator 的总消耗，`conversation.participants` 中分别保存双方 usage。训练任务 Agent 时通常使用根目录的 `trace.jsonl` 或 `trace_ACTF.json`；研究用户策略或联合训练时可以同时使用 `user_simulator/` 中的轨迹。

`conversation.json` 保存 Simulator instruction、双方可见消息、轮次和终止原因，但不复制 Harness 的隐藏推理与工具事件；这些信息仍在各自的原始/ACTF 轨迹中。

## 6. Resume

User Simulator task 的 instruction、模型、thinking 或 `max_turns` 发生变化时，Task Bundle digest 会变化并重新运行。动态 sample 只有在任务 Agent 轨迹、Simulator 轨迹和 `conversation.json` 都完整存在时才能被 `--resume` 复用。
