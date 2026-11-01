"""Assigned Windows drive-absolute paths that map lexically inside workspace_root.

The Grok ACP broker must accept an absolute Windows path to a file inside the assigned
workspace as if it were the equivalent `project/<...>` form. Every existing guard (scope,
traversal, controls, reserved names, metadata, executable Fleet scripts, links, hardlinks,
non-file Write, read-only/missing-allowance modes) stays in force for absolute paths too.
Diagnostics still redact any path that would resolve outside the workspace, never exposing
full paths, commands or content."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import grok_acp  # noqa: E402

SECRET = "TOP-SECRET-CONTENT-4711"


def request(raw_input, *, kind="edit", options=("allow_once", "reject_once")):
    return {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "session/request_permission",
        "params": {
            "toolCall": {"kind": kind, "title": "C:\\fixtures\\someone\\secret\\x.txt", "rawInput": raw_input},
            "options": [{"kind": k, "optionId": f"opt-{k}", "name": SECRET} for k in options],
        },
    }


class AbsolutePathAllowanceTests(unittest.TestCase):
    """Absolute Windows paths that fall lexically inside the assigned workspace work."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        (self.root / "existing.txt").write_text("x", encoding="utf-8")
        (self.root / "sub").mkdir()
        (self.root / "adir").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def allow(self, raw, *, allow_writes=True):
        return grok_acp._permission_allow_once_option(
            request(raw), allow_writes=allow_writes, workspace_root=self.root
        )

    def test_normal_relative_path_still_allowed(self):
        self.assertEqual(
            self.allow({"variant": "SearchReplace", "file_path": "project/existing.txt"}),
            "opt-allow_once",
        )
        self.assertEqual(
            self.allow({"variant": "Write", "file_path": "project/new.txt", "content": "a"}),
            "opt-allow_once",
        )

    def test_absolute_existing_file_backslash_allowed_for_both_variants(self):
        p = str(self.root / "existing.txt")
        self.assertEqual(self.allow({"variant": "SearchReplace", "file_path": p}), "opt-allow_once")
        self.assertEqual(self.allow({"variant": "Write", "file_path": p, "content": "y"}), "opt-allow_once")

    def test_absolute_existing_file_forward_slash_allowed(self):
        p = (self.root / "existing.txt").as_posix()
        self.assertEqual(self.allow({"variant": "SearchReplace", "file_path": p}), "opt-allow_once")
        self.assertEqual(self.allow({"variant": "Write", "file_path": p, "content": "y"}), "opt-allow_once")

    def test_absolute_existing_file_windows_case_variant_allowed(self):
        # Build an absolute path whose directory components are uppercase. The Windows
        # filesystem is case-insensitive, so containment must still resolve.
        parts = self.root.parts
        upper_root = Path(parts[0]) / "/".join(part.upper() for part in parts[1:])
        p = str(upper_root / "existing.txt")
        self.assertEqual(self.allow({"variant": "SearchReplace", "file_path": p}), "opt-allow_once")
        self.assertEqual(self.allow({"variant": "Write", "file_path": p, "content": "y"}), "opt-allow_once")

    def test_absolute_new_file_allowed(self):
        p = str(self.root / "new.txt")
        self.assertEqual(self.allow({"variant": "Write", "file_path": p, "content": "a"}), "opt-allow_once")

    def test_absolute_nested_new_file_allowed(self):
        p = str(self.root / "sub" / "deeper" / "new.py")
        self.assertEqual(self.allow({"variant": "Write", "file_path": p, "content": ""}), "opt-allow_once")


class AbsolutePathRefusalTests(unittest.TestCase):
    """Absolute paths outside the workspace and other unsafe variants stay refused."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        (self.root / "existing.txt").write_text("x", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def allow(self, raw, *, allow_writes=True):
        return grok_acp._permission_allow_once_option(
            request(raw), allow_writes=allow_writes, workspace_root=self.root
        )

    def _other_drive(self) -> str:
        workspace_drive = str(self.root)[0].lower()
        return "Z" if workspace_drive != "z" else "Y"

    def test_root_alone_refused(self):
        self.assertIsNone(self.allow({"variant": "SearchReplace", "file_path": str(self.root)}))
        self.assertIsNone(self.allow({"variant": "Write", "file_path": str(self.root), "content": "x"}))

    def test_bare_project_relative_root_refused_for_both_variants(self):
        # Bare `project` (the workspace root alone, no trailing segment) must be refused
        # regardless of variant: the structural containment check rejects it before
        # any of the existing guards run.
        self.assertIsNone(self.allow({"variant": "SearchReplace", "file_path": "project"}))
        self.assertIsNone(self.allow({"variant": "Write", "file_path": "project", "content": "x"}))

    def test_absolute_drive_path_refused_when_os_name_is_posix(self):
        # On a non-Windows host the drive-absolute mapping must be refused up front;
        # the Windows-only containment guard short-circuits before any path parsing.
        # `self.root` is built in setUp before this mock is applied so its platform
        # class is locked in (WindowsPath on Windows, PosixPath on POSIX) and cannot
        # be coerced by changing `os.name` later.
        p = str(self.root / "existing.txt")
        with mock.patch("os.name", "posix"):
            self.assertIsNone(self.allow({"variant": "SearchReplace", "file_path": p}))
            self.assertIsNone(self.allow({"variant": "Write", "file_path": p, "content": "x"}))

    def test_absolute_outside_workspace_refused(self):
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("o", encoding="utf-8")
        p = str(outside)
        self.assertIsNone(self.allow({"variant": "SearchReplace", "file_path": p}))
        self.assertIsNone(self.allow({"variant": "Write", "file_path": p, "content": "x"}))

    def test_absolute_sibling_prefix_refused(self):
        sibling_root = Path(self.tmp.name) / "project_other"
        sibling_root.mkdir()
        (sibling_root / "file.txt").write_text("s", encoding="utf-8")
        p = str(sibling_root / "file.txt")
        self.assertIsNone(self.allow({"variant": "SearchReplace", "file_path": p}))
        self.assertIsNone(self.allow({"variant": "Write", "file_path": p, "content": "x"}))

    def test_absolute_other_drive_refused(self):
        other = self._other_drive()
        p = f"{other}:\\some\\other\\path\\file.txt"
        self.assertIsNone(self.allow({"variant": "SearchReplace", "file_path": p}))
        self.assertIsNone(self.allow({"variant": "Write", "file_path": p, "content": "x"}))

    def test_absolute_drive_relative_refused(self):
        # "D:foo" without a separator after the colon is a drive-relative path.
        drive = str(self.root)[0]
        p = f"{drive}:project\\file.txt"
        self.assertIsNone(self.allow({"variant": "Write", "file_path": p, "content": "x"}))

    def test_absolute_device_extended_path_refused(self):
        p = "\\\\?\\" + str(self.root) + "\\file.txt"
        self.assertIsNone(self.allow({"variant": "Write", "file_path": p, "content": "x"}))

    def test_absolute_unc_refused(self):
        p = "\\\\host\\share\\project\\file.txt"
        self.assertIsNone(self.allow({"variant": "SearchReplace", "file_path": p}))

    def test_absolute_traversal_refused(self):
        p = str(self.root) + "\\sub\\..\\..\\outside.txt"
        self.assertIsNone(self.allow({"variant": "Write", "file_path": p, "content": "x"}))

    def test_absolute_ads_colon_refused(self):
        # "D:\workspace\project\x:foo.txt" has an ADS colon in a segment.
        p = str(self.root) + "\\x:foo.txt"
        self.assertIsNone(self.allow({"variant": "Write", "file_path": p, "content": "x"}))

    def test_absolute_reserved_name_refused(self):
        for name in ("nul", "CON.txt", "com9.log", "lpt1"):
            p = str(self.root) + "\\" + name
            self.assertIsNone(self.allow({"variant": "Write", "file_path": p, "content": "x"}), p)

    def test_absolute_trailing_dot_refused(self):
        p = str(self.root) + "\\x."
        self.assertIsNone(self.allow({"variant": "Write", "file_path": p, "content": "x"}))

    def test_absolute_control_characters_refused(self):
        p = str(self.root) + "\\a\x00b.txt"
        self.assertIsNone(self.allow({"variant": "Write", "file_path": p, "content": "x"}))
        p = str(self.root) + "\\a\nb.txt"
        self.assertIsNone(self.allow({"variant": "SearchReplace", "file_path": p}))

    def test_read_only_mode_refuses_absolute(self):
        p = str(self.root / "existing.txt")
        self.assertIsNone(self.allow({"variant": "SearchReplace", "file_path": p}, allow_writes=False))
        self.assertIsNone(self.allow({"variant": "Write", "file_path": p, "content": "x"}, allow_writes=False))

    def test_missing_allow_once_refuses_absolute(self):
        msg = request(
            {"variant": "Write", "file_path": str(self.root / "x.txt"), "content": "x"},
            options=("reject_once",),
        )
        self.assertIsNone(
            grok_acp._permission_allow_once_option(msg, allow_writes=True, workspace_root=self.root)
        )

    def test_absolute_dot_git_refused(self):
        git = self.root / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")
        p = str(git / "HEAD")
        self.assertIsNone(self.allow({"variant": "SearchReplace", "file_path": p}))
        self.assertIsNone(self.allow({"variant": "Write", "file_path": p, "content": "x"}))

    def test_absolute_fleet_executable_refused(self):
        fleet = self.root / ".fleet"
        fleet.mkdir()
        ps1 = fleet / "run.ps1"
        ps1.write_text("echo hi", encoding="utf-8")
        p = str(ps1)
        self.assertIsNone(self.allow({"variant": "SearchReplace", "file_path": p}))
        self.assertIsNone(self.allow({"variant": "Write", "file_path": p, "content": "x"}))

    def test_absolute_fleet_evidence_still_allowed(self):
        fleet = self.root / ".fleet"
        fleet.mkdir()
        evidence = fleet / "evidence"
        evidence.mkdir()
        md = evidence / "review.md"
        md.write_text("r", encoding="utf-8")
        p = str(md)
        self.assertEqual(self.allow({"variant": "Write", "file_path": p, "content": "e"}), "opt-allow_once")

    def test_absolute_write_to_directory_refused(self):
        (self.root / "adir").mkdir(exist_ok=True)
        p = str(self.root / "adir")
        self.assertIsNone(self.allow({"variant": "Write", "file_path": p, "content": "x"}))

    def test_absolute_symlink_target_refused(self):
        outside = Path(self.tmp.name) / "outside_dir"
        outside.mkdir()
        link = self.root / "jn"
        try:
            grok_acp._create_directory_link(outside, link)
        except Exception as exc:  # pragma: no cover - platform without link rights
            self.skipTest(f"cannot create a directory link: {exc}")
        p = str(link / "new.txt")
        self.assertIsNone(self.allow({"variant": "Write", "file_path": p, "content": "x"}))

    def test_absolute_hardlink_target_refused(self):
        target = self.root / "hl.txt"
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        try:
            os.link(outside, target)
        except OSError as exc:  # pragma: no cover
            self.skipTest(f"hard links unavailable: {exc}")
        p = str(target)
        self.assertIsNone(self.allow({"variant": "Write", "file_path": p, "content": "x"}))
        self.assertIsNone(self.allow({"variant": "SearchReplace", "file_path": p}))


class AbsoluteDiagnosticTests(unittest.TestCase):
    """Diagnostics never leak full paths, commands or content for outside absolute paths."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        (self.root / "existing.txt").write_text("x", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def classify(self, raw, **kw):
        record = grok_acp.classify_permission_denial(
            request(raw, **kw), allow_writes=True, workspace_root=self.root, deny_all_tools=False
        )
        dumped = json.dumps(record)
        self.assertNotIn(SECRET, dumped)
        self.assertNotIn(self.tmp.name, dumped)
        return record

    def test_outside_absolute_path_is_not_leaked(self):
        outside = Path(self.tmp.name) / "secret"
        outside.mkdir()
        (outside / "x.txt").write_text("y", encoding="utf-8")
        r = self.classify({"variant": "SearchReplace", "file_path": str(outside / "x.txt")})
        self.assertEqual(r["reason"], "path_drive_absolute")
        self.assertNotIn("project_relative_path", r)
        self.assertNotIn(str(outside), json.dumps(r))

    def test_sibling_prefix_absolute_path_is_not_leaked(self):
        sibling = Path(self.tmp.name) / "project_other"
        sibling.mkdir()
        (sibling / "x.txt").write_text("s", encoding="utf-8")
        p = str(sibling / "x.txt")
        r = self.classify({"variant": "SearchReplace", "file_path": p})
        self.assertEqual(r["reason"], "path_drive_absolute")
        self.assertNotIn("project_relative_path", r)
        self.assertNotIn(p, json.dumps(r))

    def test_other_drive_absolute_path_is_not_leaked(self):
        workspace_drive = str(self.root)[0].lower()
        other = "Z" if workspace_drive != "z" else "Y"
        p = f"{other}:\\outside\\secret\\x.txt"
        r = self.classify({"variant": "SearchReplace", "file_path": p})
        self.assertEqual(r["reason"], "path_drive_absolute")
        self.assertNotIn("project_relative_path", r)
        self.assertNotIn(p, json.dumps(r))

    def test_inside_absolute_path_is_exposed_as_project_relative(self):
        # When the absolute path maps inside the workspace, classifier must expose only
        # the canonical project/... form, never the full drive-letter path.
        abs_path = str(self.root / "existing.txt")
        r = self.classify({"variant": "Delete", "file_path": abs_path})
        self.assertEqual(r["reason"], "write_variant_not_allowed")
        self.assertEqual(r["path_shape"], "project_relative")
        self.assertEqual(r["project_relative_path"], "project/existing.txt")
        dumped = json.dumps(r)
        self.assertNotIn(abs_path, dumped)
        self.assertNotIn(str(self.root), dumped)

    def test_relative_diagnostic_does_not_leak_full_path(self):
        # Relative project/... diagnostic also never contains tmp paths or content.
        r = self.classify({"variant": "Delete", "file_path": "project/existing.txt", "content": SECRET})
        self.assertEqual(r["reason"], "write_variant_not_allowed")
        self.assertEqual(r["path_shape"], "project_relative")
        self.assertEqual(r["project_relative_path"], "project/existing.txt")
        self.assertNotIn(SECRET, json.dumps(r))
        self.assertNotIn(self.tmp.name, json.dumps(r))


if __name__ == "__main__":
    unittest.main()