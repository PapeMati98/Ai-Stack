from __future__ import annotations

import json
import os
import subprocess
import sys

from support import PACKAGE_ROOT, CoordTestCase

from coordfs.hashing import hash_tree


COORD_PY = PACKAGE_ROOT / "coord.py"


class DirectoryHashingTests(CoordTestCase):
    def _directory(self):
        target = self.cwd / "module"
        nested = target / "nested"
        nested.mkdir(parents=True)
        (target / "root.txt").write_text("root", encoding="utf-8")
        (nested / "child.txt").write_text("child", encoding="utf-8")
        return target

    def test_hash_tree_handles_nested_directories(self) -> None:
        target = self._directory()
        first = hash_tree(target)
        second = hash_tree(target)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)

    def test_cli_claim_accepts_directory_resource(self) -> None:
        target = self._directory()
        env = os.environ.copy()
        env["COORDFS_HOME"] = str(self.home)
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                str(COORD_PY),
                "claim",
                "--resource",
                str(target),
                "--kind",
                "path",
                "--agent-id",
                "agent-directory",
                "--ttl",
                "30",
            ],
            cwd=str(self.cwd),
            env=env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["lease"]["resource_id"], str(target).lower())
        self.assertEqual(len(payload["lease"]["file_hash"]), 64)
