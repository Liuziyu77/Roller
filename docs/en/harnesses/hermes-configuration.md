# Hermes Harness

See the [coverage guide](../../../examples/coverage/README.en.md) for the test matrix, model configuration, result checks, and Resume auditing.

Roller runs [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) through its Python `AIAgent` interface, pinned to commit `d15ed4445207dda418b984e8bda0f68f48b8c6f3` (0.21.1). Both the task agent and the User Simulator can select `"harness": "hermes"`.

## Build and run

From the Roller repository root:

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

The model ID is sent to the provider as supplied. It does not require an OpenClaw provider prefix. The service must support OpenAI-compatible **Chat Completions with tool calls**. No separate Hermes gateway is used. This Hermes revision requires a model context window of at least **64,000 tokens**. If the endpoint reports an incorrect window, set `--hermes-context-length` to the model’s actual supported size.

The default Docker base is Python 3.11. `--build-arg BASE_IMAGE=...` accepts an alternative Linux image with Python 3.11–3.13, `apt-get`, and `pip`. Hermes dependencies come from its frozen `uv.lock`. The runner checks the revision and required hooks before making model calls. Changing only the image tag does not bypass that check; adapting another Hermes revision requires reviewing and testing the bridge.

## Configuration

| Option | Default / behavior |
| --- | --- |
| `--hermes-image` | Overrides shared `--docker-image`; otherwise `ROLLER_HERMES_IMAGE`, then `roller-hermes:d15ed4445207` |
| `--hermes-api-base` | Shared `--api-base`, then `OPENAI_BASE_URL`, then `https://api.openai.com/v1` |
| `--hermes-api-key-env` | Shared `--api-key-env`, then `OPENAI_API_KEY`; provide a variable name, not a key |
| `--hermes-context-length` | Optional context window override, at least 64000 tokens |
| `--hermes-max-iterations` | `12` model iterations per user turn; total rollout timeout also applies |
| `--thinking` / `runtime.thinking` | Optional native reasoning hint: `off/minimal/low/medium/high/xhigh`; actual support and normalization depend on Hermes and the provider/model |
| `--wire-trace` | Native request snapshots always preserve schemas and assembled prompts. `full` also records HTTP traffic; requires an explicit API base and `--network host` |
| `--no-keep-thinking` | Removes reasoning from ACTF/SFT export; raw/native history remains available |

Participant-specific API/image settings apply to Hermes on either side of a mixed conversation. Run locks include the Hermes revision, image digest, endpoint, key variable name, iteration cap, toolsets, and export settings. Keys are read at execution time and are not written into the public examples or run configuration.

## Execution and traces

Each participant gets its own disposable container. Hermes uses its `local` terminal backend **inside that container**, with the `terminal` and `file` toolsets. Immutable inputs, context files, setup commands, task environment variables, output directories, and container-based grading use Roller's normal workspace contract.

Context files of all roles are copied to their declared workspace paths and listed in the task system prompt. Tasks may explicitly read them. The bridge disables implicit profile/context discovery, persistent memory, SOUL identity loading, checkpoints, background review, and automatic dependency installation during rollouts. This supports file-based context and skill fixtures; it does not enable Hermes' skill discovery/learning service.

Each user turn starts a Python agent and restores Hermes' native message history in the same container. Workspace files and conversation history persist across turns. Python process state, including background process handles, does not. Models should finish terminal work within each turn. Sessions are isolated across Agent/Simulator, workers, and retry attempts.

The append-only `rollout/trace.jsonl` journal records actual request messages/tool schemas, model responses/usage, and tool results observed at native persistence hooks before compression. Full request snapshots remain in raw data; ACTF/SFT represent the logical executed conversation. Context summaries in subsequent API requests do not replace earlier executed tools in the exported trajectory. This is a Roller bridge journal, not Hermes' optional ShareGPT export or an OpenClaw trace.

The usual outputs are `trace_ACTF.json`, `trace_SFT.json`, results, provenance, and usage. The bridge rejects incomplete turns, empty final replies, malformed tool arguments, missing schemas, and corrupt JSONL. Timeouts stop execution and retain available partial artifacts. Hermes emits no OpenClaw cache trace, so Resume does not require one; `--wire-trace full` still requires the wire trace. Context with `capture: none` still prevents reuse.

## Verification and scope

```bash
python examples/coverage/suite.py validate --case '*/hermes'
python examples/coverage/suite.py run --case '*/hermes' --exclude-case 'vision/*' --dry-run
python examples/coverage/suite.py run --case data/txt/hermes \
  --hermes-model your-model-id
python examples/coverage/suite.py check --case data/txt/hermes
```

The matrix contains 204 Hermes input tasks and 28 additional mixed Simulator tasks (seven new pairings, four cases each). Tasks include text, files, context, setup/environment, binary inspection, output collection, grading, and short conversations. The complete project matrix is 1368 positive tasks and 20 isolated negative cases.

The initial adapter supports Chat Completions and text-oriented terminal/file execution. Vision tools, direct multimodal messages, external thinking, provider-specific authentication flows, Hermes gateways, remote terminal backends, persistent memory, and skill learning are outside this scope. The optional Hermes vision group records that capability boundary and is excluded from the required passing set. Binary file access is tested separately from media understanding.

Build networks can set `--build-arg PIP_INDEX_URL=...` and `--build-arg PYPI_DOWNLOAD_BASE=...`. The latter replaces the download origin in `uv.lock` while preserving package versions and SHA-256 hashes; use a mirror with the same `/packages/` layout.

## Reproducible adapter tests

Run the normal suite without Docker or model credentials, then opt into the real container test with a local deterministic API:

```bash
PYTHONPATH=src python -m unittest discover -s tests
ROLLER_HERMES_DOCKER_TEST=1 PYTHONPATH=src \
  python -m unittest discover -s tests -p test_hermes_docker.py -v
```

The Docker test uses a localhost HTTP server and a synthetic key. It verifies actual terminal execution, read-only inputs, context/setup/environment, two-turn history, full and disabled wire capture, reasoning export, Resume/run-lock behavior, and timeout cleanup. No external model credentials or model charges are involved. It requires Docker host networking, as used by the adapter’s full wire mode.

Use `--case '*/hermes' --exclude-case 'vision/*'` to select the 200 supported input tasks. Add `--case 'simulators/pair/*'` to include all 196 Agent/Simulator tasks (396 in total, requiring all seven Harnesses). Both `suite.py` and `merge.py` accept these filters.
