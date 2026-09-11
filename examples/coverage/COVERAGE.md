# 覆盖清单

由 `generate.py` 生成。每个链接是一份恰好含 4 条任务的独立清单；运行说明见 [README](README.md)。

## 输入场景 × 四种 Harness

| 场景 | OpenClaw | Codex | Claude Code | Hermes | 验证点 |
| --- | --- | --- | --- | --- | --- |
| formats/jsonl | [4 条](cases/formats/jsonl/openclaw.jsonl) | [4 条](cases/formats/jsonl/codex.jsonl) | [4 条](cases/formats/jsonl/claude-code.jsonl) | [4 条](cases/formats/jsonl/hermes.jsonl) | Task 清单格式：jsonl |
| formats/json_array | [4 条](cases/formats/json_array/openclaw.json) | [4 条](cases/formats/json_array/codex.json) | [4 条](cases/formats/json_array/claude-code.json) | [4 条](cases/formats/json_array/hermes.json) | Task 清单格式：json_array |
| formats/json_object | [4 条](cases/formats/json_object/openclaw.json) | [4 条](cases/formats/json_object/codex.json) | [4 条](cases/formats/json_object/claude-code.json) | [4 条](cases/formats/json_object/hermes.json) | Task 清单格式：json_object |
| formats/jsonl_bom | [4 条](cases/formats/jsonl_bom/openclaw.jsonl) | [4 条](cases/formats/jsonl_bom/codex.jsonl) | [4 条](cases/formats/jsonl_bom/claude-code.jsonl) | [4 条](cases/formats/jsonl_bom/hermes.jsonl) | Task 清单格式：jsonl_bom |
| dialogue/question | [4 条](cases/dialogue/question/openclaw.jsonl) | [4 条](cases/dialogue/question/codex.jsonl) | [4 条](cases/dialogue/question/claude-code.jsonl) | [4 条](cases/dialogue/question/hermes.jsonl) | 对话输入：question；最多两轮 |
| dialogue/custom_keys | [4 条](cases/dialogue/custom_keys/openclaw.jsonl) | [4 条](cases/dialogue/custom_keys/codex.jsonl) | [4 条](cases/dialogue/custom_keys/claude-code.jsonl) | [4 条](cases/dialogue/custom_keys/hermes.jsonl) | 对话输入：custom_keys；最多两轮 |
| dialogue/messages_strings | [4 条](cases/dialogue/messages_strings/openclaw.jsonl) | [4 条](cases/dialogue/messages_strings/codex.jsonl) | [4 条](cases/dialogue/messages_strings/claude-code.jsonl) | [4 条](cases/dialogue/messages_strings/hermes.jsonl) | 对话输入：messages_strings；最多两轮 |
| dialogue/messages_objects | [4 条](cases/dialogue/messages_objects/openclaw.jsonl) | [4 条](cases/dialogue/messages_objects/codex.jsonl) | [4 条](cases/dialogue/messages_objects/claude-code.jsonl) | [4 条](cases/dialogue/messages_objects/hermes.jsonl) | 对话输入：messages_objects；最多两轮 |
| dialogue/multiline | [4 条](cases/dialogue/multiline/openclaw.jsonl) | [4 条](cases/dialogue/multiline/codex.jsonl) | [4 条](cases/dialogue/multiline/claude-code.jsonl) | [4 条](cases/dialogue/multiline/hermes.jsonl) | 对话输入：multiline；最多两轮 |
| routing/aliases | [4 条](cases/routing/aliases/openclaw.jsonl) | [4 条](cases/routing/aliases/codex.jsonl) | [4 条](cases/routing/aliases/claude-code.jsonl) | [4 条](cases/routing/aliases/hermes.jsonl) | Harness / model / ID 声明：aliases |
| routing/model_cli | [4 条](cases/routing/model_cli/openclaw.jsonl) | [4 条](cases/routing/model_cli/codex.jsonl) | [4 条](cases/routing/model_cli/claude-code.jsonl) | [4 条](cases/routing/model_cli/hermes.jsonl) | Harness / model / ID 声明：model_cli |
| routing/id_variants | [4 条](cases/routing/id_variants/openclaw.jsonl) | [4 条](cases/routing/id_variants/codex.jsonl) | [4 条](cases/routing/id_variants/claude-code.jsonl) | [4 条](cases/routing/id_variants/hermes.jsonl) | Harness / model / ID 声明：id_variants |
| data/csv | [4 条](cases/data/csv/openclaw.jsonl) | [4 条](cases/data/csv/codex.jsonl) | [4 条](cases/data/csv/claude-code.jsonl) | [4 条](cases/data/csv/hermes.jsonl) | 来自 Roller_Data 的小型 .csv 文件 |
| data/html | [4 条](cases/data/html/openclaw.jsonl) | [4 条](cases/data/html/codex.jsonl) | [4 条](cases/data/html/claude-code.jsonl) | [4 条](cases/data/html/hermes.jsonl) | 来自 Roller_Data 的小型 .html 文件 |
| data/json | [4 条](cases/data/json/openclaw.jsonl) | [4 条](cases/data/json/codex.jsonl) | [4 条](cases/data/json/claude-code.jsonl) | [4 条](cases/data/json/hermes.jsonl) | 来自 Roller_Data 的小型 .json 文件 |
| data/jsonl | [4 条](cases/data/jsonl/openclaw.jsonl) | [4 条](cases/data/jsonl/codex.jsonl) | [4 条](cases/data/jsonl/claude-code.jsonl) | [4 条](cases/data/jsonl/hermes.jsonl) | 来自 Roller_Data 的小型 .jsonl 文件 |
| data/log | [4 条](cases/data/log/openclaw.jsonl) | [4 条](cases/data/log/codex.jsonl) | [4 条](cases/data/log/claude-code.jsonl) | [4 条](cases/data/log/hermes.jsonl) | 来自 Roller_Data 的小型 .log 文件 |
| data/md | [4 条](cases/data/md/openclaw.jsonl) | [4 条](cases/data/md/codex.jsonl) | [4 条](cases/data/md/claude-code.jsonl) | [4 条](cases/data/md/hermes.jsonl) | 来自 Roller_Data 的小型 .md 文件 |
| data/py | [4 条](cases/data/py/openclaw.jsonl) | [4 条](cases/data/py/codex.jsonl) | [4 条](cases/data/py/claude-code.jsonl) | [4 条](cases/data/py/hermes.jsonl) | 来自 Roller_Data 的小型 .py 文件 |
| data/svg | [4 条](cases/data/svg/openclaw.jsonl) | [4 条](cases/data/svg/codex.jsonl) | [4 条](cases/data/svg/claude-code.jsonl) | [4 条](cases/data/svg/hermes.jsonl) | 来自 Roller_Data 的小型 .svg 文件 |
| data/txt | [4 条](cases/data/txt/openclaw.jsonl) | [4 条](cases/data/txt/codex.jsonl) | [4 条](cases/data/txt/claude-code.jsonl) | [4 条](cases/data/txt/hermes.jsonl) | 来自 Roller_Data 的小型 .txt 文件 |
| data/yaml | [4 条](cases/data/yaml/openclaw.jsonl) | [4 条](cases/data/yaml/codex.jsonl) | [4 条](cases/data/yaml/claude-code.jsonl) | [4 条](cases/data/yaml/hermes.jsonl) | 来自 Roller_Data 的小型 .yaml 文件 |
| inputs/object | [4 条](cases/inputs/object/openclaw.jsonl) | [4 条](cases/inputs/object/codex.jsonl) | [4 条](cases/inputs/object/claude-code.jsonl) | [4 条](cases/inputs/object/hermes.jsonl) | 文件包输入方式：object |
| inputs/sha256 | [4 条](cases/inputs/sha256/openclaw.jsonl) | [4 条](cases/inputs/sha256/codex.jsonl) | [4 条](cases/inputs/sha256/claude-code.jsonl) | [4 条](cases/inputs/sha256/hermes.jsonl) | 文件包输入方式：sha256 |
| inputs/multiple | [4 条](cases/inputs/multiple/openclaw.jsonl) | [4 条](cases/inputs/multiple/codex.jsonl) | [4 条](cases/inputs/multiple/claude-code.jsonl) | [4 条](cases/inputs/multiple/hermes.jsonl) | 文件包输入方式：multiple |
| inputs/directory | [4 条](cases/inputs/directory/openclaw.jsonl) | [4 条](cases/inputs/directory/codex.jsonl) | [4 条](cases/inputs/directory/claude-code.jsonl) | [4 条](cases/inputs/directory/hermes.jsonl) | 文件包输入方式：directory |
| inputs/optional_missing | [4 条](cases/inputs/optional_missing/openclaw.jsonl) | [4 条](cases/inputs/optional_missing/codex.jsonl) | [4 条](cases/inputs/optional_missing/claude-code.jsonl) | [4 条](cases/inputs/optional_missing/hermes.jsonl) | 文件包输入方式：optional_missing |
| inputs/unicode_paths | [4 条](cases/inputs/unicode_paths/openclaw.jsonl) | [4 条](cases/inputs/unicode_paths/codex.jsonl) | [4 条](cases/inputs/unicode_paths/claude-code.jsonl) | [4 条](cases/inputs/unicode_paths/hermes.jsonl) | 文件包输入方式：unicode_paths |
| inputs/empty | [4 条](cases/inputs/empty/openclaw.jsonl) | [4 条](cases/inputs/empty/codex.jsonl) | [4 条](cases/inputs/empty/claude-code.jsonl) | [4 条](cases/inputs/empty/hermes.jsonl) | 文件包输入方式：empty |
| inputs/default_root | [4 条](fixtures/openclaw-default-root.jsonl) | [4 条](fixtures/codex-default-root.jsonl) | [4 条](fixtures/claude-code-default-root.jsonl) | [4 条](fixtures/hermes-default-root.jsonl) | 文件包输入方式：default_root |
| inputs/absolute_root | [4 条](cases/inputs/absolute_root/openclaw.jsonl) | [4 条](cases/inputs/absolute_root/codex.jsonl) | [4 条](cases/inputs/absolute_root/claude-code.jsonl) | [4 条](cases/inputs/absolute_root/hermes.jsonl) | 文件包输入方式：absolute_root |
| inputs/legacy_workspace | [4 条](cases/inputs/legacy_workspace/openclaw.jsonl) | [4 条](cases/inputs/legacy_workspace/codex.jsonl) | [4 条](cases/inputs/legacy_workspace/claude-code.jsonl) | [4 条](cases/inputs/legacy_workspace/hermes.jsonl) | 文件包输入方式：legacy_workspace |
| context/full | [4 条](cases/context/full/openclaw.jsonl) | [4 条](cases/context/full/codex.jsonl) | [4 条](cases/context/full/claude-code.jsonl) | [4 条](cases/context/full/hermes.jsonl) | Context 注入 / capture：full |
| context/hash | [4 条](cases/context/hash/openclaw.jsonl) | [4 条](cases/context/hash/codex.jsonl) | [4 条](cases/context/hash/claude-code.jsonl) | [4 条](cases/context/hash/hermes.jsonl) | Context 注入 / capture：hash |
| context/none | [4 条](cases/context/none/openclaw.jsonl) | [4 条](cases/context/none/codex.jsonl) | [4 条](cases/context/none/claude-code.jsonl) | [4 条](cases/context/none/hermes.jsonl) | Context 注入 / capture：none |
| context/roles | [4 条](cases/context/roles/openclaw.jsonl) | [4 条](cases/context/roles/codex.jsonl) | [4 条](cases/context/roles/claude-code.jsonl) | [4 条](cases/context/roles/hermes.jsonl) | Context 注入 / capture：roles |
| context/skill | [4 条](cases/context/skill/openclaw.jsonl) | [4 条](cases/context/skill/codex.jsonl) | [4 条](cases/context/skill/claude-code.jsonl) | [4 条](cases/context/skill/hermes.jsonl) | Context 注入 / capture：skill |
| context/optional | [4 条](cases/context/optional/openclaw.jsonl) | [4 条](cases/context/optional/codex.jsonl) | [4 条](cases/context/optional/claude-code.jsonl) | [4 条](cases/context/optional/hermes.jsonl) | Context 注入 / capture：optional |
| runtime/setup_string | [4 条](cases/runtime/setup_string/openclaw.jsonl) | [4 条](cases/runtime/setup_string/codex.jsonl) | [4 条](cases/runtime/setup_string/claude-code.jsonl) | [4 条](cases/runtime/setup_string/hermes.jsonl) | 运行设置：setup_string |
| runtime/setup_list | [4 条](cases/runtime/setup_list/openclaw.jsonl) | [4 条](cases/runtime/setup_list/codex.jsonl) | [4 条](cases/runtime/setup_list/claude-code.jsonl) | [4 条](cases/runtime/setup_list/hermes.jsonl) | 运行设置：setup_list |
| runtime/env | [4 条](cases/runtime/env/openclaw.jsonl) | [4 条](cases/runtime/env/codex.jsonl) | [4 条](cases/runtime/env/claude-code.jsonl) | [4 条](cases/runtime/env/hermes.jsonl) | 运行设置：env |
| runtime/thinking | [4 条](cases/runtime/thinking/openclaw.jsonl) | [4 条](cases/runtime/thinking/codex.jsonl) | [4 条](cases/runtime/thinking/claude-code.jsonl) | [4 条](cases/runtime/thinking/hermes.jsonl) | 运行设置：thinking |
| outputs/nested | [4 条](cases/outputs/nested/openclaw.jsonl) | [4 条](cases/outputs/nested/codex.jsonl) | [4 条](cases/outputs/nested/claude-code.jsonl) | [4 条](cases/outputs/nested/hermes.jsonl) | 输出回收：nested |
| outputs/multiple | [4 条](cases/outputs/multiple/openclaw.jsonl) | [4 条](cases/outputs/multiple/codex.jsonl) | [4 条](cases/outputs/multiple/claude-code.jsonl) | [4 条](cases/outputs/multiple/hermes.jsonl) | 输出回收：multiple |
| outputs/no_files | [4 条](cases/outputs/no_files/openclaw.jsonl) | [4 条](cases/outputs/no_files/codex.jsonl) | [4 条](cases/outputs/no_files/claude-code.jsonl) | [4 条](cases/outputs/no_files/hermes.jsonl) | 输出回收：no_files |
| binary/png | [4 条](cases/binary/png/openclaw.jsonl) | [4 条](cases/binary/png/codex.jsonl) | [4 条](cases/binary/png/claude-code.jsonl) | [4 条](cases/binary/png/hermes.jsonl) | 二进制 .png 传输和读取；不验证语义理解 |
| binary/pdf | [4 条](cases/binary/pdf/openclaw.jsonl) | [4 条](cases/binary/pdf/codex.jsonl) | [4 条](cases/binary/pdf/claude-code.jsonl) | [4 条](cases/binary/pdf/hermes.jsonl) | 二进制 .pdf 传输和读取；不验证语义理解 |
| binary/wav | [4 条](cases/binary/wav/openclaw.jsonl) | [4 条](cases/binary/wav/codex.jsonl) | [4 条](cases/binary/wav/claude-code.jsonl) | [4 条](cases/binary/wav/hermes.jsonl) | 二进制 .wav 传输和读取；不验证语义理解 |
| binary/zip | [4 条](cases/binary/zip/openclaw.jsonl) | [4 条](cases/binary/zip/codex.jsonl) | [4 条](cases/binary/zip/claude-code.jsonl) | [4 条](cases/binary/zip/hermes.jsonl) | 二进制 .zip 传输和读取；不验证语义理解 |
| vision/png | [4 条](cases/vision/png/openclaw.jsonl) | [4 条](cases/vision/png/codex.jsonl) | [4 条](cases/vision/png/claude-code.jsonl) | [4 条](cases/vision/png/hermes.jsonl) | 可选：真正调用图像查看工具识别纯色，需模型和工具支持视觉 |
| bench/file_grader | [4 条](cases/bench/file_grader/openclaw.jsonl) | [4 条](cases/bench/file_grader/codex.jsonl) | [4 条](cases/bench/file_grader/claude-code.jsonl) | [4 条](cases/bench/file_grader/hermes.jsonl) | 容器销毁前用标准库评分；不调用 LLM judge |

## Task Agent × User Simulator

每个配对的 4 条任务依次验证：纯文本、CSV 两轮与文件产物、Memory 与自定义输出目录、max_turns 截断。

| Task Agent | User Simulator | 任务 |
| --- | --- | --- |
| openclaw | openclaw | [4 条](cases/simulators/pair/openclaw--openclaw.jsonl) |
| openclaw | codex | [4 条](cases/simulators/pair/openclaw--codex.jsonl) |
| openclaw | claude-code | [4 条](cases/simulators/pair/openclaw--claude-code.jsonl) |
| openclaw | hermes | [4 条](cases/simulators/pair/openclaw--hermes.jsonl) |
| codex | openclaw | [4 条](cases/simulators/pair/codex--openclaw.jsonl) |
| codex | codex | [4 条](cases/simulators/pair/codex--codex.jsonl) |
| codex | claude-code | [4 条](cases/simulators/pair/codex--claude-code.jsonl) |
| codex | hermes | [4 条](cases/simulators/pair/codex--hermes.jsonl) |
| claude-code | openclaw | [4 条](cases/simulators/pair/claude-code--openclaw.jsonl) |
| claude-code | codex | [4 条](cases/simulators/pair/claude-code--codex.jsonl) |
| claude-code | claude-code | [4 条](cases/simulators/pair/claude-code--claude-code.jsonl) |
| claude-code | hermes | [4 条](cases/simulators/pair/claude-code--hermes.jsonl) |
| hermes | openclaw | [4 条](cases/simulators/pair/hermes--openclaw.jsonl) |
| hermes | codex | [4 条](cases/simulators/pair/hermes--codex.jsonl) |
| hermes | claude-code | [4 条](cases/simulators/pair/hermes--claude-code.jsonl) |
| hermes | hermes | [4 条](cases/simulators/pair/hermes--hermes.jsonl) |

## 预期拒绝的输入

这些文件是带预期错误的测试描述，不能整份传给 `roller collect`；`suite.py validate` 会逐条隔离验证。

- [negative/dialogue](negative/dialogue.json)：4 条。
- [negative/paths](negative/paths.json)：4 条。
- [negative/integrity](negative/integrity.json)：4 条。
- [negative/collisions](negative/collisions.json)：4 条。
- [negative/configuration](negative/configuration.json)：4 条。
