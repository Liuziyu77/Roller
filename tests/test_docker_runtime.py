from __future__ import annotations

import subprocess
import unittest

from roller.harnesses.docker import remove_container


class FakeRuntime:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []

    def run(self, args, timeout=None):
        self.calls.append((list(args), timeout))
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class RemoveContainerTests(unittest.TestCase):
    def test_retries_timeout_with_longer_budget(self):
        timeout = subprocess.TimeoutExpired(["docker", "rm"], 30)
        success = subprocess.CompletedProcess([], 0, "", "")
        runtime = FakeRuntime([timeout, success])

        result = remove_container(runtime, "roller-test")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            runtime.calls,
            [(["rm", "-f", "roller-test"], 30), (["rm", "-f", "roller-test"], 60)],
        )

    def test_retries_nonzero_result(self):
        failure = subprocess.CompletedProcess([], 1, "", "busy")
        success = subprocess.CompletedProcess([], 0, "", "")
        runtime = FakeRuntime([failure, success])

        result = remove_container(runtime, "roller-test")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(runtime.calls), 2)

    def test_raises_last_timeout(self):
        first = subprocess.TimeoutExpired(["docker", "rm"], 30)
        second = subprocess.TimeoutExpired(["docker", "rm"], 60)
        runtime = FakeRuntime([first, second])

        with self.assertRaises(subprocess.TimeoutExpired) as raised:
            remove_container(runtime, "roller-test")

        self.assertIs(raised.exception, second)


if __name__ == "__main__":
    unittest.main()
