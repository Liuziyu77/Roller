# 显式兼容选项

普通任务保持上游默认。不要仅因使用自定义 URL 就启用兼容补丁。

- `--claude-unique-tool-ids`：在响应交给 Claude CLI 前修复重复工具 ID；要求显式 API base。即使 `--wire-trace off`，修复仍生效，映射保存在 `tool_id_mappings.jsonl`。
- `--codex-image-detail auto|low|high|original`：只补 Responses 输入图片缺少的 `detail`，不覆盖已有值，不处理 Chat Completions。要求 API base、host 网络、禁用 WebSockets；日志关闭不关闭补丁。默认不启用。
- `--deepseek-profile sdk-minimal|sdk`：默认 `sdk-minimal` 不变；`sdk` 保持标准 SDK 的原生 prompt 和工具目录，不声明它只有 bash。
- `--deepseek-permission-mode danger-full-access`：仅提供已经验证过的原生选项，不设置时保留 SDK/显式传入环境策略。任务需自行决定是否授权该权限。
- `--deepseek-telemetry-disabled` / `--no-deepseek-telemetry-disabled`：默认不覆盖原生环境值。
- `--deepseek-vision`：显式声明工具读图能力；选中 `files.attach_images` 也会声明图片输入。默认无图任务不改模型声明。

DeepSeek 标准 SDK 的 bash 子进程只继承 `--pass-env` 或任务显式声明的工具环境变量，不把全部宿主机变量传入。任务专属 endpoint、路径去空格和权限启用策略不属于这些通用默认。

`info.json` 现在分别记录 `status`（执行）、`conversion`（raw/ACTF/SFT）、`training.eligible`（通用训练资格）。转换失败不把成功执行改成失败；有完整 raw 和结构化 `unavailable` 的结果可复用但不能进入 SFT。缺失/损坏产物的 `failed` 结果不可复用。`skipped` 只表示复用，`resumed_from_status` 保存原执行结果。

本文配置已通过 mock/离线测试，真实服务与镜像验证另记，不将配置可解析等同于服务可用。
