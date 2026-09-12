# Roller coverage examples

[中文完整说明](README.md) · [Case matrix](COVERAGE.md) · [Merged tasks](all_tasks.json) · [Expectations](manifest.json) · [Fixture provenance](sources.json)

This suite contains **1624 runnable tasks in 406 groups of four**, plus **20 isolated negative cases**. It covers OpenClaw, Codex, Claude Code, Hermes, OpenCode, DeepSeek, Pi, and all 49 Agent/Simulator pairings. Tasks perform short arithmetic, file reads, or small writes; conversations have at most two Agent turns.

Fixtures are included. Running and regenerating does not require the original `Roller_Data` directory. Run commands from the **Roller repository root** using **Python 3.10+**.

## Offline checks

No Docker, API credentials, or additional Python packages are needed:

```bash
python examples/coverage/suite.py list
python examples/coverage/suite.py validate
python examples/coverage/validate_merged.py
python examples/coverage/suite.py run --case '*' --dry-run
```

These checks validate parsing, routing, bundles, capture policies, expected errors and 40 fixture checksums. They do not test live model behavior.

## Run four tasks

Install Roller and prepare Harness images following [Getting Started](../../docs/en/start/getting-started.md). Copy and edit the environment template:

```bash
cp examples/coverage/config/.env.example examples/coverage/config/.env
# Edit config/.env with the model, endpoint and key for each selected Harness.
set -a
source examples/coverage/config/.env
set +a

python examples/coverage/suite.py run --case formats/jsonl/codex \
  -- --codex-api-base "$ROLLER_CODEX_BASE_URL" \
     --codex-api-key-env ROLLER_CODEX_API_KEY

python examples/coverage/suite.py check --case formats/jsonl/codex
```

The local `.env` is ignored by Git and must be explicitly exported. Defaults: concurrency 2, timeout 180 seconds per rollout, zero retries. Outputs go to `runs/coverage/<case__harness>/`. Use the same `--output` when running and checking.

| Harness | Required protocol | Model variable | Collector arguments after `--` |
| --- | --- | --- | --- |
| OpenClaw | Responses or Chat Completions | `ROLLER_TEST_OPENCLAW_MODEL` (`provider/model-id`) | `--model-config examples/coverage/config/openclaw.models.json` |
| Codex | Responses | `ROLLER_TEST_CODEX_MODEL` | `--codex-api-base` and `--codex-api-key-env` |
| Claude Code | Anthropic Messages | `ROLLER_TEST_CLAUDE_CODE_MODEL` | `--claude-api-base` and `--claude-api-key-env` |
| Hermes | Chat Completions | `ROLLER_TEST_HERMES_MODEL` | `--hermes-api-base` and `--hermes-api-key-env` |
| OpenCode | Streaming Chat Completions | `ROLLER_TEST_OPENCODE_MODEL` | `--opencode-api-base`, `--opencode-api-key-env` |
| DeepSeek Harness | DeepSeek Chat Completions | `ROLLER_TEST_DEEPSEEK_MODEL` | `--deepseek-api-base`, `--deepseek-api-key-env` |
| Pi Agent | Streaming Chat Completions | `ROLLER_TEST_PI_MODEL` | `--pi-api-base`, `--pi-api-key-env` |

OpenClaw's model ID and protocol are set by `ROLLER_TEST_OPENCLAW_MODEL_ID` and `ROLLER_TEST_OPENCLAW_API` in `.env`. One endpoint is not guaranteed to implement all protocols. The environment template also lists optional image overrides.

`--exclude-case` excludes matching groups; for example, `--case '*/hermes' --exclude-case 'vision/*'` selects 200 Hermes tasks. `--case` accepts quoted globs and can be repeated: `--case 'data/*/codex'` selects 40 tasks; `--case 'simulators/pair/*'` selects 196 tasks and requires all seven Harnesses. CLI options such as `--codex-model` override environment values. The runner binds both Agent and Simulator models while preserving inheritance cases.

## One JSON for a mixed run

`all_tasks.json` is the complete portable template with placeholder models. Create a local copy with your models; this example selects 24 arithmetic tasks:

```bash
python examples/coverage/merge.py \
  --case 'formats/jsonl/*' \
  --openclaw-model "$ROLLER_TEST_OPENCLAW_MODEL" \
  --codex-model "$ROLLER_TEST_CODEX_MODEL" \
  --claude-code-model "$ROLLER_TEST_CLAUDE_CODE_MODEL" \
  --hermes-model "$ROLLER_TEST_HERMES_MODEL" \
  --opencode-model "$ROLLER_TEST_OPENCODE_MODEL" \
  --deepseek-model "$ROLLER_TEST_DEEPSEEK_MODEL" \
  --pi-model "$ROLLER_TEST_PI_MODEL" \
  --absolute-roots --output runs/coverage/merged/tasks.json

python examples/coverage/validate_merged.py --tasks runs/coverage/merged/tasks.json

python -m roller collect --tasks runs/coverage/merged/tasks.json \
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
  --pi-api-base "$ROLLER_PI_BASE_URL" \
  --pi-api-key-env ROLLER_PI_API_KEY \
  --parallel 2 --timeout 180 --retries 0 --wire-trace full \
  --output runs/coverage/merged --run-name collection

python examples/coverage/report.py runs/coverage/merged/collection \
  --tasks runs/coverage/merged/tasks.json \
  --output runs/coverage/merged/report.json --fail-on-check

python examples/coverage/audit_resume.py runs/coverage/merged/collection \
  --tasks runs/coverage/merged/tasks.json \
  --output runs/coverage/merged/resume-audit.json
```

Use `--case '*'` for all 1624 tasks, including optional vision cases. Add `--dry-run` to the collector command to check its plan first. `merge.py` uses explicit model arguments rather than environment defaults to keep regeneration deterministic. Export `ROLLER_EXAMPLE_TOKEN=ENV_OK` for `runtime/env` tasks; the supplied `.env` already does this.

Merging normalizes input formats, custom CLI keys and IDs. Use `suite.py` for original JSON/JSONL and ID coverage. Absolute-root cases are stored with relative paths and materialized by the runner; `--absolute-roots` does this for a local merged file. The merged JSON references bundled fixtures, so copying it alone is insufficient.

Reports distinguish collection success from strict correctness; `--fail-on-check` returns nonzero for incomplete or incorrect results. Resume audits use recorded trace settings and each participant's actual Harness; they check sample reuse, not the run lock. To resume collection, reuse the task file and command with `--resume`. Context captured with `none` is intentionally rerun.

## Scope and maintenance

- The matrix covers formats, dialogues, routing, ten text formats, bundles, context, runtime setup/environment, outputs, four binary formats, optional vision, grading and Simulators. See [COVERAGE.md](COVERAGE.md) for individual groups.
- Binary file access does not imply visual, PDF text or speech understanding. Vision requires image tools, a capable model and compatible API handling. OpenClaw also needs `"input": ["text", "image"]`; use an ignored `config/openclaw.local.json` copy for local changes. Non-text tool output can encounter current SFT export limitations.
- Negative cases are checked individually, never submitted as a normal task list. String-only message content and expected path/configuration errors are tested offline.
- Fixture provenance includes dataset-relative paths and SHA-256 hashes. Source Python files are read as text, not executed. Additional tiny media and context files are generated locally.

Regenerate the public suite from bundled fixtures without model calls:

```bash
python examples/coverage/generate.py
python examples/coverage/merge.py
python examples/coverage/suite.py validate
python examples/coverage/validate_merged.py
```

To import another complete dataset, use `generate.py --data-root /path/to/Roller_Data`, then merge and validate. Add scenarios and four expectations in the generator, rather than editing generated JSON alone. Keep credentials, local configs and reports out of the public suite; `runs/` is ignored by Git.

Hermes uses the pinned `terminal` and `file` toolsets. Vision and external thinking are outside the initial adapter scope; Hermes vision cases document that boundary and are excluded from the required passing set. See the [Hermes guide](../../docs/en/harnesses/hermes-configuration.md).

OpenCode uses native CLI sessions and text/file tools. Its vision cases are optional capability-boundary checks. See the [OpenCode guide](../../docs/en/harnesses/opencode-configuration.md).

DeepSeek uses the official `sdk-minimal` profile with persistent Bash. Vision cases are outside initial acceptance. See the [DeepSeek guide](../../docs/en/harnesses/deepseek-configuration.md).

Pi uses a native AgentSession and seven text/file tools. Conversation history and files persist across turns; vision cases record the capability boundary. See the [Pi guide](../../docs/en/harnesses/pi-configuration.md).
