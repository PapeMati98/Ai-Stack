#!/usr/bin/env python3
"""Regression suite for Capability Hub V1 read-only and Git-source contracts."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import capability as cli
import capability_core as core


def write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def git(repo: Path, *arguments: str) -> str:
    return core.run_git(repo, list(arguments)).decode("utf-8", errors="strict").strip()


class CapabilityV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="capability-v1-test-"))
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "--quiet", "--initial-branch=main")
        git(self.repo, "config", "user.name", "Capability V1 Test")
        git(self.repo, "config", "user.email", "capability-v1@invalid.local")
        git(self.repo, "config", "core.autocrlf", "false")
        self.source_path = "capabilities/test-skill"
        self.skill = self.repo / self.source_path
        write_bytes(
            self.skill / "SKILL.md",
            b"---\nname: test-skill\ndescription: Safe fixture.\n---\n\n# Test Skill\n",
        )
        write_bytes(
            self.skill / "capability.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "capability_id": "test-skill",
                    "capability_version": "1.0.0",
                    "kind": "instruction-skill",
                    "entrypoint": "SKILL.md",
                    "declared_permissions": [],
                    "enforcement": "none",
                    "revoked_at": None,
                },
                sort_keys=True,
                indent=2,
            ).encode("ascii")
            + b"\n",
        )
        write_bytes(self.skill / "references/design.md", b"# Design\n\nExact bytes.\n")
        git(self.repo, "add", "--all")
        git(self.repo, "commit", "--quiet", "-m", "fixture")
        self.commit = git(self.repo, "rev-parse", "HEAD")

    def tearDown(self) -> None:
        resolved = self.root.resolve(strict=True)
        temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
        self.assertEqual(resolved.parent, temp_root)
        self.assertTrue(resolved.name.startswith("capability-v1-test-"))

        def make_writable(function: object, path: str, _: object) -> None:
            os.chmod(path, stat.S_IWRITE)
            function(path)

        shutil.rmtree(resolved, onexc=make_writable)

    def source(self) -> core.GitSource:
        return core.inspect_git_source(
            self.repo, self.commit, self.source_path, pilot_policy=True
        )

    def install_fixture(self, source: core.GitSource) -> Path:
        target = self.root / "installed"
        target.mkdir()
        for entry in source.entries:
            write_bytes(target.joinpath(*entry.relative_path.split("/")), entry.content)
        return target

    def write_marker(
        self,
        target: Path,
        marker: dict[str, object],
        marker_name: str = core.MARKER_NAME,
    ) -> None:
        write_bytes(
            target / marker_name,
            json.dumps(marker, sort_keys=True, indent=2).encode("ascii") + b"\n",
        )

    def snapshot(self, root: Path) -> dict[str, tuple[int, str]]:
        result: dict[str, tuple[int, str]] = {}
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            relative = path.relative_to(root).as_posix()
            result[relative] = (
                path.stat().st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        return result

    def test_materializer_uses_committed_blobs_and_excludes_ignored_file(self) -> None:
        write_bytes(self.skill / "scripts/__pycache__/unsafe.pyc", b"ignored-bytecode")
        write_bytes(self.repo / ".gitignore", b"__pycache__/\n*.pyc\n")
        source = self.source()
        self.assertEqual(len(source.entries), 3)
        self.assertEqual(len(source.worktree_contamination), 1)
        self.assertIn("__pycache__", source.worktree_contamination[0]["path"])
        result = core.materialize_git_source(source)
        output = Path(result["materialized_to"])
        try:
            self.assertTrue(result["post_materialize_verified"])
            self.assertFalse((output / "scripts").exists())
            for entry in source.entries:
                self.assertEqual(
                    output.joinpath(*entry.relative_path.split("/")).read_bytes(),
                    entry.content,
                )
        finally:
            shutil.rmtree(output)

    def test_git_blob_bytes_ignore_checkout_eol_configuration(self) -> None:
        source_before = self.source()
        git(self.repo, "config", "core.autocrlf", "true")
        source_after = self.source()
        self.assertEqual(source_before.payload_fingerprint, source_after.payload_fingerprint)
        self.assertEqual(
            [entry.content for entry in source_before.entries],
            [entry.content for entry in source_after.entries],
        )

    def test_materialization_rejects_non_temp_output(self) -> None:
        with self.assertRaisesRegex(core.CapabilityError, "system TEMP"):
            core.materialize_git_source(self.source(), self.repo / "output")

    def test_post_materialization_tamper_is_detected(self) -> None:
        source = self.source()
        result = core.materialize_git_source(source)
        output = Path(result["materialized_to"])
        try:
            (output / "SKILL.md").write_bytes(b"tampered\n")
            scanned = core.scan_directory(output)
            self.assertNotEqual(scanned["payload_fingerprint"], source.payload_fingerprint)
        finally:
            shutil.rmtree(output)

    def test_staging_rejects_hardlinks(self) -> None:
        source = self.source()
        result = core.materialize_git_source(source)
        output = Path(result["materialized_to"])
        try:
            alias = output / "references/hardlink.md"
            os.link(output / "SKILL.md", alias)
            with self.assertRaisesRegex(core.CapabilityError, "Hard-linked"):
                core.scan_directory(output, reject_hardlinks=True)
        finally:
            shutil.rmtree(output)

    @unittest.skipUnless(os.name == "nt", "NTFS ADS is Windows-specific")
    def test_staging_rejects_named_ntfs_streams(self) -> None:
        target = self.root / "ads-target"
        target.mkdir()
        payload = target / "SKILL.md"
        payload.write_bytes(b"payload\n")
        try:
            with open(str(payload) + ":hidden", "wb") as stream:
                stream.write(b"hidden\n")
        except OSError as exc:
            self.skipTest(f"Filesystem does not support ADS: {exc}")
        with self.assertRaisesRegex(core.CapabilityError, "NTFS stream"):
            core.scan_directory(target)

    @unittest.skipUnless(os.name == "nt", "junction test is Windows-specific")
    def test_scanner_rejects_junction_root(self) -> None:
        real = self.root / "real-target"
        real.mkdir()
        (real / "SKILL.md").write_bytes(b"payload\n")
        junction = self.root / "junction-target"
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(real)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if created.returncode:
            self.skipTest("Junction creation is unavailable")
        with self.assertRaisesRegex(core.CapabilityError, "reparse point"):
            core.scan_directory(junction)
        os.rmdir(junction)

    def test_pilot_rejects_executable_mode(self) -> None:
        git(self.repo, "update-index", "--chmod=+x", f"{self.source_path}/SKILL.md")
        git(self.repo, "commit", "--quiet", "-m", "executable mode")
        commit = git(self.repo, "rev-parse", "HEAD")
        with self.assertRaisesRegex(core.CapabilityError, "mode/type"):
            core.inspect_git_source(self.repo, commit, self.source_path, pilot_policy=True)

    def test_pilot_rejects_symlink_and_submodule_modes(self) -> None:
        blob_file = self.root / "blob.txt"
        write_bytes(blob_file, b"target\n")
        blob_oid = git(self.repo, "hash-object", "-w", str(blob_file))
        git(
            self.repo,
            "update-index",
            "--add",
            "--cacheinfo",
            f"120000,{blob_oid},{self.source_path}/link.md",
        )
        git(self.repo, "commit", "--quiet", "-m", "symlink mode")
        symlink_commit = git(self.repo, "rev-parse", "HEAD")
        with self.assertRaisesRegex(core.CapabilityError, "mode/type"):
            core.inspect_git_source(
                self.repo, symlink_commit, self.source_path, pilot_policy=True
            )

        git(self.repo, "reset", "--quiet", "--hard", self.commit)
        git(
            self.repo,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{self.commit},{self.source_path}/submodule",
        )
        git(self.repo, "commit", "--quiet", "-m", "submodule mode")
        submodule_commit = git(self.repo, "rev-parse", "HEAD")
        with self.assertRaisesRegex(core.CapabilityError, "mode/type"):
            core.inspect_git_source(
                self.repo, submodule_commit, self.source_path, pilot_policy=True
            )

    def test_path_policy_rejects_hostile_and_colliding_paths(self) -> None:
        bad_paths = (
            "../escape.md",
            "/absolute.md",
            "references/CON.md",
            "references/trailing ",
            "references/name.md:stream",
            "references\\name.md",
        )
        for path in bad_paths:
            with self.subTest(path=path), self.assertRaises(core.CapabilityError):
                core.validate_relative_path(path, ascii_only=True)
        with self.assertRaisesRegex(core.CapabilityError, "collision"):
            core._validate_path_set(
                ("references/Case.md", "references/case.md"), ascii_only=True
            )

    def test_pilot_rejects_unknown_extension(self) -> None:
        write_bytes(self.skill / "run.py", b"print('no')\n")
        git(self.repo, "add", f"{self.source_path}/run.py")
        git(self.repo, "commit", "--quiet", "-m", "unknown file")
        commit = git(self.repo, "rev-parse", "HEAD")
        with self.assertRaisesRegex(core.CapabilityError, "instruction-only layout"):
            core.inspect_git_source(self.repo, commit, self.source_path, pilot_policy=True)

    def test_pilot_rejects_crlf_and_revoked_release(self) -> None:
        write_bytes(
            self.skill / "SKILL.md",
            b"---\r\nname: test-skill\r\ndescription: Unsafe EOL.\r\n---\r\n",
        )
        git(self.repo, "add", f"{self.source_path}/SKILL.md")
        git(self.repo, "commit", "--quiet", "-m", "crlf payload")
        crlf_commit = git(self.repo, "rev-parse", "HEAD")
        with self.assertRaisesRegex(core.CapabilityError, "LF line endings"):
            core.inspect_git_source(
                self.repo, crlf_commit, self.source_path, pilot_policy=True
            )

        git(self.repo, "reset", "--quiet", "--hard", self.commit)
        manifest_path = self.skill / "capability.json"
        manifest = json.loads(manifest_path.read_text(encoding="ascii"))
        manifest["revoked_at"] = "2026-07-18T00:00:00Z"
        write_bytes(
            manifest_path,
            json.dumps(manifest, sort_keys=True, indent=2).encode("ascii") + b"\n",
        )
        git(self.repo, "add", f"{self.source_path}/capability.json")
        git(self.repo, "commit", "--quiet", "-m", "revoked capability")
        revoked_commit = git(self.repo, "rev-parse", "HEAD")
        source = core.inspect_git_source(
            self.repo, revoked_commit, self.source_path, pilot_policy=True
        )
        with self.assertRaisesRegex(core.CapabilityError, "revoked"):
            core.materialize_git_source(source)

    def test_marker_dual_read_and_fail_closed_cases(self) -> None:
        fingerprint = "a" * 64
        windows = core.parse_marker_data(
            {
                "schema_version": 1,
                "managed_by": core.MANAGED_BY,
                "tool": "codex",
                "source_fingerprint": fingerprint,
            }
        )
        linux = core.parse_marker_data(
            {
                "schema_version": 1,
                "managed_by": core.MANAGED_BY,
                "tool": "hermes",
                "fingerprint": fingerprint,
            }
        )
        self.assertEqual(windows.variant, "v1-windows-source_fingerprint")
        self.assertEqual(linux.variant, "v1-linux-fingerprint")
        managed_copy = core.parse_marker_data(
            {
                "schema_version": 2,
                "managed_by": core.MANAGED_BY,
                "tool": "codex",
                "canonical_source": "C:\\hub\\skills\\llm-wiki",
                "source_fingerprint": fingerprint,
                "installed_at": "2026-08-23T22:09:09Z",
                "activation_boundary": "new session",
                "transaction_id": "1" * 32,
            }
        )
        self.assertEqual(managed_copy.variant, "v2-managed-copy-windows")
        self.assertTrue(managed_copy.legacy)
        with self.assertRaisesRegex(core.CapabilityError, "conflicting"):
            core.parse_marker_data(
                {
                    "schema_version": 1,
                    "managed_by": core.MANAGED_BY,
                    "tool": "codex",
                    "source_fingerprint": "a" * 64,
                    "fingerprint": "b" * 64,
                }
            )
        with self.assertRaisesRegex(core.CapabilityError, "Unsupported"):
            core.parse_marker_data(
                {
                    "schema_version": 99,
                    "managed_by": core.MANAGED_BY,
                    "tool": "codex",
                }
            )
        with self.assertRaisesRegex(core.CapabilityError, "Unknown v2"):
            core.parse_marker_data(
                {
                    "schema_version": 2,
                    "managed_by": core.MANAGED_BY,
                    "tool": "codex",
                    "capability_id": "test-skill",
                    "capability_version": "1.0.0",
                    "scope": "user",
                    "source_commit": "a" * 40,
                    "source_tree": "b" * 40,
                    "payload_fingerprint": "c" * 64,
                    "manifest_fingerprint": "d" * 64,
                    "installed_at": "2026-07-18T00:00:00Z",
                    "activation_boundary": "new session",
                    "unexpected": "blocked",
                }
            )

    def test_verifier_is_read_only_and_detects_legacy_drift(self) -> None:
        source = self.source()
        target = self.install_fixture(source)
        self.write_marker(
            target,
            {
                "schema_version": 1,
                "managed_by": core.MANAGED_BY,
                "tool": "codex",
                "source_fingerprint": core.scan_directory(target)[
                    "legacy_windows_payload_fingerprint"
                ],
            },
        )
        before = self.snapshot(target)
        temp_before = {
            path.name
            for path in Path(tempfile.gettempdir()).iterdir()
            if path.name.startswith("capability-materialize-")
        }
        result = core.verify_target(
            target,
            "codex",
            repo=self.repo,
            commit=self.commit,
            source_path=self.source_path,
            activation_boundary="new session",
        )
        temp_after = {
            path.name
            for path in Path(tempfile.gettempdir()).iterdir()
            if path.name.startswith("capability-materialize-")
        }
        self.assertEqual(result["status"], "LEGACY_MARKER")
        self.assertEqual(result["writes_performed"], 0)
        self.assertEqual(before, self.snapshot(target))
        self.assertEqual(temp_before, temp_after)

        (target / "SKILL.md").write_bytes(b"tampered\n")
        drift = core.verify_target(target, "codex")
        self.assertEqual(drift["status"], "DRIFT")

    def test_verifier_accepts_transactional_managed_copy_marker(self) -> None:
        source = self.source()
        target = self.install_fixture(source)
        self.write_marker(
            target,
            {
                "schema_version": 2,
                "managed_by": core.MANAGED_BY,
                "tool": "codex",
                "canonical_source": str(self.repo / self.source_path),
                "source_fingerprint": core.scan_directory(target)[
                    "legacy_windows_payload_fingerprint"
                ],
                "installed_at": "2026-08-23T22:09:09Z",
                "activation_boundary": "new session",
                "transaction_id": "1" * 32,
            },
        )
        result = core.verify_target(target, "codex")
        self.assertEqual(result["status"], "LEGACY_MARKER")
        self.assertEqual(result["marker"]["variant"], "v2-managed-copy-windows")

    def test_v2_marker_current_stale_and_source_unavailable(self) -> None:
        source = self.source()
        target = self.install_fixture(source)
        marker = core.marker_v2(
            capability_id="test-skill",
            capability_version="1.0.0",
            tool="codex",
            scope="user",
            source=source,
            activation_boundary="new session",
        )
        self.write_marker(target, marker)
        stale = core.verify_target(
            target,
            "codex",
            repo=self.repo,
            commit=self.commit,
            source_path=self.source_path,
        )
        self.assertEqual(stale["status"], "STALE_VIEW")
        current = core.verify_target(
            target,
            "codex",
            repo=self.repo,
            commit=self.commit,
            source_path=self.source_path,
            last_fetch="2026-07-18T00:00:00Z",
        )
        self.assertEqual(current["status"], "CURRENT")
        unavailable = core.verify_target(
            target,
            "codex",
            repo=self.repo,
            commit="0" * 40,
            source_path=self.source_path,
        )
        self.assertEqual(unavailable["status"], "SOURCE_UNAVAILABLE")

    def test_generic_marker_name_and_duplicate_marker_fail_closed(self) -> None:
        source = self.source()
        target = self.install_fixture(source)
        marker = core.marker_v2(
            capability_id="test-skill",
            capability_version="1.0.0",
            tool="codex",
            scope="user",
            source=source,
            activation_boundary="new session",
        )
        self.write_marker(target, marker, core.CAPABILITY_MARKER_NAME)
        current = core.verify_target(
            target,
            "codex",
            repo=self.repo,
            commit=self.commit,
            source_path=self.source_path,
            last_fetch="2026-07-18T00:00:00Z",
        )
        self.assertEqual(current["status"], "CURRENT")
        self.assertEqual(current["marker_file"], core.CAPABILITY_MARKER_NAME)
        self.assertEqual(core.scan_directory(target)["file_count"], 3)

        self.write_marker(target, marker, core.LEGACY_MARKER_NAME)
        conflict = core.verify_target(target, "codex")
        self.assertEqual(conflict["status"], "MALFORMED_MARKER")
        self.assertIn("Multiple managed marker", conflict["reason"])

    def test_status_all_managed_detects_revocation_and_backup_like_target(self) -> None:
        profile = self.root / "profile"
        appdata = self.root / "appdata"
        target = profile / ".codex/skills/test-skill"
        target.mkdir(parents=True)
        source = self.source()
        for entry in source.entries:
            write_bytes(target.joinpath(*entry.relative_path.split("/")), entry.content)
        marker = core.marker_v2(
            capability_id="test-skill",
            capability_version="1.0.0",
            tool="codex",
            scope="user",
            source=source,
            activation_boundary="new session",
        )
        self.write_marker(target, marker, core.CAPABILITY_MARKER_NAME)

        with patch("capability.Path.home", return_value=profile), patch.dict(
            os.environ, {"APPDATA": str(appdata)}
        ):
            active = cli.status_all_managed(
                tools=["codex"],
                hub_root=self.repo,
                catalog_commit=self.commit,
                last_fetch="2026-07-18T00:00:00Z",
            )
        self.assertEqual(active["status_counts"], {"CURRENT": 1})
        self.assertEqual(active["writes_performed"], 0)

        skill_path = self.skill / "SKILL.md"
        write_bytes(skill_path, b"---\nname: test-skill\n---\nchanged same version\n")
        git(self.repo, "add", f"{self.source_path}/SKILL.md")
        git(self.repo, "commit", "--quiet", "-m", "reuse version with changed bytes")
        changed_commit = git(self.repo, "rev-parse", "HEAD")
        with patch("capability.Path.home", return_value=profile), patch.dict(
            os.environ, {"APPDATA": str(appdata)}
        ):
            changed = cli.status_all_managed(
                tools=["codex"],
                hub_root=self.repo,
                catalog_commit=changed_commit,
                last_fetch="2026-07-18T01:00:00Z",
            )
            with redirect_stdout(io.StringIO()):
                changed_exit = cli.main(
                    [
                        "status",
                        "--all-managed",
                        "--tools",
                        "codex",
                        "--hub-root",
                        str(self.repo),
                        "--catalog-commit",
                        changed_commit,
                        "--last-fetch",
                        "2026-07-18T01:00:00Z",
                    ]
                )
        self.assertEqual(changed["status_counts"], {"STALE_VIEW": 1})
        self.assertEqual(changed_exit, 3)

        manifest_path = self.skill / "capability.json"
        manifest = json.loads(manifest_path.read_text(encoding="ascii"))
        manifest["revoked_at"] = "2026-07-18T01:00:00Z"
        write_bytes(
            manifest_path,
            json.dumps(manifest, sort_keys=True, indent=2).encode("ascii") + b"\n",
        )
        git(self.repo, "add", f"{self.source_path}/capability.json")
        git(self.repo, "commit", "--quiet", "-m", "revoke fixture")
        catalog_commit = git(self.repo, "rev-parse", "HEAD")
        with patch("capability.Path.home", return_value=profile), patch.dict(
            os.environ, {"APPDATA": str(appdata)}
        ):
            revoked = cli.status_all_managed(
                tools=["codex"],
                hub_root=self.repo,
                catalog_commit=catalog_commit,
                last_fetch="2026-07-18T02:00:00Z",
            )
        self.assertEqual(revoked["status_counts"], {"REVOKED": 1})
        self.assertEqual(revoked["results"][0]["revoked_at"], "2026-07-18T01:00:00Z")
        with patch("capability.Path.home", return_value=profile), patch.dict(
            os.environ, {"APPDATA": str(appdata)}
        ), redirect_stdout(io.StringIO()):
            revoked_exit = cli.main(
                [
                    "status",
                    "--all-managed",
                    "--tools",
                    "codex",
                    "--hub-root",
                    str(self.repo),
                    "--catalog-commit",
                    catalog_commit,
                    "--last-fetch",
                    "2026-07-18T02:00:00Z",
                ]
            )
        self.assertEqual(revoked_exit, 3)

        backup = target.with_name("test-skill.backup-fixture")
        target.rename(backup)
        with patch("capability.Path.home", return_value=profile), patch.dict(
            os.environ, {"APPDATA": str(appdata)}
        ):
            backup_result = cli.status_all_managed(
                tools=["codex"],
                hub_root=self.repo,
                catalog_commit=catalog_commit,
                last_fetch="2026-07-18T02:00:00Z",
            )
        self.assertEqual(backup_result["results"][0]["severity"], "HIGH")

    def test_malformed_marker_is_not_treated_as_unmanaged(self) -> None:
        source = self.source()
        target = self.install_fixture(source)
        write_bytes(target / core.MARKER_NAME, b"{not-json\n")
        result = core.verify_target(target, "codex")
        self.assertEqual(result["status"], "MALFORMED_MARKER")

        write_bytes(
            target / core.MARKER_NAME,
            b'{"schema_version":1,"schema_version":1,"managed_by":"shared-agent-knowledge-hub","tool":"codex","source_fingerprint":"'
            + b"a" * 64
            + b'"}',
        )
        duplicate = core.verify_target(target, "codex")
        self.assertEqual(duplicate["status"], "MALFORMED_MARKER")


if __name__ == "__main__":
    unittest.main(verbosity=2)
