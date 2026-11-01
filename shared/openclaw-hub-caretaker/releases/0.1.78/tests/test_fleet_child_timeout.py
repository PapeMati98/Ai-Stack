"""Fleet child timeout is explicit and strictly inside the outer guard.

Runners are injected. These tests never launch a model or Fleet process.
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from caretaker.config import TransportSpec
from caretaker import tick_budget
from caretaker.transports import (
    AgentFleetArgvTransport,
    NATIVE_TIMEOUT_PLACEHOLDER,
    TransportError,
    _fleet_default_run,
)


def _spec(argv=None, **extra):
    return TransportSpec(
        kind="openclaw_argv",
        executable="python.exe",
        argv=list(argv or ["--agent", "grok", "--model", "grok-4.6"]),
        **extra,
    )


def _ok():
    return json.dumps({
        "ok": True,
        "status": "DRY_RUN",
        "adapter": "grok",
        "model": "grok-4.6",
        "attempt_id": "attempt-1",
        "final": "{\"decision\":\"accept\"}",
    })


def _timed_out():
    return json.dumps({
        "ok": False,
        "status": "TIMEOUT",
        "adapter": "grok",
        "model": "grok-4.6",
        "attempt_id": "attempt-timeout",
        "error": "native timeout",
    })


def _timeouts(argv):
    values = []
    index = 0
    while index < len(argv):
        if argv[index] == "--timeout":
            values.append(argv[index + 1])
            index += 2
            continue
        index += 1
    return values


class FleetChildTimeoutTests(unittest.TestCase):
    def _invoke(self, *, timeout, argv=None, prompt="hello", spec_extra=None,
                runner_side_effect=None):
        seen = {}

        def runner(*, executable, argv, cwd, prompt, timeout):
            seen["argv"] = list(argv)
            seen["timeout"] = timeout
            seen["prompt"] = prompt
            task = Path(argv[argv.index("--task-file") + 1])
            seen["task_bytes"] = task.read_bytes()
            if runner_side_effect is not None:
                raise runner_side_effect
            return 0, _ok(), ""

        transport = AgentFleetArgvTransport(
            run_callable=runner,
            expected_identity=("grok", "grok-4.6"),
        )
        env = transport.invoke(
            spec=_spec(argv, **(spec_extra or {})),
            cwd="ws",
            prompt=prompt,
            run_config_path="rc",
            state_dir="state",
            timeout=timeout,
        )
        return seen, env

    def test_outer_900_and_120_derive_cleanup_margin(self):
        for outer, child in ((900, "870"), (120, "110")):
            with self.subTest(outer=outer):
                seen, env = self._invoke(timeout=outer)
                self.assertEqual(_timeouts(seen["argv"]), [child])
                self.assertLess(int(child), outer)
                self.assertEqual(seen["timeout"], outer)
                self.assertEqual(env.provider, "grok")
                self.assertEqual(env.model, "grok-4.6")

    def test_spec_without_placeholder_still_has_one_timeout(self):
        seen, _env = self._invoke(
            timeout=900,
            argv=["agent", "exec", "--json"],
        )
        self.assertEqual(_timeouts(seen["argv"]), ["870"])

    def test_placeholder_is_replaced_by_derived_timeout(self):
        seen, _env = self._invoke(
            timeout=360,
            argv=["--timeout", NATIVE_TIMEOUT_PLACEHOLDER,
                  "--agent", "grok", "--model", "grok-4.6"],
        )
        self.assertEqual(_timeouts(seen["argv"]), ["330"])
        self.assertNotIn(NATIVE_TIMEOUT_PLACEHOLDER, seen["argv"])

    def test_compatible_literal_is_not_increased(self):
        seen, _env = self._invoke(
            timeout=900,
            argv=["--timeout", "120", "--agent", "grok", "--model", "grok-4.6"],
        )
        self.assertEqual(_timeouts(seen["argv"]), ["120"])
        # A literal already inside the CLI range and strictly below the
        # outer guard is kept even when the derived fallback would be
        # outside 30..7200. It is not raised to the fallback.
        tight, _env = self._invoke(
            timeout=40,
            argv=["--timeout", "30", "--agent", "grok", "--model", "grok-4.6"],
        )
        self.assertEqual(_timeouts(tight["argv"]), ["30"])
        wide, _env = self._invoke(
            timeout=8000,
            argv=["--timeout", "120", "--agent", "grok", "--model", "grok-4.6"],
        )
        self.assertEqual(_timeouts(wide["argv"]), ["120"])

    def test_low_and_high_native_boundaries(self):
        low, _env = self._invoke(timeout=40)
        self.assertEqual(_timeouts(low["argv"]), ["30"])
        high, _env = self._invoke(timeout=7230)
        self.assertEqual(_timeouts(high["argv"]), ["7200"])
        self.assertEqual(high["timeout"], 7230)

    def test_equals_literal_is_preserved(self):
        seen, _env = self._invoke(timeout=900, argv=["--timeout=120"])
        self.assertEqual(_timeouts(seen["argv"]), ["120"])

    def test_margin_and_mixed_duplicates_fail_before_spawn(self):
        for outer, argv in ((35, ["--timeout", "30"]),
                            (900, ["--timeout", "899"]),
                            (900, ["--timeout=120", "--timeout", "110"])):
            calls = []
            def runner(**kwargs):
                calls.append(kwargs)
                return 0, _ok(), ""
            with self.subTest(outer=outer, argv=argv):
                with self.assertRaises(TransportError) as cm:
                    AgentFleetArgvTransport(run_callable=runner).invoke(
                        spec=_spec(argv), cwd="ws", prompt="x",
                        run_config_path="rc", state_dir="state", timeout=outer)
                self.assertEqual(cm.exception.reason_code, "invalid_budget")
                self.assertEqual(calls, [])

    def test_impossible_outer_budgets_fail_before_spawn(self):
        for bad in (
            True, False, 1.5, -5, 0, 29, 39, 7231, 8000,
            float("nan"), float("inf"), "900", None,
        ):
            calls = []

            def runner(*, executable, argv, cwd, prompt, timeout):
                calls.append(timeout)
                return 0, _ok(), ""

            transport = AgentFleetArgvTransport(run_callable=runner)
            with self.subTest(bad=bad):
                with self.assertRaises(TransportError) as cm:
                    transport.invoke(
                        spec=_spec(), cwd="ws", prompt="hello",
                        run_config_path="rc", state_dir="state",
                        timeout=bad)
                self.assertEqual(cm.exception.reason_code, "invalid_budget")
                self.assertEqual(calls, [])

    def test_cli_invalid_literals_fail_before_spawn(self):
        for token in ("29", "7201", "-1", "120.0", "nan", "True", ""):
            calls = []

            def runner(*, executable, argv, cwd, prompt, timeout):
                calls.append(argv)
                return 0, _ok(), ""

            transport = AgentFleetArgvTransport(run_callable=runner)
            with self.subTest(token=token):
                with self.assertRaises(TransportError) as cm:
                    transport.invoke(
                        spec=_spec(["--timeout", token]),
                        cwd="ws", prompt="hello",
                        run_config_path="rc", state_dir="state",
                        timeout=900)
                self.assertEqual(cm.exception.reason_code, "invalid_budget")
                self.assertEqual(calls, [])

    def test_duplicate_timeout_fails_before_spawn(self):
        calls = []

        def runner(*, executable, argv, cwd, prompt, timeout):
            calls.append(argv)
            return 0, _ok(), ""

        transport = AgentFleetArgvTransport(run_callable=runner)
        with self.assertRaises(TransportError) as cm:
            transport.invoke(
                spec=_spec(["--timeout", "120", "--timeout", "110"]),
                cwd="ws", prompt="hello",
                run_config_path="rc", state_dir="state",
                timeout=900)
        self.assertEqual(cm.exception.reason_code, "invalid_budget")
        self.assertEqual(calls, [])

    def test_literal_not_below_outer_guard_fails_closed(self):
        calls = []

        def runner(*, executable, argv, cwd, prompt, timeout):
            calls.append(argv)
            return 0, _ok(), ""

        transport = AgentFleetArgvTransport(run_callable=runner)
        with self.assertRaises(TransportError) as cm:
            transport.invoke(
                spec=_spec(["--timeout", "900"]),
                cwd="ws", prompt="hello",
                run_config_path="rc", state_dir="state",
                timeout=900)
        self.assertEqual(cm.exception.reason_code, "invalid_budget")
        self.assertEqual(calls, [])

    def test_process_timeout_exception_keeps_explicit_child_timeout(self):
        seen = {}

        def runner(*, executable, argv, cwd, prompt, timeout):
            seen["argv"] = list(argv)
            seen["timeout"] = timeout
            raise subprocess.TimeoutExpired(cmd=executable, timeout=timeout)

        transport = AgentFleetArgvTransport(run_callable=runner)
        with self.assertRaises(TransportError) as cm:
            transport.invoke(
                spec=_spec(), cwd="ws", prompt="hello",
                run_config_path="rc", state_dir="state", timeout=120)
        self.assertEqual(cm.exception.reason_code, "process_timeout")
        self.assertEqual(_timeouts(seen["argv"]), ["110"])
        self.assertEqual(seen["timeout"], 120)

    def test_outer_clipping_does_not_promote_unchanged_native_timeout(self):
        seen = {}

        def runner(*, executable, argv, cwd, prompt, timeout):
            seen["argv"] = list(argv)
            seen["timeout"] = timeout
            return 1, _timed_out(), "native timed out"

        budget = tick_budget.TickBudget(seconds=60, clock=lambda: 0.0)
        transport = AgentFleetArgvTransport(
            run_callable=runner, expected_identity=("grok", "grok-4.6"))
        with budget.activate():
            with self.assertRaises(TransportError) as cm:
                transport.invoke(
                    spec=_spec(["--timeout", "30", "--agent", "grok",
                                "--model", "grok-4.6"]),
                    cwd="ws", prompt="hello", run_config_path="rc",
                    state_dir="state", timeout=120)

        self.assertEqual(cm.exception.reason_code, "native_timeout")
        self.assertEqual(_timeouts(seen["argv"]), ["30"])
        self.assertEqual(seen["timeout"], 55)

    def test_native_timeout_clipped_by_tick_is_cooperative(self):
        seen = {}

        def runner(*, executable, argv, cwd, prompt, timeout):
            seen["argv"] = list(argv)
            seen["timeout"] = timeout
            return 1, _timed_out(), "native timed out"

        budget = tick_budget.TickBudget(seconds=60, clock=lambda: 0.0)
        transport = AgentFleetArgvTransport(
            run_callable=runner, expected_identity=("grok", "grok-4.6"))
        with budget.activate():
            with self.assertRaises(tick_budget.TickBudgetExceeded) as cm:
                transport.invoke(
                    spec=_spec(["--timeout", "60", "--agent", "grok",
                                "--model", "grok-4.6"]),
                    cwd="ws", prompt="hello", run_config_path="rc",
                    state_dir="state", timeout=120)

        self.assertIs(getattr(cm.exception, "_caretaker_budget_clipped_timeout", False), True)
        self.assertEqual(_timeouts(seen["argv"]), ["45"])
        self.assertEqual(seen["timeout"], 55)

    def test_task_file_is_utf8_and_assessment_keeps_single_timeout(self):
        prompt = "zażółć \u2014 publikacja"
        seen, env = self._invoke(
            timeout=120,
            prompt=prompt,
            spec_extra={"data_classification": "public", "fleet_role": "reviewer"},
        )
        self.assertEqual(seen["task_bytes"], prompt.encode("utf-8"))
        self.assertEqual(seen["prompt"], "")
        self.assertEqual(_timeouts(seen["argv"]), ["110"])
        self.assertIn("--assessment-file", seen["argv"])
        self.assertEqual(env.session_id, "attempt-1")

    def test_default_fleet_spawn_sets_no_console(self):
        with patch.dict(os.environ, {"HUB_CARETAKER_ALLOW_EXEC": "1"}):
            with patch("caretaker.transports.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=b"{}", stderr=b"")
                _fleet_default_run(
                    executable="python.exe",
                    argv=["fleet.py", "run"],
                    cwd=".",
                    prompt="",
                    timeout=40,
                )
        flags = run.call_args.kwargs["creationflags"]
        self.assertEqual(flags & subprocess.CREATE_NO_WINDOW,
                         subprocess.CREATE_NO_WINDOW)


if __name__ == "__main__":
    unittest.main()
