"""Offline store snapshot/verify/restore on synthetic coord homes built with the real Office API."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

from support import PACKAGE_ROOT  # noqa: F401  (puts the package on sys.path)

import office_store_snapshot as tool
from coordfs.office import Office
from coordfs.wake import Wake


def tree(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "<dir>")
            for p in root.rglob("*")}


class StoreSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "coord"
        alice = Office(agent_id="alice-host-a", home=self.home)
        bob = Office(agent_id="bob-host-b", home=self.home)
        alice.presence(ttl_s=600, status="busy")
        bob.presence(ttl_s=600, status="idle")
        sent = alice.send("bob-host-b", "syntetyczne zlecenie: Zażółć gęślą jaźń", kind="question")
        bob.send("alice-host-a", "odpowiedz", kind="note", reply_to=sent["id"])
        Wake(home=self.home).stop()
        Wake(home=self.home).start()
        self.frozen = self.home / "office" / "FROZEN"
        self.frozen.parent.mkdir(parents=True, exist_ok=True)
        self.frozen.write_text("paused for snapshot\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_tool(self, *argv: str) -> tuple[int, dict]:
        buffer = io.StringIO()
        from contextlib import redirect_stdout
        with redirect_stdout(buffer):
            code = tool.main(list(argv))
        return code, json.loads(buffer.getvalue())

    def take(self, name: str = "snap") -> Path:
        out = self.root / name
        code, result = self.run_tool("snapshot", "--home", str(self.home), "--out", str(out))
        self.assertEqual((code, result["status"]), (0, "ok"), result)
        return out

    def test_snapshot_verify_restore_reproduce_the_home_and_leave_it_untouched(self) -> None:
        before = tree(self.home)
        out = self.take()
        self.assertEqual(tree(self.home), before)  # the source is only read
        self.assertEqual(self.run_tool("verify", "--snapshot", str(out))[0], 0)
        dest = self.root / "restored"
        code, result = self.run_tool("restore", "--snapshot", str(out), "--dest", str(dest))
        self.assertEqual((code, result["code"]), (0, "restore_verified"), result)
        self.assertEqual(tree(dest), before)
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        self.assertGreater(manifest["counts"]["jsonl_lines"], 0)
        self.assertEqual(manifest["totals"]["files"], sum(1 for v in before.values() if v != "<dir>"))
        self.assertEqual(len(manifest["directories"]), sum(1 for v in before.values() if v == "<dir>"))

    def test_missing_or_non_regular_frozen_marker_is_refused(self) -> None:
        self.frozen.rename(self.frozen.with_name("FROZEN.off"))
        code, result = self.run_tool("snapshot", "--home", str(self.home), "--out", str(self.root / "a"))
        self.assertEqual((code, result["code"]), (1, "refused"))
        self.assertIn("FROZEN", result["reason"])
        self.frozen.mkdir()
        code, result = self.run_tool("snapshot", "--home", str(self.home), "--out", str(self.root / "b"))
        self.assertEqual(code, 1)
        self.assertFalse((self.root / "b").exists())

    def test_non_empty_out_is_refused_and_left_as_is(self) -> None:
        out = self.root / "busy"
        out.mkdir()
        (out / "keep.txt").write_text("x", encoding="utf-8")
        code, result = self.run_tool("snapshot", "--home", str(self.home), "--out", str(out))
        self.assertEqual(code, 1)
        self.assertEqual([p.name for p in out.iterdir()], ["keep.txt"])

    def test_a_link_inside_the_store_is_refused(self) -> None:
        target = self.root / "outside.txt"
        target.write_text("outside", encoding="utf-8")
        link = self.home / "office" / "link.txt"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            if os.name != "nt":
                self.skipTest("this account cannot create symlinks")
            import subprocess  # a directory junction needs no privilege on Windows
            outside = self.root / "outside-dir"
            outside.mkdir()
            link = self.home / "office" / "junction"
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                                  capture_output=True, check=False)
            if made.returncode != 0:
                self.skipTest("neither a symlink nor a junction can be created")
        code, result = self.run_tool("snapshot", "--home", str(self.home), "--out", str(self.root / "c"))
        self.assertEqual((code, result["code"]), (1, "refused"))
        self.assertIn("link", result["reason"])

    def test_verify_detects_changed_extra_and_missing_members(self) -> None:
        out = self.take()
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        original = (out / "store.tar").read_bytes()
        victim = manifest["files"][0]
        with tarfile.open(out / "store.tar", "r:") as archive:
            data = {m.name: (m, archive.extractfile(m).read() if m.isreg() else None) for m in archive}

        def rebuild(members) -> None:
            with tarfile.open(out / "store.tar", "w", format=tarfile.PAX_FORMAT) as archive:
                for member, payload in members:
                    archive.addfile(member, io.BytesIO(payload) if payload is not None else None)

        changed = [(m, (p[:-1] + bytes([p[-1] ^ 1])) if (m.name == victim["path"] and p) else p)
                   for m, p in data.values()]
        rebuild(changed)
        self.assertIn("changed file: " + victim["path"], self.run_tool("verify", "--snapshot", str(out))[1]["problems"])
        extra_member = tarfile.TarInfo("office/extra.txt")
        extra_member.size = 3
        rebuild(list(data.values()) + [(extra_member, b"abc")])
        self.assertIn("extra file: office/extra.txt", self.run_tool("verify", "--snapshot", str(out))[1]["problems"])
        rebuild([(m, p) for m, p in data.values() if m.name != victim["path"]])
        self.assertIn("missing file: " + victim["path"], self.run_tool("verify", "--snapshot", str(out))[1]["problems"])
        (out / "store.tar").write_bytes(original)
        self.assertEqual(self.run_tool("verify", "--snapshot", str(out))[0], 0)

    def test_restore_refuses_traversal_members_and_non_empty_dest(self) -> None:
        out = self.take()
        busy = self.root / "busy-dest"
        busy.mkdir()
        (busy / "x").write_text("keep", encoding="utf-8")
        code, result = self.run_tool("restore", "--snapshot", str(out), "--dest", str(busy))
        self.assertEqual(code, 1)
        self.assertEqual([p.name for p in busy.iterdir()], ["x"])
        for bad in ("../escape.txt", "/abs.txt", "C:/drive.txt", "office/../../up.txt",
                    ".. /evil.txt", "office/NUL", "office/nul.txt", "a./b.txt", "b /c.txt", "office/...",
                    "office/x?.txt", "office\\y.txt"):
            with self.subTest(member=bad):
                evil = self.root / ("evil-" + str(abs(hash(bad))))
                evil.mkdir()
                with tarfile.open(evil / "store.tar", "w", format=tarfile.PAX_FORMAT) as archive:
                    info = tarfile.TarInfo(bad)
                    info.size = 1
                    archive.addfile(info, io.BytesIO(b"x"))
                (evil / "manifest.json").write_text(json.dumps({
                    "schema": tool.SCHEMA, "directories": [], "files": [{"path": bad, "bytes": 1,
                    "sha256": hashlib.sha256(b"x").hexdigest()}],
                    "archive_sha256": hashlib.sha256((evil / "store.tar").read_bytes()).hexdigest()}), encoding="utf-8")
                code, result = self.run_tool("restore", "--snapshot", str(evil), "--dest", str(self.root / "d"))
                self.assertEqual((code, result["code"]), (1, "refused"), result)
                self.assertFalse((self.root / "escape.txt").exists())
                self.assertFalse((self.root / "d").exists())

    def test_link_members_in_the_archive_are_refused(self) -> None:
        evil = self.root / "evil-link"
        evil.mkdir()
        with tarfile.open(evil / "store.tar", "w", format=tarfile.PAX_FORMAT) as archive:
            info = tarfile.TarInfo("office/link")
            info.type, info.linkname = tarfile.SYMTYPE, "../../outside"
            archive.addfile(info)
        (evil / "manifest.json").write_text(json.dumps({"schema": tool.SCHEMA, "directories": [], "files": [],
                                                        "archive_sha256": "0" * 64}), encoding="utf-8")
        self.assertEqual(self.run_tool("verify", "--snapshot", str(evil))[1]["code"], "refused")

    def test_unparsable_jsonl_line_is_reported_not_fatal(self) -> None:
        broken = self.home / "office" / "notes.jsonl"
        broken.write_text('{"ok": 1}\n{not json\n', encoding="utf-8")
        out = self.take()
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn("office/notes.jsonl:2", manifest["counts"]["unreadable"])

    # Review 0a0f6579 (Grok 4.7): one regression test per finding. -------------------
    def _link_dir(self, link: Path, target: Path) -> bool:
        target.mkdir(exist_ok=True)
        try:
            os.symlink(target, link, target_is_directory=True)
            return True
        except (OSError, NotImplementedError):
            if os.name != "nt":
                return False
            import subprocess
            return subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                  capture_output=True, check=False).returncode == 0

    def test_out_or_dest_given_as_a_link_is_refused(self) -> None:
        link = self.root / "out-link"
        if not self._link_dir(link, self.root / "real-empty"):
            self.skipTest("cannot create a symlink or junction")
        code, result = self.run_tool("snapshot", "--home", str(self.home), "--out", str(link))
        self.assertEqual((code, result["code"]), (1, "refused"))
        self.assertEqual(list((self.root / "real-empty").iterdir()), [])
        out = self.take()
        code, result = self.run_tool("restore", "--snapshot", str(out), "--dest", str(link))
        self.assertEqual((code, result["code"]), (1, "refused"))
        self.assertEqual(list((self.root / "real-empty").iterdir()), [])

    def test_out_inside_the_home_is_refused(self) -> None:
        code, result = self.run_tool("snapshot", "--home", str(self.home), "--out", str(self.home / "snap"))
        self.assertEqual((code, result["code"]), (1, "refused"))
        self.assertFalse((self.home / "snap").exists())

    def test_out_through_a_linked_parent_into_the_home_is_refused_before_writing(self) -> None:
        link = self.root / "parent-link"
        if not self._link_dir(link, self.home / "office"):
            self.skipTest("cannot create a symlink or junction")
        before = tree(self.home)
        code, result = self.run_tool("snapshot", "--home", str(self.home), "--out", str(link / "snap"))
        self.assertEqual((code, result["code"]), (1, "refused"))
        self.assertIn("outside the coord home", result["reason"])
        self.assertEqual(tree(self.home), before)  # nothing was created inside the live store

    def test_a_mount_point_inside_the_home_is_refused(self) -> None:
        from unittest import mock
        inner = (self.home / "office").resolve()
        with mock.patch.object(tool, "_linux_mount_points", return_value=[Path("/"), inner]):
            code, result = self.run_tool("snapshot", "--home", str(self.home), "--out", str(self.root / "m"))
        self.assertEqual((code, result["code"]), (1, "refused"))
        self.assertIn("mount point", result["reason"])
        self.assertFalse((self.root / "m").exists())
        sample = "36 35 98:0 /mnt1 /mnt/with\\040space rw - ext3 /dev/root rw\n"
        with mock.patch.object(Path, "read_text", return_value=sample):
            self.assertEqual(tool._linux_mount_points(), [Path("/mnt/with space")])

    def test_a_change_of_the_tree_during_the_snapshot_fails_it(self) -> None:
        from unittest import mock
        original = tool._walk
        calls = []

        def walk_then_write(home, dev):
            calls.append(1)
            if len(calls) == 2:  # between archiving and the re-walk a writer adds a file
                (self.home / "office" / "late.jsonl").write_text('{"late": 1}\n', encoding="utf-8")
            return original(home, dev)

        with mock.patch.object(tool, "_walk", side_effect=walk_then_write):
            code, result = self.run_tool("snapshot", "--home", str(self.home), "--out", str(self.root / "e"))
        self.assertEqual((code, result["code"]), (1, "refused"))
        self.assertIn("changed", result["reason"])

    def test_restore_refuses_a_member_added_after_verify(self) -> None:
        out = self.take()
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        with tarfile.open(out / "store.tar", "a") as archive:
            info = tarfile.TarInfo("office/extra.json")
            info.size = 2
            archive.addfile(info, io.BytesIO(b"{}"))
        manifest["archive_sha256"] = hashlib.sha256((out / "store.tar").read_bytes()).hexdigest()
        (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        dest = self.root / "f"
        code, result = self.run_tool("restore", "--snapshot", str(out), "--dest", str(dest))
        self.assertEqual(code, 1)
        self.assertIn("extra file: office/extra.json", result["problems"])
        self.assertFalse(dest.exists())

    def test_a_missing_directory_or_archive_hash_is_detected(self) -> None:
        (self.home / "office" / "empty-dir").mkdir()
        out = self.take()
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn("office/empty-dir", manifest["directories"])
        with tarfile.open(out / "store.tar", "r:") as archive:
            members = [(m, archive.extractfile(m).read() if m.isreg() else None) for m in archive]
        with tarfile.open(out / "store.tar", "w", format=tarfile.PAX_FORMAT) as archive:
            for member, payload in members:
                if member.name != "office/empty-dir":
                    archive.addfile(member, io.BytesIO(payload) if payload is not None else None)
        manifest["archive_sha256"] = hashlib.sha256((out / "store.tar").read_bytes()).hexdigest()
        (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.assertIn("missing directory: office/empty-dir", self.run_tool("verify", "--snapshot", str(out))[1]["problems"])
        del manifest["archive_sha256"]
        (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(self.run_tool("verify", "--snapshot", str(out))[1]["code"], "refused")

    def test_usage_error_exit_code(self) -> None:
        self.assertEqual(tool.main(["snapshot"]), 2)


if __name__ == "__main__":
    unittest.main()
