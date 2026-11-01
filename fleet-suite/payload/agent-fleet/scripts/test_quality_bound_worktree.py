"""Real Git fixtures for persisted Graph-bound worktree admission."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import fleet


class BoundWorktreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.root = self.home / "fleet"
        (self.root / "runs").mkdir(parents=True)
        self.repo = self.home / "repo"
        self.repo.mkdir()
        self.git(self.repo, "init", "-q")
        self.git(self.repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "--allow-empty", "-qm", "base")
        self.base = self.git(self.repo, "rev-parse", "HEAD")
        self.worktree = self.home / "external" / "bound"
        self.git(self.repo, "worktree", "add", "--detach", str(self.worktree), self.base)
        self.record = {
            "run_id": "run-20260912T193400Z-7a58d05230", "mode": "implement",
            "workspace": str(self.repo), "execution_workspace": str(self.worktree),
            "worktree_reused": True, "worktree_created": False,
            "worktree_base_commit_sha": self.base, "worktree_base_provenance": "preserved",
            "git_evidence": {"worktree": str(self.worktree), "base_commit_sha": self.base,
                             "base_provenance": "preserved"},
        }
        self.path = self.root / "runs" / (self.record["run_id"] + ".json")
        self.persist()

    def git(self, cwd, *args):
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                                timeout=15, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def persist(self):
        self.path.write_text(json.dumps(self.record), encoding="utf-8")

    def admit(self):
        return fleet.guarded_quality_worktree(self.root, self.record)

    def test_persisted_detached_graph_worktree_is_admitted(self):
        self.assertEqual(self.admit(), self.worktree)

    def test_nested_execution_is_admitted(self):
        nested = self.worktree / "project"
        nested.mkdir()
        self.record["execution_workspace"] = str(nested)
        self.persist()
        self.assertEqual(self.admit(), self.worktree)

    def test_missing_ledger_is_rejected(self):
        self.path.unlink()
        with self.assertRaises(fleet.FleetError):
            self.admit()

    def test_in_memory_path_override_is_rejected(self):
        self.record["workspace"] = str(self.worktree)
        with self.assertRaises(fleet.FleetError):
            self.admit()

    def test_reuse_boolean_alone_is_not_admission(self):
        self.record["git_evidence"]["base_provenance"] = "recorded"
        self.persist()
        with self.assertRaises(fleet.FleetError):
            self.admit()

    def test_legacy_outside_evidence_still_rejected(self):
        del self.record["worktree_reused"]
        self.persist()
        with self.assertRaisesRegex(fleet.FleetError, "escapes Fleet evidence"):
            self.admit()

    def test_primary_checkout_rejected(self):
        self.record["git_evidence"]["worktree"] = str(self.repo)
        self.record["execution_workspace"] = str(self.repo)
        self.persist()
        with self.assertRaises(fleet.FleetError):
            self.admit()

    def test_attached_worktree_rejected(self):
        self.git(self.worktree, "checkout", "-qb", "attached")
        with self.assertRaises(fleet.FleetError):
            self.admit()

    def test_foreign_execution_rejected(self):
        self.record["execution_workspace"] = str(self.repo)
        self.persist()
        with self.assertRaises(fleet.FleetError):
            self.admit()

    def test_base_mismatch_rejected(self):
        self.record["worktree_base_commit_sha"] = "a" * 40
        self.persist()
        with self.assertRaises(fleet.FleetError):
            self.admit()

    def test_unknown_base_rejected(self):
        self.record["worktree_base_commit_sha"] = "a" * 40
        self.record["git_evidence"]["base_commit_sha"] = "a" * 40
        self.persist()
        with self.assertRaises(fleet.FleetError):
            self.admit()

    def test_foreign_repository_rejected(self):
        other = self.home / "other"
        other.mkdir()
        self.git(other, "init", "-q")
        self.record["workspace"] = str(other)
        self.persist()
        with self.assertRaises(fleet.FleetError):
            self.admit()

    def test_git_backpointer_mismatch_rejected(self):
        metadata = Path(self.git(self.worktree, "rev-parse", "--absolute-git-dir"))
        (metadata / "gitdir").write_text(str(self.repo / ".git"), encoding="utf-8")
        with self.assertRaises(fleet.FleetError):
            self.admit()

    def test_parent_symlink_rejected_before_resolving(self):
        alias = self.home / "alias"
        try:
            alias.symlink_to(self.worktree.parent, target_is_directory=True)
        except OSError as exc:
            self.skipTest("Host does not permit fixture symlink: " + str(exc.winerror))
        self.record["git_evidence"]["worktree"] = str(alias / self.worktree.name)
        self.record["execution_workspace"] = str(alias / self.worktree.name)
        self.persist()
        with self.assertRaises(fleet.FleetError):
            self.admit()

    def test_existing_evidence_path_remains_compatible(self):
        inside = self.root / "evidence" / "original" / "worktree"
        inside.mkdir(parents=True)
        record = copy.deepcopy(self.record)
        record["git_evidence"]["worktree"] = str(inside)
        record.pop("worktree_reused")
        self.assertEqual(fleet.guarded_quality_worktree(self.root, record), inside)

    def test_large_unchanged_blob_has_baseline_reference(self):
        original = self.repo / "dependency.js"
        original.write_bytes(b"// baseline dependency\n" * 100000)
        self.git(self.repo, "add", "dependency.js")
        self.git(self.repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-qm", "dependency")
        base = self.git(self.repo, "rev-parse", "HEAD")
        self.git(self.worktree, "checkout", "--detach", base)
        source = self.worktree / "copy.js"
        source.write_bytes(original.read_bytes())
        reference = fleet.quality_baseline_blob_reference(self.worktree, base, source, "copy.js")
        self.assertEqual(reference["baseline_path"], "dependency.js")
        self.assertEqual(reference["baseline_commit_sha"], base)
        self.assertEqual(reference["path"], "copy.js")
        self.assertTrue(reference["content_omitted"])

    def test_changed_large_blob_cannot_claim_baseline(self):
        source = self.worktree / "new.js"
        source.write_bytes(b"// changed code\n" * 150000)
        self.assertIsNone(fleet.quality_baseline_blob_reference(self.worktree, self.base, source, "new.js"))

    def test_missing_baseline_never_exempts_large_blob(self):
        source = self.worktree / "new.js"
        source.write_bytes(b"// changed code\n" * 150000)
        self.assertIsNone(fleet.quality_baseline_blob_reference(self.worktree, None, source, "new.js"))


if __name__ == "__main__":
    unittest.main()
