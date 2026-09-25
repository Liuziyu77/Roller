"""Standard-library render recorder. metadata.json is each attempt's commit record.

A lock spans the whole render: invocations share output.png. SIGKILL leaves a
running attempt for inspection, never a fabricated success. This is an audit
journal in the model's workspace, not a tamper-proof execution sandbox.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
import zlib

SCHEMA = "roller.render-attempt/v1"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def atomic_json(path, value):
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def copy_atomic(source, destination):
    temp = destination.with_suffix(destination.suffix + ".tmp")
    shutil.copyfile(source, temp)
    os.replace(temp, destination)


def png_dimensions(path):
    """Check PNG structure and CRCs in bounded chunks; no pixel decoding."""
    with path.open("rb") as stream:
        if stream.read(8) != b"\x89PNG\r\n\x1a\n":
            raise ValueError("invalid PNG signature")
        dimensions = None
        idat = False
        while True:
            header = stream.read(8)
            if len(header) != 8:
                raise ValueError("truncated PNG")
            length, kind = struct.unpack(">I4s", header)
            if dimensions is None and (kind != b"IHDR" or length != 13):
                raise ValueError("missing PNG IHDR")
            if kind == b"IHDR" and (length != 13 or dimensions is not None):
                raise ValueError("invalid PNG IHDR")
            crc, remaining, ihdr = zlib.crc32(kind), length, b""
            while remaining:
                block = stream.read(min(remaining, 65536))
                if not block:
                    raise ValueError("truncated PNG chunk")
                if kind == b"IHDR":
                    ihdr += block
                crc = zlib.crc32(block, crc)
                remaining -= len(block)
            expected = stream.read(4)
            if len(expected) != 4 or struct.unpack(">I", expected)[0] != crc & 0xFFFFFFFF:
                raise ValueError("invalid PNG CRC")
            if kind == b"IHDR":
                dimensions = list(struct.unpack(">II", ihdr[:8]))
                if min(dimensions) <= 0:
                    raise ValueError("invalid PNG dimensions")
            if kind == b"IDAT":
                idat = True
            if kind == b"IEND":
                if length or not idat or stream.read(1):
                    raise ValueError("invalid PNG end")
                return dimensions


def tee(source, log, destination, errors):
    try:
        with source, log.open("wb") as stream:
            while True:
                data = os.read(source.fileno(), 65536)
                if not data:
                    break
                stream.write(data)
                stream.flush()
                if destination is not None:
                    try:
                        destination.write(data)
                        destination.flush()
                    except (BrokenPipeError, OSError):
                        destination = None
    except OSError as exc:
        errors.append(str(exc))


def run(script_arg):
    if any(c in script_arg or c in str(Path.cwd()) for c in ("\n", "\r")):
        raise ValueError("paths must be single line")
    script = Path(script_arg).absolute()
    for path in (script, *script.parents):
        if path.is_symlink():
            raise ValueError("symlinked script paths are not supported")
    output = script.parent
    output.mkdir(parents=True, exist_ok=True)
    iterations = output / "iterations"
    if iterations.is_symlink():
        raise ValueError("iterations must not be a symlink")
    iterations.mkdir(exist_ok=True)
    lock_path = iterations / ".render.lock"
    with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return render(script_arg, script, output, iterations)


def render(script_arg, script, output, iterations):
    existing = list(iterations.glob("attempt_*"))
    for path in existing:
        if path.is_symlink() or not path.is_dir():
            raise ValueError("unsafe attempt directory")
        meta = path / "metadata.json"
        if not meta.is_file() or json.loads(meta.read_text()).get("status") == "running":
            raise ValueError(f"unfinished render {path.name}; preserve and inspect it before retrying")
    number = max((int(p.name[8:]) for p in existing if p.name[8:].isdigit()), default=0) + 1
    attempt_id = f"attempt_{number:06d}"
    attempt = iterations / attempt_id
    attempt.mkdir()
    image = output / "output.png"
    backup = attempt / "previous_output.png"
    metadata = {
        "schema_version": SCHEMA, "attempt_id": attempt_id, "attempt": number,
        "status": "running", "started_at": now(), "finished_at": None,
        "command": ["python3", script_arg], "recorder_version": 2,
        "cwd": str(Path.cwd()), "script_path": script_arg,
        "recorder_pid": os.getpid(),
        "exit_code": None, "wrapper_exit_code": None, "step": None,
        "files": {}, "image_wh": None,
    }
    atomic_json(attempt / "metadata.json", metadata)
    start = time.monotonic()
    received_signal, log_errors = [], []
    child = None
    previous_handlers = {}

    def forward(signum, _frame):
        received_signal.append(signum)
        if child is not None:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        previous_handlers[signum] = signal.signal(signum, forward)
    status, error, exit_code = "recorder_error", None, None
    had_previous, image_owned = False, False
    previous_stamp = image.stat().st_mtime_ns if image.is_file() else None
    try:
        if image.is_symlink() or (image.exists() and not image.is_file()):
            raise ValueError("output.png must be a regular file")
        copy_atomic(script, attempt / "plot.py")
        if image.exists():
            copy_atomic(image, backup)
            had_previous = True
        image_owned = True
        child = subprocess.Popen([sys.executable, script_arg], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, start_new_session=True)
        if received_signal:
            forward(received_signal[-1], None)
        threads = [
            threading.Thread(target=tee, args=(child.stdout, attempt / "stdout.log", sys.stdout.buffer, log_errors), daemon=True),
            threading.Thread(target=tee, args=(child.stderr, attempt / "stderr.log", sys.stderr.buffer, log_errors), daemon=True),
        ]
        for thread in threads:
            thread.start()
        signal_deadline = None
        while True:
            try:
                exit_code = child.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if received_signal:
                    if signal_deadline is None:
                        signal_deadline = time.monotonic() + 5
                    elif time.monotonic() >= signal_deadline:
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
        for thread in threads:
            thread.join(timeout=1)
        # Do not kill descendants merely to complete recorder bookkeeping.
        if any(thread.is_alive() for thread in threads):
            log_errors.append("background processes retained render output pipes; log capture incomplete")
        status = "success"
        if received_signal or exit_code < 0:
            status = "interrupted"
        elif exit_code != 0:
            status = "execution_failed"
        elif not image.is_file():
            status = "missing_image"
        elif previous_stamp is not None and image.stat().st_mtime_ns == previous_stamp:
            status = "missing_image"
            error = "output image was not updated by this execution"
        elif image.is_symlink():
            status = "invalid_image"
        else:
            try:
                metadata["image_wh"] = png_dimensions(image)
            except (ValueError, OSError) as exc:
                status, error = "invalid_image", str(exc)
        if not script.is_file() or script.is_symlink() or digest(script) != digest(attempt / "plot.py"):
            status = "script_changed"
        if log_errors:
            status, error = "recorder_error", "; ".join(log_errors)
        if image.is_file() and not image.is_symlink():
            copy_atomic(image, attempt / ("output.png" if status == "success" else "candidate.png"))
        if status == "success":
            step_number = max((int(p.stem[5:]) for p in iterations.glob("step_*.*")
                               if p.suffix in (".py", ".png") and p.stem[5:].isdigit()), default=0) + 1
            while any(os.path.lexists(iterations / f"step_{step_number:02d}{suffix}")
                      for suffix in (".py", ".png")):
                step_number += 1
            step = f"step_{step_number:02d}"
            copy_atomic(attempt / "plot.py", iterations / f"{step}.py")
            copy_atomic(attempt / "output.png", iterations / f"{step}.png")
            metadata["step"] = step
    except (OSError, ValueError) as exc:
        error, status = str(exc), "recorder_error"
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)

    wrapper_code = (exit_code if exit_code is not None and exit_code >= 0 else
                    128 - exit_code if exit_code is not None else 1)
    for filename in ("plot.py", "output.png", "candidate.png", "stdout.log", "stderr.log"):
        path = attempt / filename
        if path.is_file():
            metadata["files"][filename] = {"sha256": digest(path), "size": path.stat().st_size}
    metadata.update(status=status, error=error, exit_code=exit_code, wrapper_exit_code=wrapper_code,
                    finished_at=now(), elapsed_seconds=round(time.monotonic() - start, 6))
    atomic_json(attempt / "metadata.json", metadata)
    manifest = iterations / "render_manifest.jsonl"
    with os.fdopen(os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600), "w") as stream:
        stream.write(json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return wrapper_code


if __name__ == "__main__":
    try:
        sys.exit(run(sys.argv[1]))
    except (OSError, ValueError) as exc:
        print(f"Chart2Code recorder: {exc}", file=sys.stderr)
        sys.exit(1)
