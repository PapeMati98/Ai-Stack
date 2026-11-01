import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fleet
import supplemental_evidence as supplemental


class SupplementalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.source = self.workspace / 'proof.md'
        self.source.write_bytes(b'Observed result: all tests pass.\n')
        self.manifest = self.workspace / 'evidence.json'
        self.profiles = {'codex': {'cli_provider_id': 'openai'}}
        self.value = dict(schema_version=1, files=[dict(path='proof.md', sha256=hashlib.sha256(self.source.read_bytes()).hexdigest())], maker_run_ids=[], additional_maker_provider_ids=['openai'])

    def capture(self):
        self.manifest.write_text(json.dumps(self.value), encoding='utf-8')
        return supplemental.capture(fleet, self.root, self.profiles, self.workspace, self.manifest)

    def test_exact_capture(self):
        item = self.capture()
        self.assertEqual(item.metadata['files'][0]['sha256'], self.value['files'][0]['sha256'])
        self.assertEqual(item.payloads['supplemental/files/proof.md'], self.source.read_bytes())
        self.assertEqual(item.identities[0]['model_provider_id'], 'openai')

    def test_reject_hash_mismatch(self):
        self.value['files'][0]['sha256'] = 'a' * 64
        with self.assertRaisesRegex(fleet.FleetError, 'hash'):
            self.capture()

    def test_reject_path_escapes_and_windows_ambiguity(self):
        for path in ['../proof.md','C:/proof.md','//server/file','proof.md:stream','CON','dir./proof.md','proof.md ', 'dir\n/file']:
            with self.subTest(path=path):
                self.value['files'][0]['path'] = path
                with self.assertRaises(fleet.FleetError):
                    self.capture()

    def test_duplicate_case_path(self):
        self.value['files'].append(dict(path='PROOF.md', sha256=self.value['files'][0]['sha256']))
        with self.assertRaisesRegex(fleet.FleetError, 'Duplicate'):
            self.capture()

    def test_secret_and_invalid_utf8(self):
        for content in [b'\xff', b'a\x00b', ('value = "' + 'sk-' + 'A'*48 + '"').encode()]:
            with self.subTest(content_kind=content[:1]):
                self.source.write_bytes(content)
                self.value['files'][0]['sha256'] = hashlib.sha256(content).hexdigest()
                with self.assertRaises(fleet.FleetError):
                    self.capture()

    def test_maker_ledger_resolution_is_additive(self):
        self.value['maker_run_ids'] = ['run-20260907T000000Z-aaaaaaaaaa']
        record = dict(run_id=self.value['maker_run_ids'][0], evaluation={'status':'accepted'})
        identity = dict(agent_id='qwen', cli_provider_id='alibaba', model_provider_id='deepseek')
        with (mock.patch.object(fleet, 'load_run_record', return_value=(record, self.root/'record.json')),
              mock.patch.object(fleet, 'execution_provider_identity', return_value=identity)):
            item = self.capture()
        self.assertEqual(len(item.identities), 2)
        self.assertEqual(item.identities[0], identity)
        self.assertEqual(item.metadata['maker_identities'][0]['run_id'], record['run_id'])

    def test_unknown_provider(self):
        self.value['additional_maker_provider_ids'] = ['invented-lab']
        with self.assertRaisesRegex(fleet.FleetError, 'Unknown'):
            self.capture()

    def test_manifest_must_be_inside_workspace(self):
        outside = self.root / 'outside.json'
        outside.write_text(json.dumps(self.value), encoding='utf-8')
        with self.assertRaises(fleet.FleetError):
            supplemental.capture(fleet, self.root, self.profiles, self.workspace, outside)

    def test_manifest_argument_is_workspace_relative(self):
        self.capture()
        item = supplemental.capture(fleet, self.root, self.profiles, self.workspace, Path('evidence.json'))
        self.assertEqual(item.metadata['manifest_path'], 'evidence.json')

    def test_root_parent_and_file_links_are_refused(self):
        self.manifest.write_text(json.dumps(self.value), encoding='utf-8')
        for linked in [self.workspace, self.workspace.parent, self.source, self.manifest]:
            with self.subTest(linked=linked.name):
                with mock.patch.object(fleet, 'is_filesystem_link', side_effect=lambda p: p == linked):
                    with self.assertRaisesRegex(fleet.FleetError, 'Linked'):
                        supplemental.capture(fleet, self.root, self.profiles, self.workspace, self.manifest)

    def test_file_size_and_combined_size_limits(self):
        with mock.patch.object(supplemental, 'FILE_LIMIT', 4):
            with self.assertRaisesRegex(fleet.FleetError, 'bounded'):
                self.capture()
        item = self.capture()
        with self.assertRaisesRegex(fleet.FleetError, '32 MiB'):
            item.write(fleet, None, supplemental.GROUP_LIMIT)
        self.assertFalse((self.root / 'supplemental').exists())

    def test_manifest_size_limit(self):
        with mock.patch.object(supplemental, 'MANIFEST_LIMIT', 8):
            with self.assertRaisesRegex(fleet.FleetError, 'bounded'):
                self.capture()

    def test_file_count_and_metadata_shape(self):
        for files in [[], self.value['files'] * 65]:
            self.value['files'] = files
            with self.assertRaisesRegex(fleet.FleetError, '1..64'):
                self.capture()
        self.value['files'] = [dict(path='proof.md', sha256='a'*64)]
        self.value['unexpected'] = True
        with self.assertRaisesRegex(fleet.FleetError, 'schema'):
            self.capture()

    def test_duplicate_json_key_is_refused(self):
        self.manifest.write_text('{"schema_version":1,"schema_version":1}', encoding='utf-8')
        with self.assertRaisesRegex(fleet.FleetError, 'Duplicate'):
            supplemental.capture(fleet, self.root, self.profiles, self.workspace, self.manifest)

    def test_retry_context_is_immutable(self):
        item = self.capture()
        previous = dict(supplemental_evidence=item.metadata)
        supplemental.enforce_retry(fleet, previous, item)
        for candidate in [None, supplemental.PreparedEvidence({}, {}, [])]:
            with self.assertRaisesRegex(fleet.FleetError, 'fresh group'):
                supplemental.enforce_retry(fleet, previous, candidate)
        with self.assertRaisesRegex(fleet.FleetError, 'fresh group'):
            supplemental.enforce_retry(fleet, {'gate_id':'legacy'}, item)

    def test_c1_and_unicode_record_separators_in_paths_are_refused(self):
        for char in ['\x85', '\u2028', '\u2029']:
            self.value['files'][0]['path'] = 'proof' + char + '.md'
            with self.assertRaisesRegex(fleet.FleetError, 'Control'):
                self.capture()


if __name__ == '__main__':
    unittest.main()
