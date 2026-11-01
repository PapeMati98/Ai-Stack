from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import fleet


class TestMuseOutputSchema(unittest.TestCase):
    def test_muse_command_includes_output_schema_in_all_three_modes(self) -> None:
        """Verify --output-schema is present in all 3 modes: advisory, review, implement."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "worktree"
            workspace.mkdir()
            for mode in ("advisory", "review", "implement"):
                with self.subTest(mode=mode):
                    run_dir = root / f"run-{mode}"
                    run_dir.mkdir()
                    command, stdin_text, final_message = fleet.build_command(
                        "muse",
                        "muse-bin-1.4.6-R6000.1.exe",
                        prompt=f"Task for mode {mode}.",
                        workspace=workspace,
                        mode=mode,
                        model="muse-spark-1.3",
                        complexity="medium",
                        timeout=300,
                        run_dir=run_dir,
                        native_subagent_limit=0,
                        enable_web=False,
                        materialize=True,
                        reasoning_level="medium",
                    )
                    self.assertIsNone(stdin_text)
                    self.assertIsNone(final_message)
                    self.assertIn("--output-schema", command)
                    schema_index = command.index("--output-schema")
                    self.assertEqual(
                        command[schema_index + 1],
                        str(fleet.HANDOFF_SCHEMA_PATH),
                    )
                    self.assertIn("--prompt-file", command)
                    prompt_index = command.index("--prompt-file")
                    self.assertEqual(
                        command[prompt_index + 1],
                        str((run_dir / "muse-prompt.txt").resolve()),
                    )
                    if mode in {"advisory", "review"}:
                        self.assertIn("--disable-write", command)
                        self.assertIn("--disable-shell", command)
                    else:
                        self.assertNotIn("--disable-write", command)
                        self.assertNotIn("--disable-shell", command)

    def test_muse_default_schema_uses_handoff_schema_path(self) -> None:
        """When handoff_schema is None, build_command targets HANDOFF_SCHEMA_PATH."""
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            workspace = Path(temporary) / "worktree"
            workspace.mkdir()
            command, _, _ = fleet.build_command(
                "muse",
                "muse-bin-1.4.6-R6000.1.exe",
                prompt="Task with default schema.",
                workspace=workspace,
                mode="review",
                model="muse-spark-1.3",
                complexity="low",
                timeout=300,
                run_dir=run_dir,
                native_subagent_limit=0,
                enable_web=False,
                materialize=True,
                handoff_schema=None,
            )
            self.assertIn("--output-schema", command)
            self.assertEqual(
                command[command.index("--output-schema") + 1],
                str(fleet.HANDOFF_SCHEMA_PATH),
            )
            self.assertTrue(fleet.HANDOFF_SCHEMA_PATH.is_file())
            # Default schema does not write custom expected-handoff file
            self.assertFalse((run_dir / "expected-handoff.schema.json").exists())

    def test_muse_custom_schema_materialize_true_and_dryrun_false(self) -> None:
        """Custom schema exact content verified for materialize=True and dryrun (materialize=False)."""
        custom_schema = {
            "type": "object",
            "required": ["status", "summary"],
            "properties": {
                "status": {"type": "string", "enum": ["completed", "failed"]},
                "summary": {"type": "string"},
            },
            "additionalProperties": False,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "worktree"
            workspace.mkdir()

            # materialize=True: schema file must exist with exact JSON content
            run_dir_mat = root / "run-materialized"
            command_mat, _, _ = fleet.build_command(
                "muse",
                "muse-bin-1.4.6-R6000.1.exe",
                prompt="Task with custom schema materialized.",
                workspace=workspace,
                mode="implement",
                model="muse-spark-1.3",
                complexity="high",
                timeout=600,
                run_dir=run_dir_mat,
                native_subagent_limit=0,
                enable_web=False,
                materialize=True,
                handoff_schema=custom_schema,
            )
            expected_schema_file = run_dir_mat / "expected-handoff.schema.json"
            self.assertIn("--output-schema", command_mat)
            self.assertEqual(
                command_mat[command_mat.index("--output-schema") + 1],
                str(expected_schema_file),
            )
            self.assertTrue(expected_schema_file.is_file())
            written_data = json.loads(expected_schema_file.read_text(encoding="utf-8"))
            self.assertEqual(written_data, custom_schema)
            self.assertTrue((run_dir_mat / "muse-prompt.txt").is_file())

            # materialize=False (dry-run): schema path in command but no file created on disk
            run_dir_dry = root / "run-dry"
            command_dry, _, _ = fleet.build_command(
                "muse",
                "muse-bin-1.4.6-R6000.1.exe",
                prompt="Task with custom schema dryrun.",
                workspace=workspace,
                mode="implement",
                model="muse-spark-1.3",
                complexity="high",
                timeout=600,
                run_dir=run_dir_dry,
                native_subagent_limit=0,
                enable_web=False,
                materialize=False,
                handoff_schema=custom_schema,
            )
            dry_schema_file = run_dir_dry / "expected-handoff.schema.json"
            self.assertIn("--output-schema", command_dry)
            self.assertEqual(
                command_dry[command_dry.index("--output-schema") + 1],
                str(dry_schema_file),
            )
            self.assertFalse(dry_schema_file.exists())
            self.assertFalse((run_dir_dry / "muse-prompt.txt").exists())

    def test_muse_parent_contract_cannot_be_silently_replaced(self) -> None:
        """Custom schema never alters or replaces the parent HANDOFF_SCHEMA_PATH file."""
        original_bytes = fleet.HANDOFF_SCHEMA_PATH.read_bytes()
        injected_schema = {
            "type": "object",
            "title": "MaliciousOrInjectedSchema",
            "properties": {"tampered": {"type": "boolean"}},
        }
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            workspace = Path(temporary) / "worktree"
            workspace.mkdir()
            command, _, _ = fleet.build_command(
                "muse",
                "muse-bin-1.4.6-R6000.1.exe",
                prompt="Task attempting contract replacement.",
                workspace=workspace,
                mode="review",
                model="muse-spark-1.3",
                complexity="low",
                timeout=300,
                run_dir=run_dir,
                native_subagent_limit=0,
                enable_web=False,
                materialize=True,
                handoff_schema=injected_schema,
            )
            # The parent schema file on disk must remain pristine and unmutated
            current_bytes = fleet.HANDOFF_SCHEMA_PATH.read_bytes()
            self.assertEqual(current_bytes, original_bytes)
            # The argv points strictly to the isolated child expected-handoff file
            self.assertIn("--output-schema", command)
            self.assertEqual(
                command[command.index("--output-schema") + 1],
                str(run_dir / "expected-handoff.schema.json"),
            )
            self.assertNotEqual(
                command[command.index("--output-schema") + 1],
                str(fleet.HANDOFF_SCHEMA_PATH),
            )

    def test_muse_unicode_and_spaces_run_and_workspace_path(self) -> None:
        """Run path and workspace containing Unicode and spaces are preserved accurately."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "katalog z odstępami 🚀 & spółka"
            workspace = root / "obszar roboczy ✨ i testy"
            run_dir.mkdir(parents=True)
            workspace.mkdir(parents=True)
            custom_schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
            command, _, _ = fleet.build_command(
                "muse",
                "muse-bin-1.4.6-R6000.1.exe",
                prompt="Sprawdzenie ścieżki z odstępami i znakami unicode.",
                workspace=workspace,
                mode="advisory",
                model="muse-spark-1.3",
                complexity="medium",
                timeout=300,
                run_dir=run_dir,
                native_subagent_limit=0,
                enable_web=False,
                materialize=True,
                handoff_schema=custom_schema,
            )
            self.assertIn("--output-schema", command)
            schema_arg = command[command.index("--output-schema") + 1]
            expected_schema_path = run_dir / "expected-handoff.schema.json"
            self.assertEqual(schema_arg, str(expected_schema_path))
            self.assertTrue(expected_schema_path.is_file())

            self.assertIn("--prompt-file", command)
            prompt_arg = command[command.index("--prompt-file") + 1]
            self.assertEqual(prompt_arg, str((run_dir / "muse-prompt.txt").resolve()))
            self.assertTrue((run_dir / "muse-prompt.txt").is_file())

            self.assertIn("--workspace", command)
            workspace_arg = command[command.index("--workspace") + 1]
            self.assertEqual(workspace_arg, str(workspace.resolve()))

    def test_muse_unchanged_exact_cli_argv_invariants(self) -> None:
        """Exact provider, model, contributor off, sandbox, readonly and CLI argv flags remain unchanged."""
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            workspace = Path(temporary) / "worktree"
            workspace.mkdir()
            command, stdin_text, final_message = fleet.build_command(
                "muse",
                "muse-bin-1.4.6-R6000.1.exe",
                prompt="Verify exact argv invariants.",
                workspace=workspace,
                mode="review",
                model="muse-spark-1.3",
                complexity="medium",
                timeout=300,
                run_dir=run_dir,
                native_subagent_limit=0,
                enable_web=False,
                materialize=True,
                reasoning_level="medium",
            )
            self.assertIsNone(stdin_text)
            self.assertIsNone(final_message)
            self.assertEqual(command[:2], ["muse-bin-1.4.6-R6000.1.exe", "exec"])
            self.assertIn("--json", command)
            self.assertEqual(command[command.index("--provider") + 1], "meta")
            self.assertEqual(command[command.index("--model") + 1], "muse-spark-1.3")
            self.assertEqual(command[command.index("--contributor-mode") + 1], "off")
            self.assertEqual(command[command.index("--reasoning-effort") + 1], "medium")
            self.assertIn("--no-parallel-tool-calls", command)
            self.assertEqual(command[command.index("--workspace") + 1], str(workspace.resolve()))
            self.assertEqual(command[command.index("--worktree") + 1], "off")
            self.assertIn("--max-model-steps", command)
            self.assertEqual(
                command[command.index("--context-compaction-strategy") + 1],
                "summary-preserved-suffix/v1",
            )
            self.assertEqual(command[command.index("--context-compaction-soft-threshold") + 1], "0.12")
            self.assertEqual(command[command.index("--context-compaction-hard-threshold") + 1], "0.18")
            self.assertEqual(command[command.index("--max-tool-output-bytes") + 1], "65536")
            self.assertIn("--user-input-auto-resolve", command)
            self.assertIn("--no-foreign-personal-context", command)
            self.assertEqual(command[command.index("--approval-mode") + 1], "never")
            self.assertEqual(command[command.index("--sandbox-network") + 1], "restricted")
            self.assertIn("--disable-write", command)
            self.assertIn("--disable-shell", command)
            self.assertIn("--disable-web-tools", command)
            self.assertIn("--output-schema", command)
            self.assertEqual(
                command[command.index("--output-schema") + 1],
                str(fleet.HANDOFF_SCHEMA_PATH),
            )
            self.assertIn("--prompt-file", command)
            self.assertFalse(fleet.MUSE_UNSAFE_FLAGS.intersection(command))

            # Test implement mode with web enabled
            run_dir_web = Path(temporary) / "run-web"
            cmd_web, _, _ = fleet.build_command(
                "muse",
                "muse-bin-1.4.6-R6000.1.exe",
                prompt="Implement with web.",
                workspace=workspace,
                mode="implement",
                model="muse-spark-1.3",
                complexity="medium",
                timeout=300,
                run_dir=run_dir_web,
                native_subagent_limit=0,
                enable_web=True,
                materialize=True,
                reasoning_level="medium",
            )
            self.assertEqual(cmd_web[cmd_web.index("--sandbox-network") + 1], "proxy-only")
            self.assertNotIn("--disable-web-tools", cmd_web)
            self.assertNotIn("--disable-write", cmd_web)
            self.assertNotIn("--disable-shell", cmd_web)
            self.assertIn("--output-schema", cmd_web)
            self.assertEqual(
                cmd_web[cmd_web.index("--output-schema") + 1],
                str(fleet.HANDOFF_SCHEMA_PATH),
            )


if __name__ == "__main__":
    unittest.main()
