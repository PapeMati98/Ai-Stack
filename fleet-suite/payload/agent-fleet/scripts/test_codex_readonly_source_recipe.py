"""Tests for Codex read-only source inspection recipe (HD-9dbf6e)."""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import fleet


class TestCodexReadonlySourceRecipe(unittest.TestCase):
    def setUp(self):
        root = os.environ.get("TEAM_TEST_ROOT") or tempfile.gettempdir()
        self.fixture_dir = Path(root).resolve()
        self.fixture_dir.mkdir(parents=True, exist_ok=True)
        self.fixture = self.fixture_dir / "test_codex_recipe_source.py"
        self.fixture_bytes = b"# Synthetic source fixture for codex recipe test\r\nanswer = 42\r\n"
        self.fixture.write_bytes(self.fixture_bytes)
        self.expected_hash = hashlib.sha256(self.fixture.read_bytes()).hexdigest()

    def _get_codex_prompt(self):
        return fleet.build_worker_prompt(
            "Inspect scoped source.",
            agent_id="codex",
            adapter_id="codex",
            mode="review",
            workspace=self.fixture_dir,
            kind="review",
            complexity="low",
            native_subagent_limit=0,
            enable_web=False,
            knowledge_snapshot=None,
            schema={"type": "object"},
        )

    def test_compact_hash_command_survives_cmd_argv(self):
        prompt = self._get_codex_prompt()
        recipes = [x for x in re.findall(r"`([^`]+)`", prompt) if "-I -S -c " in x]
        self.assertTrue(recipes, "No compact hash recipe found in Codex prompt")
        command = recipes[0].replace("SCOPED_FILE", self.fixture.name)
        cmd_exe = r"C:\Windows\System32\cmd.exe"
        if not os.path.isfile(cmd_exe):
            cmd_exe = "cmd.exe"
        result = subprocess.run(
            [cmd_exe, "/d", "/c", command],
            cwd=str(self.fixture_dir),
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(
            result.returncode,
            0,
            f"cmd execution failed: {result.stderr.decode('utf-8', errors='replace')}",
        )
        stdout = result.stdout.decode("utf-8", errors="replace")
        self.assertIn(self.expected_hash, stdout)

    def test_cmd_type_basename_reads_scoped_text(self):
        prompt = self._get_codex_prompt()
        type_recipes = [x for x in re.findall(r"`([^`]+)`", prompt) if x.startswith("type ")]
        self.assertTrue(type_recipes, "No cmd type recipe found in Codex prompt")
        command = type_recipes[0].replace("SCOPED_FILE", self.fixture.name)
        cmd_exe = r"C:\Windows\System32\cmd.exe"
        if not os.path.isfile(cmd_exe):
            cmd_exe = "cmd.exe"
        result = subprocess.run(
            [cmd_exe, "/d", "/c", command],
            cwd=str(self.fixture_dir),
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(
            result.returncode,
            0,
            f"type command failed: {result.stderr.decode('utf-8', errors='replace')}",
        )
        stdout_bytes = result.stdout
        self.assertIn(b"answer = 42", stdout_bytes)

    def test_mocked_absent_and_spaced_interpreter_fallback(self):
        for bad_interp in ("", None, r"C:\Program Files\Python\python.exe", "python with spaces.exe"):
            with self.subTest(interpreter=bad_interp):
                with mock.patch.object(sys, "executable", bad_interp):
                    prompt = self._get_codex_prompt()
                    hash_recipes = [x for x in re.findall(r"`([^`]+)`", prompt) if "-I -S -c " in x]
                    self.assertEqual(
                        hash_recipes,
                        [],
                        "Should not emit Python hash recipe when interpreter is absent/spaced",
                    )
                    self.assertIn("no unquoted no-space interpreter path is known", prompt)
                    self.assertIn("mark the hash for parent verification", prompt)
                    self.assertIn("type SCOPED_FILE", prompt)


if __name__ == "__main__":
    unittest.main()
