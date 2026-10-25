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
from pathlib import Path
from typing import BinaryIO

DEFAULT_GATEWAY_PORT = 18789
# A fixed path rather than $TMPDIR: ports are host-wide, so every run on the
# host, whatever its environment, has to see the same locks.
LOCK_DIRECTORY = Path("/tmp/roller-gateway-ports")
MAX_PORT = 65535


class GatewayPortsUnavailable(RuntimeError):
    """The requested gateway port range, or every candidate range, is taken."""


class GatewayPortReservation:
    """Gateway ``ports`` held until :meth:`release`."""

    def __init__(self, ports: range, locks: list[BinaryIO]) -> None:
        self.ports = ports
        self._locks = locks

    def release(self) -> None:
        for lock in self._locks:
            lock.close()
        self._locks = []


def reserve_gateway_ports(start: int | None, step: int, slots: int) -> GatewayPortReservation:
    """Reserve ``slots`` gateway ports ``step`` apart.

    ``start=None`` picks the first free range from :data:`DEFAULT_GATEWAY_PORT`
    upward; an explicit ``start`` is reserved as given or not at all.
    """

    if step < 1 or slots < 1:
        raise ValueError("gateway port step and slot count must be positive")
    _prepare_directory(LOCK_DIRECTORY)
    width = slots * step
    if start is not None:
        _check_range(start, step, slots)
        try:
            return _reserve(range(start, start + width, step), LOCK_DIRECTORY)
        except GatewayPortsUnavailable as exc:
            raise GatewayPortsUnavailable(
                f"{exc}; choose another --gateway-port, "
                "or omit it to pick a free range automatically"
            ) from None
    last_start = MAX_PORT - (slots - 1) * step
    for candidate in range(DEFAULT_GATEWAY_PORT, last_start + 1, width):
        try:
            return _reserve(range(candidate, candidate + width, step), LOCK_DIRECTORY)
        except GatewayPortsUnavailable:
            continue
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


def _reserve(ports: range, directory: Path) -> GatewayPortReservation:
    locks: list[BinaryIO] = []
    try:
        for port in ports:
            locks.append(_lock_port(port, directory))
    except BaseException:
        for lock in locks:
            lock.close()
        raise
    return GatewayPortReservation(ports, locks)


def _lock_port(port: int, directory: Path) -> BinaryIO:
    """Lock ``port`` for this process, or raise if another run or server has it."""

    path = directory / f"{port}.lock"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CREAT, 0o666)
    except PermissionError:
        # Another user's lock file in the sticky directory refuses O_CREAT.
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except PermissionError:
            raise GatewayPortsUnavailable(
                f"OpenClaw gateway port {port} has a lock file this user cannot open"
            ) from None
    else:
        try:
            # Leave the file usable by other users' runs whatever our umask.
            os.fchmod(descriptor, 0o666)
        except OSError:
            pass  # another user's file; being able to open it is enough
    lock = os.fdopen(descriptor, "rb")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise GatewayPortsUnavailable(
            f"OpenClaw gateway port {port} is reserved by another Roller run on this host"
        ) from None
    if _in_use(port):
        lock.close()
        raise GatewayPortsUnavailable(
            f"OpenClaw gateway port {port} is already in use on this host"
        )
    return lock


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
