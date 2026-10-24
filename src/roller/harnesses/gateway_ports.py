"""Host-wide reservation of OpenClaw gateway port ranges.

With ``--network host`` every rollout's OpenClaw gateway listens on the host,
on ``start + slot * step``. Two runs on one machine that share a range do not
fail outright: the readiness probe sees the other run's gateway, the agent
connects to it, is refused, and quietly runs without its own gateway.

Each run therefore reserves the ports of its range with one lock file per
port, held with ``flock`` for the run's lifetime and released by the kernel
when the process exits, however it exits. A port is also skipped when
something already listens on it, which covers older Roller runs and other
servers that take no lock.
"""
from __future__ import annotations

import errno
import fcntl
import os
import socket
import tempfile
from pathlib import Path
from typing import BinaryIO

DEFAULT_GATEWAY_PORT = 18789
LOCK_DIRECTORY = Path(tempfile.gettempdir()) / "roller-gateway-ports"
MAX_PORT = 65535


class GatewayPortsUnavailable(RuntimeError):
    """The requested gateway port range, or every candidate range, is taken."""


class GatewayPortReservation:
    """Ports ``start, start + step, ...`` held until :meth:`release`."""

    def __init__(self, start: int, step: int, slots: int, locks: list[BinaryIO]) -> None:
        self.start = start
        self.step = step
        self.slots = slots
        self._locks = locks

    @property
    def ports(self) -> range:
        return range(self.start, self.start + self.slots * self.step, self.step)

    def release(self) -> None:
        for lock in self._locks:
            lock.close()
        self._locks = []

    def __enter__(self) -> "GatewayPortReservation":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def reserve_gateway_ports(
    start: int | None,
    step: int,
    slots: int,
    *,
    lock_directory: Path | None = None,
) -> GatewayPortReservation:
    """Reserve ``slots`` gateway ports ``step`` apart.

    ``start=None`` picks the first free range from :data:`DEFAULT_GATEWAY_PORT`
    upward; an explicit ``start`` is reserved as given or not at all.
    """

    if step < 1 or slots < 1:
        raise ValueError("gateway port step and slot count must be positive")
    directory = lock_directory or LOCK_DIRECTORY
    _prepare_directory(directory)
    if start is not None:
        _check_range(start, step, slots)
        reservation, reason = _try_reserve(start, step, slots, directory)
        if reservation is None:
            raise GatewayPortsUnavailable(
                f"OpenClaw gateway port {reason}; choose another --gateway-port, "
                "or omit it to pick a free range automatically"
            )
        return reservation
    candidate = DEFAULT_GATEWAY_PORT
    while candidate + (slots - 1) * step <= MAX_PORT:
        reservation, _reason = _try_reserve(candidate, step, slots, directory)
        if reservation is not None:
            return reservation
        candidate += slots * step
    raise GatewayPortsUnavailable(
        f"no free range of {slots} OpenClaw gateway ports {step} apart "
        f"from {DEFAULT_GATEWAY_PORT} upward"
    )


def _check_range(start: int, step: int, slots: int) -> None:
    last = start + (slots - 1) * step
    if start < 1 or last > MAX_PORT:
        raise ValueError(
            f"--gateway-port {start} with {slots} slots {step} apart ends at {last}, "
            f"outside 1-{MAX_PORT}"
        )


def _prepare_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    try:
        # Shared by every user's runs: world-writable, sticky like /tmp.
        directory.chmod(0o1777)
    except PermissionError:
        pass  # created by another user, who already set the mode


def _try_reserve(
    start: int, step: int, slots: int, directory: Path
) -> tuple[GatewayPortReservation | None, str]:
    locks: list[BinaryIO] = []
    for port in range(start, start + slots * step, step):
        reason = _lock_port(port, directory, locks)
        if reason is not None:
            for lock in locks:
                lock.close()
            return None, f"{port} {reason}"
    return GatewayPortReservation(start, step, slots, locks), ""


def _lock_port(port: int, directory: Path, locks: list[BinaryIO]) -> str | None:
    path = directory / f"{port}.lock"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CREAT, 0o666)
    except PermissionError:
        descriptor = os.open(path, os.O_RDONLY)
    lock = os.fdopen(descriptor, "rb")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return "is reserved by another Roller run on this host"
    if _in_use(port):
        lock.close()
        return "is already in use on this host"
    locks.append(lock)
    return None


def _in_use(port: int) -> bool:
    # Bind the way the gateway does: loopback, with SO_REUSEADDR (Node's
    # default), so only a live listener counts, not sockets in TIME_WAIT left
    # by an earlier run.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            return exc.errno == errno.EADDRINUSE
    return False
