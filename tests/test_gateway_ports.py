from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from roller.cli import _build_parser, _collect
from roller.harnesses.gateway_ports import (
    GatewayPortsUnavailable,
    reserve_gateway_ports,
)
from roller.models import HarnessResult
from roller.run_lock import ensure_run_lock


# Far from the default range, where real runs on this host reserve ports.
TEST_GATEWAY_PORT = 45000


def isolate_gateway_ports(test: unittest.TestCase) -> Path:
    """Keep ``test``'s gateway port reservations away from real runs on this host.

    Lock files go to a temporary directory, which is returned, and automatic
    ranges are searched from :data:`TEST_GATEWAY_PORT`. Call it in ``setUp`` of
    any test that runs ``roller collect`` with an OpenClaw participant.
    """
    temporary = tempfile.TemporaryDirectory()
    test.addCleanup(temporary.cleanup)
    directory = Path(temporary.name)
    patcher = patch.multiple(
        "roller.harnesses.gateway_ports",
        LOCK_DIRECTORY=directory,
        DEFAULT_GATEWAY_PORT=TEST_GATEWAY_PORT,
    )
    patcher.start()
    test.addCleanup(patcher.stop)
    return directory


def _listen(port: int) -> socket.socket:
    listener = socket.socket()
    listener.bind(("127.0.0.1", port))
    listener.listen()
    return listener


def _free_port_block(slots: int, step: int) -> int:
    """A start port whose block is currently unbound, well above the default range."""
    for start in range(TEST_GATEWAY_PORT, 60000, slots * step):
        try:
            sockets = [_listen(port) for port in range(start, start + slots * step, step)]
        except OSError:
            continue
        for listener in sockets:
            listener.close()
        return start
    raise unittest.SkipTest("no free port block")


class GatewayPortReservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.locks = isolate_gateway_ports(self)

    def reserve(self, start, step=10, slots=3):
        reservation = reserve_gateway_ports(start, step, slots)
        self.addCleanup(reservation.release)
        return reservation

    def test_concurrent_runs_get_disjoint_ranges(self) -> None:
        first = self.reserve(None)
        second = self.reserve(None)
        self.assertFalse(set(first.ports) & set(second.ports))
        self.assertEqual(len(second.ports), 3)

    def test_explicit_range_held_by_another_run_is_an_error(self) -> None:
        start = _free_port_block(3, 10)
        self.reserve(start)
        with self.assertRaisesRegex(GatewayPortsUnavailable, "reserved by another Roller run"):
            self.reserve(start + 10)

    def test_explicit_range_with_a_listener_is_an_error(self) -> None:
        start = _free_port_block(3, 10)
        listener = _listen(start + 20)
        self.addCleanup(listener.close)
        with self.assertRaisesRegex(GatewayPortsUnavailable, f"{start + 20} is already in use"):
            self.reserve(start)

    def test_automatic_range_skips_ports_in_use(self) -> None:
        probe = self.reserve(None)
        busy = probe.ports[1]
        probe.release()
        listener = _listen(busy)
        self.addCleanup(listener.close)
        chosen = self.reserve(None)
        self.assertNotIn(busy, chosen.ports)

    def test_release_frees_the_range(self) -> None:
        first = self.reserve(None)
        first.release()
        self.assertEqual(self.reserve(None).ports, first.ports)

    def test_lock_files_stay_usable_by_other_users(self) -> None:
        umask = os.umask(0o077)
        try:
            reservation = self.reserve(None, slots=1)
        finally:
            os.umask(umask)
        lock = self.locks / f"{reservation.ports.start}.lock"
        self.assertEqual(lock.stat().st_mode & 0o777, 0o666)

    @unittest.skipIf(os.geteuid() == 0, "root can open any lock file")
    def test_port_whose_lock_file_cannot_be_opened_is_taken(self) -> None:
        probe = self.reserve(None, slots=1)
        probe.release()
        port = probe.ports.start
        (self.locks / f"{port}.lock").chmod(0)  # e.g. left by another user's umask
        self.assertNotIn(port, self.reserve(None, slots=1).ports)
        with self.assertRaisesRegex(GatewayPortsUnavailable, "lock file this user cannot open"):
            self.reserve(port, slots=1)

    def test_reservation_ends_with_the_process_that_held_it(self) -> None:
        start = _free_port_block(2, 10)
        holder = subprocess.Popen(
            [
                sys.executable, "-c",
                "import sys, time; from pathlib import Path; "
                "from roller.harnesses import gateway_ports; "
                "gateway_ports.LOCK_DIRECTORY = Path(sys.argv[1]); "
                f"held = gateway_ports.reserve_gateway_ports({start}, 10, 2); "
                "print('held', flush=True); time.sleep(60)",
                str(self.locks),
            ],
            stdout=subprocess.PIPE,
            text=True,
            env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        )
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        with self.assertRaises(GatewayPortsUnavailable):
            self.reserve(start, slots=2)
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=10)
        self.assertEqual(self.reserve(start, slots=2).ports.start, start)

    def test_explicit_range_must_fit_below_65536(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside 1-65535"):
            self.reserve(65530, step=10, slots=2)


class _NoRunHarness:
    name = "openclaw"

    def __init__(self, config, slot) -> None:
        self.config = config

    def run(self, request, output_dir, attempt):
        return HarnessResult.error_result("not run in this test")


class CliGatewayPortTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        isolate_gateway_ports(self)
        self.tasks = self.root / "tasks.jsonl"
        self.tasks.write_text(json.dumps({"id": "task", "prompt": "hi"}) + "\n")
        self.configs = []
        patches = contextlib.ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(patch("roller.cli._docker_preflight", return_value="image-id"))
        patches.enter_context(
            patch("roller.cli.OpenClawHarness", side_effect=self._harness)
        )

    def _harness(self, config, slot):
        self.configs.append(config)
        return _NoRunHarness(config, slot)

    def invoke(self, *extra: str, run_name: str = "run") -> int:
        args = [
            "collect", "--tasks", str(self.tasks), "--model", "provider/model",
            "--output", str(self.root / "runs"), "--run-name", run_name, *extra,
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            return _collect(_build_parser().parse_args(args))

    def info(self, run_name: str = "run") -> dict:
        return json.loads((self.root / "runs" / run_name / "info.json").read_text())

    def test_run_uses_a_range_no_other_run_holds_and_records_it(self) -> None:
        other = reserve_gateway_ports(None, 10, 4)
        self.addCleanup(other.release)

        self.invoke("--parallel", "2")

        start = self.configs[0].gateway_port_start
        self.assertFalse({start, start + 10} & set(other.ports))
        self.assertEqual(self.info()["configuration"]["openclaw"]["gateway_port_start"], start)
        # The range is released once collection ends.
        reserve_gateway_ports(start, 10, 2).release()

    def test_busy_explicit_port_fails_before_any_run_files_are_written(self) -> None:
        start = _free_port_block(1, 10)
        other = reserve_gateway_ports(start, 10, 1)
        self.addCleanup(other.release)
        with self.assertRaisesRegex(GatewayPortsUnavailable, "choose another --gateway-port"):
            self.invoke("--gateway-port", str(start))
        self.assertFalse((self.root / "runs" / "run").exists())

    def test_simulator_slots_are_reserved_too(self) -> None:
        self.tasks.write_text(json.dumps({
            "id": "task", "harness": "openclaw",
            "user_simulator": {"harness": "openclaw", "instruction": "ask", "max_turns": 1},
        }) + "\n")
        with patch("roller.cli.reserve_gateway_ports", wraps=reserve_gateway_ports) as reserve:
            self.invoke("--parallel", "2")
        # Slots 0-1 for the agents, 2-3 for their OpenClaw simulators.
        reserve.assert_called_once_with(None, 10, 4)

    def test_no_reservation_without_host_networking(self) -> None:
        other = reserve_gateway_ports(None, 10, 1)
        self.addCleanup(other.release)
        self.invoke("--network", "bridge")
        self.assertEqual(self.configs[0].gateway_port_start, 18789)

    def test_resume_accepts_a_different_range(self) -> None:
        self.invoke()
        first = self.configs[-1].gateway_port_start
        holder = reserve_gateway_ports(first, 10, 1)
        self.addCleanup(holder.release)
        self.assertEqual(self.invoke("--resume"), 1)  # the sample still fails
        self.assertNotEqual(self.configs[-1].gateway_port_start, first)


class LockComparisonTests(unittest.TestCase):
    def test_gateway_ports_do_not_block_resume_but_other_changes_do(self) -> None:
        def lock(**openclaw):
            return {
                "schema_version": "roller.run-lock/v1",
                "execution": {"openclaw": {"network": "host", **openclaw}},
            }

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lock.json"
            ensure_run_lock(
                path, lock(gateway_port_start=18789, gateway_port_step=10),
                resume=False, existing_run=False,
            )
            decision = ensure_run_lock(
                path, lock(gateway_port_start=18829, gateway_port_step=20),
                resume=True, existing_run=True,
            )
            self.assertTrue(decision.reuse_existing_samples)
            # The saved lock is never rewritten.
            self.assertEqual(
                json.loads(path.read_text())["execution"]["openclaw"]["gateway_port_start"],
                18789,
            )
            with self.assertRaises(FileExistsError):
                ensure_run_lock(
                    path, lock(gateway_port_start=18789, gateway_port_step=10, network="bridge"),
                    resume=True, existing_run=True,
                )


if __name__ == "__main__":
    unittest.main()
