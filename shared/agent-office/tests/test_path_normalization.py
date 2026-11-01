from __future__ import annotations

import os
import sys

from coordfs.identity import canonicalize_resource

from support import CoordTestCase


class PathNormalizationTests(CoordTestCase):
    def test_windows_path_case_and_slash_aliases(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows case-alias collapse (os.path.normcase is identity on POSIX)")
        target = self.cwd / "Alpha" / "File.TXT"
        target.parent.mkdir()
        target.write_text("payload", encoding="utf-8")
        first = canonicalize_resource(str(target), kind="path", cwd=self.cwd)
        mixed = str(target).swapcase().replace("\\", "/")
        second = canonicalize_resource(mixed, kind="path", cwd=self.cwd)
        self.assertEqual(first, second)
        self.assertEqual(first, os.path.normcase(os.path.abspath(str(target.resolve()))))
        claimed = self.board.claim(str(target), "agent-a", ttl_s=30, kind="path")
        who = self.board.who(mixed, kind="path")
        self.assertEqual(who["holder_agent"], "agent-a")
        self.assertEqual(who["resource_id"], claimed["resource_id"])

    def test_write_scope_uses_agent_graph_namespace(self) -> None:
        scope = canonicalize_resource("frontend/src/", kind="scope")
        self.assertEqual(scope, "frontend/src/")
        prefixed = canonicalize_resource("scope:frontend/package.json")
        self.assertEqual(prefixed, "frontend/package.json")
        data = canonicalize_resource("data:asset-9")
        self.assertEqual(data, "data:asset-9")

    def test_unprefixed_scope_identity_does_not_depend_on_cwd_or_existence(self) -> None:
        first_root = self.cwd / "first"
        second_root = self.cwd / "second"
        (first_root / "frontend" / "src").mkdir(parents=True)
        second_root.mkdir()

        first = canonicalize_resource("frontend/src", cwd=first_root)
        second = canonicalize_resource("frontend/src", cwd=second_root)
        self.assertEqual(first, "frontend/src")
        self.assertEqual(second, first)

        explicit_path = canonicalize_resource("frontend/src", kind="path", cwd=first_root)
        self.assertEqual(
            explicit_path,
            os.path.normcase(os.path.abspath(str((first_root / "frontend" / "src").resolve()))),
        )

    def test_short_path_alias_on_windows(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows short-path aliases")
        import ctypes

        target = self.cwd / "Long Directory Name" / "document.txt"
        target.parent.mkdir()
        target.write_text("x", encoding="utf-8")
        get_short = ctypes.windll.kernel32.GetShortPathNameW
        buf = ctypes.create_unicode_buffer(32768)
        length = get_short(str(target), buf, 32768)
        if length == 0 or buf.value.lower() == str(target).lower():
            self.skipTest("filesystem did not allocate an 8.3 alias")
        long_id = canonicalize_resource(str(target), kind="path", cwd=self.cwd)
        short_id = canonicalize_resource(buf.value, kind="path", cwd=self.cwd)
        self.assertEqual(long_id, short_id)
