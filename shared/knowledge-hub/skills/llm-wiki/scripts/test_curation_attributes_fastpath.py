"""Windows attribute fast-path checks; fixtures remain synthetic and local."""

from pathlib import Path
import os
import unittest
from unittest import mock

import test_curation

curation = test_curation.curation


@unittest.skipUnless(os.name == "nt", "GetFileAttributesW is Windows-only")
class CurationAttributeFastpathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parent / ".test-output" / "absolute-fastpath"
        cls.parent = cls.root / "tree" / "level-a" / "level-b"
        cls.parent.mkdir(parents=True, exist_ok=True)
        cls.file = cls.parent / "synthetic.txt"
        cls.file.write_bytes(b"synthetic")

    def test_native_reparse_bit_on_ancestor_fails_without_lstat(self):
        reparse_ancestor = self.root / "tree" / "level-a"
        flagged = curation._windows_attribute_path(reparse_ancestor)
        calls = []

        def attributes(value):
            calls.append(value)
            return 0x400 if value == flagged else 0

        with mock.patch.object(curation, "_GET_FILE_ATTRIBUTES_W", attributes), \
                mock.patch.object(Path, "lstat", side_effect=AssertionError("lstat should be skipped")):
            with self.assertRaisesRegex(curation.CurationError, "reparse or symlink"):
                curation._absolute(self.file)

        self.assertIn(flagged, calls)
        self.assertNotIn(curation._windows_attribute_path(self.file), calls)

    def test_invalid_attribute_result_falls_back_and_missing_leaf_keeps_old_behavior(self):
        missing = self.parent / "not-created.txt"
        missing_api_path = curation._windows_attribute_path(missing)
        real_attributes = curation._GET_FILE_ATTRIBUTES_W
        real_lstat = Path.lstat
        fallback_paths = []

        def attributes(value):
            if value == missing_api_path:
                return 0xFFFFFFFF
            return real_attributes(value)

        def counted_lstat(path):
            fallback_paths.append(path)
            return real_lstat(path)

        with mock.patch.object(curation, "_GET_FILE_ATTRIBUTES_W", attributes), \
                mock.patch.object(Path, "lstat", counted_lstat):
            resolved = curation._absolute(missing)

        self.assertEqual(resolved, missing)
        self.assertIn(missing, fallback_paths)

    def test_native_api_exception_preserves_lstat_error(self):
        target_api_path = curation._windows_attribute_path(self.file)
        real_attributes = curation._GET_FILE_ATTRIBUTES_W
        real_lstat = Path.lstat

        def attributes(value):
            if value == target_api_path:
                raise OSError("synthetic unsupported API path")
            return real_attributes(value)

        def failing_lstat(path):
            if path == self.file:
                raise PermissionError("synthetic denied path")
            return real_lstat(path)

        with mock.patch.object(curation, "_GET_FILE_ATTRIBUTES_W", attributes), \
                mock.patch.object(Path, "lstat", failing_lstat):
            with self.assertRaisesRegex(PermissionError, "synthetic denied path"):
                curation._absolute(self.file)

    def test_unavailable_api_uses_full_lstat_fallback(self):
        real_lstat = Path.lstat
        checked = []

        def counted_lstat(path):
            checked.append(path)
            return real_lstat(path)

        with mock.patch.object(curation, "_GET_FILE_ATTRIBUTES_W", None), \
                mock.patch.object(Path, "lstat", counted_lstat):
            result = curation._absolute(self.file)

        self.assertEqual(result, self.file)
        self.assertIn(self.file, checked)
        self.assertGreater(len(checked), 1)

    def test_long_path_uses_extended_native_path_without_lstat(self):
        parent = self.root
        for index in range(12):
            parent /= f"deep-segment-{index:02d}"
        parent.mkdir(parents=True, exist_ok=True)
        target = parent / "synthetic-long-path.txt"
        target.write_bytes(b"long-path")
        self.assertGreater(len(os.fspath(target)), 260)
        self.assertTrue(curation._windows_attribute_path(target).startswith("\\\\?\\"))

        real_attributes = curation._GET_FILE_ATTRIBUTES_W
        calls = []

        def counted_attributes(value):
            calls.append(value)
            return real_attributes(value)

        with mock.patch.object(curation, "_GET_FILE_ATTRIBUTES_W", counted_attributes), \
                mock.patch.object(Path, "lstat", side_effect=AssertionError("long path should use native attributes")):
            result = curation._absolute(target)

        self.assertEqual(result, target)
        self.assertGreater(len(calls), 1)

    def test_unc_paths_use_extended_namespace(self):
        value = Path(r"\\server\share\synthetic\item.txt")
        self.assertEqual(
            curation._windows_attribute_path(value),
            r"\\?\UNC\server\share\synthetic\item.txt",
        )
        self.assertIsNone(
            curation._windows_attribute_path(Path(r"\\?\GLOBALROOT\Device\synthetic"))
        )


if __name__ == "__main__":
    unittest.main()
