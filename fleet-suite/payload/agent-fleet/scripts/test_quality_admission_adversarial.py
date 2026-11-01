"""Adversarial regression tests for quality admission boundaries."""
import copy
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock

import fleet


class QualityAdmissionAdversarialTests(unittest.TestCase):
    """Reuse the real-Git fixture without inheriting its test methods."""

    def setUp(self):
        from test_quality_bound_worktree import BoundWorktreeTests

        self._fixture_cls = BoundWorktreeTests
        self._fixture_cls.setUp(self)

    def git(self, cwd, *args):
        return self._fixture_cls.git(self, cwd, *args)

    def persist(self):
        return self._fixture_cls.persist(self)

    def _git(self, *args):
        return self.git(self.repo, *args)

    def _baseline_source(self):
        original = self.repo / "dependency.js"
        original.write_bytes(b"// baseline dependency\n" * 4096)
        self._git("add", "dependency.js")
        self._git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                  "commit", "-qm", "dependency")
        base = self._git("rev-parse", "HEAD")
        self._git_from_worktree("checkout", "--detach", base)
        source = self.worktree / "copy.js"
        source.write_bytes(original.read_bytes())
        return base, source

    def _git_from_worktree(self, *args):
        result = subprocess.run(
            ["git", *args], cwd=self.worktree, capture_output=True, text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_parent_link_is_rejected_before_resolve_mocked_windows_reparse(self):
        """Capability-independent stand-in for a Windows parent junction/symlink."""
        parent = self.home / "linked-parent"
        candidate = parent / "bound"
        record = copy.deepcopy(self.record)
        record["git_evidence"]["worktree"] = str(candidate)
        record["execution_workspace"] = str(candidate)

        def is_link(path):
            return path == parent

        with mock.patch.object(fleet, "is_filesystem_link", side_effect=is_link), \
                mock.patch.object(Path, "resolve", wraps=Path.resolve) as resolve:
            with self.assertRaisesRegex(fleet.FleetError, "worktree is linked"):
                fleet.guarded_quality_worktree(self.root, record)
        resolve.assert_not_called()

    def test_reused_binding_must_match_persisted_record(self):
        record = copy.deepcopy(self.record)
        record["execution_workspace"] = str(self.worktree / "different")
        with self.assertRaisesRegex(fleet.FleetError, "does not match its persisted run"):
            fleet.guarded_quality_worktree(self.root, record)

    def test_primary_checkout_is_denied_as_quality_worktree(self):
        record = copy.deepcopy(self.record)
        record["git_evidence"]["worktree"] = str(self.repo)
        record["execution_workspace"] = str(self.repo)
        with self.assertRaises(fleet.FleetError):
            fleet.guarded_quality_worktree(self.root, record)

    def test_attached_worktree_is_denied_as_quality_worktree(self):
        self._git_from_worktree("checkout", "-qb", "attached")
        with self.assertRaises(fleet.FleetError):
            fleet.guarded_quality_worktree(self.root, self.record)

    def test_changed_blob_cannot_claim_baseline_equality(self):
        base, source = self._baseline_source()
        source.write_bytes(source.read_bytes() + b"changed after baseline\n")
        self.assertIsNone(
            fleet.quality_baseline_blob_reference(self.worktree, base, source, "copy.js")
        )

    def test_baseline_content_race_is_rejected_after_capture(self):
        base, source = self._baseline_source()
        original_stat = Path.stat
        source_stat_calls = 0

        def racing_stat():
            nonlocal source_stat_calls
            result = original_stat(source)
            source_stat_calls += 1
            if source_stat_calls == 3:
                return type("RacingStat", (), {
                    "st_size": result.st_size,
                    "st_mtime_ns": result.st_mtime_ns + 1,
                })()
            return result

        with mock.patch.object(Path, "stat", side_effect=racing_stat):
            with self.assertRaisesRegex(fleet.FleetError, "changed during capture"):
                fleet.quality_baseline_blob_reference(
                    self.worktree, base, source, "copy.js"
                )


if __name__ == "__main__":
    unittest.main()
