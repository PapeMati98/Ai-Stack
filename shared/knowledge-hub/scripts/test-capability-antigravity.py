#!/usr/bin/env python3
"""Fixture-only tests for the V1.1 Antigravity design-ui adapter."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import stat
import tempfile
import unittest
import uuid
from dataclasses import dataclass
from pathlib import Path

import capability_core as core
from capability_antigravity import (
    ACTIVATION_BOUNDARY,
    ALLOWED_ACTIONS,
    APPROVAL_KIND,
    CAPABILITY_VERSION,
    FIXTURE_PREFIX,
    SOURCE_PATH,
    TOOL,
    AntigravityFirstInstall,
    AntigravityPaths,
    InjectedCrash,
    canonical_json_bytes,
    resolve_paths,
)


def write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def git(repo: Path, *arguments: str) -> str:
    return core.run_git(repo, list(arguments)).decode("utf-8", errors="strict").strip()


@dataclass
class Fixture:
    root: Path
    paths: AntigravityPaths
    repo: Path
    commit: str
    installer: AntigravityFirstInstall
    approval: Path
    config_bytes: bytes
    rules_bytes: bytes


class AntigravityAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = Path(__file__).resolve().parents[1]
        self.release = self.project / SOURCE_PATH
        self.roots: list[Path] = []

    def tearDown(self) -> None:
        temp_root = Path(tempfile.gettempdir()).resolve(strict=True)

        def make_writable(function: object, path: str, _: object) -> None:
            os.chmod(path, stat.S_IWRITE)
            function(path)

        for root in self.roots:
            resolved = root.resolve(strict=True)
            self.assertEqual(resolved.parent, temp_root)
            self.assertTrue(resolved.name.startswith(FIXTURE_PREFIX))
            shutil.rmtree(resolved, onerror=make_writable)

    def release_files(self) -> dict[str, bytes]:
        result: dict[str, bytes] = {}
        for path in self.release.rglob("*"):
            if path.is_symlink():
                self.fail(f"Release contains a symlink: {path}")
            if path.is_file():
                result[path.relative_to(self.release).as_posix()] = path.read_bytes()
        return result

    def new_fixture(self) -> Fixture:
        root = Path(tempfile.mkdtemp(prefix=FIXTURE_PREFIX))
        self.roots.append(root)
        paths = resolve_paths(root)
        paths.scan_root.mkdir(parents=True)
        config_bytes = b'{"fixture":true,"grant":"reviewed"}\n'
        rules_bytes = b"# Fixture Antigravity rules\n"
        write_bytes(paths.config, config_bytes)
        write_bytes(paths.rules, rules_bytes)

        repo = root / "repo"
        repo.mkdir()
        git(repo, "init", "--quiet", "--initial-branch=main")
        git(repo, "config", "user.name", "Capability Antigravity Test")
        git(repo, "config", "user.email", "capability-antigravity@invalid.local")
        git(repo, "config", "core.autocrlf", "false")
        write_bytes(repo / ".gitattributes", b"capabilities/** -text\n")
        destination = repo / SOURCE_PATH
        for relative, content in self.release_files().items():
            write_bytes(destination.joinpath(*relative.split("/")), content)
        git(repo, "add", "--all")
        git(repo, "commit", "--quiet", "-m", "design-ui 0.1.0 fixture")
        commit = git(repo, "rev-parse", "HEAD")
        config_hash = hashlib.sha256(config_bytes).hexdigest()
        rules_hash = hashlib.sha256(rules_bytes).hexdigest()
        posture_hash = hashlib.sha256(b"fixture reviewed grant").hexdigest()
        installer = AntigravityFirstInstall(
            repo=repo,
            commit=commit,
            paths=paths,
            expected_config_sha256=config_hash,
            expected_rules_sha256=rules_hash,
            approval_posture_sha256=posture_hash,
        )
        _, context = installer._context(require_current_hashes=True)
        now = dt.datetime.now(dt.timezone.utc)
        approval_data = {
            key: value
            for key, value in context.items()
            if not key.startswith("observed_")
        }
        approval_data.update(
            {
                "schema_version": 1,
                "approval_kind": APPROVAL_KIND,
                "approval_id": f"v11b-{uuid.uuid4().hex}",
                "approved_by": "Mateusz",
                "approved_at": (now - dt.timedelta(minutes=1)).isoformat().replace(
                    "+00:00", "Z"
                ),
                "expires_at": (now + dt.timedelta(hours=1)).isoformat().replace(
                    "+00:00", "Z"
                ),
                "owner_decision": (
                    "GO V1.1B: design-ui -> Antigravity; "
                    "install, observed discovery and rollback drill."
                ),
                "authorized_actions": sorted(ALLOWED_ACTIONS),
            }
        )
        approval = root / "approval.json"
        approval.write_bytes(canonical_json_bytes(approval_data))
        return Fixture(
            root=root,
            paths=paths,
            repo=repo,
            commit=commit,
            installer=installer,
            approval=approval,
            config_bytes=config_bytes,
            rules_bytes=rules_bytes,
        )

    def profile_snapshot(self, fixture: Fixture) -> dict[str, tuple[int, str]]:
        result: dict[str, tuple[int, str]] = {}
        for path in sorted(
            item for item in fixture.paths.profile.rglob("*") if item.is_file()
        ):
            result[path.relative_to(fixture.paths.profile).as_posix()] = (
                path.stat().st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        return result

    def test_plan_is_zero_write_and_requires_a_full_oid(self) -> None:
        fixture = self.new_fixture()
        before = self.profile_snapshot(fixture)
        result = fixture.installer.plan()
        self.assertEqual(result["status"], "READY_FOR_V1.1B_APPROVAL")
        self.assertEqual(result["writes_performed"], 0)
        self.assertEqual(result["release"]["file_count"], 4)
        self.assertFalse(result["target_preexisting"])
        self.assertFalse(result["state_root_preexisting"])
        self.assertEqual(before, self.profile_snapshot(fixture))
        self.assertFalse(fixture.paths.target.exists())
        self.assertFalse(fixture.paths.state_root.exists())

        symbolic = AntigravityFirstInstall(
            repo=fixture.repo,
            commit="HEAD",
            paths=fixture.paths,
            expected_config_sha256=hashlib.sha256(fixture.config_bytes).hexdigest(),
            expected_rules_sha256=hashlib.sha256(fixture.rules_bytes).hexdigest(),
            approval_posture_sha256=hashlib.sha256(
                b"fixture reviewed grant"
            ).hexdigest(),
        )
        with self.assertRaisesRegex(core.CapabilityError, "full Git object ID"):
            symbolic.plan()

    def test_install_requires_owner_and_canonical_unexpired_receipt(self) -> None:
        fixture = self.new_fixture()
        with self.assertRaisesRegex(core.CapabilityError, "owner authorization"):
            fixture.installer.install(fixture.approval, owner_authorized=False)
        self.assertFalse(fixture.paths.state_root.exists())

        data = json.loads(fixture.approval.read_text(encoding="ascii"))
        data["expires_at"] = "2020-01-01T00:00:00Z"
        fixture.approval.write_bytes(canonical_json_bytes(data))
        with self.assertRaisesRegex(core.CapabilityError, "expired"):
            fixture.installer.install(fixture.approval, owner_authorized=True)
        self.assertFalse(fixture.paths.state_root.exists())

    def test_approval_reuse_is_rechecked_under_lock(self) -> None:
        fixture = self.new_fixture()
        calls = 0

        def raced_approval(_: str) -> bool:
            nonlocal calls
            calls += 1
            return calls >= 2

        fixture.installer._approval_used_for_install = raced_approval
        with self.assertRaisesRegex(core.CapabilityError, "already consumed"):
            fixture.installer.install(fixture.approval, owner_authorized=True)
        self.assertEqual(calls, 2)
        self.assertFalse(fixture.paths.target.exists())
        self.assertFalse(fixture.paths.lock_path.exists())
        self.assertTrue(fixture.paths.state_root.is_dir())
        self.assertEqual(list(fixture.paths.operations_root.iterdir()), [])

    def test_install_verify_rollback_and_approval_single_use(self) -> None:
        fixture = self.new_fixture()
        installed = fixture.installer.install(
            fixture.approval, owner_authorized=True
        )
        self.assertEqual(installed["status"], "COMMITTED")
        self.assertTrue(fixture.paths.target.is_dir())
        verified = core.verify_target(
            fixture.paths.target,
            TOOL,
            repo=fixture.repo,
            commit=fixture.commit,
            source_path=SOURCE_PATH,
            activation_boundary=ACTIVATION_BOUNDARY,
            last_fetch=core.utc_now(),
        )
        self.assertEqual(verified["status"], "CURRENT")
        self.assertEqual(verified["marker"]["schema_version"], 2)
        self.assertEqual(
            core.read_marker(fixture.paths.target / core.CAPABILITY_MARKER_NAME).data[
                "capability_version"
            ],
            CAPABILITY_VERSION,
        )
        rolled_back = fixture.installer.rollback(
            fixture.approval, owner_authorized=True
        )
        self.assertEqual(rolled_back["status"], "ROLLED_BACK")
        self.assertFalse(fixture.paths.target.exists())
        self.assertTrue(Path(rolled_back["quarantine"]).is_dir())
        with self.assertRaisesRegex(core.CapabilityError, "already consumed"):
            fixture.installer.install(fixture.approval, owner_authorized=True)
        self.assertEqual(fixture.paths.config.read_bytes(), fixture.config_bytes)
        self.assertEqual(fixture.paths.rules.read_bytes(), fixture.rules_bytes)

    def test_install_crash_recovery_is_idempotent(self) -> None:
        states = (
            "preparing",
            "approval_recorded",
            "staged",
            "prior_state_verified",
            "prepared",
            "target_renamed",
            "target_activated",
            "active_recorded",
            "committed",
        )
        for state in states:
            with self.subTest(state=state):
                fixture = self.new_fixture()
                with self.assertRaises(InjectedCrash):
                    fixture.installer.install(
                        fixture.approval,
                        owner_authorized=True,
                        crash_after=state,
                    )
                recovered = fixture.installer.recover_install(
                    fixture.approval, owner_authorized=True
                )
                if state == "committed":
                    self.assertEqual(recovered["status"], "COMMITTED")
                    self.assertTrue(fixture.paths.target.is_dir())
                else:
                    self.assertEqual(recovered["status"], "ROLLED_BACK")
                    self.assertFalse(fixture.paths.target.exists())
                self.assertFalse(fixture.paths.lock_path.exists())

    def test_rollback_crash_recovery_restores_exact_absence(self) -> None:
        states = (
            "rollback_preparing",
            "approval_recorded",
            "rollback_prepared",
            "rollback_target_renamed",
            "rollback_target_quarantined",
            "active_cleared",
            "rolled_back",
        )
        for state in states:
            with self.subTest(state=state):
                fixture = self.new_fixture()
                fixture.installer.install(fixture.approval, owner_authorized=True)
                with self.assertRaises(InjectedCrash):
                    fixture.installer.rollback(
                        fixture.approval,
                        owner_authorized=True,
                        crash_after=state,
                    )
                recovered = fixture.installer.recover_rollback(
                    fixture.approval, owner_authorized=True
                )
                self.assertEqual(recovered["status"], "ROLLED_BACK")
                self.assertFalse(fixture.paths.target.exists())
                quarantine = Path(recovered["quarantine"])
                self.assertTrue(quarantine.is_dir())
                self.assertFalse(fixture.paths.lock_path.exists())

    def test_config_drift_before_activation_fails_and_recovers_absence(self) -> None:
        fixture = self.new_fixture()

        def drift_config() -> None:
            fixture.paths.config.write_bytes(b'{"fixture":false}\n')

        with self.assertRaisesRegex(core.CapabilityError, "config changed"):
            fixture.installer.install(
                fixture.approval,
                owner_authorized=True,
                after_prepare=drift_config,
            )
        recovered = fixture.installer.recover_install(
            fixture.approval, owner_authorized=True
        )
        self.assertEqual(recovered["status"], "ROLLED_BACK")
        self.assertFalse(fixture.paths.target.exists())

    def test_unexpected_target_and_tampered_active_target_are_preserved(self) -> None:
        fixture = self.new_fixture()

        def create_owner_target() -> None:
            fixture.paths.target.mkdir()
            (fixture.paths.target / "owner-file.txt").write_bytes(b"unmanaged\n")

        with self.assertRaisesRegex(core.CapabilityError, "appeared"):
            fixture.installer.install(
                fixture.approval,
                owner_authorized=True,
                after_prepare=create_owner_target,
            )
        with self.assertRaisesRegex(core.CapabilityError, "ambiguous"):
            fixture.installer.recover_install(
                fixture.approval, owner_authorized=True
            )
        self.assertEqual(
            (fixture.paths.target / "owner-file.txt").read_bytes(), b"unmanaged\n"
        )

        second = self.new_fixture()
        second.installer.install(second.approval, owner_authorized=True)
        (second.paths.target / "SKILL.md").write_bytes(b"tampered\n")
        with self.assertRaisesRegex(core.CapabilityError, "fingerprint mismatch"):
            second.installer.rollback(second.approval, owner_authorized=True)
        self.assertTrue(second.paths.target.is_dir())

    def test_backup_like_directory_blocks_plan_without_cleanup(self) -> None:
        fixture = self.new_fixture()
        backup = fixture.paths.scan_root / "llm-wiki.backup-20260718T000000Z-test"
        backup.mkdir()
        (backup / "SKILL.md").write_bytes(b"stale\n")
        with self.assertRaisesRegex(core.CapabilityError, "Backup-like directories"):
            fixture.installer.plan()
        self.assertTrue(backup.is_dir())
        self.assertFalse(fixture.paths.state_root.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
