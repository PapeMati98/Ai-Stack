"""Tests for Codex native patch write failure mitigation (HD-3d461b)."""
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


class TestCodexPatchFallbackPrompt(unittest.TestCase):
    schema = {"type": "object"}

    def _get_prompt(self, adapter_id="codex", mode="implement", **extra):
        return fleet.build_worker_prompt(
            "Fix one scoped file.",
            agent_id=adapter_id,
            adapter_id=adapter_id,
            mode=mode,
            workspace=Path("C:/work/project"),
            kind="implementation",
            complexity="low",
            native_subagent_limit=0,
            enable_web=False,
            knowledge_snapshot=None,
            schema=self.schema,
            **extra,
        )

    def test_implement_mode_emits_patch_fallback_recipe_and_rules(self):
        prompt = self._get_prompt("codex", mode="implement")
        self.assertIn("Codex Windows sandbox rules:", prompt)
        self.assertIn("Native patch fallback:", prompt)
        self.assertIn("only AFTER ONE generic native apply_patch", prompt)
        self.assertIn("`Failed to write file`", prompt)
        self.assertIn("at most ONE exact precondition-checked byte replacement", prompt)
        self.assertIn("EXPECTED_HEX", prompt)
        self.assertIn("REPLACEMENT_HEX", prompt)
        self.assertIn("SCOPED_FILE", prompt)
        self.assertIn("bytes.fromhex", prompt)
        self.assertIn("login=false", prompt)
        self.assertIn("type SCOPED_FILE", prompt)
        self.assertIn("No write occurs if expected bytes differ", prompt)

        recipes = [
            x
            for x in re.findall(r"`([^`]+)`", prompt)
            if "EXPECTED_HEX" in x and "REPLACEMENT_HEX" in x
        ]
        self.assertEqual(len(recipes), 1, "Expected exactly one patch fallback recipe in backticks")
        recipe = recipes[0]
        self.assertIn("-I -S -c ", recipe)
        parts = recipe.split(" -c ")
        self.assertEqual(len(parts), 2)
        code_token = parts[1]
        self.assertNotIn(" ", code_token, "Python -c program must be a single compact token without spaces")
        self.assertNotIn('"', code_token, "Python -c program must not contain double quotes")

    def test_negatives_review_and_advisory_modes_do_not_emit_recipe(self):
        for mode in ("review", "advisory"):
            with self.subTest(mode=mode):
                prompt = self._get_prompt("codex", mode=mode)
                self.assertNotIn("Native patch fallback:", prompt)
                self.assertNotIn("Failed to write file", prompt)
                self.assertNotIn("EXPECTED_HEX", prompt)
                self.assertNotIn("REPLACEMENT_HEX", prompt)

    def test_negatives_other_adapters_do_not_emit_recipe(self):
        for adapter in (
            "grok",
            "claude",
            "antigravity",
            "minimax",
            "qwen",
            "mistral",
            "opencode",
            "openclaw",
        ):
            with self.subTest(adapter=adapter):
                prompt = self._get_prompt(adapter, mode="implement")
                self.assertNotIn("Native patch fallback:", prompt)
                self.assertNotIn("Failed to write file", prompt)
                self.assertNotIn("EXPECTED_HEX", prompt)
                self.assertNotIn("REPLACEMENT_HEX", prompt)

    def test_missing_and_spaced_interpreter_negatives(self):
        bad_interpreters = (
            "",
            None,
            "   ",
            r"C:\Program Files\Python\python.exe",
            "python with spaces.exe",
            'python"quote.exe',
            "python\t.exe",
        )
        for bad_interp in bad_interpreters:
            with self.subTest(interpreter=bad_interp):
                with mock.patch.object(sys, "executable", bad_interp):
                    prompt = self._get_prompt("codex", mode="implement")
                    self.assertNotIn(
                        "Native patch fallback:",
                        prompt,
                        "Should not emit patch fallback when interpreter is absent/spaced",
                    )
                    self.assertNotIn("EXPECTED_HEX", prompt)
                    self.assertNotIn("REPLACEMENT_HEX", prompt)
                    self.assertNotIn("Failed to write file", prompt)

    def test_dll_and_boundary_rules_in_prompt(self):
        prompt = self._get_prompt("codex", mode="implement")
        self.assertIn("STATUS_DLL_INIT_FAILED (0xC0000142)", prompt)
        self.assertIn("DLL load failed while importing", prompt)
        self.assertIn("stop on the first such failure", prompt)
        self.assertIn("remain PARTIAL with no retry", prompt)
        self.assertIn("only AFTER ONE generic native apply_patch", prompt)
        self.assertIn("at most ONE exact precondition-checked byte replacement", prompt)


class TestCodexPatchFallbackExecution(unittest.TestCase):
    def setUp(self):
        root = os.environ.get("TEAM_TEST_ROOT") or tempfile.gettempdir()
        self.fixture_dir = (Path(root).resolve() / "codex patch fallback fixture dir").resolve()
        self.fixture_dir.mkdir(parents=True, exist_ok=True)
        self.fixture = self.fixture_dir / "test_codex_patch_source.py"
        self.fixture_bytes = b"# Synthetic source fixture for codex patch test\r\nanswer = 42\r\n"
        self.fixture.write_bytes(self.fixture_bytes)
        self.expected_hash = hashlib.sha256(self.fixture_bytes).hexdigest()

    def _get_codex_implement_prompt(self):
        return fleet.build_worker_prompt(
            "Fix scoped file.",
            agent_id="codex",
            adapter_id="codex",
            mode="implement",
            workspace=self.fixture_dir,
            kind="implementation",
            complexity="low",
            native_subagent_limit=0,
            enable_web=False,
            knowledge_snapshot=None,
            schema={"type": "object"},
        )

    def test_cmd_argv_valid_byte_replacement_in_dir_with_spaces(self):
        self.assertIn(" ", str(self.fixture_dir), "Fixture directory path must contain spaces")
        prompt = self._get_codex_implement_prompt()
        recipes = [
            x
            for x in re.findall(r"`([^`]+)`", prompt)
            if "EXPECTED_HEX" in x and "REPLACEMENT_HEX" in x
        ]
        self.assertTrue(recipes, "No patch fallback recipe found in Codex implement prompt")
        recipe = recipes[0]

        new_bytes = b"# Updated fixture via byte replacement\r\nanswer = 100\r\n"
        command = (
            recipe.replace("SCOPED_FILE", self.fixture.name)
            .replace("EXPECTED_HEX", self.fixture_bytes.hex())
            .replace("REPLACEMENT_HEX", new_bytes.hex())
        )
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
        self.assertEqual(self.fixture.read_bytes(), new_bytes)
        new_hash = hashlib.sha256(self.fixture.read_bytes()).hexdigest()
        self.assertEqual(new_hash, hashlib.sha256(new_bytes).hexdigest())

    def test_cmd_argv_mismatched_expected_bytes_does_not_mutate_and_leaves_hash_unchanged(self):
        self.assertIn(" ", str(self.fixture_dir), "Fixture directory path must contain spaces")
        self.fixture.write_bytes(self.fixture_bytes)
        prompt = self._get_codex_implement_prompt()
        recipes = [
            x
            for x in re.findall(r"`([^`]+)`", prompt)
            if "EXPECTED_HEX" in x and "REPLACEMENT_HEX" in x
        ]
        self.assertTrue(recipes, "No patch fallback recipe found in Codex implement prompt")
        recipe = recipes[0]

        mismatched_expected = b"# Different bytes that will not match fixture\r\n".hex()
        unwanted_replacement = b"# This replacement should NEVER be written\r\n".hex()
        command = (
            recipe.replace("SCOPED_FILE", self.fixture.name)
            .replace("EXPECTED_HEX", mismatched_expected)
            .replace("REPLACEMENT_HEX", unwanted_replacement)
        )
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
        self.assertNotEqual(
            result.returncode,
            0,
            "Precondition mismatch must exit nonzero via assertion failure",
        )
        self.assertEqual(
            self.fixture.read_bytes(),
            self.fixture_bytes,
            "File must remain untouched when expected bytes differ",
        )
        current_hash = hashlib.sha256(self.fixture.read_bytes()).hexdigest()
        self.assertEqual(current_hash, self.expected_hash, "Hash must be unchanged after precondition mismatch")


if __name__ == "__main__":
    unittest.main()
