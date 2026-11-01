"""Tests for Codex exact-byte fallback raw-byte acquisition (HD-3d461b)."""
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


class TestCodexRawBytesPrompt(unittest.TestCase):
    schema = {"type": "object"}

    def _get_prompt(self, adapter_id="codex", mode="implement", **extra):
        return fleet.build_worker_prompt(
            "Fix scoped file.",
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

    def test_implement_mode_emits_raw_read_and_write_recipes(self):
        prompt = self._get_prompt("codex", mode="implement")
        self.assertIn("Codex Windows sandbox rules:", prompt)
        self.assertIn("Native patch fallback:", prompt)

        # Extract raw read recipe
        raw_recipes = [
            x
            for x in re.findall(r"`([^`]+)`", prompt)
            if "open('SCOPED_FILE','rb').read().hex()" in x
        ]
        self.assertEqual(len(raw_recipes), 1, "Expected exactly one raw read recipe in backticks")
        raw_recipe = raw_recipes[0]
        self.assertIn("-I -S -c ", raw_recipe)
        raw_parts = raw_recipe.split(" -c ")
        self.assertEqual(len(raw_parts), 2)
        self.assertNotIn(" ", raw_parts[1], "Raw read -c program must be a single compact token without spaces")
        self.assertNotIn('"', raw_parts[1], "Raw read -c program must not contain double quotes")

        # Extract write recipe
        write_recipes = [
            x
            for x in re.findall(r"`([^`]+)`", prompt)
            if "EXPECTED_HEX" in x and "REPLACEMENT_HEX" in x
        ]
        self.assertEqual(len(write_recipes), 1, "Expected exactly one write fallback recipe in backticks")
        write_recipe = write_recipes[0]
        self.assertIn("-I -S -c ", write_recipe)
        write_parts = write_recipe.split(" -c ")
        self.assertEqual(len(write_parts), 2)
        self.assertNotIn(" ", write_parts[1], "Write fallback -c program must be a single compact token without spaces")
        self.assertNotIn('"', write_parts[1], "Write fallback -c program must not contain double quotes")

    def test_mandatory_before_construction_and_no_inference_rules(self):
        prompt = self._get_prompt("codex", mode="implement")
        self.assertIn("AFTER ONE generic native apply_patch", prompt)
        self.assertIn("Require exactly one raw HEX read AFTER the one generic native patch failure", prompt)
        self.assertIn("BEFORE constructing EXPECTED_HEX or REPLACEMENT_HEX", prompt)
        self.assertIn("Derive both from that exact output", prompt)
        self.assertIn("preserve all unchanged bytes including CRLF/LF/BOM/nonASCII", prompt)
        self.assertIn("never infer bytes/line endings from rendered text", prompt)
        self.assertIn("No write occurs if expected bytes differ", prompt)
        self.assertIn("remain PARTIAL with no retry", prompt)

    def test_negatives_review_and_advisory_modes_do_not_emit_recipes(self):
        for mode in ("review", "advisory"):
            with self.subTest(mode=mode):
                prompt = self._get_prompt("codex", mode=mode)
                self.assertNotIn("Native patch fallback:", prompt)
                self.assertNotIn(".read().hex()", prompt)
                self.assertNotIn("EXPECTED_HEX", prompt)
                self.assertNotIn("REPLACEMENT_HEX", prompt)

    def test_negatives_other_adapters_do_not_emit_recipes(self):
        for adapter in (
            "grok",
            "claude",
            "antigravity",
            "minimax",
            "qwen",
            "mistral",
            "opencode",
            "openclaw",
            "mimo",
            "muse",
        ):
            with self.subTest(adapter=adapter):
                prompt = self._get_prompt(adapter, mode="implement")
                self.assertNotIn("Native patch fallback:", prompt)
                self.assertNotIn(".read().hex()", prompt)
                self.assertNotIn("EXPECTED_HEX", prompt)
                self.assertNotIn("REPLACEMENT_HEX", prompt)

    def test_negatives_missing_or_spaced_interpreter(self):
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
                    self.assertNotIn("Native patch fallback:", prompt)
                    self.assertNotIn(".read().hex()", prompt)
                    self.assertNotIn("EXPECTED_HEX", prompt)
                    self.assertNotIn("REPLACEMENT_HEX", prompt)


class TestCodexRawBytesExecution(unittest.TestCase):
    def setUp(self):
        root = os.environ.get("TEAM_TEST_ROOT") or tempfile.gettempdir()
        self.fixture_dir = (Path(root).resolve() / "codex raw bytes fixture dir").resolve()
        self.fixture_dir.mkdir(parents=True, exist_ok=True)
        self.cmd_exe = r"C:\Windows\System32\cmd.exe"
        if not os.path.isfile(self.cmd_exe):
            self.cmd_exe = "cmd.exe"

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

    def _extract_recipes(self):
        prompt = self._get_codex_implement_prompt()
        raw_recipes = [
            x
            for x in re.findall(r"`([^`]+)`", prompt)
            if "open('SCOPED_FILE','rb').read().hex()" in x
        ]
        self.assertEqual(len(raw_recipes), 1, "Expected exactly one raw read recipe in prompt")
        write_recipes = [
            x
            for x in re.findall(r"`([^`]+)`", prompt)
            if "EXPECTED_HEX" in x and "REPLACEMENT_HEX" in x
        ]
        self.assertEqual(len(write_recipes), 1, "Expected exactly one write recipe in prompt")
        return raw_recipes[0], write_recipes[0]

    def test_crlf_utf8_bom_nonascii_raw_read_and_fallback_write(self):
        self.assertIn(" ", str(self.fixture_dir), "Fixture directory path must contain spaces")
        raw_recipe, write_recipe = self._extract_recipes()

        fixture = self.fixture_dir / "fixture_crlf_bom_nonascii.py"
        bom = b"\xef\xbb\xbf"
        original_bytes = (
            bom
            + b"# Header with non-ASCII: Za\xc5\xbc\xc3\xb3\xc5\x82\xc4\x87 g\xc4\x99\xc5\x9bl\xc4\x85 ja\xc5\xba\xc5\x84\r\n"
            b"config_value = 100\r\n"
            b"footer_line = 'end'\r\n"
        )
        fixture.write_bytes(original_bytes)

        # Step 1: execute raw read command via cmd.exe
        read_command = raw_recipe.replace("SCOPED_FILE", fixture.name)
        res_read = subprocess.run(
            [self.cmd_exe, "/d", "/c", read_command],
            cwd=str(self.fixture_dir),
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(
            res_read.returncode,
            0,
            f"cmd raw read failed: {res_read.stderr.decode('utf-8', errors='replace')}",
        )

        # Step 2: parse exact HEX and verify it matches original raw bytes
        raw_hex_output = res_read.stdout.decode("ascii").strip()
        acquired_bytes = bytes.fromhex(raw_hex_output)
        self.assertEqual(acquired_bytes, original_bytes, "Parsed hex must match exact file raw bytes")
        self.assertTrue(acquired_bytes.startswith(bom), "BOM must be preserved in acquired bytes")
        self.assertIn(b"\r\n", acquired_bytes, "CRLF must be preserved in acquired bytes")
        self.assertIn(b"Za\xc5\xbc\xc3\xb3\xc5\x82\xc4\x87", acquired_bytes, "Non-ASCII must be preserved")

        # Step 3: derive replacement by single byte-span edit preserving all other bytes
        old_span = b"config_value = 100"
        new_span = b"config_value = 250"
        self.assertIn(old_span, acquired_bytes)
        derived_replacement_bytes = acquired_bytes.replace(old_span, new_span, 1)

        self.assertTrue(derived_replacement_bytes.startswith(bom))
        self.assertIn(b"\r\n", derived_replacement_bytes)
        self.assertIn(b"Za\xc5\xbc\xc3\xb3\xc5\x82\xc4\x87", derived_replacement_bytes)
        self.assertIn(b"footer_line = 'end'\r\n", derived_replacement_bytes)

        # Step 4: execute emitted fallback write recipe
        expected_hex = acquired_bytes.hex()
        replacement_hex = derived_replacement_bytes.hex()
        write_command = (
            write_recipe.replace("SCOPED_FILE", fixture.name)
            .replace("EXPECTED_HEX", expected_hex)
            .replace("REPLACEMENT_HEX", replacement_hex)
        )
        res_write = subprocess.run(
            [self.cmd_exe, "/d", "/c", write_command],
            cwd=str(self.fixture_dir),
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(
            res_write.returncode,
            0,
            f"cmd write fallback failed: {res_write.stderr.decode('utf-8', errors='replace')}",
        )

        # Step 5: bytes check and hash check
        final_file_bytes = fixture.read_bytes()
        self.assertEqual(final_file_bytes, derived_replacement_bytes)
        self.assertEqual(
            hashlib.sha256(final_file_bytes).hexdigest(),
            hashlib.sha256(derived_replacement_bytes).hexdigest(),
        )

    def test_lf_raw_read_and_fallback_write(self):
        self.assertIn(" ", str(self.fixture_dir), "Fixture directory path must contain spaces")
        raw_recipe, write_recipe = self._extract_recipes()

        fixture = self.fixture_dir / "fixture_lf_only.py"
        original_bytes = (
            b"# LF source fixture\n"
            b"item_status = 'pending'\n"
            b"retries = 0\n"
        )
        fixture.write_bytes(original_bytes)

        # Step 1: execute raw read command via cmd.exe
        read_command = raw_recipe.replace("SCOPED_FILE", fixture.name)
        res_read = subprocess.run(
            [self.cmd_exe, "/d", "/c", read_command],
            cwd=str(self.fixture_dir),
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(
            res_read.returncode,
            0,
            f"cmd raw read failed: {res_read.stderr.decode('utf-8', errors='replace')}",
        )

        # Step 2: parse exact HEX and verify it matches original raw bytes
        raw_hex_output = res_read.stdout.decode("ascii").strip()
        acquired_bytes = bytes.fromhex(raw_hex_output)
        self.assertEqual(acquired_bytes, original_bytes)
        self.assertNotIn(b"\r\n", acquired_bytes, "Acquired bytes must have LF only, no CRLF")
        self.assertIn(b"\n", acquired_bytes)

        # Step 3: derive replacement by single byte-span edit preserving LF
        old_span = b"item_status = 'pending'"
        new_span = b"item_status = 'verified'"
        self.assertIn(old_span, acquired_bytes)
        derived_replacement_bytes = acquired_bytes.replace(old_span, new_span, 1)
        self.assertNotIn(b"\r\n", derived_replacement_bytes, "Replacement must preserve LF only")

        # Step 4: execute emitted fallback write recipe
        expected_hex = acquired_bytes.hex()
        replacement_hex = derived_replacement_bytes.hex()
        write_command = (
            write_recipe.replace("SCOPED_FILE", fixture.name)
            .replace("EXPECTED_HEX", expected_hex)
            .replace("REPLACEMENT_HEX", replacement_hex)
        )
        res_write = subprocess.run(
            [self.cmd_exe, "/d", "/c", write_command],
            cwd=str(self.fixture_dir),
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(
            res_write.returncode,
            0,
            f"cmd write fallback failed: {res_write.stderr.decode('utf-8', errors='replace')}",
        )

        # Step 5: bytes check and hash check
        final_file_bytes = fixture.read_bytes()
        self.assertEqual(final_file_bytes, derived_replacement_bytes)
        self.assertEqual(
            hashlib.sha256(final_file_bytes).hexdigest(),
            hashlib.sha256(derived_replacement_bytes).hexdigest(),
        )

    def test_mismatched_precondition_nonzero_exit_and_unchanged_file(self):
        self.assertIn(" ", str(self.fixture_dir), "Fixture directory path must contain spaces")
        _, write_recipe = self._extract_recipes()

        fixture = self.fixture_dir / "fixture_race_mismatch.py"
        original_bytes = b"# Original file bytes\r\nvalue = 42\r\n"
        fixture.write_bytes(original_bytes)
        original_hash = hashlib.sha256(original_bytes).hexdigest()

        # Simulate mismatched expected hex (e.g. guessed LF instead of CRLF, or race condition where file changed)
        guessed_lf_bytes = b"# Original file bytes\nvalue = 42\n"
        mismatched_expected_hex = guessed_lf_bytes.hex()
        unwanted_replacement_hex = b"# Corrupted bytes\r\n".hex()

        write_command = (
            write_recipe.replace("SCOPED_FILE", fixture.name)
            .replace("EXPECTED_HEX", mismatched_expected_hex)
            .replace("REPLACEMENT_HEX", unwanted_replacement_hex)
        )
        res_write = subprocess.run(
            [self.cmd_exe, "/d", "/c", write_command],
            cwd=str(self.fixture_dir),
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        # Must fail with nonzero exit code because assertion failed
        self.assertNotEqual(
            res_write.returncode,
            0,
            "Assertion must fail when expected bytes do not exactly match file on disk",
        )
        # File must remain untouched and hash must match original
        self.assertEqual(
            fixture.read_bytes(),
            original_bytes,
            "File must remain completely untouched on precondition mismatch",
        )
        self.assertEqual(
            hashlib.sha256(fixture.read_bytes()).hexdigest(),
            original_hash,
            "Hash must remain completely unchanged",
        )


if __name__ == "__main__":
    unittest.main()
