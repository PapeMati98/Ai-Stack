"""Owned MiniMax MCP stops on a native failure envelope without becoming success.

HD-0344ec: limit_exceeded and the exact structured-output rejection are terminal
for the invocation-owned job only. A boolean True remains a valid handoff.
The structured-output rejection accepts native null or omitted output.
"""
import ctypes
import json
import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

import fleet
import process_supervision as supervisor


def model():
    return {
        "providerId": "minimax",
        "modelId": "MiniMax-M3",
        "providerKind": "minimax-managed",
        "providerSource": "provider",
        "structuredOutputMode": "native_strict",
    }


def limit_exceeded():
    return {
        "schemaVersion": 1,
        "type": "exec.result",
        "status": "limit_exceeded",
        "runId": "synthetic-run",
        "sessionId": "synthetic-session",
        "turnId": "synthetic-turn",
        "durationMs": 1,
        "model": model(),
    }


def structured_failure():
    return {
        "schemaVersion": 1,
        "type": "exec.result",
        "status": "failed",
        "runId": "synthetic-run",
        "sessionId": "synthetic-session",
        "turnId": "synthetic-turn",
        "output": None,
        "error": {
            "category": "runtime",
            "code": "STRUCTURED_OUTPUT_INVALID",
            "message": "Structured output was not valid JSON.",
            "retryable": True,
        },
        "model": model(),
        "durationMs": 1,
    }


def success_line():
    handoff = {
        "status": "completed",
        "summary": "Synthetic terminal contract checked",
        "files_changed": [],
        "verification": ["exact terminal fixture"],
        "risks": [],
        "next_steps": [],
    }
    body = limit_exceeded()
    body["status"] = "succeeded"
    body["output"] = handoff
    return json.dumps(body)


def failure_exit(line):
    outcome = fleet.minimax_terminal_completion(line)
    if outcome is True or outcome is False or outcome is None:
        return None
    code = getattr(outcome, "exit_code", None)
    if type(code) is not int or isinstance(code, bool):
        return None
    return code


def scratch():
    root = Path(os.environ.get("TEAM_TEST_ROOT") or os.environ.get("TEMP") or ".")
    path = root / ("minimax-terminal-" + uuid.uuid4().hex)
    path.mkdir(parents=True)
    return path


def run_code(code, **kwargs):
    timeout = kwargs.pop("timeout", 4)
    completion = kwargs.pop("terminal_completion", fleet.minimax_terminal_completion)
    return supervisor.run(
        [sys.executable, "-u", "-c", code],
        cwd=Path(__file__).resolve().parent,
        env=dict(os.environ),
        stdin_text=None,
        timeout=timeout,
        terminal_completion=completion,
        **kwargs,
    )


def hang_code(line, pid_path):
    return (
        "import os,subprocess,sys,time\n"
        "flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],creationflags=flags)\n"
        f"open({str(pid_path)!r},'w',encoding='ascii').write(str(child.pid))\n"
        f"print({line!r},flush=True)\n"
        "time.sleep(60)\n"
    )


def assert_stopped(testcase, result, exit_code):
    testcase.assertEqual(result["exit_code"], exit_code)
    testcase.assertTrue(result["protocol_terminal_completed"])
    testcase.assertFalse(result["timed_out"])
    testcase.assertFalse(result["supervision_failed"])
    testcase.assertLess(result["execution_seconds"], 3.5)


def owned_pid(pid_path):
    if not pid_path.is_file():
        return None
    text = pid_path.read_text(encoding="ascii").strip()
    return int(text) if text else None


def process_still_active(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == 259
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def stop_pid(pid):
    if not pid or not process_still_active(pid):
        return
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        check=False,
    )


class MiniMaxTerminalFailurePredicateTests(unittest.TestCase):
    def test_success_handoff_stays_boolean_true(self):
        self.assertIs(fleet.minimax_terminal_completion(success_line()), True)

    def test_native_failures_expose_exit_codes_and_stay_falsy(self):
        limit = json.dumps(limit_exceeded())
        failed = json.dumps(structured_failure())
        self.assertFalse(fleet.minimax_terminal_completion(limit))
        self.assertFalse(fleet.minimax_terminal_completion(failed))
        self.assertEqual(failure_exit(limit), 7)
        self.assertEqual(failure_exit(failed), 4)

    def test_malformed_foreign_prose_duplicates_and_non_native_failures_are_not_terminal(self):
        limit = limit_exceeded()
        failed = structured_failure()
        samples = [
            None,
            "",
            "limit_exceeded",
            "mcode exec limit_exceeded: The run reached its step limit.",
            json.dumps({"type": "assistant", "text": json.dumps(limit)}),
            json.dumps(limit)[:-3],
            json.dumps(limit).replace('"schemaVersion": 1', '"schemaVersion": 1, "schemaVersion": 1'),
            json.dumps(failed).replace('"status": "failed"', '"status": "failed", "status": "limit_exceeded"'),
            json.dumps(limit).replace('"durationMs": 1', '"durationMs": NaN'),
            "INFO " + json.dumps(limit),
        ]
        for key, value in (
            ("schemaVersion", True),
            ("schemaVersion", 2),
            ("type", "exec.progress"),
            ("status", "failed"),
            ("model", {}),
        ):
            item = limit_exceeded()
            item[key] = value
            samples.append(json.dumps(item))
        for key, value in (
            ("providerId", "foreign"),
            ("modelId", "foreign"),
            ("providerSource", "output"),
            ("providerKind", "foreign"),
        ):
            item = limit_exceeded()
            item["model"][key] = value
            samples.append(json.dumps(item))
        nonempty = structured_failure()
        nonempty["output"] = "not-json"
        samples.append(json.dumps(nonempty))
        wrong = structured_failure()
        wrong["error"]["code"] = "RUNTIME_ERROR"
        samples.append(json.dumps(wrong))
        for text in samples:
            with self.subTest(text=text):
                outcome = fleet.minimax_terminal_completion(text)
                self.assertFalse(outcome)
                self.assertIsNone(failure_exit(text))

    def test_omitted_output_native_failure_exposes_exit_code_4(self):
        omitted = structured_failure()
        del omitted["output"]
        line = json.dumps(omitted)
        outcome = fleet.minimax_terminal_completion(line)
        self.assertIsInstance(outcome, supervisor.TerminalFailureOutcome)
        self.assertEqual(outcome.exit_code, 4)
        self.assertEqual(failure_exit(line), 4)


class MiniMaxTerminalFailureProcessTests(unittest.TestCase):
    def test_limit_exceeded_stops_owned_tree_with_exit_7_and_spares_unrelated_process(self):
        directory = scratch()
        pid_path = directory / "owned.txt"
        unrelated = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        owned = None
        try:
            self.assertIsNone(unrelated.poll())
            result = run_code(hang_code(json.dumps(limit_exceeded()), pid_path))
            assert_stopped(self, result, 7)
            self.assertIsNone(unrelated.poll())
            owned = owned_pid(pid_path)
            self.assertIsNotNone(owned)
            self.assertFalse(process_still_active(owned))
        finally:
            stop_pid(owned_pid(pid_path))
            unrelated.terminate()
            unrelated.wait(timeout=5)

    def test_structured_failure_stops_owned_tree_with_exit_4(self):
        directory = scratch()
        pid_path = directory / "owned.txt"
        try:
            result = run_code(hang_code(json.dumps(structured_failure()), pid_path))
            assert_stopped(self, result, 4)
            owned = owned_pid(pid_path)
            self.assertIsNotNone(owned)
            self.assertFalse(process_still_active(owned))
        finally:
            stop_pid(owned_pid(pid_path))

    def test_late_native_nonzero_is_preserved(self):
        limit = (
            "import sys\n"
            f"print({json.dumps(limit_exceeded())!r},flush=True)\n"
            "sys.exit(9)\n"
        )
        result = run_code(limit)
        assert_stopped(self, result, 9)
        self.assertEqual(result["native_exit_before_cleanup"], 9)
        failed = (
            "import sys\n"
            f"print({json.dumps(structured_failure())!r},flush=True)\n"
            "sys.exit(5)\n"
        )
        result = run_code(failed)
        assert_stopped(self, result, 5)
        self.assertEqual(result["native_exit_before_cleanup"], 5)

    def test_native_zero_does_not_hide_the_typed_failure(self):
        code = (
            "import sys\n"
            f"print({json.dumps(limit_exceeded())!r},flush=True)\n"
            "sys.exit(0)\n"
        )
        result = run_code(code)
        assert_stopped(self, result, 7)
        self.assertEqual(result["native_exit_before_cleanup"], 0)
        code = (
            "import sys\n"
            f"print({json.dumps(structured_failure())!r},flush=True)\n"
            "sys.exit(0)\n"
        )
        result = run_code(code)
        assert_stopped(self, result, 4)
        self.assertEqual(result["native_exit_before_cleanup"], 0)

    def test_first_failure_exit_wins_over_a_later_envelope(self):
        directory = scratch()
        pid_path = directory / "owned.txt"
        code = (
            "import os,subprocess,sys,time\n"
            "flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0\n"
            "subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],creationflags=flags)\n"
            f"print({json.dumps(limit_exceeded())!r},flush=True)\n"
            f"print({json.dumps(structured_failure())!r},flush=True)\n"
            "time.sleep(60)\n"
        )
        try:
            result = run_code(code)
            assert_stopped(self, result, 7)
        finally:
            stop_pid(owned_pid(pid_path))

    def test_tool_and_context_guards_beat_typed_failure(self):
        line = json.dumps(limit_exceeded())
        code = f"import time\nprint({line!r},flush=True)\ntime.sleep(60)\n"
        tool = run_code(code, tool_limit=1, tool_events=lambda _line: ["one", "two"])
        self.assertEqual(tool["exit_code"], 125)
        self.assertTrue(tool["tool_budget_exceeded"])
        self.assertFalse(tool["timed_out"])
        self.assertLess(tool["execution_seconds"], 3.5)
        context = run_code(code, context_limit=10, context_tokens=lambda _line: 11)
        self.assertEqual(context["exit_code"], 127)
        self.assertTrue(context["context_budget_exceeded"])
        self.assertFalse(context["timed_out"])
        self.assertLess(context["execution_seconds"], 3.5)

    def test_supervision_failure_beats_typed_failure(self):
        import grok_acp
        from unittest import mock

        code = f"import time\nprint({json.dumps(structured_failure())!r},flush=True)\ntime.sleep(60)\n"
        with mock.patch.object(grok_acp._WindowsJob, "close", side_effect=RuntimeError("synthetic")):
            result = run_code(code)
        self.assertEqual(result["exit_code"], 126)
        self.assertTrue(result["supervision_failed"])
        self.assertFalse(result["timed_out"])

    def test_bad_lines_leave_the_owned_process_on_the_timeout_guard(self):
        failed = structured_failure()
        foreign = limit_exceeded()
        foreign["model"]["modelId"] = "foreign"
        lines = [
            "limit_exceeded",
            "mcode exec limit_exceeded: The run reached its step limit.",
            json.dumps({"type": "assistant", "text": json.dumps(limit_exceeded())}),
            json.dumps(limit_exceeded())[:-3],
            json.dumps(limit_exceeded()).replace(
                '"schemaVersion": 1', '"schemaVersion": 1, "schemaVersion": 1'
            ),
            json.dumps(foreign),
            json.dumps(failed).replace('"output": null', '"output": "text"'),
        ]
        code = "import time\n" + "".join(f"print({line!r},flush=True)\n" for line in lines) + "time.sleep(60)\n"
        result = run_code(code, timeout=1)
        self.assertEqual(result["exit_code"], 124)
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["protocol_terminal_completed"])

    def test_boolean_callback_still_means_success_or_not_terminal(self):
        directory = scratch()
        pid_path = directory / "owned.txt"
        try:
            stopped = run_code(hang_code("ordinary progress", pid_path), terminal_completion=lambda _line: True)
            assert_stopped(self, stopped, 0)
            self.assertIsNone(stopped["native_exit_before_cleanup"])
        finally:
            stop_pid(owned_pid(pid_path))
        waiting = run_code(
            "import time\nprint('ordinary progress',flush=True)\ntime.sleep(60)\n",
            timeout=1,
            terminal_completion=lambda _line: False,
        )
        self.assertEqual(waiting["exit_code"], 124)
        self.assertTrue(waiting["timed_out"])
        self.assertFalse(waiting["protocol_terminal_completed"])

    def test_typed_failure_callback_is_not_treated_as_boolean_success(self):
        outcome_type = getattr(supervisor, "TerminalFailureOutcome", None)
        if outcome_type is None:
            self.fail("TerminalFailureOutcome is required for a native failure stop")
        directory = scratch()
        pid_path = directory / "owned.txt"
        try:
            result = run_code(
                hang_code(json.dumps(limit_exceeded()), pid_path),
                terminal_completion=lambda _line: outcome_type(7),
            )
            assert_stopped(self, result, 7)
            self.assertFalse(result["protocol_terminal_completed"] and result["exit_code"] == 0)
        finally:
            stop_pid(owned_pid(pid_path))

    def test_omitted_output_native_failure_stops_owned_tree_with_exit_4(self):
        directory = scratch()
        pid_path = directory / "owned.txt"
        omitted = structured_failure()
        del omitted["output"]
        try:
            result = run_code(hang_code(json.dumps(omitted), pid_path))
            assert_stopped(self, result, 4)
            self.assertFalse(result["protocol_terminal_completed"] and result["exit_code"] == 0)
        finally:
            stop_pid(owned_pid(pid_path))


if __name__ == "__main__":
    unittest.main()
