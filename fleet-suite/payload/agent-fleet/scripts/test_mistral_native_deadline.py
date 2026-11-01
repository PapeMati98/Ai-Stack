"""Native Vibe deadline margin below the Fleet wall timeout (HD-20261014-0c526e).

Vibe 2.26.1 ends a headless run at ``--time-limit`` with outcome ``deadline``
(exit 3) and writes the export; measured against a hung model stream it exits
about two seconds after the limit. Fleet used to pass its own wall timeout as
the limit, so the parent kill (exit 124, no export, identity unavailable) always
won. These tests pin the margin and that the command carries it.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mistral_runtime as runtime


class NativeTimeLimitTests(unittest.TestCase):
    def test_margin_is_thirty_seconds_below_the_wall_timeout(self):
        self.assertEqual(runtime.MISTRAL_NATIVE_DEADLINE_MARGIN_SECONDS, 30)
        for timeout, expected in ((3600, 3570), (1500, 1470), (900, 870), (120, 90), (60, 30)):
            with self.subTest(timeout=timeout):
                self.assertEqual(runtime.native_time_limit(timeout), expected)

    def test_short_runs_without_room_keep_the_wall_timeout(self):
        for timeout in (59, 30, 1):
            with self.subTest(timeout=timeout):
                self.assertEqual(runtime.native_time_limit(timeout), timeout)

    def test_limit_is_always_positive_and_never_above_the_timeout(self):
        for timeout in range(1, 3601, 7):
            limit = runtime.native_time_limit(timeout)
            self.assertGreaterEqual(limit, 1)
            self.assertLessEqual(limit, timeout)

    def test_invalid_timeouts_are_refused(self):
        for timeout in (0, -1, True, 900.0, "900", None):
            with self.subTest(timeout=timeout), self.assertRaises(runtime.MistralRuntimeError):
                runtime.native_time_limit(timeout)


class BuildCommandDeadlineTests(unittest.TestCase):
    def command(self, timeout):
        return runtime.build_command(
            runtime.MISTRAL_NATIVE_EXECUTABLE, model="mistral-large-4", mode="review",
            run_dir=Path("synthetic run with spaces"), timeout=timeout)

    def test_time_limit_argument_is_below_the_parent_timeout(self):
        for timeout in (3600, 900, 120, 60):
            with self.subTest(timeout=timeout):
                command = self.command(timeout)
                limit = int(command[command.index("--time-limit") + 1])
                self.assertEqual(limit, timeout - 30)
                self.assertLess(limit, timeout)

    def test_time_limit_argument_matches_native_time_limit_everywhere(self):
        for timeout in (1, 45, 59, 60, 61, 1200, 1500, 3600):
            with self.subTest(timeout=timeout):
                command = self.command(timeout)
                self.assertEqual(command[command.index("--time-limit") + 1],
                                 str(runtime.native_time_limit(timeout)))

    def test_other_limits_and_shape_are_unchanged(self):
        command = self.command(900)
        self.assertEqual(command[command.index("--max-turns") + 1], str(runtime.MISTRAL_MAX_TURNS))
        self.assertEqual(command[command.index("--max-tokens") + 1], str(runtime.session_token_budget(None)))
        self.assertIn("--legacy-harness", command)
        self.assertEqual(command.count("--time-limit"), 1)

    def test_parent_timeout_cap_still_applies(self):
        with self.assertRaises(runtime.MistralRuntimeError):
            self.command(3601)


if __name__ == "__main__":
    unittest.main()
