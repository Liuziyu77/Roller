# Harness tool availability

The native Harness version, container dependencies, credentials and explicit user configuration determine available tools. ACTF records schemas from actual requests; configured tool lists are not evidence of runtime availability.

| Harness | Selection and remaining boundaries |
| --- | --- |
| OpenClaw | Native registry/model configuration. Roller adds no tool allowlist. Native owner/session policies and service credentials still apply. |
| Codex | Native tools and `--codex-config`. Web search defaults to disabled and can be selected with `--codex-web-search cached/live`. Isolated CODEX_HOME does not inherit host accounts or MCP. |
| Claude Code | Native tools, `--claude-settings` and explicit permission mode. Container isolation prevents implicit host integrations. |
| Hermes | Native discovery by default (`enabled_toolsets=None`). Repeat `--hermes-toolset NAME` for an explicit selection; `--no-hermes-vision` explicitly excludes vision. Optional tools require installed dependencies and credentials. |
| OpenCode | Native registry, without a fixed allowlist. Noninteractive execution permits tools by default; repeat `--opencode-deny-tool NAME` for explicit exclusions. LSP/formatters and implicit plugin/project discovery remain disabled to avoid background services and implicit configuration. |
| DeepSeek | Full native `sdk` profile by default. `--deepseek-profile sdk-minimal` explicitly selects shell-only. Custom profiles must be installed in the image/task setup and retain the SDK JSON-RPC server. |
| Pi | All seven built-in tools of the pinned version: read/bash/edit/write/grep/find/ls. Automatic extensions, host skills and MCP discovery remain disabled for task isolation. |

Hermes retains a local terminal backend inside the container, disables lazy dependency installation, host identity, persistent memory and background review. These isolation settings do not limit the registry to terminal/file. Supply optional dependencies in the image and credentials through `--pass-env` or task `runtime.env`.

DeepSeek's former bash-only behavior came from Roller selecting `sdk-minimal`, not from the SDK lacking filesystem tools. Full sdk also has native search, skills, subagents and Web tools, subject to native prerequisites. Its bash runs separate processes; sdk-minimal retains a shell PTY. Both retain files and sessions. The request journal covers both subprocess paths and keeps explicitly passed task environment variables.

`--deepseek-permission-mode` defaults to `danger-full-access` **inside the disposable container**. This avoids requiring a second sandbox backend and interactive approvals. Users can select read-only/workspace-write with a supported native sandbox; unanswered approvals fail closed. Host input mounts remain read-only. Roller retains request logging, step limits, and disables provider session/plugin uploads and automatic model retries without removing tool plugins.

Run locks record Hermes toolsets, OpenCode deny lists and DeepSeek profile/permission mode. Changing them requires a new run. A null configured tool list means runtime discovery; inspect raw requests and ACTF for actual schemas.
