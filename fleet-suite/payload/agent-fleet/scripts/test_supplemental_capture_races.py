import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import fleet
import supplemental_evidence as supplemental

SOURCE_BYTES = b'Observed result: all tests pass.\n'


def handle_snapshot(path: Path, **changed: int) -> SimpleNamespace:
    actual = path.stat()
    return SimpleNamespace(**{'st_mode': actual.st_mode, 'st_nlink': actual.st_nlink,
                              'st_size': actual.st_size, 'st_mtime_ns': actual.st_mtime_ns,
                              'st_ino': actual.st_ino, **changed})


class SupplementalCaptureRaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.source = self.workspace / 'proof.md'
        self.source.write_bytes(SOURCE_BYTES)
        self.profiles = {'codex': {'cli_provider_id': 'openai'}}
        self.value = dict(schema_version=1, files=[dict(path='proof.md', sha256=hashlib.sha256(SOURCE_BYTES).hexdigest())], maker_run_ids=[], additional_maker_provider_ids=['openai'])

    def test_read_rejects_drift_between_the_two_handle_fstats(self):
        stable = handle_snapshot(self.source)
        with mock.patch.object(os, 'fstat', return_value=stable) as reads:
            self.assertEqual(supplemental._read(fleet, self.source, supplemental.FILE_LIMIT), SOURCE_BYTES)
        self.assertEqual(reads.call_count, 2)
        for field in ['st_size', 'st_mtime_ns', 'st_ino']:
            with self.subTest(field=field):
                drifted = SimpleNamespace(**{**vars(stable), field: getattr(stable, field) + 7})
                with mock.patch.object(os, 'fstat', side_effect=[stable, drifted]) as reads:
                    with self.assertRaisesRegex(ValueError, 'changed or exceeded'):
                        supplemental._read(fleet, self.source, supplemental.FILE_LIMIT)
                self.assertEqual(reads.call_count, 2)
                self.assertTrue(stat.S_ISREG(stable.st_mode))

    def test_read_rejects_path_replaced_after_the_handle_closes(self):
        stable = handle_snapshot(self.source)
        replaced = handle_snapshot(self.source, st_ino=stable.st_ino + 7)
        io_path = fleet.windows_extended_io_path(self.source)
        real_stat = Path.stat

        def stat_after_replacement(candidate, *args, **kwargs):
            if candidate == io_path:
                return replaced
            return real_stat(candidate, *args, **kwargs)

        with (mock.patch.object(os, 'fstat', return_value=stable) as reads,
              mock.patch.object(fleet, 'is_filesystem_link', wraps=fleet.is_filesystem_link) as link_checks,
              mock.patch.object(Path, 'stat', stat_after_replacement)):
            with self.assertRaisesRegex(ValueError, 'changed during capture'):
                supplemental._read(fleet, self.source, supplemental.FILE_LIMIT)
        self.assertEqual(reads.call_count, 2)
        self.assertGreaterEqual(link_checks.call_count, 2 * (len(self.source.parents) + 1))
        self.assertEqual((replaced.st_size, replaced.st_mtime_ns), (stable.st_size, stable.st_mtime_ns))
        self.assertNotEqual(replaced.st_ino, stable.st_ino)

    def test_public_capture_refuses_boolean_schema_version(self):
        boolean = self.workspace / 'boolean-schema.json'
        boolean.write_text(json.dumps({**self.value, 'schema_version': True}), encoding='utf-8')
        self.assertIs(json.loads(boolean.read_text(encoding='utf-8'))['schema_version'], True)
        with self.assertRaisesRegex(fleet.FleetError, 'schema'):
            supplemental.capture(fleet, self.root, self.profiles, self.workspace, boolean)
        boolean.write_text(json.dumps({**self.value, 'schema_version': 1}), encoding='utf-8')
        item = supplemental.capture(fleet, self.root, self.profiles, self.workspace, boolean)
        self.assertEqual(item.metadata['schema_version'], 1)


if __name__ == '__main__':
    unittest.main()
