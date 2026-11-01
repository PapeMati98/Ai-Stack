#!/usr/bin/env python3
"""TEMP-only deployment transaction proof for Capability Hub V1."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable

from capability_core import (
    CapabilityError,
    absolute_plain_path,
    scan_directory,
    sha256_bytes,
    strict_json_loads,
    utc_now,
)


PROOF_PREFIX = "capability-deploy-proof-"
START_MARKER = "<!-- shared-agent-knowledge:start -->"
END_MARKER = "<!-- shared-agent-knowledge:end -->"


class InjectedCrash(RuntimeError):
    """A deliberate process-loss checkpoint used by the proof suite."""


def assert_proof_root(root: Path | str) -> Path:
    candidate = absolute_plain_path(root, must_exist=True)
    temp_root = absolute_plain_path(tempfile.gettempdir(), must_exist=True)
    try:
        candidate.relative_to(temp_root)
    except ValueError as exc:
        raise CapabilityError(f"Deployment proof root is outside TEMP: {candidate}") from exc
    if candidate == temp_root or not candidate.name.startswith(PROOF_PREFIX):
        raise CapabilityError(
            f"Deployment proof root must be a unique {PROOF_PREFIX}* child of TEMP"
        )
    return candidate


def fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def validate_managed_block(text: str) -> None:
    starts = text.count(START_MARKER)
    ends = text.count(END_MARKER)
    if starts != ends or starts > 1:
        raise CapabilityError("Managed rules file has malformed or duplicate blocks")


def atomic_write_bytes(path: Path, value: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("xb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_parent(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    body = json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2).encode("ascii")
    atomic_write_bytes(path, body + b"\n")


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, CapabilityError) as exc:
        raise CapabilityError(f"Invalid deployment proof JSON: {path}") from exc
    if not isinstance(value, dict):
        raise CapabilityError(f"Deployment proof JSON must be an object: {path}")
    return value


class TempDeploymentTransaction:
    """A recoverable two-rename transaction constrained to one TEMP fixture."""

    def __init__(self, root: Path | str, new_rules: bytes) -> None:
        self.root = assert_proof_root(root)
        self.target = self.root / "target"
        self.staged = self.root / "staged"
        self.rules = self.root / "IDENTITY.md"
        self.state_root = self.root / ".transaction-state"
        self.backup_root = self.root / "backups"
        self.quarantine_root = self.root / "quarantine"
        self.lock_path = self.state_root / "target.lock.json"
        self.journal_path = self.state_root / "journal.json"
        self.rules_backup = self.state_root / "IDENTITY.md.backup"
        self.backup_complete = self.state_root / "backup-complete.json"
        self.new_rules = new_rules

    def plan(self) -> dict[str, Any]:
        if not self.target.is_dir() or not self.staged.is_dir() or not self.rules.is_file():
            raise CapabilityError("Proof fixture requires target, staged and IDENTITY.md")
        current_rules = self.rules.read_bytes()
        validate_managed_block(current_rules.decode("utf-8", errors="strict"))
        validate_managed_block(self.new_rules.decode("utf-8", errors="strict"))
        old = scan_directory(self.target, reject_hardlinks=True)
        new = scan_directory(self.staged, reject_hardlinks=True)
        return {
            "strategy": "same-volume-two-rename-journal",
            "target": str(self.target),
            "staged": str(self.staged),
            "old_fingerprint": old["payload_fingerprint"],
            "new_fingerprint": new["payload_fingerprint"],
            "old_rules_sha256": sha256_bytes(current_rules),
            "new_rules_sha256": sha256_bytes(self.new_rules),
            "writes_performed": 0,
        }

    def _acquire(self, transaction_id: str) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        data = {
            "transaction_id": transaction_id,
            "pid": os.getpid(),
            "created_at": utc_now(),
        }
        try:
            descriptor = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError as exc:
            raise CapabilityError(
                "Per-target lock already exists; automatic stale-lock removal is forbidden"
            ) from exc
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(json.dumps(data, sort_keys=True).encode("ascii") + b"\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _release(self, transaction_id: str) -> None:
        lock = read_json(self.lock_path)
        if lock.get("transaction_id") != transaction_id:
            raise CapabilityError("Refusing to release a lock owned by another transaction")
        self.lock_path.unlink()
        fsync_parent(self.lock_path)

    def _checkpoint(
        self,
        journal: dict[str, Any],
        state: str,
        crash_after: str | None,
    ) -> None:
        journal["state"] = state
        journal["updated_at"] = utc_now()
        atomic_write_json(self.journal_path, journal)
        if crash_after == state:
            raise InjectedCrash(state)

    def run(
        self,
        *,
        crash_after: str | None = None,
        fail_before: str | None = None,
        after_prepare: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        plan = self.plan()
        transaction_id = uuid.uuid4().hex
        self._acquire(transaction_id)
        if fail_before == "journal":
            self._release(transaction_id)
            raise OSError("simulated disk-full journal write failure")
        self.backup_root.mkdir(exist_ok=True)
        self.quarantine_root.mkdir(exist_ok=True)
        backup = self.backup_root / f"target-{transaction_id}"
        quarantine = self.quarantine_root / f"new-target-{transaction_id}"
        old_rules = self.rules.read_bytes()
        journal: dict[str, Any] = {
            "schema_version": 1,
            "transaction_id": transaction_id,
            "strategy": plan["strategy"],
            "target": str(self.target),
            "staged": str(self.staged),
            "backup": str(backup),
            "quarantine": str(quarantine),
            "rules": str(self.rules),
            "rules_backup": str(self.rules_backup),
            "old_fingerprint": plan["old_fingerprint"],
            "new_fingerprint": plan["new_fingerprint"],
            "old_rules_sha256": plan["old_rules_sha256"],
            "new_rules_sha256": plan["new_rules_sha256"],
            "created_at": utc_now(),
        }
        self._checkpoint(journal, "preparing", crash_after)
        if fail_before == "rules_backup":
            raise OSError("simulated rules backup write failure")
        atomic_write_bytes(self.rules_backup, old_rules)
        if sha256_bytes(self.rules_backup.read_bytes()) != plan["old_rules_sha256"]:
            raise CapabilityError("Rules backup verification failed")
        self._checkpoint(journal, "rules_backed_up", crash_after)
        self._checkpoint(journal, "prepared", crash_after)
        if after_prepare:
            after_prepare()
        if scan_directory(self.staged)["payload_fingerprint"] != plan["new_fingerprint"]:
            raise CapabilityError("Staged payload changed after preparation")
        if fail_before == "target_move":
            raise OSError("simulated file-lock or permission denial")
        os.replace(self.target, backup)
        self._checkpoint(journal, "target_moved", crash_after)
        backup_hash = scan_directory(backup)["payload_fingerprint"]
        if backup_hash != plan["old_fingerprint"]:
            raise CapabilityError("Backup payload fingerprint mismatch")
        atomic_write_json(
            self.backup_complete,
            {
                "transaction_id": transaction_id,
                "backup": str(backup),
                "payload_fingerprint": backup_hash,
                "rules_sha256": plan["old_rules_sha256"],
                "complete_at": utc_now(),
            },
        )
        self._checkpoint(journal, "backup_verified", crash_after)
        if scan_directory(self.staged)["payload_fingerprint"] != plan["new_fingerprint"]:
            raise CapabilityError("Staged payload was tampered before activation")
        os.replace(self.staged, self.target)
        activated = scan_directory(self.target)["payload_fingerprint"]
        if activated != plan["new_fingerprint"]:
            raise CapabilityError("Post-activate payload fingerprint mismatch")
        self._checkpoint(journal, "target_activated", crash_after)
        if fail_before == "rules_replace":
            raise OSError("simulated rules-file lock denial")
        atomic_write_bytes(self.rules, self.new_rules)
        if sha256_bytes(self.rules.read_bytes()) != plan["new_rules_sha256"]:
            raise CapabilityError("Post-activate rules fingerprint mismatch")
        self._checkpoint(journal, "rules_activated", crash_after)
        self._checkpoint(journal, "committed", crash_after)
        self._release(transaction_id)
        return {
            "status": "COMMITTED",
            "transaction_id": transaction_id,
            "backup": str(backup),
            "backup_retained": True,
            "post_activate_fingerprint": activated,
            "rules_sha256": plan["new_rules_sha256"],
        }

    def recover(
        self,
        *,
        owner_authorized: bool,
        crash_after: str | None = None,
    ) -> dict[str, Any]:
        if not owner_authorized:
            raise CapabilityError("Recovery requires explicit owner authorization")
        journal = read_json(self.journal_path)
        transaction_id = str(journal.get("transaction_id", ""))
        lock = read_json(self.lock_path)
        if not transaction_id or lock.get("transaction_id") != transaction_id:
            raise CapabilityError("Journal and lock transaction IDs do not match")
        expected_paths = {
            "target": self.target,
            "staged": self.staged,
            "rules": self.rules,
            "rules_backup": self.rules_backup,
        }
        for key, expected in expected_paths.items():
            if Path(str(journal.get(key, ""))).resolve(strict=False) != expected.resolve(strict=False):
                raise CapabilityError(f"Journal path mismatch for {key}")
        backup = Path(str(journal.get("backup", ""))).resolve(strict=False)
        quarantine = Path(str(journal.get("quarantine", ""))).resolve(strict=False)
        for candidate in (backup, quarantine):
            try:
                candidate.relative_to(self.root)
            except ValueError as exc:
                raise CapabilityError("Journal path escapes proof root") from exc
        state = str(journal.get("state", ""))
        if state == "committed":
            self._release(transaction_id)
            return {"status": "COMMITTED", "transaction_id": transaction_id}

        backup_required_states = {
            "target_moved",
            "backup_verified",
            "target_activated",
            "rules_activated",
            "rollback_new_quarantined",
        }
        if state in backup_required_states:
            if not backup.is_dir():
                raise CapabilityError("Recovery backup is missing")
            backup_hash = scan_directory(backup)["payload_fingerprint"]
            if backup_hash != journal.get("old_fingerprint"):
                raise CapabilityError("Recovery backup is corrupt; target was not overwritten")

        active_new_states = {"target_activated", "rules_activated"}
        if state in active_new_states:
            if not self.target.is_dir():
                raise CapabilityError("Active target is missing during rollback")
            if scan_directory(self.target)["payload_fingerprint"] != journal.get("new_fingerprint"):
                raise CapabilityError("Active target changed; automatic rollback refused")
            os.replace(self.target, quarantine)
            self._checkpoint(journal, "rollback_new_quarantined", crash_after)
            state = "rollback_new_quarantined"

        if state in {"target_moved", "backup_verified", "rollback_new_quarantined"}:
            if self.target.exists():
                raise CapabilityError("Rollback target unexpectedly exists")
            os.replace(backup, self.target)
            if scan_directory(self.target)["payload_fingerprint"] != journal.get(
                "old_fingerprint"
            ):
                raise CapabilityError("Restored target fingerprint mismatch")
            self._checkpoint(journal, "rollback_target_restored", crash_after)
            state = "rollback_target_restored"

        untouched_target_states = {"preparing", "rules_backed_up", "prepared"}
        if state in untouched_target_states:
            if not self.target.is_dir():
                raise CapabilityError("Original target is missing before activation")
            if scan_directory(self.target)["payload_fingerprint"] != journal.get(
                "old_fingerprint"
            ):
                raise CapabilityError("Original target changed before activation")

        if state in {
            "rules_backed_up",
            "prepared",
            "rollback_target_restored",
            "rollback_rules_restored",
        }:
            rules_backup = self.rules_backup.read_bytes()
            if sha256_bytes(rules_backup) != journal.get("old_rules_sha256"):
                raise CapabilityError("Rules backup is corrupt")
            if sha256_bytes(self.rules.read_bytes()) != journal.get("old_rules_sha256"):
                atomic_write_bytes(self.rules, rules_backup)
            self._checkpoint(journal, "rollback_rules_restored", crash_after)
            state = "rollback_rules_restored"

        if state != "rolled_back":
            self._checkpoint(journal, "rolled_back", crash_after)
        self._release(transaction_id)
        return {
            "status": "ROLLED_BACK",
            "transaction_id": transaction_id,
            "target_fingerprint": scan_directory(self.target)["payload_fingerprint"],
            "rules_sha256": sha256_bytes(self.rules.read_bytes()),
            "quarantine_retained": quarantine.exists(),
        }


class TempFirstInstallTransaction:
    """Recoverable first install for a plain skill target constrained to TEMP."""

    def __init__(self, root: Path | str) -> None:
        self.root = assert_proof_root(root)
        self.target = self.root / "target"
        self.staged = self.root / "staged"
        self.state_root = self.root / ".first-install-state"
        self.quarantine_root = self.root / "quarantine"
        self.lock_path = self.state_root / "target.lock.json"
        self.journal_path = self.state_root / "journal.json"
        self.rollback_journal_path = self.state_root / "rollback-journal.json"
        self.prior_state = self.state_root / "prior-state.json"

    def plan(self) -> dict[str, Any]:
        if self.target.exists():
            raise CapabilityError("First-install target must be absent")
        if not self.staged.is_dir():
            raise CapabilityError("First-install proof requires a staged directory")
        staged = scan_directory(self.staged, reject_hardlinks=True)
        return {
            "strategy": "same-volume-first-install-journal",
            "target": str(self.target),
            "staged": str(self.staged),
            "target_preexisting": False,
            "new_fingerprint": staged["payload_fingerprint"],
            "writes_performed": 0,
        }

    def _acquire(self, transaction_id: str) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        data = {
            "transaction_id": transaction_id,
            "pid": os.getpid(),
            "created_at": utc_now(),
        }
        try:
            descriptor = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError as exc:
            raise CapabilityError(
                "Per-target lock already exists; automatic stale-lock removal is forbidden"
            ) from exc
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(json.dumps(data, sort_keys=True).encode("ascii") + b"\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _release(self, transaction_id: str) -> None:
        lock = read_json(self.lock_path)
        if lock.get("transaction_id") != transaction_id:
            raise CapabilityError("Refusing to release a lock owned by another transaction")
        self.lock_path.unlink()
        fsync_parent(self.lock_path)

    def _checkpoint(
        self,
        journal: dict[str, Any],
        state: str,
        crash_after: str | None,
    ) -> None:
        journal["state"] = state
        journal["updated_at"] = utc_now()
        atomic_write_json(self.journal_path, journal)
        if crash_after == state:
            raise InjectedCrash(state)

    def run(
        self,
        *,
        crash_after: str | None = None,
        fail_before: str | None = None,
        after_prepare: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        plan = self.plan()
        transaction_id = uuid.uuid4().hex
        self._acquire(transaction_id)
        if fail_before == "journal":
            self._release(transaction_id)
            raise OSError("simulated disk-full journal write failure")
        self.quarantine_root.mkdir(exist_ok=True)
        quarantine = self.quarantine_root / f"new-target-{transaction_id}"
        journal: dict[str, Any] = {
            "schema_version": 1,
            "transaction_id": transaction_id,
            "strategy": plan["strategy"],
            "target": str(self.target),
            "staged": str(self.staged),
            "quarantine": str(quarantine),
            "target_preexisting": False,
            "new_fingerprint": plan["new_fingerprint"],
            "created_at": utc_now(),
        }
        self._checkpoint(journal, "preparing", crash_after)
        self._checkpoint(journal, "prepared", crash_after)
        if after_prepare:
            after_prepare()
        if self.target.exists():
            raise CapabilityError("First-install target appeared after preparation")
        if scan_directory(self.staged, reject_hardlinks=True)[
            "payload_fingerprint"
        ] != plan["new_fingerprint"]:
            raise CapabilityError("Staged payload changed after preparation")
        atomic_write_json(
            self.prior_state,
            {
                "transaction_id": transaction_id,
                "target": str(self.target),
                "target_preexisting": False,
                "verified_at": utc_now(),
            },
        )
        self._checkpoint(journal, "prior_state_verified", crash_after)
        if fail_before == "activate":
            raise OSError("simulated target lock or permission denial")
        os.replace(self.staged, self.target)
        if crash_after == "target_renamed":
            raise InjectedCrash("target_renamed")
        activated = scan_directory(self.target, reject_hardlinks=True)[
            "payload_fingerprint"
        ]
        if activated != plan["new_fingerprint"]:
            raise CapabilityError("Post-activate payload fingerprint mismatch")
        self._checkpoint(journal, "target_activated", crash_after)
        self._checkpoint(journal, "committed", crash_after)
        self._release(transaction_id)
        return {
            "status": "COMMITTED",
            "transaction_id": transaction_id,
            "target_preexisting": False,
            "post_activate_fingerprint": activated,
            "rollback_contract": "restore verified target absence",
        }

    def recover(self, *, owner_authorized: bool) -> dict[str, Any]:
        if not owner_authorized:
            raise CapabilityError("Recovery requires explicit owner authorization")
        journal = read_json(self.journal_path)
        transaction_id = str(journal.get("transaction_id", ""))
        lock = read_json(self.lock_path)
        if not transaction_id or lock.get("transaction_id") != transaction_id:
            raise CapabilityError("Journal and lock transaction IDs do not match")
        if journal.get("target_preexisting") is not False:
            raise CapabilityError("First-install journal has an invalid prior state")
        expected_paths = {
            "target": self.target,
            "staged": self.staged,
        }
        for key, expected in expected_paths.items():
            if Path(str(journal.get(key, ""))).resolve(strict=False) != expected.resolve(
                strict=False
            ):
                raise CapabilityError(f"Journal path mismatch for {key}")
        quarantine = Path(str(journal.get("quarantine", ""))).resolve(strict=False)
        try:
            quarantine.relative_to(self.root)
        except ValueError as exc:
            raise CapabilityError("Journal quarantine path escapes proof root") from exc
        state = str(journal.get("state", ""))
        if state == "committed":
            if not self.target.is_dir():
                raise CapabilityError("Committed first-install target is missing")
            if scan_directory(self.target, reject_hardlinks=True)[
                "payload_fingerprint"
            ] != journal.get("new_fingerprint"):
                raise CapabilityError(
                    "Committed first-install target changed; recovery refused"
                )
            self._release(transaction_id)
            return {
                "status": "COMMITTED",
                "transaction_id": transaction_id,
                "target_present": self.target.is_dir(),
            }

        if self.target.exists():
            if not self.target.is_dir():
                raise CapabilityError("Unexpected non-directory first-install target")
            active = scan_directory(self.target, reject_hardlinks=True)[
                "payload_fingerprint"
            ]
            if active != journal.get("new_fingerprint"):
                raise CapabilityError(
                    "Active first-install target changed; automatic rollback refused"
                )
            if quarantine.exists():
                raise CapabilityError("First-install quarantine already exists")
            os.replace(self.target, quarantine)
            self._checkpoint(journal, "rollback_new_quarantined", None)
        elif state in {"target_activated", "rollback_new_quarantined"} and not quarantine.is_dir():
            raise CapabilityError("Activated target and rollback quarantine are both missing")

        if self.target.exists():
            raise CapabilityError("First-install rollback did not restore target absence")
        if quarantine.exists() and scan_directory(
            quarantine, reject_hardlinks=True
        )["payload_fingerprint"] != journal.get("new_fingerprint"):
            raise CapabilityError("First-install rollback quarantine changed")
        self._checkpoint(journal, "rolled_back", None)
        self._release(transaction_id)
        return {
            "status": "ROLLED_BACK",
            "transaction_id": transaction_id,
            "target_preexisting": False,
            "target_present": False,
            "quarantine_retained": quarantine.exists(),
        }

    def _rollback_checkpoint(
        self,
        journal: dict[str, Any],
        state: str,
        crash_after: str | None,
    ) -> None:
        journal["state"] = state
        journal["updated_at"] = utc_now()
        atomic_write_json(self.rollback_journal_path, journal)
        if crash_after == state:
            raise InjectedCrash(state)

    def rollback_committed(
        self,
        *,
        owner_authorized: bool,
        crash_after: str | None = None,
    ) -> dict[str, Any]:
        if not owner_authorized:
            raise CapabilityError("Committed rollback requires explicit owner authorization")
        install = read_json(self.journal_path)
        if install.get("state") != "committed":
            raise CapabilityError("First-install transaction is not committed")
        original_transaction_id = str(install.get("transaction_id", ""))
        if not original_transaction_id or install.get("target_preexisting") is not False:
            raise CapabilityError("First-install transaction journal is invalid")
        if Path(str(install.get("target", ""))).resolve(
            strict=False
        ) != self.target.resolve(strict=False) or Path(
            str(install.get("staged", ""))
        ).resolve(strict=False) != self.staged.resolve(strict=False):
            raise CapabilityError("First-install transaction journal path mismatch")
        prior = read_json(self.prior_state)
        if (
            prior.get("transaction_id") != original_transaction_id
            or prior.get("target_preexisting") is not False
            or Path(str(prior.get("target", ""))).resolve(strict=False)
            != self.target.resolve(strict=False)
        ):
            raise CapabilityError("First-install prior-state receipt is invalid")
        if self.lock_path.exists():
            raise CapabilityError("Another first-install operation holds the target lock")
        if not self.target.is_dir():
            raise CapabilityError("Committed first-install target is missing")
        expected = str(install.get("new_fingerprint", ""))
        if scan_directory(self.target, reject_hardlinks=True)[
            "payload_fingerprint"
        ] != expected:
            raise CapabilityError("Committed target changed; rollback refused")

        rollback_id = uuid.uuid4().hex
        self._acquire(rollback_id)
        quarantine = self.quarantine_root / f"rollback-target-{rollback_id}"
        journal: dict[str, Any] = {
            "schema_version": 1,
            "transaction_id": rollback_id,
            "original_transaction_id": original_transaction_id,
            "target": str(self.target),
            "quarantine": str(quarantine),
            "expected_fingerprint": expected,
            "target_preexisting": False,
            "created_at": utc_now(),
        }
        self._rollback_checkpoint(journal, "rollback_preparing", crash_after)
        if scan_directory(self.target, reject_hardlinks=True)[
            "payload_fingerprint"
        ] != expected:
            raise CapabilityError("Committed target changed during rollback preparation")
        self._rollback_checkpoint(journal, "rollback_prepared", crash_after)
        os.replace(self.target, quarantine)
        if crash_after == "rollback_target_renamed":
            raise InjectedCrash("rollback_target_renamed")
        self._rollback_checkpoint(journal, "rollback_target_quarantined", crash_after)
        if self.target.exists() or not quarantine.is_dir():
            raise CapabilityError("Committed rollback did not restore target absence")
        if scan_directory(quarantine, reject_hardlinks=True)[
            "payload_fingerprint"
        ] != expected:
            raise CapabilityError("Committed rollback quarantine fingerprint mismatch")
        self._rollback_checkpoint(journal, "rolled_back", crash_after)
        self._release(rollback_id)
        return {
            "status": "ROLLED_BACK",
            "transaction_id": rollback_id,
            "original_transaction_id": original_transaction_id,
            "target_preexisting": False,
            "target_present": False,
            "quarantine": str(quarantine),
            "quarantine_retained": True,
        }

    def recover_committed_rollback(self, *, owner_authorized: bool) -> dict[str, Any]:
        if not owner_authorized:
            raise CapabilityError("Rollback recovery requires explicit owner authorization")
        journal = read_json(self.rollback_journal_path)
        rollback_id = str(journal.get("transaction_id", ""))
        lock = read_json(self.lock_path)
        if not rollback_id or lock.get("transaction_id") != rollback_id:
            raise CapabilityError("Rollback journal and lock transaction IDs do not match")
        if journal.get("target_preexisting") is not False:
            raise CapabilityError("Rollback journal has an invalid prior state")
        install = read_json(self.journal_path)
        if (
            install.get("state") != "committed"
            or journal.get("original_transaction_id") != install.get("transaction_id")
            or journal.get("expected_fingerprint") != install.get("new_fingerprint")
        ):
            raise CapabilityError("Rollback journal does not match committed install")
        if Path(str(journal.get("target", ""))).resolve(strict=False) != self.target.resolve(
            strict=False
        ):
            raise CapabilityError("Rollback journal target path mismatch")
        quarantine = Path(str(journal.get("quarantine", ""))).resolve(strict=False)
        try:
            quarantine.relative_to(self.root)
        except ValueError as exc:
            raise CapabilityError("Rollback quarantine path escapes proof root") from exc
        expected = str(journal.get("expected_fingerprint", ""))

        if self.target.exists():
            if not self.target.is_dir():
                raise CapabilityError("Unexpected non-directory rollback target")
            if quarantine.exists():
                raise CapabilityError("Rollback target and quarantine both exist")
            if scan_directory(self.target, reject_hardlinks=True)[
                "payload_fingerprint"
            ] != expected:
                raise CapabilityError("Committed target changed; rollback recovery refused")
            os.replace(self.target, quarantine)
            self._rollback_checkpoint(journal, "rollback_target_quarantined", None)
        elif not quarantine.is_dir():
            raise CapabilityError("Rollback target and quarantine are both missing")

        if scan_directory(quarantine, reject_hardlinks=True)[
            "payload_fingerprint"
        ] != expected:
            raise CapabilityError("Rollback quarantine fingerprint mismatch")
        if self.target.exists():
            raise CapabilityError("Rollback recovery did not restore target absence")
        self._rollback_checkpoint(journal, "rolled_back", None)
        self._release(rollback_id)
        return {
            "status": "ROLLED_BACK",
            "transaction_id": rollback_id,
            "target_preexisting": False,
            "target_present": False,
            "quarantine": str(quarantine),
            "quarantine_retained": True,
        }


def probe_versioned_link_switch(root: Path | str) -> dict[str, Any]:
    proof_root = assert_proof_root(root)
    area = proof_root / "link-strategy"
    area.mkdir()
    version_one = area / "version-1"
    version_two = area / "version-2"
    version_one.mkdir()
    version_two.mkdir()
    (version_one / "value.txt").write_text("one\n", encoding="ascii")
    (version_two / "value.txt").write_text("two\n", encoding="ascii")
    current = area / "current"
    candidate = area / "candidate"
    result: dict[str, Any] = {
        "strategy": "versioned-directory-link-switch",
        "link_created": False,
        "atomic_replace_supported": False,
    }
    try:
        if os.name == "nt":
            create_one = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(current), str(version_one)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            create_two = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(candidate), str(version_two)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if create_one.returncode or create_two.returncode:
                result["reason"] = "junction creation unavailable"
                return result
        else:
            os.symlink(version_one.name, current, target_is_directory=True)
            os.symlink(version_two.name, candidate, target_is_directory=True)
        result["link_created"] = True
        try:
            os.replace(candidate, current)
            result["atomic_replace_supported"] = (
                (current / "value.txt").read_text(encoding="ascii") == "two\n"
            )
        except OSError as exc:
            result["reason"] = f"one-call link replacement failed: {type(exc).__name__}"
        return result
    finally:
        for link in (candidate, current):
            if link.exists() or link.is_symlink():
                try:
                    if link.is_dir() and not link.is_symlink() and os.name == "nt":
                        os.rmdir(link)
                    else:
                        link.unlink()
                except OSError:
                    pass
