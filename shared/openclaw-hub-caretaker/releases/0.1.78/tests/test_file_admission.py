"""File-admission tests for SPEC-0.1.8-FILES.

The tests exercise the public API of :mod:`caretaker.files` against synthetic
fixtures. They never use real credentials — secrets are dummy fragments.

All filesystem mutations happen inside the workspace's ``.test-tmp`` directory
and are cleaned up at process exit.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Iterable, List, Tuple
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from caretaker.files import (
    AdmissionStats,
    FileAdmissionCursor,
    FileCandidate,
    FileRejection,
    FileSourceConfig,
    RootConfig,
    default_walk,
    is_within_root,
    safe_relpath,
    scan_files,
)


def _synthetic_test_credential():
    return "synthetic-test-only-" + secrets.token_hex(16)


def _ck(root: Path, rel: str) -> str:
    return FileAdmissionCursor.entry_key(str(root.resolve()), rel)


TASK_TMP = _ROOT / ".test-tmp"
TASK_TMP.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# Dummy fragments that look like secrets but are not real credentials.
DUMMY_AWS = "AKIA" + ("A" * 16)
DUMMY_GH = "ghp_" + ("a" * 20)


def _make_root(tmp: Path, *, layout: dict) -> Path:
    """Build a directory tree described by ``layout``.

    Layout keys are file or directory names; directory values are themselves
    layouts. Files are written with ``file`` as their contents (str).
    """
    for name, value in layout.items():
        path = tmp / name
        if isinstance(value, dict):
            path.mkdir(parents=True, exist_ok=True)
            _make_root(path, layout=value)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(value), "utf-8")
    return tmp


def _set_mtime(path: Path, mtime: float) -> None:
    """Force ``path`` to have the given mtime."""
    os.utime(str(path), (mtime, mtime))


def _make_config(roots: List[Path],
                 *,
                 extensions=(".md", ".txt"),
                 quiet_minutes: int = 15,
                 max_file_bytes: int = 262_144,
                 max_files_per_tick: int = 50,
                 depth: int = 4,
                 project_slug: str = "demo") -> FileSourceConfig:
    return FileSourceConfig(
        source_id="owner-notes",
        roots=tuple(
            RootConfig(path=str(r), project_slug=project_slug,
                       max_depth=depth)
            for r in roots
        ),
        extensions=extensions,
        quiet_minutes=quiet_minutes,
        max_file_bytes=max_file_bytes,
        max_files_per_tick=max_files_per_tick,
    )


# ---------------------------------------------------------------------------
# 1. Empty allowlist is a valid no-scan state.
# ---------------------------------------------------------------------------


class EmptyAllowlistTests(unittest.TestCase):

    def test_empty_roots_returns_empty_results(self):
        cfg = FileSourceConfig(
            source_id="owner-notes",
            roots=(),
            extensions=(".md", ".txt"),
        )
        cursor = FileAdmissionCursor()
        candidates, rejections, stats = scan_files(cfg, cursor, now=1_000_000.0)

        self.assertEqual(candidates, [])
        self.assertEqual(rejections, [])
        self.assertEqual(stats.roots_total, 0)
        self.assertEqual(stats.files_visited, 0)
        self.assertEqual(stats.files_admitted, 0)
        # Cursor must be untouched.
        self.assertEqual(cursor.pending, {})
        self.assertEqual(cursor.stable, {})

    def test_configured_exclusion_is_pruned_during_walk(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as temporary:
            root = Path(temporary)
            _make_root(root, layout={"README.md": "project",
                                     "cache": {"hidden.md": "cache"},
                                     "_build": {"hidden.md": "private"},
                                     ".private": {"hidden.md": "private"}})
            cfg = FileSourceConfig(
                source_id="owner-notes",
                roots=(RootConfig(path=str(root), project_slug="demo", max_depth=3),),
                extensions=(".md",), quiet_minutes=0,
                exclude_directory_names=("cache",))
            _candidates, _rejections, stats = scan_files(
                cfg, FileAdmissionCursor(), now=time.time() + 3600)
            self.assertEqual(stats.files_visited, 1)

    def test_per_root_cap_prevents_first_large_root_starving_second(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as temporary:
            base = Path(temporary)
            first, second = base / "first", base / "second"
            first.mkdir()
            second.mkdir()
            for root in (first, second):
                for index in range(3):
                    (root / f"{index}.md").write_text("stable", encoding="utf-8")
            cfg = FileSourceConfig(
                source_id="owner-notes",
                roots=(RootConfig(path=str(first), project_slug="one"),
                       RootConfig(path=str(second), project_slug="two")),
                extensions=(".md",), quiet_minutes=0,
                max_files_per_tick=4, max_files_per_root=1)
            cursor = FileAdmissionCursor()
            now = time.time() + 3600
            scan_files(cfg, cursor, now=now)
            candidates, _rejections, _stats = scan_files(cfg, cursor, now=now)
            self.assertEqual(len(candidates), 2)
            self.assertEqual({candidate.project_slug for candidate in candidates},
                             {"one", "two"})

    def test_empty_roots_does_not_raise(self):
        cfg = FileSourceConfig(
            source_id="owner-notes",
            roots=(),
            extensions=(".md",),
        )
        # No exception is the success criterion.
        scan_files(cfg, FileAdmissionCursor(), now=time.time())


# ---------------------------------------------------------------------------
# 2. Quiet period: recent file is skipped, older one is not.
# ---------------------------------------------------------------------------


class QuietPeriodTests(unittest.TestCase):

    def test_recent_file_is_not_a_candidate_older_file_is(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            tmp = Path(d)
            root = _make_root(tmp, layout={
                "notes": {
                    "fresh.md": "still being written",
                    "stable.md": "older knowledge note",
                }
            })

            now = 2_000_000.0
            # fresh.md was modified 1 minute ago — inside quiet window (15min)
            _set_mtime(root / "notes" / "fresh.md", now - 60)
            # stable.md was modified 2 hours ago — well past the quiet window
            _set_mtime(root / "notes" / "stable.md", now - 7200)

            cfg = _make_config([root], extensions=(".md",), quiet_minutes=15)

            # Scan 1 — stable.md is admitted only after two consecutive scans.
            cur1 = FileAdmissionCursor()
            c1, r1, s1 = scan_files(cfg, cur1, now=now)
            self.assertEqual(c1, [], "first scan only arms the pending state")
            self.assertEqual(s1.files_quiet, 1,
                             "fresh.md must be classified as quiet, not a "
                             "candidate")

            # fresh.md is in the quiet window: never armed, never admitted.
            self.assertNotIn(_ck(root, "notes/fresh.md"), cur1.pending)
            self.assertNotIn(_ck(root, "notes/fresh.md"), cur1.stable)

            # stable.md is armed and waiting for the second scan.
            self.assertIn(_ck(root, "notes/stable.md"), cur1.pending)

            # Scan 2 — stable.md becomes a candidate.
            cur2 = cur1
            c2, r2, s2 = scan_files(cfg, cur2, now=now + 1)
            paths = [c.relative_path for c in c2]
            self.assertIn("notes/stable.md", paths)
            self.assertNotIn("notes/fresh.md", paths)
            self.assertEqual(s2.files_admitted, 1)
            self.assertEqual(s2.files_quiet, 1,
                             "fresh.md is still within quiet window")

    def test_file_at_quiet_boundary_is_admitted(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            tmp = Path(d)
            root = _make_root(tmp, layout={"a.md": "stable content"})

            now = 2_000_000.0
            # Exactly quiet_minutes ago — just past the boundary, admissible.
            _set_mtime(root / "a.md", now - 15 * 60)

            cfg = _make_config([root], extensions=(".md",), quiet_minutes=15)
            cur = FileAdmissionCursor()
            # Scan 1 arms the pending state (stability check).
            scan_files(cfg, cur, now=now)
            # Scan 2 admits (two equal observations).
            candidates, _, stats = scan_files(cfg, cur, now=now + 1)
            self.assertEqual(stats.files_admitted, 1)
            self.assertEqual([c.relative_path for c in candidates], ["a.md"])
            # Scan 3 must be a no-op: cursor recognises the unchanged file.
            candidates3, _, stats3 = scan_files(cfg, cur, now=now + 2)
            self.assertEqual(candidates3, [])
            self.assertEqual(stats3.files_admitted, 0)
            self.assertEqual(stats3.files_unchanged, 1)


# ---------------------------------------------------------------------------
# 3. Stability: changing content is not admitted until it stops changing.
# ---------------------------------------------------------------------------


class StabilityTests(unittest.TestCase):

    def test_changing_content_is_not_admitted_until_stable(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            tmp = Path(d)
            root = _make_root(tmp, layout={"note.md": "version one"})

            now = 2_000_000.0
            path = root / "note.md"
            # Make file old enough to clear the quiet window.
            _set_mtime(path, now - 7200)

            cfg = _make_config([root], extensions=(".md",), quiet_minutes=15)
            cur = FileAdmissionCursor()

            # Scan 1 — version one is armed, not admitted.
            c1, _, s1 = scan_files(cfg, cur, now=now)
            self.assertEqual(c1, [])
            self.assertEqual(s1.files_awaiting_stability, 1)
            self.assertIn(_ck(root, "note.md"), cur.pending)
            sha_v1 = cur.pending[_ck(root, "note.md")][1]

            # Content keeps changing between scans: the pending entry never
            # survives two consecutive equal scans, so the file is never
            # admitted even though it is stable on its own wall-clock.
            for tick in range(5):
                path.write_text(f"version {tick}", "utf-8")
                _set_mtime(path, now - 7200)
                c, _, s = scan_files(cfg, cur, now=now + tick + 1)
                self.assertEqual(c, [],
                                 f"tick {tick}: changing content must not be "
                                 "admitted yet")
                self.assertEqual(s.files_admitted, 0)
                # Pending was overwritten with the new content snapshot.
                self.assertIn(_ck(root, "note.md"), cur.pending)
                # Each tick computes a different sha.
                self.assertNotEqual(cur.pending[_ck(root, "note.md")][1], sha_v1)

            # Now freeze the content. The next two scans observe the same
            # bytes and the file is admitted.
            path.write_text("final stable content", "utf-8")
            _set_mtime(path, now - 7200)
            scan_files(cfg, cur, now=now + 100)
            c_final, _, s_final = scan_files(cfg, cur, now=now + 101)
            self.assertEqual([c.relative_path for c in c_final], ["note.md"])
            self.assertEqual(s_final.files_admitted, 1)
            self.assertEqual(c_final[0].sha256,
                             cur.stable[_ck(root, "note.md")][1])


# ---------------------------------------------------------------------------
# 4. Content-sha256 cursor: unchanged file does not emit a second event.
# ---------------------------------------------------------------------------


class MultiRootCursorTests(unittest.TestCase):

    def test_same_relative_path_in_two_roots_does_not_collide(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            tmp = Path(d)
            root_a = _make_root(tmp / "proj-a", layout={"note.md": "alpha body"})
            root_b = _make_root(tmp / "proj-b", layout={"note.md": "beta body"})
            now = 2_000_000.0
            _set_mtime(root_a / "note.md", now - 7200)
            _set_mtime(root_b / "note.md", now - 7200)

            cfg = FileSourceConfig(
                source_id="owner-notes",
                roots=(
                    RootConfig(path=str(root_a), project_slug="proj-a",
                               max_depth=4),
                    RootConfig(path=str(root_b), project_slug="proj-b",
                               max_depth=4),
                ),
                extensions=(".md",),
                quiet_minutes=0,
            )
            cur = FileAdmissionCursor()
            scan_files(cfg, cur, now=now)
            cands, _, stats = scan_files(cfg, cur, now=now + 1)
            pairs = sorted((c.project_slug, c.relative_path) for c in cands)
            self.assertEqual(pairs, [
                ("proj-a", "note.md"),
                ("proj-b", "note.md"),
            ])
            self.assertEqual(stats.files_admitted, 2)
            self.assertNotEqual(cands[0].sha256, cands[1].sha256)

            c2, _, s2 = scan_files(cfg, cur, now=now + 2)
            self.assertEqual(c2, [])
            self.assertEqual(s2.files_unchanged, 2)
            self.assertEqual(s2.files_admitted, 0)


class CursorUnchangedTests(unittest.TestCase):

    def test_unchanged_file_does_not_create_second_event(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            tmp = Path(d)
            root = _make_root(tmp, layout={"stable.md": "constant body"})
            path = root / "stable.md"
            _set_mtime(path, 2_000_000.0 - 7200)

            cfg = _make_config([root], extensions=(".md",), quiet_minutes=15)
            cur = FileAdmissionCursor()
            now = 2_000_000.0

            # Scan 1 arms, scan 2 admits, scan 3 (unchanged) is a no-op.
            scan_files(cfg, cur, now=now)
            scan_files(cfg, cur, now=now + 1)
            c3, _, s3 = scan_files(cfg, cur, now=now + 2)
            self.assertEqual(c3, [])
            self.assertEqual(s3.files_admitted, 0)
            self.assertEqual(s3.files_unchanged, 1,
                             "unchanged file must be classified as unchanged")
            self.assertEqual(s3.files_awaiting_stability, 0)

            # Many ticks later, still no second event.
            for tick in range(10):
                c, _, s = scan_files(cfg, cur, now=now + 100 + tick)
                self.assertEqual(c, [])
                self.assertEqual(s.files_unchanged, 1)

    def test_changed_content_emits_new_event(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            tmp = Path(d)
            root = _make_root(tmp, layout={"note.md": "v1"})
            path = root / "note.md"
            _set_mtime(path, 2_000_000.0 - 7200)

            cfg = _make_config([root], extensions=(".md",), quiet_minutes=15)
            cur = FileAdmissionCursor()
            now = 2_000_000.0

            # Admit v1
            scan_files(cfg, cur, now=now)
            c1, _, _ = scan_files(cfg, cur, now=now + 1)
            sha1 = c1[0].sha256

            # Change the content (after the quiet window).
            path.write_text("v2 different content", "utf-8")
            _set_mtime(path, 2_000_000.0 - 7200)
            scan_files(cfg, cur, now=now + 100)
            c2, _, _ = scan_files(cfg, cur, now=now + 101)
            self.assertEqual([c.relative_path for c in c2], ["note.md"])
            sha2 = c2[0].sha256
            self.assertNotEqual(sha1, sha2,
                                "changed content must produce a new sha")
            self.assertEqual(cur.stable[_ck(root, "note.md")][1], sha2)

            # Cursor's content hash is the new sha — a third scan must
            # classify the file as unchanged.
            c3, _, s3 = scan_files(cfg, cur, now=now + 102)
            self.assertEqual(c3, [])
            self.assertEqual(s3.files_unchanged, 1)


# ---------------------------------------------------------------------------
# 5. Containment: paths outside the root and max_depth are rejected.
# ---------------------------------------------------------------------------


class ContainmentTests(unittest.TestCase):

    def test_max_depth_is_respected(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            tmp = Path(d)
            # depth-0 = a.md, depth-1 = d1/b.md, depth-4 = d1/d2/d3/d4/deep.md
            root = _make_root(tmp, layout={
                "a.md": "shallow",
                "d1": {
                    "b.md": "one level",
                    "d2": {
                        "c.md": "two levels",
                        "d3": {
                            "e.md": "three levels",
                            "d4": {
                                "deep.md": "four levels — admissible",
                                "d5": {
                                    "too_deep.md": "five levels — rejected",
                                },
                            },
                        },
                    },
                },
            })
            now = 2_000_000_000.0  # far future so mtimes look "old"
            # Backdate every file so the quiet-period check does not apply.
            for child in root.rglob("*"):
                if child.is_file():
                    _set_mtime(child, now - 7200)

            cfg = _make_config([root], extensions=(".md",),
                               quiet_minutes=0, depth=4)
            cur = FileAdmissionCursor()
            scan_files(cfg, cur, now=now)
            cands, rejs, stats = scan_files(cfg, cur, now=now + 1)

            admitted = sorted(c.relative_path for c in cands)
            self.assertIn("a.md", admitted)
            self.assertIn("d1/b.md", admitted)
            self.assertIn("d1/d2/c.md", admitted)
            self.assertIn("d1/d2/d3/e.md", admitted)
            self.assertIn("d1/d2/d3/d4/deep.md", admitted)
            self.assertNotIn("d1/d2/d3/d4/d5/too_deep.md", admitted)
            # ``too_deep.md`` was pruned during the walk — its mtime was
            # set but the walker never yielded it, so it never even shows
            # up in stats.files_visited.
            self.assertNotIn("d1/d2/d3/d4/d5/too_deep.md", stats.rejections)
            for r in rejs:
                self.assertNotEqual(r.reason, "max_depth_exceeded")
            # Five admissible files (a, b, c, e, deep) were visited.
            self.assertEqual(stats.files_visited, 5)

    def test_path_outside_root_via_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            tmp = Path(d)
            root = _make_root(tmp / "root", layout={"ok.md": "harmless"})
            outside_dir = tmp / "outside"
            outside_dir.mkdir()
            outside_file = outside_dir / "escaped.md"
            outside_file.write_text("escaped", "utf-8")

            link = root / "escape.md"
            link_created = False
            try:
                # ``os.symlink`` requires SeCreateSymbolicLinkPrivilege on
                # Windows. We try anyway; if the platform refuses, fall back
                # to the custom-walker test which exercises the same
                # containment check.
                os.symlink(str(outside_file), str(link))
                link_created = True
            except (OSError, NotImplementedError):
                link_created = False

            # Backdate both files so the quiet-period check does not apply.
            now = 2_000_000_000.0
            _set_mtime(root / "ok.md", now - 7200)
            _set_mtime(outside_file, now - 7200)

            cfg = _make_config([root], extensions=(".md",), quiet_minutes=0)
            cur = FileAdmissionCursor()

            if link_created:
                # First scan only arms pending; escape must never enter cursor.
                c1, r1, _ = scan_files(cfg, cur, now=now)
                self.assertEqual(c1, [])
                self.assertTrue(
                    any(r.reason == "path_outside_root" for r in r1),
                    f"expected path_outside_root in {r1!r}",
                )
                self.assertIn(_ck(root, "ok.md"), cur.pending)
                for key in list(cur.pending) + list(cur.stable):
                    self.assertNotIn("escape.md", key)
                    self.assertNotIn("escaped.md", key)
                    self.assertNotIn("outside", key.replace("\\", "/").split("/")[-1])

                cands, rejs, _ = scan_files(cfg, cur, now=now + 1)
                self.assertEqual(
                    [c.relative_path for c in cands],
                    ["ok.md"],
                )
                self.assertTrue(
                    any(r.reason == "path_outside_root" for r in rejs),
                    f"expected path_outside_root in {rejs!r}",
                )
                for key in list(cur.pending) + list(cur.stable):
                    self.assertNotIn("escape.md", key)
            else:
                self._path_outside_root_via_custom_walker(root, outside_file)

    def _path_outside_root_via_custom_walker(
        self, root: Path, outside_file: Path
    ) -> None:
        """Custom-walker version of the symlink-escape test.

        We inject a walker that yields the *resolved* path of an outside
        file. This bypasses the platform's symlink privileges but exercises
        exactly the same containment check.
        """

        def fake_walker(r: Path, _max_depth: int) \
                -> Iterable[Tuple[Path, int]]:
            yield outside_file, 0

        cfg = _make_config([root], extensions=(".md",), quiet_minutes=0)
        cur = FileAdmissionCursor()
        cands, rejs, _ = scan_files(cfg, cur, now=2_000_000_000.0,
                                    walker=fake_walker)
        self.assertEqual(cands, [])
        self.assertTrue(
            any(r.reason == "path_outside_root" for r in rejs),
            f"expected path_outside_root in {rejs!r}",
        )
        for key in list(cur.pending) + list(cur.stable):
            self.assertNotIn("escaped.md", key)

    def test_path_outside_root_via_dotdot_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            tmp = Path(d)
            root = _make_root(tmp / "root", layout={"ok.md": "in root"})
            outside = tmp / "outside.md"
            outside.write_text("escaped via ..", "utf-8")
            _set_mtime(outside, 2_000_000_000.0 - 7200)

            def fake_walker(r: Path, _max_depth: int) \
                    -> Iterable[Tuple[Path, int]]:
                yield outside, 0

            cfg = _make_config([root], extensions=(".md",), quiet_minutes=0)
            cur = FileAdmissionCursor()
            cands, rejs, _ = scan_files(cfg, cur, now=2_000_000_000.0,
                                        walker=fake_walker)
            self.assertEqual(cands, [])
            self.assertTrue(
                any(r.reason == "path_outside_root" for r in rejs)
            )


# ---------------------------------------------------------------------------
# 6. Limits: max_file_bytes and max_files_per_tick.
# ---------------------------------------------------------------------------


class LimitTests(unittest.TestCase):

    def test_max_file_bytes_rejects_oversize_file(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            tmp = Path(d)
            root = _make_root(tmp, layout={
                "small.md": "x",
                "huge.md": "y",
            })
            huge = root / "huge.md"
            huge.write_bytes(b"a" * 2048)
            _set_mtime(huge, 2_000_000.0 - 7200)
            _set_mtime(root / "small.md", 2_000_000.0 - 7200)

            cfg = _make_config(
                [root], extensions=(".md",), quiet_minutes=0,
                max_file_bytes=1024,
            )
            cur = FileAdmissionCursor()
            now = 2_000_000.0
            c1, r1, s1 = scan_files(cfg, cur, now=now)
            self.assertTrue(
                any(
                    r.reason == "max_file_bytes_exceeded"
                    and r.relative_path == "huge.md"
                    for r in r1
                ),
                f"expected max_file_bytes_exceeded for huge.md in {r1!r}",
            )
            self.assertEqual(s1.rejections.get("max_file_bytes_exceeded"), 1)
            self.assertIn(_ck(root, "huge.md"), cur.stable)

            cands, rejs, stats = scan_files(cfg, cur, now=now + 1)

            admitted = [c.relative_path for c in cands]
            self.assertEqual(admitted, ["small.md"])
            self.assertEqual(
                [r.reason for r in rejs].count("max_file_bytes_exceeded"),
                0,
            )

            cands2, rejs2, _stats2 = scan_files(cfg, cur, now=now + 2)
            self.assertEqual(cands2, [])
            self.assertEqual(
                [r.reason for r in rejs2].count("max_file_bytes_exceeded"),
                0,
            )

    def test_max_files_per_tick_caps_admitted_candidates(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            tmp = Path(d)
            root = _make_root(tmp, layout={
                "a.md": "alpha",
                "b.md": "bravo",
                "c.md": "charlie",
                "d.md": "delta",
                "e.md": "echo",
            })
            # Make all files stable and old.
            now = 2_000_000_000.0
            for child in root.iterdir():
                _set_mtime(child, now - 7200)

            cfg = _make_config(
                [root], extensions=(".md",), quiet_minutes=0,
                max_files_per_tick=2,
            )
            cur = FileAdmissionCursor()

            # First tick only arms (stability requires two scans).
            scan_files(cfg, cur, now=now)
            c1, _, s1 = scan_files(cfg, cur, now=now + 1)
            self.assertEqual(len(c1), 2,
                             "cap must limit candidates emitted in one tick")
            self.assertEqual(s1.files_admitted, 2)
            self.assertGreaterEqual(s1.files_deferred_overflow, 1,
                                    "overflow files must be tracked")

            # Drain over subsequent ticks. With cap=2 and 3 deferred files,
            # three ticks are needed to admit them all.
            all_cands = list(c1)
            current = now + 2
            for _ in range(5):  # bounded loop just in case
                c_tick, _, _ = scan_files(cfg, cur, now=current)
                all_cands.extend(c_tick)
                current += 1
                if len(all_cands) == 5:
                    break
            admitted = sorted(c.relative_path for c in all_cands)
            self.assertEqual(admitted, ["a.md", "b.md", "c.md", "d.md", "e.md"])
            self.assertLessEqual(len(c1), 2,
                                 "first tick must respect the cap")

    def test_max_files_per_tick_is_shared_across_roots(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            tmp = Path(d)
            root_a = _make_root(tmp / "a", layout={
                "one.md": "a1",
                "two.md": "a2",
            })
            root_b = _make_root(tmp / "b", layout={
                "three.md": "b1",
                "four.md": "b2",
            })
            now = 2_000_000_000.0
            for root in (root_a, root_b):
                for child in root.iterdir():
                    if child.is_file():
                        _set_mtime(child, now - 7200)

            cfg = FileSourceConfig(
                source_id="owner-notes",
                roots=(
                    RootConfig(path=str(root_a), project_slug="proj-a",
                               max_depth=4),
                    RootConfig(path=str(root_b), project_slug="proj-b",
                               max_depth=4),
                ),
                extensions=(".md",),
                quiet_minutes=0,
                max_files_per_tick=2,
            )
            cur = FileAdmissionCursor()
            scan_files(cfg, cur, now=now)
            c1, _, s1 = scan_files(cfg, cur, now=now + 1)
            self.assertEqual(len(c1), 2)
            self.assertEqual(s1.files_admitted, 2)
            self.assertGreaterEqual(s1.files_deferred_overflow, 1)

            all_cands = list(c1)
            current = now + 2
            for _ in range(6):
                c_tick, _, _ = scan_files(cfg, cur, now=current)
                all_cands.extend(c_tick)
                current += 1
                if len(all_cands) == 4:
                    break
            pairs = sorted((c.project_slug, c.relative_path) for c in all_cands)
            self.assertEqual(pairs, [
                ("proj-a", "one.md"),
                ("proj-a", "two.md"),
                ("proj-b", "four.md"),
                ("proj-b", "three.md"),
            ])


# ---------------------------------------------------------------------------
# 7. Unsupported extension: explicit rejection, not silent.
# ---------------------------------------------------------------------------


class UnsupportedExtensionTests(unittest.TestCase):

    def test_unsupported_extension_is_reported(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            tmp = Path(d)
            root = _make_root(tmp, layout={
                "ok.md": "admissible",
                "skip.txt": "not in extension list",
                "binary.bin": "also unsupported",
            })
            for child in root.iterdir():
                _set_mtime(child, 2_000_000.0 - 7200)

            cfg = _make_config([root], extensions=(".md",), quiet_minutes=0)
            cur = FileAdmissionCursor()
            now = 2_000_000.0
            scan_files(cfg, cur, now=now)
            cands, rejs, stats = scan_files(cfg, cur, now=now + 1)

            admitted = [c.relative_path for c in cands]
            self.assertEqual(admitted, ["ok.md"])

            unsupported = sorted(
                r.relative_path for r in rejs
                if r.reason == "unsupported_extension"
            )
            self.assertEqual(unsupported, ["binary.bin", "skip.txt"])
            self.assertEqual(stats.files_unsupported, 2)
            self.assertEqual(stats.rejections.get("unsupported_extension"), 2)

            # The verdict's rule explains *why* the file was rejected.
            for r in rejs:
                if r.reason == "unsupported_extension":
                    self.assertEqual(r.rule, "extension_not_allowlisted")


# ---------------------------------------------------------------------------
# 8. Secret gate integration: filenames and content are rejected.
# ---------------------------------------------------------------------------


class SecretGateIntegrationTests(unittest.TestCase):

    def test_filename_blacklist_rejects_without_opening(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            tmp = Path(d)
            root = tmp / "notes"
            root.mkdir()
            # Files the gate must reject without opening them.
            blocked = [
                (root / ".env", "SECRET=" + _synthetic_test_credential()),
                (root / "creds.pem", "-----BEGIN..."),
            ]
            for path, body in blocked:
                path.write_text(body, "utf-8")

            # Custom walker yields the blocked files. The test asserts the
            # gate never opens any of them.
            def fake_walker(r: Path, _max_depth: int) \
                    -> Iterable[Tuple[Path, int]]:
                for path, _body in blocked:
                    yield path, 0

            cfg = _make_config(
                [root], extensions=(".env", ".pem"), quiet_minutes=0,
            )
            cur = FileAdmissionCursor()
            opener = mock.MagicMock(side_effect=AssertionError("must not open"))
            with mock.patch("builtins.open", opener), \
                 mock.patch.object(Path, "open", opener), \
                 mock.patch.object(Path, "read_text", opener), \
                 mock.patch.object(Path, "read_bytes", opener):
                cands, rejs, _ = scan_files(
                    cfg, cur, now=2_000_000_000.0, walker=fake_walker,
                )
            # Both files were rejected as ``secret_path_blacklisted``.
            self.assertEqual(cands, [])
            blacklisted = sorted(
                (r.relative_path, r.rule)
                for r in rejs if r.reason == "secret_path_blacklisted"
            )
            self.assertEqual(len(blacklisted), 2)
            for r in rejs:
                if r.reason == "secret_path_blacklisted":
                    self.assertEqual(r.absolute_path, "")
            opener.assert_not_called()

    def test_blocked_directory_is_pruned_during_walk(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            tmp = Path(d)
            root = _make_root(tmp, layout={
                "ok.md": "harmless",
                ".ssh": {
                    "id_rsa": "private key, must never be opened",
                },
            })
            # Backdate ok.md so the quiet-period check does not apply.
            now = 2_000_000_000.0
            _set_mtime(root / "ok.md", now - 7200)
            cfg = _make_config([root], extensions=(".md",),
                               quiet_minutes=0, depth=4)
            cur = FileAdmissionCursor()
            scan_files(cfg, cur, now=now)
            cands, rejs, stats = scan_files(cfg, cur, now=now + 1)

            admitted = [c.relative_path for c in cands]
            self.assertEqual(admitted, ["ok.md"])
            # The walker prunes the directory before any read — the secret
            # gate's filename/directory checks never even see ``id_rsa``.
            for r in rejs:
                self.assertNotIn(".ssh", r.relative_path)
                self.assertNotIn("id_rsa", r.relative_path)
            # ``.ssh`` was pruned before stat() — only ``ok.md`` was visited.
            self.assertEqual(stats.files_visited, 1)

    def test_content_secret_rejected_with_dummy_value(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            tmp = Path(d)
            root = _make_root(tmp, layout={
                "leaky.md": f"export KEY={DUMMY_AWS}",
                "ok.md": "harmless knowledge note",
            })
            for child in root.iterdir():
                _set_mtime(child, 2_000_000.0 - 7200)

            cfg = _make_config([root], extensions=(".md",), quiet_minutes=0)
            cur = FileAdmissionCursor()
            now = 2_000_000.0
            scan_files(cfg, cur, now=now)
            cands, rejs, stats = scan_files(cfg, cur, now=now + 1)

            admitted = [c.relative_path for c in cands]
            self.assertEqual(admitted, ["ok.md"])
            secret_rejections = [
                r for r in rejs if r.reason == "secret_detected"
            ]
            self.assertEqual(len(secret_rejections), 1)
            sr = secret_rejections[0]
            self.assertEqual(sr.relative_path, "leaky.md")
            self.assertEqual(sr.rule, "secret_in_content")
            # Verdict must not leak the secret fragment into the record.
            blob = repr(sr) + repr(stats.rejections)
            self.assertNotIn(DUMMY_AWS, blob)

            # A subsequent tick does not re-admit the leaky file.
            cands2, rejs2, _ = scan_files(cfg, cur, now=now + 2)
            self.assertEqual(
                [c.relative_path for c in cands2],
                [],
            )

    def test_secret_gate_can_be_substituted_with_a_stub(self):
        """Tests can replace the gate; the module imports it lazily enough."""
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            tmp = Path(d)
            root = _make_root(tmp, layout={"a.md": "harmless"})
            _set_mtime(root / "a.md", 2_000_000.0 - 7200)

            # Make every inspect_text call fail closed.
            from caretaker import secrets as real_secrets
            real_verdict = real_secrets.Verdict

            def boom(_text, *, path=""):
                return real_verdict(False, "test_stub", "stub rejects all")

            cfg = _make_config([root], extensions=(".md",), quiet_minutes=0)
            cur = FileAdmissionCursor()
            now = 2_000_000.0
            with mock.patch("caretaker.files.inspect_text", side_effect=boom):
                scan_files(cfg, cur, now=now)
                _, rejs, _ = scan_files(cfg, cur, now=now + 1)
            self.assertTrue(
                any(
                    r.reason == "test_stub"
                    and r.rule == "test_stub"
                    for r in rejs
                ),
                f"stub gate must trigger: {rejs!r}",
            )

    def test_binary_and_non_utf8_are_not_secret_hits(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            tmp = Path(d)
            root = tmp / "notes"
            root.mkdir()
            binary = root / "bin.md"
            binary.write_bytes(b"hello\x00world")
            bad_utf = root / "bad.md"
            bad_utf.write_bytes(b"\xff\xfe not utf8")
            now = 2_000_000.0
            _set_mtime(binary, now - 7200)
            _set_mtime(bad_utf, now - 7200)

            cfg = _make_config([root], extensions=(".md",), quiet_minutes=0)
            cur = FileAdmissionCursor()
            scan_files(cfg, cur, now=now)
            cands, rejs, stats = scan_files(cfg, cur, now=now + 1)
            self.assertEqual(cands, [])
            reasons = sorted(r.reason for r in rejs)
            self.assertIn("binary_content", reasons)
            self.assertIn("not_utf8", reasons)
            self.assertNotIn("secret_detected", reasons)
            self.assertEqual(stats.rejections.get("secret_detected"), None)

    def test_secret_rejection_omits_absolute_path_and_secret_fragment(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            tmp = Path(d)
            root = _make_root(tmp, layout={
                "leaky.md": f"export KEY={DUMMY_AWS}",
            })
            _set_mtime(root / "leaky.md", 2_000_000.0 - 7200)
            cfg = _make_config([root], extensions=(".md",), quiet_minutes=0)
            cur = FileAdmissionCursor()
            now = 2_000_000.0
            scan_files(cfg, cur, now=now)
            _, rejs, _ = scan_files(cfg, cur, now=now + 1)
            secret_rejections = [
                r for r in rejs if r.reason == "secret_detected"
            ]
            self.assertEqual(len(secret_rejections), 1)
            sr = secret_rejections[0]
            self.assertEqual(sr.relative_path, "leaky.md")
            self.assertEqual(sr.rule, "secret_in_content")
            self.assertEqual(sr.absolute_path, "")
            blob = json.dumps(sr.__dict__, default=str)
            self.assertNotIn(DUMMY_AWS, blob)
            self.assertNotIn(str(root.resolve()), blob)


# ---------------------------------------------------------------------------
# 9. Cursor round-trip and supporting helpers.
# ---------------------------------------------------------------------------


class CursorRoundTripTests(unittest.TestCase):

    def test_to_from_dict_roundtrip(self):
        cur = FileAdmissionCursor(
            pending={"a.md": (10, "deadbeef" * 8)},
            stable={"b.md": (20, "feedface" * 8)},
        )
        blob = json.loads(json.dumps(cur.to_dict()))
        cur2 = FileAdmissionCursor.from_dict(blob)
        self.assertEqual(cur2.pending, cur.pending)
        self.assertEqual(cur2.stable, cur.stable)

    def test_from_dict_ignores_garbage(self):
        cur = FileAdmissionCursor.from_dict({
            "pending": {"x": [1, "aa"]},
            "stable": "not a mapping",
        })
        self.assertEqual(cur.pending, {"x": (1, "aa")})
        self.assertEqual(cur.stable, {})

    def test_from_dict_handles_none(self):
        self.assertEqual(FileAdmissionCursor.from_dict(None).pending, {})


class HelperTests(unittest.TestCase):

    def test_is_within_root(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            root = Path(d) / "root"
            root.mkdir()
            child = root / "sub" / "file.md"
            child.parent.mkdir(parents=True)
            child.write_text("x", "utf-8")
            outside = Path(d) / "outside.md"
            outside.write_text("y", "utf-8")
            self.assertTrue(is_within_root(child, root))
            self.assertFalse(is_within_root(outside, root))
            self.assertTrue(is_within_root(root, root))  # root contains itself

    def test_safe_relpath_uses_posix_separators(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            root = Path(d) / "r"
            root.mkdir()
            child = root / "sub" / "file.md"
            child.parent.mkdir(parents=True)
            child.write_text("x", "utf-8")
            self.assertEqual(safe_relpath(child, root), "sub/file.md")


class SourceConfigValidationTests(unittest.TestCase):

    def test_from_mapping_validates_shape(self):
        with self.assertRaises(ValueError):
            FileSourceConfig.from_mapping({}, source_id="x")  # no extensions
        with self.assertRaises(ValueError):
            FileSourceConfig.from_mapping(
                {"extensions": [".md"]}, source_id=""
            )  # empty source_id
        with self.assertRaises(ValueError):
            FileSourceConfig.from_mapping(
                {"extensions": ["md"]}, source_id="x"
            )  # extension missing leading dot
        with self.assertRaises(ValueError):
            FileSourceConfig.from_mapping(
                {"extensions": [".md"], "quiet_minutes": -1}, source_id="x"
            )

    def test_root_post_init_validates(self):
        with self.assertRaises(ValueError):
            RootConfig(path="", project_slug="x")
        with self.assertRaises(ValueError):
            RootConfig(path="/x", project_slug="x", max_depth=-1)
        with self.assertRaises(ValueError):
            RootConfig(path="/x", project_slug="x", max_depth="4")  # type: ignore[arg-type]


class DefaultWalkerTests(unittest.TestCase):

    def test_default_walk_skips_blocked_dirs(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            tmp = Path(d)
            (tmp / "ok.md").write_text("a", "utf-8")
            (tmp / ".git").mkdir()
            (tmp / ".git" / "config").write_text("b", "utf-8")
            (tmp / "node_modules").mkdir()
            (tmp / "node_modules" / "x.md").write_text("c", "utf-8")

            walked = sorted(p.name for p, _ in default_walk(tmp, 4))
            self.assertIn("ok.md", walked)
            self.assertNotIn("config", walked)
            self.assertNotIn("x.md", walked)


if __name__ == "__main__":
    unittest.main()
