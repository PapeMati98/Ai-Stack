#!/usr/bin/env python3
"""Failure-injection tests for the Capability Hub V1 TEMP deployment proof."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from capability_core import CapabilityError, scan_directory
from capability_deployment_proof import (
    END_MARKER,
    PROOF_PREFIX,
    START_MARKER,
    InjectedCrash,
    TempDeploymentTransaction,
    TempFirstInstallTransaction,
    probe_versioned_link_switch,
)


OLD_RULES = (
    "# Identity\n\n"
    f"{START_MARKER}\nold rules\n{END_MARKER}\n"
).encode("ascii")
NEW_RULES = (
    "# Identity\n\n"
    f"{START_MARKER}\nnew rules\n{END_MARKER}\n"
).encode("ascii")


def remove_tree_portably(path: Path, callback: object) -> None:
    keyword = "onexc" if "onexc" in inspect.signature(shutil.rmtree).parameters else "onerror"
    shutil.rmtree(path, **{keyword: callback})


class DeploymentProofTests(unittest.TestCase):
    def setUp(self) -> None:
        self.roots: list[Path] = []

    def tearDown(self) -> None:
        temp_root = Path(tempfile.gettempdir()).resolve(strict=True)

        def make_writable(function: object, path: str, _: object) -> None:
            os.chmod(path, stat.S_IWRITE)
            function(path)

        for root in self.roots:
            resolved = root.resolve(strict=True)
            self.assertEqual(resolved.parent, temp_root)
            self.assertTrue(resolved.name.startswith(PROOF_PREFIX))
            remove_tree_portably(resolved, make_writable)

    def fixture(self) -> tuple[Path, TempDeploymentTransaction, str, str]:
        root = Path(tempfile.mkdtemp(prefix=PROOF_PREFIX))
        self.roots.append(root)
        target = root / "target"
        staged = root / "staged"
        target.mkdir()
        staged.mkdir()
        (target / "SKILL.md").write_bytes(b"old payload\n")
        (staged / "SKILL.md").write_bytes(b"new payload\n")
        (root / "IDENTITY.md").write_bytes(OLD_RULES)
        old_hash = scan_directory(target)["payload_fingerprint"]
        new_hash = scan_directory(staged)["payload_fingerprint"]
        return root, TempDeploymentTransaction(root, NEW_RULES), old_hash, new_hash

    def tree_snapshot(self, root: Path) -> dict[str, tuple[int, str]]:
        result: dict[str, tuple[int, str]] = {}
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            result[path.relative_to(root).as_posix()] = (
                path.stat().st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        return result

    def test_dry_run_plan_has_zero_writes(self) -> None:
        root, transaction, old_hash, new_hash = self.fixture()
        before = self.tree_snapshot(root)
        plan = transaction.plan()
        self.assertEqual(plan["writes_performed"], 0)
        self.assertEqual(plan["old_fingerprint"], old_hash)
        self.assertEqual(plan["new_fingerprint"], new_hash)
        self.assertEqual(before, self.tree_snapshot(root))

    def test_commit_verifies_target_rules_and_backup(self) -> None:
        root, transaction, old_hash, new_hash = self.fixture()
        result = transaction.run()
        self.assertEqual(result["status"], "COMMITTED")
        self.assertEqual(scan_directory(root / "target")["payload_fingerprint"], new_hash)
        self.assertEqual((root / "IDENTITY.md").read_bytes(), NEW_RULES)
        backup = Path(result["backup"])
        self.assertTrue(backup.is_dir())
        self.assertEqual(scan_directory(backup)["payload_fingerprint"], old_hash)
        complete = json.loads(
            (root / ".transaction-state/backup-complete.json").read_text(encoding="ascii")
        )
        self.assertEqual(complete["payload_fingerprint"], old_hash)
        self.assertFalse((root / ".transaction-state/target.lock.json").exists())

    def test_crash_recovery_at_every_forward_checkpoint(self) -> None:
        rollback_states = (
            "preparing",
            "rules_backed_up",
            "prepared",
            "target_moved",
            "backup_verified",
            "target_activated",
            "rules_activated",
        )
        for state in rollback_states:
            with self.subTest(state=state):
                root, transaction, old_hash, _ = self.fixture()
                with self.assertRaises(InjectedCrash):
                    transaction.run(crash_after=state)
                recovered = transaction.recover(owner_authorized=True)
                self.assertEqual(recovered["status"], "ROLLED_BACK")
                self.assertEqual(
                    scan_directory(root / "target")["payload_fingerprint"], old_hash
                )
                self.assertEqual((root / "IDENTITY.md").read_bytes(), OLD_RULES)
                self.assertFalse((root / ".transaction-state/target.lock.json").exists())

        root, transaction, _, new_hash = self.fixture()
        with self.assertRaises(InjectedCrash):
            transaction.run(crash_after="committed")
        recovered = transaction.recover(owner_authorized=True)
        self.assertEqual(recovered["status"], "COMMITTED")
        self.assertEqual(scan_directory(root / "target")["payload_fingerprint"], new_hash)
        self.assertEqual((root / "IDENTITY.md").read_bytes(), NEW_RULES)

    def test_interrupted_rollback_is_idempotently_recovered(self) -> None:
        root, transaction, old_hash, _ = self.fixture()
        with self.assertRaises(InjectedCrash):
            transaction.run(crash_after="target_activated")
        with self.assertRaises(InjectedCrash):
            transaction.recover(
                owner_authorized=True, crash_after="rollback_new_quarantined"
            )
        recovered = transaction.recover(owner_authorized=True)
        self.assertEqual(recovered["status"], "ROLLED_BACK")
        self.assertEqual(scan_directory(root / "target")["payload_fingerprint"], old_hash)
        self.assertEqual((root / "IDENTITY.md").read_bytes(), OLD_RULES)

    def test_concurrent_invocation_and_unauthorized_recovery_fail_closed(self) -> None:
        _, first, _, _ = self.fixture()
        first._acquire("first")
        second = TempDeploymentTransaction(first.root, NEW_RULES)
        with self.assertRaisesRegex(CapabilityError, "lock already exists"):
            second.run()
        first._release("first")

        _, transaction, _, _ = self.fixture()
        with self.assertRaises(InjectedCrash):
            transaction.run(crash_after="prepared")
        with self.assertRaisesRegex(CapabilityError, "owner authorization"):
            transaction.recover(owner_authorized=False)

    def test_simulated_lock_and_rules_denials_preserve_or_restore_state(self) -> None:
        for failure in ("rules_backup", "target_move", "rules_replace"):
            with self.subTest(failure=failure):
                root, transaction, old_hash, _ = self.fixture()
                with self.assertRaises(OSError):
                    transaction.run(fail_before=failure)
                transaction.recover(owner_authorized=True)
                self.assertEqual(
                    scan_directory(root / "target")["payload_fingerprint"], old_hash
                )
                self.assertEqual((root / "IDENTITY.md").read_bytes(), OLD_RULES)

        root, transaction, old_hash, _ = self.fixture()
        with self.assertRaises(OSError):
            transaction.run(fail_before="journal")
        self.assertEqual(scan_directory(root / "target")["payload_fingerprint"], old_hash)
        self.assertFalse((root / ".transaction-state/target.lock.json").exists())

    def test_staging_tamper_is_refused_before_target_move(self) -> None:
        root, transaction, old_hash, _ = self.fixture()

        def tamper() -> None:
            (root / "staged/SKILL.md").write_bytes(b"tampered\n")

        with self.assertRaisesRegex(CapabilityError, "changed after preparation"):
            transaction.run(after_prepare=tamper)
        self.assertEqual(scan_directory(root / "target")["payload_fingerprint"], old_hash)
        transaction.recover(owner_authorized=True)

    def test_corrupt_backup_blocks_rollback_without_overwriting_active_target(self) -> None:
        root, transaction, _, new_hash = self.fixture()
        with self.assertRaises(InjectedCrash):
            transaction.run(crash_after="target_activated")
        journal = json.loads(
            (root / ".transaction-state/journal.json").read_text(encoding="ascii")
        )
        backup = Path(journal["backup"])
        (backup / "SKILL.md").write_bytes(b"corrupt backup\n")
        with self.assertRaisesRegex(CapabilityError, "backup is corrupt"):
            transaction.recover(owner_authorized=True)
        self.assertEqual(scan_directory(root / "target")["payload_fingerprint"], new_hash)

    def test_duplicate_rules_block_is_rejected_before_transaction_state(self) -> None:
        root, transaction, _, _ = self.fixture()
        duplicate = OLD_RULES + START_MARKER.encode("ascii") + b"\n"
        (root / "IDENTITY.md").write_bytes(duplicate)
        before = self.tree_snapshot(root)
        with self.assertRaisesRegex(CapabilityError, "duplicate"):
            transaction.plan()
        self.assertEqual(before, self.tree_snapshot(root))
        self.assertFalse((root / ".transaction-state").exists())

    def test_versioned_link_switch_probe_records_platform_result(self) -> None:
        root, _, _, _ = self.fixture()
        result = probe_versioned_link_switch(root)
        self.assertEqual(result["strategy"], "versioned-directory-link-switch")
        self.assertIn("link_created", result)
        self.assertIn("atomic_replace_supported", result)


class FirstInstallProofTests(unittest.TestCase):
    def setUp(self) -> None:
        self.roots: list[Path] = []

    def tearDown(self) -> None:
        temp_root = Path(tempfile.gettempdir()).resolve(strict=True)

        def make_writable(function: object, path: str, _: object) -> None:
            os.chmod(path, stat.S_IWRITE)
            function(path)

        for root in self.roots:
            resolved = root.resolve(strict=True)
            self.assertEqual(resolved.parent, temp_root)
            self.assertTrue(resolved.name.startswith(PROOF_PREFIX))
            remove_tree_portably(resolved, make_writable)

    def fixture(self) -> tuple[Path, TempFirstInstallTransaction, str]:
        root = Path(tempfile.mkdtemp(prefix=PROOF_PREFIX))
        self.roots.append(root)
        staged = root / "staged"
        staged.mkdir()
        (staged / "SKILL.md").write_bytes(b"first install payload\n")
        new_hash = scan_directory(staged)["payload_fingerprint"]
        return root, TempFirstInstallTransaction(root), new_hash

    def tree_snapshot(self, root: Path) -> dict[str, tuple[int, str]]:
        result: dict[str, tuple[int, str]] = {}
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            result[path.relative_to(root).as_posix()] = (
                path.stat().st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        return result

    def test_first_install_plan_and_commit(self) -> None:
        root, transaction, new_hash = self.fixture()
        before = self.tree_snapshot(root)
        plan = transaction.plan()
        self.assertEqual(plan["writes_performed"], 0)
        self.assertFalse(plan["target_preexisting"])
        self.assertEqual(plan["new_fingerprint"], new_hash)
        self.assertEqual(before, self.tree_snapshot(root))

        result = transaction.run()
        self.assertEqual(result["status"], "COMMITTED")
        self.assertFalse(result["target_preexisting"])
        self.assertEqual(
            scan_directory(root / "target")["payload_fingerprint"], new_hash
        )
        prior = json.loads(
            (root / ".first-install-state/prior-state.json").read_text(
                encoding="ascii"
            )
        )
        self.assertFalse(prior["target_preexisting"])
        self.assertFalse((root / ".first-install-state/target.lock.json").exists())

    def test_first_install_committed_rollback_restores_exact_absence(self) -> None:
        root, transaction, new_hash = self.fixture()
        transaction.run()
        with self.assertRaisesRegex(CapabilityError, "owner authorization"):
            transaction.rollback_committed(owner_authorized=False)
        result = transaction.rollback_committed(owner_authorized=True)
        self.assertEqual(result["status"], "ROLLED_BACK")
        self.assertFalse(result["target_present"])
        self.assertFalse((root / "target").exists())
        quarantine = Path(result["quarantine"])
        self.assertTrue(quarantine.is_dir())
        self.assertEqual(
            scan_directory(quarantine)["payload_fingerprint"], new_hash
        )
        self.assertFalse((root / ".first-install-state/target.lock.json").exists())

    def test_first_install_committed_rollback_crash_recovery(self) -> None:
        for state in (
            "rollback_preparing",
            "rollback_prepared",
            "rollback_target_renamed",
            "rollback_target_quarantined",
            "rolled_back",
        ):
            with self.subTest(state=state):
                root, transaction, new_hash = self.fixture()
                transaction.run()
                with self.assertRaises(InjectedCrash):
                    transaction.rollback_committed(
                        owner_authorized=True, crash_after=state
                    )
                recovered = transaction.recover_committed_rollback(
                    owner_authorized=True
                )
                self.assertEqual(recovered["status"], "ROLLED_BACK")
                self.assertFalse(recovered["target_present"])
                self.assertFalse((root / "target").exists())
                quarantine = Path(recovered["quarantine"])
                self.assertEqual(
                    scan_directory(quarantine)["payload_fingerprint"], new_hash
                )
                self.assertFalse(
                    (root / ".first-install-state/target.lock.json").exists()
                )

    def test_first_install_committed_rollback_refuses_tamper(self) -> None:
        root, transaction, _ = self.fixture()
        transaction.run()
        (root / "target/SKILL.md").write_bytes(b"tampered after install\n")
        with self.assertRaisesRegex(CapabilityError, "changed"):
            transaction.rollback_committed(owner_authorized=True)
        self.assertTrue((root / "target").is_dir())

    def test_first_install_crash_recovery_restores_absence(self) -> None:
        for state in (
            "preparing",
            "prepared",
            "prior_state_verified",
            "target_renamed",
            "target_activated",
        ):
            with self.subTest(state=state):
                root, transaction, _ = self.fixture()
                with self.assertRaises(InjectedCrash):
                    transaction.run(crash_after=state)
                recovered = transaction.recover(owner_authorized=True)
                self.assertEqual(recovered["status"], "ROLLED_BACK")
                self.assertFalse(recovered["target_present"])
                self.assertFalse((root / "target").exists())
                self.assertFalse(
                    (root / ".first-install-state/target.lock.json").exists()
                )

        root, transaction, new_hash = self.fixture()
        with self.assertRaises(InjectedCrash):
            transaction.run(crash_after="committed")
        recovered = transaction.recover(owner_authorized=True)
        self.assertEqual(recovered["status"], "COMMITTED")
        self.assertTrue(recovered["target_present"])
        self.assertEqual(
            scan_directory(root / "target")["payload_fingerprint"], new_hash
        )

        root, transaction, _ = self.fixture()
        with self.assertRaises(InjectedCrash):
            transaction.run(crash_after="committed")
        (root / "target/SKILL.md").write_bytes(b"tampered before recovery\n")
        with self.assertRaisesRegex(CapabilityError, "changed"):
            transaction.recover(owner_authorized=True)
        self.assertTrue((root / ".first-install-state/target.lock.json").exists())

    def test_first_install_denial_and_owner_authorization(self) -> None:
        root, transaction, _ = self.fixture()
        with self.assertRaises(OSError):
            transaction.run(fail_before="activate")
        with self.assertRaisesRegex(CapabilityError, "owner authorization"):
            transaction.recover(owner_authorized=False)
        recovered = transaction.recover(owner_authorized=True)
        self.assertEqual(recovered["status"], "ROLLED_BACK")
        self.assertFalse((root / "target").exists())

    def test_first_install_concurrent_invocation_fails_closed(self) -> None:
        _, first, _ = self.fixture()
        first._acquire("first")
        second = TempFirstInstallTransaction(first.root)
        with self.assertRaisesRegex(CapabilityError, "lock already exists"):
            second.run()
        first._release("first")

    def test_first_install_unexpected_target_is_not_overwritten(self) -> None:
        root, transaction, _ = self.fixture()

        def create_unexpected_target() -> None:
            target = root / "target"
            target.mkdir()
            (target / "owner-file.txt").write_bytes(b"unmanaged\n")

        with self.assertRaisesRegex(CapabilityError, "appeared after preparation"):
            transaction.run(after_prepare=create_unexpected_target)
        self.assertEqual((root / "target/owner-file.txt").read_bytes(), b"unmanaged\n")
        with self.assertRaisesRegex(CapabilityError, "changed"):
            transaction.recover(owner_authorized=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
