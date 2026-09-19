# MEMORY

长期跨会话约定与项目事实。

## 用户偏好 / 工作流

- **文档修改流程**：用户只查看中文界面（`docs/zh/`）。
  - 用户指出一处需要修改时，**先改中文版** (`docs/zh/...`)，然后**同步修改英文版** (`docs/en/...`) 以保证中英文一致。
  - 改完两边都要能各自编译通过（`make html`，先 `DOCS_LANG=en` 再 `DOCS_LANG=zh`）。
  - 同步时优先"中文 → 英文"翻译（用户视角），而不是直接复制同一段文字。

## 项目：Roller（agent 评估框架）

- **仓库**：`git@github.com:Liuziyu77/Roller.git`（已从 https 切换为 ssh 才能 fetch）
- **远端没有 HTTPS 凭据**；必须用 ssh。
- 当前工作分支：`docs/readthedocs`（HEAD `a33a682`）。
- 主要文档目录：`docs/`（含 `en/`、`zh/`、`_static/`、`conf.py`）。
- 文档构建：`cd docs && make html`，输出 `docs/_build/html/`（中文子目录 `zh/`）。
- 预览服务器：8000 端口常驻 `python -m http.server --directory docs/_build/html`（已运行 12+ 天），用户通过本地 SSH 转发访问。

## 已知文档问题（待修复）

- `make html` 当前 9 个 `myst.xref_missing` 警告，是 refactor `2f72384` 删节后没同步引用造成的：
  - `docs/en/start/examples.md:66` 引用 `harnesses/openclaw-configuration#model-config-file`（不存在）
  - `docs/en/start/getting-started.md` 多处引用 `openclaw-configuration#thinking-system-prompt-and-wire-trace`、`codex-configuration#reasoning-configuration`、`claude-code-configuration#thinking-and-effort`
  - `docs/en/traces/trajectory-outputs.md:569` 同样引用上述不存在的小节 ID
  - `docs/en/harnesses/hermes-configuration.md:3` 与 `docs/zh/harnesses/hermes-configuration.md:54` 引用未找到的 `examples/coverage/README.en`

## Git 工作流注意事项

- 全局 `~/.gitconfig` 当前 `user.name = "Your Name"`、`user.email = "you@example.com"`，**提交会带上占位身份**。要 push 到远端前需要 `git commit --amend --author=...` 修正。