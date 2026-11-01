"""Preview dangling-link refusal for planned Mistral workspaces and run directories.

Synthetic fixtures only. No auth, inference, cleanup, or ACL changes.
Own artifacts stay under TEAM_TEST_ROOT when that directory is outside the
payload, otherwise under the process temp directory. Nothing is deleted.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import mistral_runtime as runtime

tools = runtime.tool_module()
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_SYMLINK_TAG = getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C)
_MOUNT_TAG = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)


def _artifact_root() -> Path:
    payload = Path(__file__).resolve().parents[2]
    candidates = []
    configured = os.environ.get("TEAM_TEST_ROOT")
    if configured:
        candidates.append(Path(configured))
    candidates.append(Path(tempfile.gettempdir()))
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_dir() and not resolved.is_relative_to(payload):
            root = resolved / ("fleet-mistral-preview-links-" + uuid.uuid4().hex)
            root.mkdir(exist_ok=False)
            return root
    raise RuntimeError("Synthetic test temp root must be outside the release payload")


def _same(left, right) -> bool:
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(os.path.abspath(str(right)))


def _reparse_stat(kind: str):
    if kind == "symlink":
        return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=_REPARSE,
                               st_reparse_tag=_SYMLINK_TAG, st_nlink=1, st_size=0)
    return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=_REPARSE,
                           st_reparse_tag=_MOUNT_TAG, st_nlink=1, st_size=0)


def _is_reparse(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    junction = getattr(os.path, "isjunction", None)
    return bool(stat.S_ISLNK(info.st_mode) or (attributes & _REPARSE)
                or (junction is not None and junction(path)) or path.is_symlink())


class PreviewLinkFixture(unittest.TestCase):
    def setUp(self):
        self.root = _artifact_root()
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "src").mkdir()
        self.small = self.source / "src" / "app.py"
        self.small.write_text("answer = 1\n", encoding="utf-8", newline="\n")
        self.dispatch = {"scope": ["src"], "inputs": [], "checks": []}

    def preview(self, workspace: Path, run_dir: Path):
        return tools.preview_policy(workspace=str(workspace), run_dir=str(run_dir),
                                    mode="implement", source_workspace=str(self.source),
                                    **self.dispatch)

    def assert_link_refused(self, workspace: Path, run_dir: Path):
        with self.assertRaises(tools.ToolPolicyError) as caught:
            self.preview(workspace, run_dir)
        self.assertEqual(caught.exception.code, "link")
        self.assertFalse((run_dir / tools.POLICY_FILENAME).exists())


class ProspectiveLinkMockTests(PreviewLinkFixture):
    def _patched_lstat(self, link: Path, *, kind: str | None = None, error: OSError | None = None):
        real = tools.os.lstat

        def lstat(target, *args, **kwargs):
            if _same(target, link):
                if error is not None:
                    raise error
                return _reparse_stat(kind or "symlink")
            return real(target, *args, **kwargs)

        return patch.object(tools.os, "lstat", side_effect=lstat)

    def test_mock_leaf_and_ancestor_links_refuse_workspace_and_run_dir(self):
        for kind in ("symlink", "junction"):
            for place in ("workspace-leaf", "workspace-ancestor", "run-dir-leaf", "run-dir-ancestor"):
                with self.subTest(kind=kind, place=place):
                    if place == "workspace-leaf":
                        link = workspace = self.root / f"mock-{kind}-ws-leaf"
                        run_dir = self.root / f"mock-{kind}-run-for-ws-leaf"
                    elif place == "workspace-ancestor":
                        link = self.root / f"mock-{kind}-ws-ancestor"
                        workspace = link / "planned"
                        run_dir = self.root / f"mock-{kind}-run-for-ws-ancestor"
                    elif place == "run-dir-leaf":
                        workspace = self.root / f"mock-{kind}-ws-for-run-leaf"
                        link = run_dir = self.root / f"mock-{kind}-run-leaf"
                    else:
                        workspace = self.root / f"mock-{kind}-ws-for-run-ancestor"
                        link = self.root / f"mock-{kind}-run-ancestor"
                        run_dir = link / "planned"
                    self.assertFalse(link.exists())
                    with self._patched_lstat(link, kind=kind):
                        self.assert_link_refused(workspace, run_dir)
                    self.assertFalse(workspace.exists())
                    self.assertFalse(run_dir.exists())

    def test_non_file_not_found_lstat_refuses_workspace_and_run_dir(self):
        workspace = self.root / "lstat-error-workspace"
        run_dir = self.root / "lstat-error-run"
        with self._patched_lstat(workspace, error=PermissionError(13, "synthetic lstat refusal")):
            with self.assertRaises(tools.ToolPolicyError) as caught:
                self.preview(workspace, self.root / "plain-run-for-lstat-error")
        self.assertEqual(caught.exception.code, "workspace")
        plain_workspace = self.root / "plain-workspace-for-lstat-error"
        with self._patched_lstat(run_dir, error=PermissionError(13, "synthetic lstat refusal")):
            with self.assertRaises(tools.ToolPolicyError) as caught:
                self.preview(plain_workspace, run_dir)
        self.assertEqual(caught.exception.code, "policy")
        self.assertFalse(workspace.exists())
        self.assertFalse(run_dir.exists())
        self.assertFalse(plain_workspace.exists())


class ProspectivePathBehaviorTests(PreviewLinkFixture):
    def test_ordinary_missing_future_paths_and_small_scope_plan(self):
        workspace = self.root / "future-workspace"
        run_dir = self.root / "future-run"
        before = self.small.read_bytes()
        policy = self.preview(workspace, run_dir)
        self.assertTrue(policy["preview"])
        self.assertEqual(Path(policy["workspace"]), workspace.resolve())
        self.assertEqual(Path(policy["run_dir"]), run_dir.resolve())
        self.assertEqual(Path(policy["source_workspace"]), self.source.resolve())
        self.assertEqual(policy["scope"], ["src"])
        self.assertFalse(workspace.exists())
        self.assertFalse(run_dir.exists())
        self.assertFalse((run_dir / tools.POLICY_FILENAME).exists())
        self.assertEqual(self.small.read_bytes(), before)

    def test_oversized_source_is_still_refused(self):
        target = self.source / "src" / "big.py"
        target.write_bytes(b"x" * (tools.DEFAULT_LIMITS["max_write_bytes"] + 1))
        workspace = self.root / "oversized-workspace"
        run_dir = self.root / "oversized-run"
        with self.assertRaises(tools.ToolPolicyError) as caught:
            self.preview(workspace, run_dir)
        self.assertEqual(caught.exception.code, "not_admitted")
        self.assertFalse(workspace.exists())
        self.assertFalse(run_dir.exists())

    def test_non_preview_missing_run_dir_stays_strict(self):
        run_dir = self.root / "strict-missing-run"
        with self.assertRaises(tools.ToolPolicyError):
            tools.build_policy(workspace=self.source, run_dir=run_dir, mode="implement",
                               **self.dispatch)
        self.assertFalse(run_dir.exists())
        self.assertTrue(self.small.is_file())


class RealDanglingLinkTests(PreviewLinkFixture):
    def _dangling_junction(self, link: Path) -> bool:
        target = link.with_name(link.name + "-target")
        moved = link.with_name(link.name + "-target-moved")
        target.mkdir(exist_ok=False)
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True)
        if completed.returncode != 0 or not _is_reparse(link):
            return False
        target.rename(moved)
        return _is_reparse(link) and not link.exists()

    def _dangling_symlink(self, link: Path) -> bool:
        target = link.with_name(link.name + "-target")
        moved = link.with_name(link.name + "-target-moved")
        target.mkdir(exist_ok=False)
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            return False
        if not link.is_symlink():
            return False
        target.rename(moved)
        return link.is_symlink() and not link.exists()

    def _refuse_leaf_and_ancestor(self, maker, label: str):
        leaf_workspace = self.root / f"{label}-ws-leaf"
        ancestor_workspace = self.root / f"{label}-ws-ancestor"
        leaf_run = self.root / f"{label}-run-leaf"
        ancestor_run = self.root / f"{label}-run-ancestor"
        if not maker(leaf_workspace):
            self.skipTest(f"{label} creation is not supported")
        self.assertTrue(maker(ancestor_workspace))
        self.assertTrue(maker(leaf_run))
        self.assertTrue(maker(ancestor_run))
        self.assert_link_refused(leaf_workspace, self.root / f"{label}-plain-run-for-ws-leaf")
        self.assert_link_refused(ancestor_workspace / "planned", self.root / f"{label}-plain-run-for-ws-ancestor")
        self.assert_link_refused(self.root / f"{label}-plain-ws-for-run-leaf", leaf_run)
        self.assert_link_refused(self.root / f"{label}-plain-ws-for-run-ancestor", ancestor_run / "planned")

    def test_real_dangling_junction_leaf_and_ancestor(self):
        self._refuse_leaf_and_ancestor(self._dangling_junction, "junction")

    def test_real_dangling_symlink_leaf_and_ancestor(self):
        self._refuse_leaf_and_ancestor(self._dangling_symlink, "symlink")


if __name__ == "__main__":
    unittest.main()
