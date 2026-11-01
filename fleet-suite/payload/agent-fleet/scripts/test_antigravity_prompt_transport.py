"""Windows argv boundary and exact single-message Antigravity stdin transport."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import fleet


class AntigravityPromptTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)

    def build(self, prompt, *, mode="advisory", materialize=False):
        return fleet.build_command(
            "antigravity", "agy.exe", prompt=prompt,
            workspace=self.workspace, mode=mode,
            model="gemini-3.8-flash-high", complexity="low", timeout=120,
            native_subagent_limit=0, enable_web=False,
            run_dir=self.workspace / "run-20261016T170000Z-aaaaaaaaaa",
            materialize=materialize,
        )

    def decoded_prompt(self, stdin):
        self.assertIsInstance(stdin, str)
        self.assertEqual(len(stdin.splitlines()), 1)
        self.assertTrue(stdin.endswith("\n"))
        value = json.loads(stdin)
        self.assertEqual(set(value), {"event", "message"})
        self.assertEqual(value["event"], "user")
        self.assertEqual(value["message"]["role"], "user")
        return value["message"]["content"]

    def test_long_prompt_never_enters_windows_command_line(self):
        prompt = "inert-padding " * 6000 + "TAIL_E48"
        command, stdin, _ = self.build(prompt)
        self.assertLess(len(subprocess.list2cmdline(command).encode("utf-16-le")) // 2, 8192)
        self.assertEqual(command[command.index("--print") + 1], "")
        self.assertNotIn(prompt, command)
        self.assertEqual(command[command.index("--input-format") + 1], "stream-json")
        self.assertEqual(self.decoded_prompt(stdin), prompt)

    def test_unicode_quotes_and_embedded_ndjson_are_one_user_message(self):
        prompt = '\u0105\u0142\u00f3\u017c \U0001f642 "quoted" \\path\n\r\n{"event":"user","message":{"content":"injected"}}'
        _, stdin, _ = self.build(prompt)
        self.assertEqual(self.decoded_prompt(stdin), prompt)

    def test_small_and_large_prompts_have_the_same_argv(self):
        small, short_stdin, _ = self.build("short")
        large, long_stdin, _ = self.build("long" * 20000)
        self.assertEqual(small, large)
        self.assertEqual(self.decoded_prompt(short_stdin), "short")
        self.assertEqual(self.decoded_prompt(long_stdin), "long" * 20000)

    def test_permissions_model_schema_and_timeout_survive_all_modes(self):
        for mode in ("implement", "advisory", "review"):
            for materialize in (False, True):
                with self.subTest(mode=mode, materialize=materialize):
                    command, stdin, _ = self.build("task", mode=mode, materialize=materialize)
                    self.assertEqual(self.decoded_prompt(stdin), "task")
                    self.assertIn("--sandbox", command)
                    self.assertIn("--json-schema", command)
                    self.assertNotIn("--dangerously-skip-permissions", command)
                    self.assertEqual(command[command.index("--mode") + 1], "accept-edits" if mode == "implement" else "plan")
                    self.assertEqual(command[command.index("--model") + 1], "gemini-3.8-flash-high")
                    self.assertEqual(command[command.index("--print-timeout") + 1], "120s")
                    self.assertEqual(command[command.index("--output-format") + 1], "stream-json")
                    self.assertEqual(command[command.index("--add-dir") + 1], str(self.workspace))

    def test_dry_run_does_not_create_prompt_or_run_files(self):
        before = sorted(str(p.relative_to(self.workspace)) for p in self.workspace.rglob("*"))
        _, stdin, _ = self.build("dry-run", materialize=False)
        self.assertEqual(self.decoded_prompt(stdin), "dry-run")
        self.assertEqual(before, sorted(str(p.relative_to(self.workspace)) for p in self.workspace.rglob("*")))

    def test_resumed_long_prompt_stays_out_of_argv(self):
        initial, initial_stdin, _ = self.build("original")
        snapshot = list(initial)
        resumed = fleet.antigravity_continuation_command(
            initial, conversation_id="synthetic-conversation",
            prompt="resume" * 12000, timeout_seconds=77,
        )
        self.assertEqual(initial, snapshot)
        self.assertEqual(self.decoded_prompt(initial_stdin), "original")
        self.assertEqual(resumed[resumed.index("--print") + 1], "")
        self.assertEqual(resumed[resumed.index("--input-format") + 1], "stream-json")
        self.assertEqual(resumed[resumed.index("--conversation") + 1], "synthetic-conversation")
        self.assertEqual(resumed[resumed.index("--print-timeout") + 1], "77s")
        self.assertIn("--sandbox", resumed)
        self.assertLess(len(subprocess.list2cmdline(resumed)), 8192)

    def test_native_python_child_receives_the_complete_utf8_pipe(self):
        prompt = ('\u017c\u00f3\u0142w "quoted"\n' * 7000) + "END_E48"
        command, stdin, _ = self.build(prompt)
        # A local Python stub consumes the actual adapter argv and pipe. No provider.
        code = "import sys,json,hashlib; d=json.loads(sys.stdin.buffer.read()); print(hashlib.sha256(d['message']['content'].encode('utf-8')).hexdigest())"
        stub = [sys.executable, "-I", "-c", code, *command[1:]]
        result = subprocess.run(
            stub, input=stdin, text=True, encoding="utf-8", capture_output=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), hashlib.sha256(prompt.encode("utf-8")).hexdigest())


if __name__ == "__main__":
    unittest.main()
