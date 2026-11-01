"""UserPromptSubmit STOP gate: a self-pull prompt never reaches the model while STOP is active.

Unit cases drive decide()/main() with a fake runner; integration cases run the real hook
script as the harness would (JSON on stdin, exit code) against the real Office CLI of this
checkout on a synthetic coord home.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from support import PACKAGE_ROOT

sys.path.insert(0, str(PACKAGE_ROOT / "hooks"))

import office_wake_stop_gate as gate  # noqa: E402
from coordfs.wake import Wake  # noqa: E402

HOOK = PACKAGE_ROOT / "hooks" / "office_wake_stop_gate.py"
OFFICE = PACKAGE_ROOT / "office.py"
TOKEN = ("0123" + "4567" + "89ab" + "cdef") * 2
SELF_PULL = Wake._pull_prompt("lead-pull2-88900f5b", TOKEN)  # the exact template Wake issues


def hook_input(prompt: str, session: str = "session-a") -> bytes:
    return json.dumps({"session_id": session, "hook_event_name": "UserPromptSubmit",
                       "cwd": "C:/work", "prompt": prompt}).encode("utf-8")


def completed(payload: object, code: int = 0) -> tuple[int, bytes]:
    return code, json.dumps(payload).encode()


class FakeRunner:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.calls: list[list[str]] = []
        self.result, self.error = result, error

    def __call__(self, command):
        self.calls.append(list(command))
        if self.error is not None:
            raise self.error
        return self.result


def forbidden_runner(command):
    raise AssertionError(f"an ordinary prompt must not start a process: {command}")


class StopGateUnitTests(unittest.TestCase):
    command = [sys.executable, "-I", str(OFFICE), "wake-stop-status"]

    def decide(self, prompt: str, runner) -> tuple[int, str]:
        return gate.decide(hook_input(prompt), self.command, runner)

    def test_template_matches_what_wake_issues(self) -> None:
        self.assertEqual(gate.self_pull_binding(SELF_PULL), "lead-pull2-88900f5b")

    def test_ordinary_and_lookalike_prompts_pass_without_any_process(self) -> None:
        for prompt in ("Kontynuuj prace nad raportem",
                       "[Office Wake self-pull] ale napisane recznie",
                       "Cytat: " + SELF_PULL,  # not at the start
                       SELF_PULL.replace(TOKEN, "not-a-token")):
            with self.subTest(prompt=prompt[:40]):
                self.assertEqual(self.decide(prompt, forbidden_runner), (0, ""))
        via_main = gate.main(["--office", str(OFFICE)], stdin=io.BytesIO(hook_input("zwykly prompt")),
                             runner=forbidden_runner)
        self.assertEqual(via_main, 0)

    def test_stop_inactive_lets_the_self_pull_run(self) -> None:
        runner = FakeRunner(completed({"status": "ok", "code": "wake_stop_status", "stop_active": False}))
        self.assertEqual(self.decide(SELF_PULL, runner), (0, ""))
        self.assertEqual(runner.calls, [self.command])

    def test_stop_active_blocks_before_the_model(self) -> None:
        runner = FakeRunner(completed({"status": "ok", "stop_active": True, "since": "2026-09-28T07:00:00Z"}))
        code, message = self.decide(SELF_PULL, runner)
        self.assertEqual(code, 2)
        self.assertIn("lead-pull2-88900f5b", message)
        self.assertIn("STOP jest aktywny", message)

    def test_unconfirmed_stop_state_fails_closed(self) -> None:
        cases = {
            "process_error": FakeRunner(error=OSError("no such file")),
            "timeout": FakeRunner(error=subprocess.TimeoutExpired(["office"], 12)),
            "not_json": FakeRunner((0, b"garbage")),
            "nonzero_exit_with_ok_json": FakeRunner(completed({"status": "ok", "stop_active": False}, 3)),
            "office_error": FakeRunner(completed({"status": "error", "stop_active": None}, 1)),
            "no_boolean": FakeRunner(completed({"status": "ok", "stop_active": "yes"})),
        }
        for name, runner in cases.items():
            with self.subTest(case=name):
                code, message = self.decide(SELF_PULL, runner)
                self.assertEqual(code, 2)
                self.assertIn("nie da sie potwierdzic", message)

    # Review af9a1856 (Codex): one regression test per finding. ------------------------
    def test_a_binding_id_up_to_wakes_128_characters_is_still_checked(self) -> None:
        long_id = "b" * 128
        prompt = Wake._pull_prompt(long_id, TOKEN)
        runner = FakeRunner(completed({"status": "ok", "stop_active": True}))
        self.assertEqual(self.decide(prompt, runner)[0], 2)
        self.assertEqual(len(runner.calls), 1)

    def test_a_prompt_that_extends_the_template_is_an_ordinary_prompt(self) -> None:
        for prompt in (SELF_PULL + " Wyjasnij ten cytat.",
                       SELF_PULL.split("` (albo")[0] + "` i nic wiecej"):
            with self.subTest(prompt=prompt[-30:]):
                self.assertEqual(self.decide(prompt, forbidden_runner), (0, ""))

    def test_json_escaped_marker_is_still_recognized(self) -> None:
        escaped = hook_input(SELF_PULL).replace(b"[Office Wake", b"\\u005bOffice Wake")
        self.assertNotIn(b"[Office Wake", escaped)
        runner = FakeRunner(completed({"status": "ok", "stop_active": True}))
        self.assertEqual(gate.decide(escaped, self.command, runner)[0], 2)

    def test_status_output_beyond_its_bound_fails_closed_without_buffering_it(self) -> None:
        flood = [sys.executable, "-c", "import sys; sys.stdout.write('x' * 200000); sys.stderr.write('y' * 200000)"]
        self.assertIsNone(gate.stop_state(flood))

    def test_office_cmd_is_invoked_directly_with_constant_arguments(self) -> None:
        office = PACKAGE_ROOT / "fixture path" / "office.cmd"
        self.assertTrue(office.is_absolute())
        self.assertEqual(gate.status_command(office, None),
                         [str(office), "wake-stop-status"])

    # Recheck fb553bbc (Codex). ---------------------------------------------------------
    def test_without_office_code_nothing_is_guessed_from_a_prefix(self) -> None:
        original = gate._issued_prompt
        gate._issued_prompt = lambda binding, token: None
        try:
            for prompt in (SELF_PULL.split("` (albo")[0] + "` Wyjasnij ten cytat", SELF_PULL):
                with self.subTest(prompt=prompt[-25:]):
                    self.assertEqual(self.decide(prompt, forbidden_runner), (0, ""))
        finally:
            gate._issued_prompt = original

    def test_padding_the_hook_input_beyond_a_megabyte_does_not_bypass_the_check(self) -> None:
        padded = hook_input(SELF_PULL)[:-1] + b" " * (2 * 1024 * 1024) + b"}"
        runner = FakeRunner(completed({"status": "ok", "stop_active": True}))
        code = gate.main(["--office", str(OFFICE)], stdin=io.BytesIO(padded), runner=runner)
        self.assertEqual((code, len(runner.calls)), (2, 1))

    def test_malformed_hook_input_is_never_blocked(self) -> None:
        self.assertEqual(gate.decide(b"\xff\xfe not json", self.command, forbidden_runner), (0, ""))
        self.assertEqual(gate.decide(b'["list"]', self.command, forbidden_runner), (0, ""))

    def test_office_path_must_be_absolute_but_only_matters_for_self_pull(self) -> None:
        relative = ["--office", "office.cmd"]
        self.assertEqual(gate.main(relative, stdin=io.BytesIO(hook_input("zwykly")), runner=forbidden_runner), 0)
        self.assertEqual(gate.main(relative, stdin=io.BytesIO(hook_input(SELF_PULL)), runner=forbidden_runner), 2)


class StopGateIntegrationTests(unittest.TestCase):
    """The real hook script, as the harness runs it, against the real Office CLI."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "coord"
        self.home.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_hook(self, prompt: str, office: Path = OFFICE) -> tuple[int, str, float]:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        started = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-I", str(HOOK), "--office", str(office), "--home", str(self.home)],
            input=hook_input(prompt), capture_output=True, timeout=60, env=env, check=False,
        )
        return result.returncode, result.stderr.decode("utf-8", "replace"), time.monotonic() - started

    def snapshot(self) -> dict[str, tuple[int, int]]:
        return {str(p.relative_to(self.home)): (p.stat().st_size, p.stat().st_mtime_ns)
                for p in self.home.rglob("*") if p.is_file()}

    def test_real_stop_blocks_the_self_pull_and_start_releases_it(self) -> None:
        wake = Wake(home=self.home)
        self.assertEqual(self.run_hook(SELF_PULL)[0], 0)  # no STOP yet
        wake.stop()
        code, message, _ = self.run_hook(SELF_PULL)
        self.assertEqual(code, 2)
        self.assertIn("STOP jest aktywny", message)
        wake.start()
        self.assertEqual(self.run_hook(SELF_PULL)[0], 0)

    @unittest.skipUnless(sys.platform == "win32", "office.cmd is the Windows wrapper")
    def test_a_real_cmd_wrapper_route_is_honored(self) -> None:
        wrapper = Path(self._tmp.name) / "runtime dir" / "office.cmd"
        wrapper.parent.mkdir()
        wrapper.write_text(f'@echo off\r\n"{sys.executable}" -I "{OFFICE}" %*\r\nexit /b %ERRORLEVEL%\r\n',
                           encoding="utf-8")
        Wake(home=self.home).stop()
        code, message, _ = self.run_hook(SELF_PULL, office=wrapper)
        self.assertEqual(code, 2)
        self.assertIn("STOP jest aktywny", message)
        Wake(home=self.home).start()
        self.assertEqual(self.run_hook(SELF_PULL, office=wrapper)[0], 0)

    def test_the_gate_writes_nothing_and_ordinary_prompts_stay_independent(self) -> None:
        wake = Wake(home=self.home)
        wake.stop()
        before = self.snapshot()
        blocked = self.run_hook(SELF_PULL)
        ordinary = self.run_hook("Zwykly prompt innej sesji w tym samym czasie")
        self.assertEqual(blocked[0], 2)
        self.assertEqual((ordinary[0], ordinary[1]), (0, ""))  # STOP does not touch normal prompts
        self.assertEqual(self.snapshot(), before)  # read-only: no state of any session changed


if __name__ == "__main__":
    unittest.main()
