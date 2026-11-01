"""Synthetic tests for bounded secure reads used by the curation gateway."""
import importlib.util
from pathlib import Path
import stat
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("curation", HERE / "curation.py")
curation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(curation)


class CurationReadSizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_small_file_uses_size_bound_and_returns_exact_bytes(self):
        path = self.root / "small.json"
        data = b'{"synthetic":true}'
        path.write_bytes(data)
        original_read = curation.knowledge.os.read
        calls = []

        def counted_read(descriptor, size):
            calls.append(size)
            return original_read(descriptor, size)

        with mock.patch.object(curation.knowledge.os, "read", counted_read):
            self.assertEqual(curation._read(path), data)

        self.assertEqual(len(calls), 2)
        self.assertEqual(sum(calls), len(data) + 2)

    def test_file_larger_than_one_megabyte_is_not_capped(self):
        path = self.root / "large.json"
        data = b"x" * (1024 * 1024 + 73)
        path.write_bytes(data)
        self.assertEqual(curation._read(path), data)

    def test_growth_during_read_fails_closed(self):
        path = self.root / "growing.json"
        path.write_bytes(b"x" * 128)
        original_read = curation.knowledge.os.read
        grew = False

        def grow_before_read(descriptor, size):
            nonlocal grew
            if not grew:
                grew = True
                with path.open("ab") as handle:
                    handle.write(b"+")
            return original_read(descriptor, size)

        with mock.patch.object(curation.knowledge.os, "read", grow_before_read):
            with self.assertRaises(curation.CurationError):
                curation._read(path)
        self.assertTrue(grew)

    def test_changed_inode_between_snapshot_checks_fails_closed(self):
        path = self.root / "changed.json"
        path.write_bytes(b'{"same":"size"}')
        knowledge = curation.knowledge
        original_fstat = knowledge.os.fstat
        calls = 0

        def changed_second_fstat(descriptor):
            nonlocal calls
            calls += 1
            result = original_fstat(descriptor)
            if calls != 2:
                return result
            return SimpleNamespace(
                st_dev=result.st_dev,
                st_ino=int(result.st_ino or 0) + 1,
                st_mode=result.st_mode,
                st_size=result.st_size,
                st_mtime_ns=result.st_mtime_ns,
            )

        with mock.patch.object(knowledge.os, "fstat", changed_second_fstat):
            with self.assertRaises(curation.CurationError):
                curation._read(path)
        self.assertEqual(calls, 2)

    def test_directory_is_never_returned_as_file_content(self):
        directory = self.root / "directory"
        directory.mkdir()
        with self.assertRaises((curation.CurationError, OSError)):
            curation._read(directory)

    def test_reparse_path_is_rejected(self):
        path = self.root / "reparse.json"
        path.write_bytes(b"{}")
        original_lstat = Path.lstat

        def report_reparse(candidate):
            result = original_lstat(candidate)
            if candidate == path:
                return SimpleNamespace(
                    st_mode=stat.S_IFREG | 0o600,
                    st_file_attributes=0x400,
                )
            return result

        with mock.patch.object(curation, "_GET_FILE_ATTRIBUTES_W", None), \
                mock.patch.object(Path, "lstat", report_reparse):
            with self.assertRaises(curation.CurationError):
                curation._read(path)


if __name__ == "__main__":
    unittest.main()
