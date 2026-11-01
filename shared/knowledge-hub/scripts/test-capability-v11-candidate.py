#!/usr/bin/env python3
"""TEMP-only readiness tests for the first Capability Hub V1.1 candidate."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

import capability_core as core
from capability_deployment_proof import PROOF_PREFIX, TempFirstInstallTransaction


CANDIDATE_FILES = {
    "SKILL.md",
    "capability.json",
    "references/design-playbook.md",
    "references/review-rubric.md",
}


def write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def git(repo: Path, *arguments: str) -> str:
    return core.run_git(repo, list(arguments)).decode("utf-8", errors="strict").strip()


class CandidateReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = Path(__file__).resolve().parents[1]
        self.candidate = self.project / "capability-candidates/design-ui"
        self.root = Path(tempfile.mkdtemp(prefix="capability-v11-candidate-test-"))
        self.proof_roots: list[Path] = []

    def tearDown(self) -> None:
        temp_root = Path(tempfile.gettempdir()).resolve(strict=True)

        def make_writable(function: object, path: str, _: object) -> None:
            os.chmod(path, stat.S_IWRITE)
            function(path)

        resolved = self.root.resolve(strict=True)
        self.assertEqual(resolved.parent, temp_root)
        self.assertTrue(resolved.name.startswith("capability-v11-candidate-test-"))
        shutil.rmtree(resolved, onexc=make_writable)
        for proof_root in self.proof_roots:
            resolved_proof = proof_root.resolve(strict=True)
            self.assertEqual(resolved_proof.parent, temp_root)
            self.assertTrue(resolved_proof.name.startswith(PROOF_PREFIX))
            shutil.rmtree(resolved_proof, onexc=make_writable)

    def candidate_files(self) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        for path in self.candidate.rglob("*"):
            if path.is_symlink():
                self.fail(f"Candidate contains a symlink: {path}")
            if path.is_file():
                relative = path.relative_to(self.candidate).as_posix()
                files[relative] = path.read_bytes()
        return files

    def build_fixture_repo(self) -> tuple[Path, str, core.GitSource]:
        repo = self.root / "repo"
        repo.mkdir()
        git(repo, "init", "--quiet", "--initial-branch=main")
        git(repo, "config", "user.name", "Capability V1.1 Candidate Test")
        git(repo, "config", "user.email", "capability-v11@invalid.local")
        git(repo, "config", "core.autocrlf", "false")
        write_bytes(repo / ".gitattributes", b"capabilities/** -text\n")
        destination = repo / "capabilities/design-ui"
        for relative, content in self.candidate_files().items():
            write_bytes(destination.joinpath(*relative.split("/")), content)
        git(repo, "add", "--all")
        git(repo, "commit", "--quiet", "-m", "candidate fixture")
        commit = git(repo, "rev-parse", "HEAD")
        source = core.inspect_git_source(
            repo, commit, "capabilities/design-ui", pilot_policy=True
        )
        return repo, commit, source

    def test_candidate_inventory_and_text_contract(self) -> None:
        files = self.candidate_files()
        self.assertEqual(set(files), CANDIDATE_FILES)
        for relative, content in files.items():
            with self.subTest(path=relative):
                core.validate_relative_path(relative, ascii_only=True)
                self.assertFalse(content.startswith(b"\xef\xbb\xbf"))
                self.assertNotIn(b"\r", content)
                self.assertNotIn(b"\x00", content)
                text = content.decode("utf-8", errors="strict")
                self.assertNotIn("http://", text.casefold())
                self.assertNotIn("https://", text.casefold())
        manifest = core.strict_json_loads(files["capability.json"].decode("ascii"))
        self.assertEqual(manifest["capability_id"], "design-ui")
        self.assertEqual(manifest["declared_permissions"], [])
        self.assertEqual(manifest["enforcement"], "none")
        self.assertIsNone(manifest["revoked_at"])

    def test_exact_git_release_first_install_verify_and_rollback(self) -> None:
        repo, commit, source = self.build_fixture_repo()
        self.assertEqual(len(source.entries), len(CANDIDATE_FILES))
        self.assertEqual(source.capability_manifest["capability_id"], "design-ui")
        materialized = core.materialize_git_source(source)
        materialized_path = Path(materialized["materialized_to"])

        proof_root = Path(tempfile.mkdtemp(prefix=PROOF_PREFIX))
        self.proof_roots.append(proof_root)
        staged = proof_root / "staged"
        os.replace(materialized_path, staged)
        marker = core.marker_v2(
            capability_id="design-ui",
            capability_version="0.1.0-rc.1",
            tool="antigravity",
            scope="user",
            source=source,
            activation_boundary="new Antigravity task/session",
        )
        write_bytes(
            staged / core.CAPABILITY_MARKER_NAME,
            json.dumps(marker, sort_keys=True, indent=2).encode("ascii") + b"\n",
        )

        transaction = TempFirstInstallTransaction(proof_root)
        plan = transaction.plan()
        self.assertEqual(plan["new_fingerprint"], source.payload_fingerprint)
        installed = transaction.run()
        self.assertEqual(installed["post_activate_fingerprint"], source.payload_fingerprint)
        verified = core.verify_target(
            proof_root / "target",
            "antigravity",
            repo=repo,
            commit=commit,
            source_path="capabilities/design-ui",
            activation_boundary="new Antigravity task/session",
            last_fetch="2026-07-18T00:00:00Z",
        )
        self.assertEqual(verified["status"], "CURRENT")
        self.assertEqual(verified["marker_file"], core.CAPABILITY_MARKER_NAME)
        self.assertEqual(verified["writes_performed"], 0)

        rolled_back = transaction.rollback_committed(owner_authorized=True)
        self.assertEqual(rolled_back["status"], "ROLLED_BACK")
        self.assertFalse((proof_root / "target").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
