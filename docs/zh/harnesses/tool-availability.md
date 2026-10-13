# Harness 工具开放情况

Roller 不保证每个模型都能使用每个工具。工具 schema 由固定版本的原生 Harness、容器依赖、服务凭据和显式用户配置决定；ACTF 中的 `tools` 来自实际请求，不能用固定工具名称清单代替。

| Harness | 工具选择 | 保留的边界与原因 |
| --- | --- | --- |
| OpenClaw | 原生工具注册和模型配置；Roller 不添加工具白名单 | Web 等工具依赖相应凭据；原生 owner/session 策略仍生效 |
| Codex | 原生工具；`--codex-config` 可传原生配置 | `--codex-web-search` 默认 disabled，可显式选 cached/live；临时 CODEX_HOME 不继承宿主机账号和 MCP |
| Claude Code | 原生工具；`--claude-settings` 和权限模式由用户指定 | 临时容器和账号目录不继承宿主机集成；非交互运行受原生权限规则约束 |
| Hermes | 默认 `enabled_toolsets=None`，让原生注册表决定可用工具 | `--hermes-toolset NAME` 可重复指定；`--no-hermes-vision` 显式禁用 vision。工具所需依赖和 Key 必须在容器中可用 |
| OpenCode | 原生工具注册表；不再以固定八项工具白名单默认拒绝其他工具 | 批处理默认允许原生工具，避免等待交互审批；`--opencode-deny-tool NAME` 可重复禁用。LSP/formatter、自动插件加载和宿主配置发现仍关闭，避免额外后台服务和隐式配置 |
| DeepSeek | 默认完整 `sdk` profile，由原生 profile 提供文件、搜索、shell 等工具 | `--deepseek-profile sdk-minimal` 可显式恢复 shell-only；自定义 profile 必须在镜像/任务 setup 中安装并保留 SDK JSON-RPC 服务 |
| Pi | 固定版本的七种内置工具 read/bash/edit/write/grep/find/ls 全部传入 SDK | 不自动加载扩展、宿主 skills 或 MCP；不是只读工具子集。此边界用于隔离任务，不代表支持第三方扩展 |

Hermes 仍使用 local terminal 后端，在容器内执行。自动安装依赖、宿主身份/持久记忆与后台 review 仍关闭；这些属于隔离及可复现边界，不应解释成只有 terminal/file 工具。可通过镜像和 `--pass-env` / task `runtime.env` 提供工具依赖及凭据。

DeepSeek 的 `sdk-minimal` 本身仅提供持久 shell，原先的 bash-only 来源是 Roller 固定选择该 profile，而不是 SDK 仅支持 bash。`--deepseek-permission-mode` 默认 danger-full-access，仅作用于一次性容器；可显式选择 read-only/workspace-write，但要求可用的二次沙箱后端。宿主输入仍只读挂载。完整 `sdk` 的权限、压缩、技能、子 Agent 和 Web 可用性遵循原生运行环境。Roller patch 仍记录请求、限制步骤并关闭服务端 session/plugin 附加上传和模型重试，不删除工具插件。

运行锁记录 Hermes toolsets、OpenCode deny list 和 DeepSeek profile；改变这些参数必须创建新 run。配置中的 null 工具列表表示由原生运行时发现，实际工具以原始请求和 ACTF 为准。
