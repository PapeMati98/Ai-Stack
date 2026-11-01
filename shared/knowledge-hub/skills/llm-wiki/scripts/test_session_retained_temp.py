"""Synthetic regression tests for record-session --retain-temp-root mode.

All fixtures live under the task's redaction-preserving scratch dir, which the
no_delete_runner renames to _do-usuniecia-20261017 if anything ever tries to
delete it. The test process itself is never asked to delete; cleanup happens
implicitly through the harness.

These tests fail on BASELINE because the canonical record-session path has no
optional retain argument and the function has no retained_session_note receipt
field. After the patch they prove:
  - Optional retain mode completes and returns the concrete retained path.
  - The generated bounded note is on disk and bytes match.
  - NO_DURABLE_CHANGE accepts the retain root.
  - Exact retries reuse the source/proposal pair but each retained dir is unique.
  - Staging failure retains the scratch and leaves the vault untouched.
  - Invalid (relative, file, missing, reparse) roots are refused before any vault
    mutation and never silently fall back to system TEMP.
  - No os.remove/os.rmdir/shutil.rmtree call ever fires in retain mode.
  - The CLI argparse layer accepts --retain-temp-root and PowerShell forwards it.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import knowledge as k


def scratch_root() -> Path:
    """Locate the runner's redaction-preserving scratch directory.

    The no_delete_runner exposes it via TEMP/TMP/TMPDIR; if the test ever runs
    outside the runner, it still picks a writable workspace-root subdirectory.
    """
    for key in ("TEMP", "TMP", "TMPDIR"):
        candidate = os.environ.get(key)
        if candidate and Path(candidate).is_dir():
            return Path(candidate).resolve()
    return Path(tempfile.gettempdir()).resolve()


class RetainedTempTests(unittest.TestCase):
    def setUp(self):
        self.fix = scratch_root() / f"hub-retain-{uuid.uuid4().hex}"
        self.fix.mkdir(parents=False, exist_ok=False)
        self.home = self.fix / "home"
        self.home.mkdir(parents=False, exist_ok=False)
        self.vault = self.home / "vault"
        self.vault.mkdir(parents=False, exist_ok=False)
        k.write_json(
            self.vault / "knowledge-vault.json",
            {"curator_machine": "FIXTURE", "curation_scope": "designated-machine-only"},
        )
        self.home_patch = mock.patch.object(Path, "home", return_value=self.home)
        self.home_patch.start()
        self.note = self.home / "note.md"
        self.note.write_text(
            "## Result\nSynthetic retain-mode parser repair succeeded.\n"
            "## Evidence\nFixture closeout test passed.\n"
            "## Next step\nContinue candidate review.\n",
            encoding="utf-8",
        )
        self.retain_root = self.fix / "retained"
        self.retain_root.mkdir(parents=False, exist_ok=False)

    def tearDown(self):
        self.home_patch.stop()

    def vault_snapshot(self):
        return {
            p.relative_to(self.vault).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.vault.rglob("*")
            if p.is_file()
        }

    def args(self, **changes):
        values = dict(
            agent="codex",
            session_id="session-retain-1",
            checkpoint_id="episode-retain-1",
            project_slug="alpha-project",
            outcome="COMPLETED",
            path=str(self.note),
            summary="Synthetic retain-mode result statement.",
            classification="internal",
            context_receipt=None,
            retain_temp_root=str(self.retain_root),
        )
        values.update(changes)
        return argparse.Namespace(**values)

    # --- Omission preserves legacy behavior ---------------------------------

    def test_omitting_retain_root_keeps_legacy_receipt_shape(self):
        legacy_args = self.args(retain_temp_root=None)
        result = k.record_session(self.vault, legacy_args)
        self.assertEqual(result["receipt_status"], "COMPLETE")
        self.assertNotIn("retained_session_note", result)
        # No scratch dir accidentally appeared inside the explicit retain root
        # when retain mode was not requested.
        leaked = [p for p in self.retain_root.iterdir() if p.name.startswith("hub-session-note-")]
        self.assertEqual(leaked, [])
        self.assertTrue(result["source_id"])
        self.assertTrue(result["proposal_id"])

    # --- Main journey: retain mode -------------------------------------------

    def test_retain_mode_returns_concrete_retained_path_with_bytes(self):
        result = k.record_session(self.vault, self.args())
        self.assertEqual(result["receipt_status"], "COMPLETE")
        retained = Path(result["retained_session_note"])
        self.assertTrue(retained.is_absolute())
        self.assertTrue(retained.is_dir())
        self.assertEqual(
            retained.parent.resolve(),
            self.retain_root.resolve(),
            "retained scratch must live directly under the explicit retain root",
        )
        note_file = retained / "closeout.md"
        self.assertTrue(note_file.is_file())
        bytes_kept = note_file.read_bytes()
        # The bounded note text reached the retained file.
        self.assertIn(b"Synthetic retain-mode parser repair succeeded.", bytes_kept)
        # Same source/proposal invariants as the legacy journey: exactly one of each.
        self.assertEqual(len(k.source_records(self.vault)), 1)
        self.assertEqual(len(k.proposal_records(self.vault)), 1)

    # --- NO_DURABLE_CHANGE in retain mode ----------------------------------

    def test_retain_mode_accepts_no_durable_change_without_path(self):
        journal_args = self.args(
            outcome="NO_DURABLE_CHANGE", path=None, summary="", project_slug=""
        )
        result = k.record_session(self.vault, journal_args)
        self.assertEqual(result["receipt_status"], "COMPLETE")
        retained = Path(result["retained_session_note"])
        self.assertTrue(retained.is_dir())
        self.assertTrue((retained / "closeout.md").is_file())
        proposal_text = Path(result["proposal_path"]).read_text(encoding="utf-8")
        self.assertEqual(k.fm_list(proposal_text, "target_pages"), [])

    # --- Exact retries reuse the source/proposal pair ----------------------

    def test_exact_retry_in_retain_mode_reuses_pair_with_unique_scratch(self):
        first = k.record_session(self.vault, self.args())
        before = self.vault_snapshot()
        second = k.record_session(self.vault, self.args())
        self.assertEqual(first["receipt_status"], "COMPLETE")
        self.assertEqual(first["source_id"], second["source_id"])
        self.assertEqual(first["proposal_id"], second["proposal_id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(before, self.vault_snapshot())
        # Each invocation owns its own unique retained scratch dir.
        self.assertNotEqual(
            first["retained_session_note"], second["retained_session_note"]
        )
        self.assertTrue(Path(first["retained_session_note"]).is_dir())
        self.assertTrue(Path(second["retained_session_note"]).is_dir())

    # --- Staging failure retains the scratch and refuses vault mutation ---

    def test_staging_failure_retains_note_and_keeps_vault_untouched(self):
        before = self.vault_snapshot()
        with mock.patch.object(
            k, "stage_source", side_effect=PermissionError("synthetic ACL denial")
        ):
            with self.assertRaisesRegex(PermissionError, "synthetic ACL denial"):
                k.record_session(self.vault, self.args())
        self.assertEqual(self.vault_snapshot(), before, "vault must be untouched")
        retained_subdirs = sorted(p for p in self.retain_root.iterdir() if p.is_dir() and p.name.startswith("hub-session-note-"))
        self.assertGreaterEqual(
            len(retained_subdirs), 1, "failed closeout must still leave its scratch"
        )
        last = retained_subdirs[-1]
        self.assertTrue(last.name.startswith("hub-session-note-"))
        self.assertTrue((last / "closeout.md").is_file())
        self.assertTrue((self.retain_root / "session-locks").is_dir())

    # --- Invalid-root refusal, all before any vault mutation ----------------

    def test_relative_root_rejected_before_vault_mutation(self):
        before = self.vault_snapshot()
        with self.assertRaisesRegex(k.GatewayError, "absolute"):
            k.record_session(self.vault, self.args(retain_temp_root="relative-path"))
        self.assertEqual(self.vault_snapshot(), before)

    def test_missing_root_rejected_before_vault_mutation(self):
        missing = self.fix / "definitely-missing-xyz-12345"
        before = self.vault_snapshot()
        with self.assertRaisesRegex(k.GatewayError, "does not exist"):
            k.record_session(self.vault, self.args(retain_temp_root=str(missing)))
        self.assertEqual(self.vault_snapshot(), before)

    def test_file_root_rejected_before_vault_mutation(self):
        wrong = self.fix / "not-a-directory.txt"
        wrong.write_text("not a directory", encoding="utf-8")
        before = self.vault_snapshot()
        with self.assertRaisesRegex(k.GatewayError, "not a directory"):
            k.record_session(self.vault, self.args(retain_temp_root=str(wrong)))
        self.assertEqual(self.vault_snapshot(), before)

    def test_reparse_root_rejected_before_vault_mutation(self):
        valid_dir = self.fix / "valid-for-reparse-check"
        valid_dir.mkdir(parents=False, exist_ok=False)
        before = self.vault_snapshot()
        with mock.patch.object(k, "path_has_reparse_component", return_value=True):
            with self.assertRaisesRegex(k.GatewayError, "reparse|symlink|junction"):
                k.record_session(
                    self.vault, self.args(retain_temp_root=str(valid_dir))
                )
        self.assertEqual(self.vault_snapshot(), before)

    def test_validate_helper_rejects_blank_or_relative_inputs(self):
        with self.assertRaisesRegex(k.GatewayError, "absolute"):
            k.validate_retain_temp_root("relative")
        with self.assertRaisesRegex(k.GatewayError, "absolute"):
            k.validate_retain_temp_root(" ")
        with self.assertRaisesRegex(k.GatewayError, "absolute"):
            k.validate_retain_temp_root(None)

    def test_validate_helper_rejects_missing_or_file_targets(self):
        missing = self.fix / "validate-missing-xyz"
        wrong = self.fix / "validate-file.txt"
        wrong.write_text("text", encoding="utf-8")
        with self.assertRaisesRegex(k.GatewayError, "does not exist"):
            k.validate_retain_temp_root(str(missing))
        with self.assertRaisesRegex(k.GatewayError, "not a directory"):
            k.validate_retain_temp_root(str(wrong))

    def test_validate_helper_accepts_existing_regular_directory(self):
        target = self.fix / "accept-this-dir"
        target.mkdir(parents=False, exist_ok=False)
        with mock.patch.object(k, "path_has_reparse_component", return_value=False):
            resolved = k.validate_retain_temp_root(str(target))
        self.assertEqual(resolved, target.resolve())

    # --- Process-local deletion guard --------------------------------------

    def test_no_process_local_deletion_calls_in_retain_mode(self):
        delete_events: list[tuple[str, str]] = []

        def track_unlink(path, *args, **kwargs):
            delete_events.append(("os.unlink", str(path)))

        def track_rmdir(path, *args, **kwargs):
            delete_events.append(("os.rmdir", str(path)))

        def track_rmtree(path, *args, **kwargs):
            delete_events.append(("shutil.rmtree", str(path)))

        with mock.patch("os.unlink", side_effect=track_unlink), \
             mock.patch("os.remove", side_effect=track_unlink), \
             mock.patch("os.rmdir", side_effect=track_rmdir), \
             mock.patch("shutil.rmtree", side_effect=track_rmtree):
            result = k.record_session(self.vault, self.args())
        self.assertEqual(result["receipt_status"], "COMPLETE")
        self.assertEqual(
            delete_events,
            [],
            "retain mode must never trigger a delete syscall; "
            f"observed: {delete_events}",
        )
        # The retained file is still there after the recording finished.
        retained = Path(result["retained_session_note"])
        self.assertTrue(retained.is_dir())
        self.assertTrue((retained / "closeout.md").is_file())

    # --- CLI / PowerShell forwarding (static checks; the runner wraps the
    #     subprocess with CREATE_NO_WINDOW and an audit hook for free) ------

    def test_cli_parser_admits_retain_temp_root_argument(self):
        args = k.parser().parse_args(
            [
                "record-session",
                "--agent", "grok",
                "--session-id", "s1",
                "--checkpoint-id", "c1",
                "--outcome", "COMPLETED",
                "--retain-temp-root", str(self.retain_root),
            ]
        )
        self.assertEqual(args.action, "record-session")
        self.assertEqual(args.retain_temp_root, str(self.retain_root))

    def test_powershell_companion_forwards_retain_temp_root(self):
        ps1 = Path(k.__file__).with_suffix(".ps1").read_text(encoding="utf-8")
        self.assertIn("[string]$RetainTempRoot = ''", ps1)
        self.assertIn("'--retain-temp-root', $RetainTempRoot", ps1)

    def test_empty_explicit_root_refuses_before_vault_write(self):
        before = self.vault_snapshot()
        with self.assertRaises(k.GatewayError):
            k.record_session(self.vault, self.args(retain_temp_root=""))
        self.assertEqual(before, self.vault_snapshot())

    def test_raw_reparse_is_checked_before_resolution(self):
        raw = self.fix / "synthetic-link"
        raw.mkdir()  # No real link or permission change is needed for this case.
        actual_resolve = Path.resolve
        def resolve(path, *args, **kwargs):
            return self.retain_root if path == raw else actual_resolve(path, *args, **kwargs)
        with mock.patch.object(Path, "resolve", resolve), mock.patch.object(k, "path_has_reparse_component", side_effect=lambda root, path: path == raw):
            with self.assertRaises(k.GatewayError):
                k.validate_retain_temp_root(str(raw))

    def test_partial_staging_failure_does_not_request_package_deletion(self):
        actual_write_json = k.write_json
        def write_json(path, *args, **kwargs):
            if Path(path).name == "source.json":
                raise OSError("synthetic source metadata failure")
            return actual_write_json(path, *args, **kwargs)
        with mock.patch.object(k, "write_json", side_effect=write_json), mock.patch.object(k.shutil, "rmtree", side_effect=AssertionError("delete requested")) as cleanup:
            with self.assertRaisesRegex(OSError, "synthetic source metadata failure"):
                k.record_session(self.vault, self.args())
            cleanup.assert_not_called()
        self.assertTrue(list(self.retain_root.glob("hub-session-note-*/closeout.md")))

    def test_partial_proposal_event_failure_does_not_request_deletion(self):
        actual_write_event = k.write_event
        def write_event(vault, agent, action, *args, **kwargs):
            if action == "new-proposal":
                raise OSError("synthetic proposal event failure")
            return actual_write_event(vault, agent, action, *args, **kwargs)
        with mock.patch.object(k, "write_event", side_effect=write_event), mock.patch.object(Path, "unlink", side_effect=AssertionError("delete requested")) as cleanup:
            with self.assertRaisesRegex(OSError, "synthetic proposal event failure"):
                k.record_session(self.vault, self.args())
            cleanup.assert_not_called()
        self.assertEqual(len(list((self.vault / "proposals").rglob("*.md"))), 1)
        self.assertTrue(list(self.retain_root.glob("hub-session-note-*/closeout.md")))


    # --- session_write_lock routing and confinement ------------------------

    def test_retain_mode_routes_session_lock_under_retain_root_and_never_creates_home_locks(self):
        vault_sha = k.vault_binding(self.vault)["vault_path_sha256"]
        result = k.record_session(self.vault, self.args())
        self.assertEqual(result["receipt_status"], "COMPLETE")
        lock_dir = self.retain_root / "session-locks" / vault_sha
        self.assertTrue(lock_dir.is_dir())
        locks = list(lock_dir.glob("*.lock"))
        self.assertEqual(len(locks), 1)
        self.assertFalse((self.home / ".agents" / "knowledge-context-receipts" / "session-locks").exists())
        self.assertFalse((self.home / ".agents").exists())

    def test_session_write_lock_with_lock_base_never_touches_home(self):
        vault_sha = k.vault_binding(self.vault)["vault_path_sha256"]
        with mock.patch.object(Path, "home", side_effect=AssertionError("Path.home() accessed with explicit lock_base")):
            with k.session_write_lock(self.vault, "direct-lock-test", lock_base=self.retain_root):
                lock_file = self.retain_root / "session-locks" / vault_sha / "direct-lock-test.lock"
                self.assertTrue(lock_file.is_file())
        self.assertFalse((self.home / ".agents").exists())

    def test_explicit_receipt_root_routes_session_lock(self):
        receipt_root = self.fix / "custom-receipt-root"
        receipt_root.mkdir(parents=False, exist_ok=False)
        vault_sha = k.vault_binding(self.vault)["vault_path_sha256"]
        args = self.args(retain_temp_root=None, receipt_root=str(receipt_root))
        result = k.record_session(self.vault, args)
        self.assertEqual(result["receipt_status"], "COMPLETE")
        lock_dir = receipt_root / "session-locks" / vault_sha
        self.assertTrue(lock_dir.is_dir())
        self.assertEqual(len(list(lock_dir.glob("*.lock"))), 1)
        self.assertFalse((self.home / ".agents").exists())

    def test_explicit_lock_root_routes_session_lock(self):
        lock_root = self.fix / "custom-lock-root"
        lock_root.mkdir(parents=False, exist_ok=False)
        vault_sha = k.vault_binding(self.vault)["vault_path_sha256"]
        args = self.args(retain_temp_root=None, lock_root=str(lock_root))
        result = k.record_session(self.vault, args)
        self.assertEqual(result["receipt_status"], "COMPLETE")
        lock_dir = lock_root / "session-locks" / vault_sha
        self.assertTrue(lock_dir.is_dir())
        self.assertEqual(len(list(lock_dir.glob("*.lock"))), 1)
        self.assertFalse((self.home / ".agents").exists())

    def test_lock_root_takes_precedence_over_receipt_root_and_retain_root(self):
        lock_root = self.fix / "prec-lock-root"
        receipt_root = self.fix / "prec-receipt-root"
        lock_root.mkdir(parents=False, exist_ok=False)
        receipt_root.mkdir(parents=False, exist_ok=False)
        vault_sha = k.vault_binding(self.vault)["vault_path_sha256"]
        args = self.args(
            retain_temp_root=str(self.retain_root),
            receipt_root=str(receipt_root),
            lock_root=str(lock_root),
        )
        result = k.record_session(self.vault, args)
        self.assertEqual(result["receipt_status"], "COMPLETE")
        self.assertTrue(list((lock_root / "session-locks" / vault_sha).glob("*.lock")))
        self.assertFalse((receipt_root / "session-locks").exists())
        self.assertFalse((self.retain_root / "session-locks").exists())
        self.assertTrue(Path(result["retained_session_note"]).is_dir())
        self.assertFalse((self.home / ".agents").exists())

    def test_receipt_root_takes_precedence_over_retain_root_for_session_locks(self):
        receipt_root = self.fix / "receipt-over-retain-root"
        receipt_root.mkdir(parents=False, exist_ok=False)
        vault_sha = k.vault_binding(self.vault)["vault_path_sha256"]
        args = self.args(
            retain_temp_root=str(self.retain_root),
            receipt_root=str(receipt_root),
        )
        result = k.record_session(self.vault, args)
        self.assertEqual(result["receipt_status"], "COMPLETE")
        self.assertTrue(list((receipt_root / "session-locks" / vault_sha).glob("*.lock")))
        self.assertFalse((self.retain_root / "session-locks").exists())
        self.assertTrue(Path(result["retained_session_note"]).is_dir())
        self.assertFalse((self.home / ".agents").exists())

    # --- Rejections before vault mutation for lock-root, receipt-root, retain-temp-root ---

    def test_lock_root_and_receipt_root_reject_invalid_paths_before_mutation(self):
        for flag in ("receipt_root", "lock_root"):
            flag_name = "--" + flag.replace("_", "-")
            before = self.vault_snapshot()

            # Relative path
            with self.assertRaisesRegex(k.GatewayError, "absolute"):
                k.record_session(self.vault, self.args(retain_temp_root=None, **{flag: "relative/path"}))
            self.assertEqual(self.vault_snapshot(), before)

            # Missing path
            missing = self.fix / f"missing-{flag}"
            with self.assertRaisesRegex(k.GatewayError, "does not exist"):
                k.record_session(self.vault, self.args(retain_temp_root=None, **{flag: str(missing)}))
            self.assertEqual(self.vault_snapshot(), before)

            # File path
            wrong_file = self.fix / f"{flag}-is-file.txt"
            wrong_file.write_text("text", encoding="utf-8")
            with self.assertRaisesRegex(k.GatewayError, "not a directory"):
                k.record_session(self.vault, self.args(retain_temp_root=None, **{flag: str(wrong_file)}))
            self.assertEqual(self.vault_snapshot(), before)

            # Reparse path
            valid_dir = self.fix / f"{flag}-reparse-test"
            valid_dir.mkdir(parents=False, exist_ok=False)
            with mock.patch.object(k, "path_has_reparse_component", return_value=True):
                with self.assertRaisesRegex(k.GatewayError, "reparse|symlink|junction"):
                    k.record_session(self.vault, self.args(retain_temp_root=None, **{flag: str(valid_dir)}))
            self.assertEqual(self.vault_snapshot(), before)

            # Inside vault
            inside_dir = self.vault / f"inside-vault-{flag}"
            inside_dir.mkdir(parents=False, exist_ok=False)
            with self.assertRaisesRegex(k.GatewayError, f"{flag_name} must be outside the Knowledge Hub vault."):
                k.record_session(self.vault, self.args(retain_temp_root=None, **{flag: str(inside_dir)}))
            self.assertEqual(self.vault_snapshot(), before)

            # Vault root itself
            with self.assertRaisesRegex(k.GatewayError, f"{flag_name} must be outside the Knowledge Hub vault."):
                k.record_session(self.vault, self.args(retain_temp_root=None, **{flag: str(self.vault)}))
            self.assertEqual(self.vault_snapshot(), before)

    def test_retain_temp_root_rejected_if_inside_vault_before_mutation(self):
        before = self.vault_snapshot()
        inside_dir = self.vault / "inside-vault-retain"
        inside_dir.mkdir(parents=False, exist_ok=False)
        with self.assertRaisesRegex(k.GatewayError, "--retain-temp-root must be outside the Knowledge Hub vault."):
            k.record_session(self.vault, self.args(retain_temp_root=str(inside_dir)))
        self.assertEqual(self.vault_snapshot(), before)

        with self.assertRaisesRegex(k.GatewayError, "--retain-temp-root must be outside the Knowledge Hub vault."):
            k.record_session(self.vault, self.args(retain_temp_root=str(self.vault)))
        self.assertEqual(self.vault_snapshot(), before)

    def test_validate_session_lock_root_helper_checks(self):
        # Empty / non-absolute
        with self.assertRaisesRegex(k.GatewayError, "absolute"):
            k.validate_session_lock_root(self.vault, "")
        with self.assertRaisesRegex(k.GatewayError, "absolute"):
            k.validate_session_lock_root(self.vault, "relative-path", "--lock-root")

        # Missing / file
        missing = self.fix / "helper-missing-xyz"
        with self.assertRaisesRegex(k.GatewayError, "does not exist"):
            k.validate_session_lock_root(self.vault, str(missing), "--lock-root")
        wrong_file = self.fix / "helper-file.txt"
        wrong_file.write_text("file", encoding="utf-8")
        with self.assertRaisesRegex(k.GatewayError, "not a directory"):
            k.validate_session_lock_root(self.vault, str(wrong_file), "--receipt-root")

        # Reparse
        valid_dir = self.fix / "helper-reparse-dir"
        valid_dir.mkdir(parents=False, exist_ok=False)
        with mock.patch.object(k, "path_has_reparse_component", return_value=True):
            with self.assertRaisesRegex(k.GatewayError, "reparse|symlink|junction"):
                k.validate_session_lock_root(self.vault, str(valid_dir), "--lock-root")

        # Inside vault
        inside = self.vault / "helper-inside"
        inside.mkdir(parents=False, exist_ok=False)
        with self.assertRaisesRegex(k.GatewayError, "--lock-root must be outside the Knowledge Hub vault."):
            k.validate_session_lock_root(self.vault, str(inside), "--lock-root")

        # Valid outside directory
        with mock.patch.object(k, "path_has_reparse_component", return_value=False):
            resolved = k.validate_session_lock_root(self.vault, str(valid_dir), "--receipt-root")
            self.assertEqual(resolved, valid_dir.resolve())

    # --- CLI parser and PowerShell forwarding for receipt-root / lock-root ---

    def test_cli_parser_admits_receipt_root_and_lock_root_arguments(self):
        args = k.parser().parse_args(
            [
                "record-session",
                "--agent", "grok",
                "--session-id", "s1",
                "--checkpoint-id", "c1",
                "--outcome", "COMPLETED",
                "--receipt-root", str(self.fix / "r"),
                "--lock-root", str(self.fix / "l"),
            ]
        )
        self.assertEqual(args.action, "record-session")
        self.assertEqual(args.receipt_root, str(self.fix / "r"))
        self.assertEqual(args.lock_root, str(self.fix / "l"))

    def test_powershell_companion_forwards_receipt_root_and_lock_root(self):
        ps1 = Path(k.__file__).with_suffix(".ps1").read_text(encoding="utf-8")
        self.assertIn("[string]$LockRoot = ''", ps1)
        self.assertIn("'--receipt-root', $ReceiptRoot", ps1)
        self.assertIn("'--lock-root', $LockRoot", ps1)


if __name__ == "__main__":
    unittest.main()
