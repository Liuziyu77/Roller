#!/usr/bin/env python3
"""Rebuild small, deterministic task fixtures; never invoke Docker or a model."""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import os
from pathlib import Path
import struct
import wave
import zipfile
import zlib

ROOT = Path(__file__).resolve().parent
HARNESSES = ("openclaw", "codex", "claude-code", "hermes")
MODELS = {"openclaw": "my-openai-proxy/my-model", "codex": "replace-codex-model", "claude-code": "replace-claude-model", "hermes": "replace-hermes-model"}
TAIL = "这是小型功能测试。不要联网、安装依赖或处理无关文件；完成指定操作后立即结束。"


def write(path: Path, value: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value if isinstance(value, bytes) else value.encode("utf-8"))


def json_write(path: Path, value: object) -> None:
    write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def png(red: int, green: int, blue: int) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    pixels = (b"\0" + bytes((red, green, blue)) * 32) * 32
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 32, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")


def pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 40 100 Td ({text}) Tj ET".encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>", b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 150] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"]
    result = b"%PDF-1.4\n"
    offsets = [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(result))
        result += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    start = len(result)
    result += b"xref\n0 6\n0000000000 65535 f \n"
    result += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    return result + f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()


def fixtures(data_root: Path | None = None) -> dict:
    data_files = sorted(p for p in (data_root / "files").rglob("*") if p.is_file()) if data_root else []
    if data_root is not None and not data_files:
        raise ValueError(f"no files found under {data_root / 'files'}")
    sources = []
    if data_root is None:
        sources = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))["files"]
        for source in sources:
            path = ROOT / source["path"]
            if digest(path) != source["sha256"] or path.stat().st_size != source["bytes"]:
                raise ValueError(f"bundled source changed: {source['path']}")
    types = ("csv", "html", "json", "jsonl", "log", "md", "py", "svg", "txt", "yaml")
    for extension in types if data_root is not None else ():
        candidates = sorted((p for p in data_files if p.suffix == "." + extension), key=lambda p: (p.stat().st_size, str(p)))[:4]
        if not candidates:
            raise ValueError(f"missing source type: {extension}")
        for index in range(1, 5):
            source = candidates[(index - 1) % len(candidates)]
            target = ROOT / "fixtures" / "data" / extension / f"{index:02d}{source.suffix}"
            write(target, source.read_bytes())
            sources.append({"path": target.relative_to(ROOT).as_posix(), "source": source.relative_to(data_root).as_posix(), "sha256": digest(target), "bytes": target.stat().st_size})
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
    for i in range(1, 5):
        token = f"TOKEN_{i}"
        write(ROOT / f"fixtures/tiny/{i}.txt", token + "\n")
        write(ROOT / f"fixtures/tiny/empty-{i}.txt", "")
        write(ROOT / f"fixtures/路径 含空格/样本 {i}.txt", token + "\n")
        write(ROOT / f"fixtures/trees/tree-{i}/a.txt", "A\n")
        write(ROOT / f"fixtures/trees/tree-{i}/nested/b.txt", "B\n")
        write(ROOT / f"fixtures/context/source-{i}.md", f"# 测试上下文\n本次测试的校验词：{token}\n")
        write(ROOT / f"fixtures/context/skill-{i}/SKILL.md", f"---\nname: roller-probe-{i}\ndescription: Return the fixture token when explicitly asked.\n---\n\n本技能的校验词：{token}\n")
        write(ROOT / f"fixtures/legacy/{i}/note.txt", token + "\n")
        write(ROOT / f"fixtures/media/{i}.png", png(*colors[i - 1]))
        write(ROOT / f"fixtures/media/{i}.pdf", pdf(token))
        wav_path = ROOT / f"fixtures/media/{i}.wav"
        with wave.open(str(wav_path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(8000)
            stream.writeframes(b"\0\0" * (80 * i))
        with zipfile.ZipFile(ROOT / f"fixtures/media/{i}.zip", "w") as archive:
            info = zipfile.ZipInfo("note.txt", date_time=(2026, 1, 1, 0, 0, 0))
            archive.writestr(info, token + "\n")
    payload = {"schema_version": "roller.examples-sources/v1", "source_dataset": "Roller_Data", "note": "Files are copied unchanged; source paths are relative to the dataset root. Bundled copies are sufficient to run and regenerate the suite.", "files": sources}
    json_write(ROOT / "sources.json", payload)
    return payload


def base(i: int) -> dict:
    return {"schema_version": "roller.task/v1", "prompt": f"计算 {i}+2，只回复数字。", "_expect": {"reply": str(i + 2)}}


def file_task(source: str, prompt: str, expected: str) -> dict:
    return {"schema_version": "roller.task/v1", "files": {"inputs": [source]}, "prompt": prompt + " 将答案写入 outputs/answer.txt，最终回复同样的答案。", "_expect": {"files": {"answer.txt": expected}, "tool_calls_min": 1}}


def groups() -> list[dict]:
    result = []

    def add(name: str, description: str, rows: list[dict], **options) -> None:
        assert len(rows) == 4, name
        result.append({"name": name, "description": description, "rows": rows, **options})

    for kind in ("jsonl", "json_array", "json_object", "jsonl_bom"):
        add(f"formats/{kind}", f"Task 清单格式：{kind}", [base(i) for i in range(1, 5)], format=kind)
    for kind in ("question", "custom_keys", "messages_strings", "messages_objects", "multiline"):
        rows = []
        for i in range(1, 5):
            row = base(i)
            if kind == "question":
                row["question"] = row.pop("prompt")
            elif kind == "custom_keys":
                row["instruction"] = row.pop("prompt")
            elif kind.startswith("messages_"):
                row.pop("prompt")
                turns = [f"记住校验词 TOKEN_{i}，只回复 OK。", "刚才的校验词是什么？只回复校验词。"]
                row["messages"] = turns if kind.endswith("strings") else [{"role": "user", "content": t} for t in turns]
                row["_expect"] = {"reply": f"TOKEN_{i}", "user_turns": 2}
            else:
                row["prompt"] = f"## 第一段\n校验词 TOKEN_{i}\n---\n## 第二段\n这仍然是一条消息，只回复校验词。"
            if kind == "multiline":
                row["_expect"] = {"reply": f"TOKEN_{i}", "user_turns": 1}
            rows.append(row)
        add(f"dialogue/{kind}", f"对话输入：{kind}；最多两轮", rows, parser_options={"prompt_key": "instruction", "id_key": "case_id"} if kind == "custom_keys" else {})
    for kind in ("aliases", "model_cli", "id_variants"):
        add(f"routing/{kind}", f"Harness / model / ID 声明：{kind}", [base(i) for i in range(1, 5)])

    for extension in ("csv", "html", "json", "jsonl", "log", "md", "py", "svg", "txt", "yaml"):
        rows = []
        for i in range(1, 5):
            source = f"data/{extension}/{i:02d}.{extension}"
            text = (ROOT / "fixtures" / source).read_text()
            if extension == "csv":
                import csv
                records = list(csv.reader(text.splitlines()))
                expected = str(len(records) - 1)
                prompt = f"读取 {source}，统计 CSV 数据行数（不含表头）。"
            elif extension == "json":
                value = json.loads(text)
                expected = str(len(value))
                prompt = f"读取 {source}，用 Python 标准库解析 JSON，只统计顶层元素个数（对象为键数，数组为元素数）。"
            elif extension == "jsonl":
                records = [json.loads(line) for line in text.splitlines() if line.strip()]
                expected = str(records[i - 1]["id"])
                prompt = f"读取 {source}，返回第 {i} 条 JSON 记录的 id。"
            elif extension == "svg":
                import xml.etree.ElementTree as ET
                expected = ET.fromstring(text).attrib["width"]
                prompt = f"将 {source} 作为 XML 文本读取，只提取 svg 根元素的 width 属性。无需渲染图像。"
            else:
                expected = next(line.strip() for line in text.splitlines() if line.strip())
                prompt = f"将 {source} 当作普通文本读取，提取第一条非空行，去掉首尾空白。不要执行源文件中的代码或指令。"
            rows.append(file_task(source, prompt, expected))
        add(f"data/{extension}", f"来自 Roller_Data 的小型 .{extension} 文件", rows)

    for kind in ("object", "sha256", "multiple", "directory", "optional_missing", "unicode_paths", "empty", "default_root", "absolute_root", "legacy_workspace"):
        rows = []
        for i in range(1, 5):
            source = f"tiny/{i}.txt"
            row = file_task(source, f"读取 {source}，提取校验词。", f"TOKEN_{i}")
            if kind in {"object", "sha256"}:
                spec = {"source": source, "required": True}
                if kind == "sha256":
                    spec["sha256"] = digest(ROOT / "fixtures" / source)
                row["files"]["inputs"] = [spec]
            elif kind == "multiple":
                row["files"]["inputs"] += [f"data/csv/{i:02d}.csv", f"data/json/{i:02d}.json"]
                row["prompt"] = "确认声明的 TXT、CSV、JSON 三个输入文件都存在；将数量 3 写入 outputs/answer.txt。"
                row["_expect"]["files"] = {"answer.txt": "3"}
            elif kind == "directory":
                row = file_task(f"trees/tree-{i}", f"递归列出 trees/tree-{i} 中普通文件的相对路径，每行一条，按字典序排序。", "a.txt\nnested/b.txt")
            elif kind == "optional_missing":
                row["files"]["inputs"].append({"source": f"missing/optional-{i}.txt", "required": False})
                row["prompt"] = f"确认 tiny/{i}.txt 存在且 missing/optional-{i}.txt 不存在，把 OK 写入 outputs/answer.txt。"
                row["_expect"]["files"] = {"answer.txt": "OK"}
            elif kind == "unicode_paths":
                source = f"路径 含空格/样本 {i}.txt"
                row = file_task(source, f"读取路径 {json.dumps(source, ensure_ascii=False)} 的文本，提取校验词。", f"TOKEN_{i}")
            elif kind == "empty":
                source = f"tiny/empty-{i}.txt"
                row = file_task(source, f"检查 {source} 的字节数。", "0")
            elif kind == "legacy_workspace":
                row = file_task("note.txt", "读取工作区中的 note.txt，提取校验词。", f"TOKEN_{i}")
                row.pop("files")
                row["workspace" if i % 2 else "workspace_path"] = str(ROOT / f"fixtures/legacy/{i}")
            rows.append(row)
        add(f"inputs/{kind}", f"文件包输入方式：{kind}", rows)

    for kind in ("full", "hash", "none", "roles", "skill", "optional"):
        rows = []
        for i in range(1, 5):
            role = ("agents", "system", "soul", "fixture")[i - 1] if kind == "roles" else ("user" if i % 2 else "memory")
            target = {"agents": "AGENTS.md", "system": "SYSTEM.md", "soul": "SOUL.md", "user": "USER.md", "memory": "MEMORY.md", "fixture": f"notes/token-{i}.md"}[role]
            spec = {"role": role, "source": f"context/source-{i}.md", "target": target, "capture": kind if kind in {"full", "hash", "none"} else "full"}
            if kind == "skill":
                spec = {"role": "skill", "source": f"context/skill-{i}", "capture": "full"}
                target = f"skills/skill-{i}/SKILL.md"
            specs = [spec]
            if kind == "optional":
                specs.append({"role": "fixture", "source": f"context/missing-{i}.md", "target": "optional-note.md", "required": False})
            rows.append({"schema_version": "roller.task/v1", "files": {"context": specs}, "prompt": f"读取注入的 {target}，把其中的校验词写入 outputs/answer.txt；只需读取该文件。", "_expect": {"files": {"answer.txt": f"TOKEN_{i}"}, "resume_safe": kind != "none", "tool_calls_min": 1}})
        add(f"context/{kind}", f"Context 注入 / capture：{kind}", rows)

    for kind in ("setup_string", "setup_list", "env", "thinking"):
        rows = []
        for i in range(1, 5):
            row = base(i)
            if kind.startswith("setup"):
                commands = ["mkdir -p scratch", f"printf 'SETUP_{i}\\n' > scratch/token.txt"]
                row["runtime"] = {"setup": "\n".join(commands) if kind.endswith("string") else commands}
                row["prompt"] = "读取 scratch/token.txt，把文本写入 outputs/answer.txt。"
                row["_expect"] = {"files": {"answer.txt": f"SETUP_{i}"}, "tool_calls_min": 1}
            elif kind == "env":
                row["runtime"] = {"env": "ROLLER_EXAMPLE_TOKEN" if i % 2 else ["ROLLER_EXAMPLE_TOKEN"]}
                row["prompt"] = f"读取环境变量 ROLLER_EXAMPLE_TOKEN，把它的值后接字符 {i} 写入 outputs/answer.txt。"
                row["_expect"] = {"files": {"answer.txt": f"ENV_OK{i}"}, "tool_calls_min": 1}
            else:
                # All four adapters accept low. Provider support is checked during live runs.
                row["runtime"] = {"thinking": "low"}
            rows.append(row)
        add(f"runtime/{kind}", f"运行设置：{kind}", rows, environment={"ROLLER_EXAMPLE_TOKEN": "ENV_OK"} if kind == "env" else {})

    for kind in ("nested", "multiple", "no_files"):
        rows = []
        for i in range(1, 5):
            row = base(i)
            if kind == "nested":
                out = f"deliverables/第 {i} 组"
                row.update(files={"output_dir": out}, prompt=f"创建 {out}/nested/answer.txt，内容为 RESULT_{i}。")
                row["_expect"] = {"files": {"nested/answer.txt": f"RESULT_{i}"}, "tool_calls_min": 1}
            elif kind == "multiple":
                row["prompt"] = f"创建 outputs/a.txt，内容为 A{i}；创建 outputs/nested/b.txt，内容为 B{i}。"
                row["_expect"] = {"files": {"a.txt": f"A{i}", "nested/b.txt": f"B{i}"}, "tool_calls_min": 1}
            else:
                row["prompt"] += " 不创建任何文件。"
                row["_expect"]["no_output_files"] = True
            rows.append(row)
        add(f"outputs/{kind}", f"输出回收：{kind}", rows)

    for extension in ("png", "pdf", "wav", "zip"):
        rows = []
        for i in range(1, 5):
            source = f"media/{i}.{extension}"
            if extension == "wav":
                prompt, expected = f"用 Python 标准库 wave 打开 {source}，读取采样帧数。不要播放或转录。", str(80 * i)
            elif extension == "zip":
                prompt, expected = f"用 Python 标准库 zipfile 读取 {source} 内的 note.txt 文本。", f"TOKEN_{i}"
            else:
                prompt, expected = f"以二进制读取 {source}，只统计文件字节数；无需视觉理解或 PDF 文本解析。", str((ROOT / "fixtures" / source).stat().st_size)
            rows.append(file_task(source, prompt, expected))
        add(f"binary/{extension}", f"二进制 .{extension} 传输和读取；不验证语义理解", rows)
    add("vision/png", "可选：真正调用图像查看工具识别纯色，需模型和工具支持视觉", [file_task(f"media/{i}.png", f"使用图像查看工具打开 media/{i}.png，识别整张图片的主色。只用英文小写颜色名回答，不要用代码读取像素或通过文件名猜测。", color) for i, color in enumerate(("red", "green", "blue", "yellow"), 1)], tier="vision")
    rows = []
    for i in range(1, 5):
        row = base(i)
        row["prompt"] = f"把 BENCH_{i} 写入 outputs/answer.txt。"
        row["_expect"] = {"files": {"answer.txt": f"BENCH_{i}"}, "score": 1.0}
        row["bench"] = {"schema_version": "roller.bench/v1", "name": "coverage-smoke", "category": "file-output", "grade": {"needs_transcript": False, "timeout_seconds": 10, "code": "from pathlib import Path\ndef grade(workspace_path):\n    p = Path(workspace_path) / 'outputs/answer.txt'\n    return {'overall_score': float(p.is_file() and p.read_text().strip() == " + repr(f"BENCH_{i}") + ")}\n"}}
        rows.append(row)
    add("bench/file_grader", "容器销毁前用标准库评分；不调用 LLM judge", rows)
    return result


def emit_group(group: dict, harness: str, *, simulator: str | None = None) -> dict:
    key = group["name"] + "/" + harness + ("--" + simulator if simulator else "")
    fmt = group.get("format", "jsonl")
    suffix = ".json" if fmt.startswith("json_") else ".jsonl"
    path = ROOT / "cases" / (key + suffix)
    if group["name"] == "inputs/default_root":
        path = ROOT / "fixtures" / (harness + "-default-root.jsonl")
    rows, expectations = [], []
    for i, original in enumerate(group["rows"], 1):
        row = copy.deepcopy(original)
        expected = row.pop("_expect", {})
        row["id"] = key.replace("/", "__") + f"__{i:02d}"
        row["harness"], row["model"] = harness, MODELS[harness]
        if "files" in row:
            row["file_root"] = os.path.relpath(ROOT / "fixtures", path.parent)
        if group["name"] == "inputs/default_root":
            row.pop("file_root", None)
        for name in ("workspace", "workspace_path"):
            if name in row:
                row[name] = os.path.relpath(row[name], path.parent)
        if group["name"] == "routing/aliases":
            name = ("harness", "agent_harness", "task_agent", "agent")[i - 1]
            if name in {"task_agent", "agent"}:
                row[name] = {"harness": row.pop("harness"), "model": row.pop("model")}
            elif name == "agent_harness":
                row[name] = row.pop("harness")
        if group["name"] == "routing/model_cli":
            row.pop("model")
        if group["name"] == "routing/id_variants":
            row["id"] = (101, "中文 空格", "a/b:c?d", "long_" + "x" * 100)[i - 1]
        if group.get("parser_options", {}).get("id_key"):
            row["case_id"] = row.pop("id")
        for name in ("prompt", "question", "instruction"):
            if name in row:
                row[name] += " " + TAIL
        if row.get("messages"):
            last = row["messages"][-1]
            if isinstance(last, str):
                row["messages"][-1] += " " + TAIL
            else:
                last["content"] += " " + TAIL
        expected.setdefault("status", "ok")
        expected.setdefault("user_turns", len(row.get("messages", [])) or 1)
        expected["task_id"] = str(row.get("id", row.get("case_id")))
        expectations.append(expected)
        rows.append(row)
    if fmt == "json_array":
        json_write(path, rows)
    elif fmt == "json_object":
        json_write(path, {"tasks": rows})
    else:
        text = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"
        write(path, "\ufeff\n" + text.replace("\n", "\n\n") if fmt == "jsonl_bom" else text)
    case = {"key": key, "path": path.relative_to(ROOT).as_posix(), "scenario": group["name"], "harness": harness, "simulator": simulator, "tier": group.get("tier", "standard"), "description": group["description"], "count": 4, "parser_options": group.get("parser_options", {}), "environment": group.get("environment", {}), "cli_model_required": group["name"] == "routing/model_cli", "expectations": expectations}
    if group["name"] == "inputs/absolute_root":
        case["runtime_absolute_file_root"] = True
    return case


def simulator_group(simulator: str, harness: str) -> dict:
    rows = []
    for i in range(1, 5):
        token = f"SIM_{i}"
        first = f"只回复校验词 {token}。"
        second = None
        row = {"schema_version": "roller.task/v1"}
        expected = {"reply": token, "rounds": 1, "termination": "simulator_finished"}
        if i == 2:
            source = "data/csv/01.csv"
            row["files"] = {"inputs": [source]}
            first = f"读取 {source}，只回复 CSV 数据行数，不含表头。"
            second = "把你刚才回答的数字写入 outputs/answer.txt，然后只回复 DONE。"
            with (ROOT / "fixtures" / source).open(encoding="utf-8", newline="") as stream:
                row_count = sum(1 for _ in csv.reader(stream)) - 1
            expected = {"reply": "DONE", "files": {"answer.txt": str(row_count)}, "rounds": 2, "termination": "simulator_finished", "tool_calls_min": 1}
        elif i == 3:
            row["files"] = {"context": [{"role": "memory", "source": "context/source-3.md", "capture": "hash"}], "output_dir": "deliverables"}
            first = "读取注入的 MEMORY.md，将其中校验词写入 deliverables/answer.txt；然后只回复 DONE。"
            expected = {"reply": "DONE", "files": {"answer.txt": "TOKEN_3"}, "rounds": 1, "termination": "simulator_finished", "tool_calls_min": 1}
        elif i == 4:
            expected["termination"] = "max_turns"
        instruction = "你是严格遵循轮次的测试用户。不要使用工具。只返回 JSON，不要 Markdown。根据 Manager 给出的 rounds_completed 决策：当它为 0 时，返回 " + json.dumps({"action": "message", "content": first + " " + TAIL}, ensure_ascii=False) + "。"
        if second:
            instruction += "当它为 1 时，返回 " + json.dumps({"action": "message", "content": second + " " + TAIL}, ensure_ascii=False) + "。当它大于等于 2 时，返回 {\"action\":\"finish\",\"reason\":\"test complete\"}。"
        elif i == 4:
            instruction += "当它大于等于 1 时，仍返回 {\"action\":\"message\",\"content\":\"只回复 LIMIT\"}，不要返回 finish。这用于测试 Manager 的 max_turns 截断。"
        else:
            instruction += "当它大于等于 1 时，返回 {\"action\":\"finish\",\"reason\":\"test complete\"}。"
        row["user_simulator"] = {"harness": simulator, "model": MODELS[simulator], "max_turns": 2 if second else 1, "instruction": instruction}
        if simulator == harness and i == 1:
            row["user_simulator"].pop("model")  # Exercise same-Harness model inheritance.
        expected["user_turns"] = expected["rounds"]
        row["_expect"] = expected
        rows.append(row)
    return {"name": "simulators/pair", "description": "四种短对话：纯文本 / 文件两轮 / Context+自定义输出 / max_turns；同 Harness 首例继承模型", "rows": rows, "tier": "simulator"}


def negative_groups() -> list[dict]:
    groups = []

    def add(name: str, entries: list[tuple[dict, str]]) -> None:
        rows = []
        for i, (patch, error) in enumerate(entries, 1):
            row = {"schema_version": "roller.task/v1", "id": f"negative_{name}_{i}", "prompt": "只回复 OK。", "file_root": "../fixtures", **patch}
            rows.append({"task": row, "error_contains": error})
        path = ROOT / "negative" / f"{name}.json"
        json_write(path, rows)
        groups.append({"key": "negative/" + name, "path": path.relative_to(ROOT).as_posix(), "count": 4})

    add("dialogue", [({"prompt": ""}, "missing non-empty"), ({"messages": [{"role": "assistant", "content": "hello"}]}, "only user"), ({"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": "x"}]}]}, "empty content"), ({"user_simulator": {"instruction": "test"}}, "cannot be combined")])
    add("paths", [({"files": {"inputs": ["/etc/hosts"]}}, "absolute paths"), ({"files": {"inputs": ["../outside.txt"]}}, "parent traversal"), ({"files": {"inputs": [{"source": "tiny/1.txt", "target": "renamed.txt"}]}}, "no longer supported"), ({"files": {"inputs": ["tiny/1.txt"], "output_dir": "tiny"}}, "overlaps input")])
    add("integrity", [({"files": {"inputs": ["missing/required.txt"]}}, "does not exist"), ({"files": {"context": [{"role": "memory", "source": "context/missing.md"}]}}, "does not exist"), ({"files": {"inputs": [{"source": "tiny/1.txt", "sha256": "0" * 64}]}}, "sha256 mismatch"), ({"files": {"inputs": [{"source": "tiny/1.txt", "sha256": "bad"}]}}, "64 hex")])
    add("collisions", [({"files": {"inputs": ["tiny/1.txt", "tiny/1.txt"]}}, "collision"), ({"files": {"context": [{"role": "fixture", "source": "context/source-1.md", "target": "outputs/note.md"}]}}, "reserved"), ({"files": {"context": [{"role": "fixture", "source": "context/source-1.md", "target": "notes/a.md"}], "output_dir": "notes"}}, "overlaps context"), ({"files": {"outputs": ["*.txt"]}}, "no longer supported")])
    add("configuration", [({"harness": "unknown"}, "harness must be one of"), ({"harness": "codex", "agent_harness": "openclaw"}, "must match"), ({"prompt": None, "user_simulator": {"instruction": "test", "max_turns": 0}}, "max_turns"), ({"runtime": {"thinking": "invalid"}}, "thinking")])
    return groups


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, help="optionally import a Roller_Data directory; default: reuse bundled fixtures")
    args = parser.parse_args()
    sources = fixtures(args.data_root.resolve() if args.data_root else None)
    cases = []
    for group in groups():
        for harness in HARNESSES:
            cases.append(emit_group(group, harness))
    for harness in HARNESSES:
        for simulator in HARNESSES:
            cases.append(emit_group(simulator_group(simulator, harness), harness, simulator=simulator))
    negative = negative_groups()
    payload = {"schema_version": "roller.examples-coverage/v1", "cases_per_group": 4, "positive_tasks": sum(c["count"] for c in cases), "negative_tasks": sum(c["count"] for c in negative), "source_files": len(sources["files"]), "cases": cases, "negative": negative}
    json_write(ROOT / "manifest.json", payload)
    lines = ["# 覆盖清单", "", "由 `generate.py` 生成。每个链接是一份恰好含 4 条任务的独立清单；运行说明见 [README](README.md)。", "", "## 输入场景 × 四种 Harness", "", "| 场景 | OpenClaw | Codex | Claude Code | Hermes | 验证点 |", "| --- | --- | --- | --- | --- | --- |"]
    for scenario in dict.fromkeys(case["scenario"] for case in cases if not case["simulator"]):
        members = [case for case in cases if case["scenario"] == scenario]
        links = [f"[4 条]({case['path']})" for case in members]
        lines.append("| " + " | ".join([scenario, *links, members[0]["description"]]) + " |")
    lines += ["", "## Task Agent × User Simulator", "", "每个配对的 4 条任务依次验证：纯文本、CSV 两轮与文件产物、Memory 与自定义输出目录、max_turns 截断。", "", "| Task Agent | User Simulator | 任务 |", "| --- | --- | --- |"]
    for case in cases:
        if case["simulator"]:
            lines.append(f"| {case['harness']} | {case['simulator']} | [4 条]({case['path']}) |")
    lines += ["", "## 预期拒绝的输入", "", "这些文件是带预期错误的测试描述，不能整份传给 `roller collect`；`suite.py validate` 会逐条隔离验证。", ""]
    lines += [f"- [{item['key']}]({item['path']})：4 条。" for item in negative]
    write(ROOT / "COVERAGE.md", "\n".join(lines) + "\n")
    print(f"Generated {len(cases)} positive groups / {payload['positive_tasks']} tasks; {len(negative)} negative groups / {payload['negative_tasks']} tasks.")


if __name__ == "__main__":
    main()
