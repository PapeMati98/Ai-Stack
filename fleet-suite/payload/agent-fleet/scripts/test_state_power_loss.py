"""Synthetic power-loss state; no provider clients or owner state are used."""
import datetime as dt
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock
import uuid

import adaptive
import fleet
import incidents
import process_supervision
import quota_availability as qa
import qwen_local_runtime
import routing_v2


BAD_BYTES = (
    b'\0' * 285, b'{"schema_version": 1,', b'\xff\xfe', b'[]',
    b'{"schema_version": 1, "status": []}',
    b'{"schema_version": 2, "status": "available"}',
    b'{"schema_version": 1, "status": "exhausted", "recheck_adapter": []}',
    b'{"schema_version": 1, "status": "available", "source": []}',
    b'{"schema_version": 1, "status": "available", "observed_at": {}}',
    b'{"schema_version": 1, "status": "available", "last_block": []}',
)


class StatePowerLossTests(unittest.TestCase):
    def setUp(self):
        # Normal mkdir preserves inherited Windows sandbox ACLs. Retain owned
        # fixtures outside the release rather than relying on cleanup hooks.
        parent = Path(os.environ.get('TEAM_TEST_ROOT', tempfile.gettempdir()))
        self.root = parent / ('state-power-loss-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.now = dt.datetime.now(dt.timezone.utc)
        self.clock = self.now.timestamp()
        self.profiles = {
            'broken': {'adapter': 'antigravity', 'quota_pool_id': 'broken-pool'},
            'sibling': {'adapter': 'antigravity', 'quota_pool_id': 'broken-pool'},
            'healthy': {'adapter': 'codex', 'quota_pool_id': 'healthy-pool'},
        }
        self.healthy = {'schema_version': 1, 'status': 'available', 'source': 'native-run',
                        'observed_at': qa.stamp(self.clock - 5),
                        'recheck_at': qa.stamp(self.clock + 60)}
        adaptive.atomic_write(qa.cache_path(self.root, 'healthy-pool'), self.healthy)

    def corrupt(self, data, pool='broken-pool'):
        path = qa.cache_path(self.root, pool)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def collect(self):
        with mock.patch.object(fleet.quota_collectors, 'collect_profile_quotas', return_value=({}, {})):
            return fleet.current_quota_views(self.root, self.profiles, now=self.now)

    def check(self, **kwargs):
        return qa.check(self.root, 'broken-pool', {}, now=self.clock,
                        lock_factory=fleet.ReleasableKernelLock, **kwargs)

    def assert_unknown(self, state):
        self.assertEqual(state['status'], 'unknown')
        self.assertEqual(state['reason'], 'corrupt-cache-ignored')

    def test_corrupt_cache_is_unknown_without_modifying_the_file(self):
        for data in BAD_BYTES:
            with self.subTest(data=data):
                path = self.corrupt(data)
                self.assert_unknown(qa.read_cache(path))
                self.assert_unknown(self.check())
                self.assertEqual(path.read_bytes(), data)

    def test_oversized_cache_is_ignored_without_reading_it(self):
        path = self.corrupt(b' ' * 16385)
        with mock.patch.object(Path, 'open', side_effect=AssertionError('oversized cache was opened')):
            self.assert_unknown(qa.read_cache(path))

    def test_linked_cache_is_ignored_without_reading_its_target(self):
        path = qa.cache_path(self.root, 'broken-pool')
        # Model lstat's reparse metadata portably, without requiring symlink
        # privileges on Windows. Never open the synthetic linked target.
        metadata = mock.Mock(st_mode=stat.S_IFREG, st_size=20,
                             st_file_attributes=getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024))
        with mock.patch.object(Path, 'lstat', return_value=metadata), \
                mock.patch.object(Path, 'is_symlink', return_value=True), \
                mock.patch.object(Path, 'open', side_effect=AssertionError('linked cache was opened')):
            self.assert_unknown(qa.read_cache(path))

    def test_reader_bounds_a_file_that_grows_after_stat(self):
        path = self.corrupt(b' ' * 16385)
        metadata = mock.Mock(st_mode=stat.S_IFREG, st_size=1, st_file_attributes=0)
        with mock.patch.object(Path, 'lstat', return_value=metadata):
            self.assert_unknown(qa.read_cache(path))

    def test_read_error_is_unknown_without_disclosing_file_content(self):
        path = self.corrupt(b'\0' * 285)
        with mock.patch.object(Path, 'open', side_effect=OSError('private content must stay private')):
            result = qa.read_cache(path)
        self.assert_unknown(result)
        self.assertNotIn('private content', json.dumps(result))

    def test_views_preserve_other_pools_and_routing_eligibility(self):
        for data in BAD_BYTES:
            with self.subTest(data=data):
                self.corrupt(data)
                views, diagnostics = self.collect()
                self.assertEqual(set(views), set(self.profiles))
                for agent in ('broken', 'sibling'):
                    self.assertEqual(views[agent]['status'], 'unknown')
                    self.assert_unknown(views[agent]['availability'])
                    self.assertFalse(views[agent]['availability_blocked'])
                    self.assertTrue(all(g['passed'] for g in routing_v2.quota_hard_gates({}, views[agent])))
                self.assert_unknown(diagnostics['broken-pool']['availability'])
                self.assertEqual(views['healthy']['availability'], self.healthy)

    def test_quota_status_survives_null_cache_for_all_pools(self):
        self.corrupt(b'\0' * 285)
        stdout, stderr = mock.Mock(buffer=io.BytesIO()), mock.Mock(buffer=io.BytesIO())
        with mock.patch.object(fleet, 'load_profiles', return_value=self.profiles), \
                mock.patch.object(fleet.quota_collectors, 'collect_profile_quotas', return_value=({}, {})), \
                mock.patch.object(fleet.sys, 'stdout', stdout), mock.patch.object(fleet.sys, 'stderr', stderr):
            code = fleet.main(['--state-dir', str(self.root), 'quota-status'])
        self.assertEqual(code, fleet.EXIT_SUCCESS)
        result = json.loads(stdout.buffer.getvalue())
        self.assert_unknown(result['quota_collection']['broken-pool']['availability'])
        self.assertIn('healthy-pool', result['quota_collection'])

    def test_probe_replaces_corrupt_cache_with_fresh_proof(self):
        for data in BAD_BYTES:
            with self.subTest(data=data):
                self.corrupt(data)
                probe = mock.Mock(return_value={'status': 'available', 'source': 'native-cli-check'})
                result = self.check(probe=probe)
                probe.assert_called_once_with()
                self.assertEqual(result['status'], 'available')
                self.assertEqual(result['cache_read_reason'], 'corrupt-cache-ignored')
                self.assertEqual(qa.read_cache(qa.cache_path(self.root, 'broken-pool')), result)

    def test_failed_probe_keeps_corruption_diagnostic_and_unknown(self):
        self.corrupt(b'\0' * 285)
        result = self.check(probe=mock.Mock(side_effect=OSError('do not expose this content')))
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['reason'], 'check-unavailable')
        self.assertEqual(result['cache_read_reason'], 'corrupt-cache-ignored')
        self.assertNotIn('do not expose', json.dumps(result))

    def test_costly_probe_does_not_spend_a_turn_on_corrupt_cache(self):
        self.corrupt(b'\0' * 285)
        probe = mock.Mock(side_effect=AssertionError('no cached refusal'))
        self.assert_unknown(self.check(probe=probe, costly_probe=True))
        probe.assert_not_called()

    def test_valid_exhaustion_still_blocks_when_cache_is_corrupt(self):
        self.corrupt(b'\0' * 285)
        prior = {'status': 'fresh', 'authority': 'authoritative', 'headroom': 0,
                 'observed_at': qa.stamp(self.clock - 1)}
        result = qa.check(self.root, 'broken-pool', prior, now=self.clock,
                          lock_factory=fleet.ReleasableKernelLock)
        self.assertEqual(result['status'], 'exhausted')
        self.assertTrue(qa.overlay(prior, result)['availability_blocked'])
        self.assertEqual(result['cache_read_reason'], 'corrupt-cache-ignored')

    def test_native_success_and_rejection_replace_bad_cache(self):
        for data in BAD_BYTES:
            for success in (True, False):
                with self.subTest(data=data, success=success):
                    self.corrupt(data)
                    if success:
                        result = qa.record_native_success(self.root, 'broken-pool', now=self.clock,
                                                          lock_factory=fleet.ReleasableKernelLock)
                    else:
                        result = qa.record_rejection(self.root, 'broken-pool',
                            {'status': 'exhausted', 'source': 'native-provider-error', 'reason': 'usage-limit'},
                            now=self.clock, lock_factory=fleet.ReleasableKernelLock)
                    self.assertEqual(result['status'], 'available' if success else 'exhausted')
                    self.assertEqual(result['cache_read_reason'], 'corrupt-cache-ignored')
                    self.assertEqual(qa.read_cache(qa.cache_path(self.root, 'broken-pool')), result)

    def success_row(self):
        return {'agent': 'broken', 'adapter': 'antigravity', 'model': 'synthetic-model',
                'run_status': 'COMPLETED', 'exit_code': 0, 'runtime_attestation': {'status': 'VERIFIED'},
                'model_identity': {'status': 'verified', 'expected_model': 'synthetic-model'},
                'started_at': qa.stamp(self.clock - 12), 'completed_at': qa.stamp(self.clock - 10),
                'token_usage': {'status': 'known', 'adapter': 'antigravity', 'total_tokens': 10}}

    def test_current_view_imports_success_from_real_synthetic_ledger(self):
        run = self.root / 'runs' / 'run-synthetic.json'
        adaptive.atomic_write(run, self.success_row())
        for data in BAD_BYTES:
            with self.subTest(data=data):
                self.corrupt(data)
                views, _ = self.collect()
                self.assertEqual(views['broken']['availability']['status'], 'available')
                self.assertEqual(views['broken']['availability']['source'], 'native-run')
                self.assertEqual(views['broken']['availability']['cache_read_reason'], 'corrupt-cache-ignored')
                self.assertEqual(views['healthy']['availability'], self.healthy)

    def test_import_write_failure_does_not_interrupt_other_pools(self):
        adaptive.atomic_write(self.root / 'runs' / 'good.json', self.success_row())
        path = self.corrupt(b'\0' * 285)
        with mock.patch.object(adaptive.os, 'fsync', side_effect=OSError('synthetic disk error')):
            views, diagnostics = self.collect()
        self.assert_unknown(views['broken']['availability'])
        self.assertEqual(views['healthy']['availability'], self.healthy)
        self.assertEqual(diagnostics['broken-pool']['availability_import']['reason'], 'state-write-unavailable')
        self.assertEqual(path.read_bytes(), b'\0' * 285)

    def test_directory_cache_is_ignored_even_during_native_import(self):
        adaptive.atomic_write(self.root / 'runs' / 'good.json', self.success_row())
        qa.cache_path(self.root, 'broken-pool').mkdir()
        views, diagnostics = self.collect()
        self.assert_unknown(views['broken']['availability'])
        self.assertEqual(views['healthy']['availability'], self.healthy)
        self.assertEqual(diagnostics['broken-pool']['availability_import']['reason'], 'state-write-unavailable')

    def test_pace_export_regenerates_corruption_with_explicit_reason(self):
        path = self.root / 'pace' / 'latest.json'
        path.parent.mkdir(parents=True)
        for data in (*BAD_BYTES[:4], b'{"schema_version": 1, "pools": []}'):
            with self.subTest(data=data):
                path.write_bytes(data)
                views, diagnostics = self.collect()
                value = json.loads(path.read_bytes())
                self.assertEqual(set(value['pools']), {'broken-pool', 'healthy-pool'})
                self.assertEqual(value['pools']['healthy-pool']['tempo'], views['healthy']['pace']['tempo'])
                self.assertTrue(diagnostics['pace_latest']['written'])
                self.assertEqual(diagnostics['pace_latest']['previous_state_reason'], 'corrupt-cache-ignored')

    def test_corrupt_quota_records_do_not_hide_valid_pool_data(self):
        good, _ = adaptive.record_quota(self.root, agent='healthy', quota_pool_id='healthy-pool',
            short_remaining_fraction=None, weekly_remaining_fraction=.8, credits_remaining=None,
            short_reset_at=None, weekly_reset_at=qa.stamp(self.clock + 86400), observed_at=qa.stamp(self.clock - 20),
            source='provider-dashboard', confidence=1.0, note='', valid_for_hours=24)
        invalid_schema = dict(good, agent=['broken'], valid_for_hours='bad')
        for data in (*BAD_BYTES[:4], json.dumps(invalid_schema).encode('ascii')):
            with self.subTest(data=data):
                (self.root / 'quota' / 'quota-corrupt.json').write_bytes(data)
                views, diagnostics = self.collect()
                self.assertEqual(views['healthy']['headroom'], .8)
                self.assertEqual(views['broken']['status'], 'unknown')
                self.assertEqual(diagnostics['state_files']['quota']['reason'], 'corrupt-cache-ignored')
                self.assertEqual(diagnostics['state_files']['quota']['ignored_count'], 1)

    def test_corrupt_run_records_do_not_break_native_import_or_pace(self):
        adaptive.atomic_write(self.root / 'runs' / 'good.json', self.success_row())
        invalid_schema = dict(self.success_row(), completed_at='2026-10-11T00:00:00', adapter=[])
        for data in (*BAD_BYTES[:4], json.dumps(invalid_schema).encode('ascii')):
            with self.subTest(data=data):
                (self.root / 'runs' / 'bad.json').write_bytes(data)
                views, diagnostics = self.collect()
                self.assertEqual(views['broken']['availability']['source'], 'native-run')
                self.assertEqual(diagnostics['state_files']['runs']['reason'], 'corrupt-cache-ignored')
                self.assertEqual(diagnostics['state_files']['runs']['ignored_count'], 1)

    def test_invalid_nested_run_metadata_does_not_break_other_pools(self):
        bad_attempt = dict(self.success_row(), run_status='FAILED', exit_code=1,
            token_usage={'status': 'known', 'total_tokens': 0},
            provider_attempts=[{'exit_code': 1, 'terminal_reason': [],
                                'transport_outcome': {'status': 'failed'},
                                'token_usage': {'status': 'known', 'total_tokens': 0}}])
        huge_tokens = dict(self.success_row(),
                           token_usage={'status': 'known', 'total_tokens': 10 ** 400})
        for row in (bad_attempt, huge_tokens):
            with self.subTest(row=row):
                adaptive.atomic_write(self.root / 'runs' / 'bad.json', row)
                views, diagnostics = self.collect()
                self.assertEqual(views['healthy']['availability'], self.healthy)
                self.assertEqual(views['broken']['status'], 'unknown')
                self.assertEqual(diagnostics['state_files']['runs']['reason'], 'corrupt-cache-ignored')
                self.assertEqual(diagnostics['state_files']['runs']['ignored_count'], 1)

    def test_invalid_cached_kimi_window_does_not_break_other_pools(self):
        self.profiles['broken']['adapter'] = 'kimi'
        for window in ({'scope': [], 'used': 1, 'limit': 2},
                       {'scope': 'weekly', 'used': 10 ** 400, 'limit': 2},
                       {'scope': 'weekly', 'used': 1, 'limit': 10 ** 400}):
            with self.subTest(window=window):
                adaptive.atomic_write(qa.cache_path(self.root, 'broken-pool'),
                    dict(self.healthy, source='native-cli-check', windows=[window]))
                with (mock.patch.object(fleet.kimi_usage, 'query', side_effect=OSError('synthetic probe')),
                      mock.patch.object(fleet.kimi_usage, 'native_fallback', side_effect=OSError('synthetic probe'))):
                    views, _ = self.collect()
                self.assertEqual(views['broken']['availability']['status'], 'unknown')
                self.assertEqual(views['broken']['availability']['cache_read_reason'], 'corrupt-cache-ignored')
                self.assertEqual(views['healthy']['availability'], self.healthy)

    def test_valid_cache_and_missing_cache_keep_existing_semantics(self):
        self.assertEqual(qa.read_cache(qa.cache_path(self.root, 'healthy-pool')), self.healthy)
        self.assertEqual(qa.read_cache(qa.cache_path(self.root, 'broken-pool')), {})
        self.assertEqual(self.check(), {'status': 'unknown', 'source': 'no-check-adapter'})
        _, diagnostics = self.collect()
        self.assertNotIn('state_files', diagnostics)
        self.assertNotIn('previous_state_reason', diagnostics['pace_latest'])

    def observe_write(self, writer, expected, path=None):
        path = path or self.root / ('write-' + uuid.uuid4().hex + '.json')
        events = []
        actual_replace = os.replace
        actual_open = os.open
        opened_files = {}
        synced_files = []
        def open_file(file, flags, mode=0o777, **kwargs):
            descriptor = actual_open(file, flags, mode, **kwargs)
            opened_files[descriptor] = Path(file)
            return descriptor
        def sync(descriptor):
            temporary = opened_files[descriptor]
            self.assertTrue(stat.S_ISREG(os.fstat(descriptor).st_mode))
            self.assertEqual(os.fstat(descriptor).st_ino, temporary.stat().st_ino)
            # Read by path: a valid fsync descriptor may be write-only. The
            # payload must already be visible before fsync/replace, proving
            # the buffered writer flushed it rather than merely calling fsync.
            self.assertEqual(json.loads(temporary.read_bytes()), expected)
            synced_files.append(temporary)
            events.append('fsync')
        def replace(temporary, target):
            self.assertEqual(events, ['fsync'])
            self.assertEqual(synced_files, [Path(temporary)])
            self.assertEqual(Path(target), path)
            self.assertEqual(Path(temporary).parent, path.parent)
            events.append('replace')
            return actual_replace(temporary, target)
        with mock.patch.object(adaptive.os, 'open', side_effect=open_file), \
                mock.patch.object(adaptive.os, 'fsync', side_effect=sync), \
                mock.patch.object(adaptive.os, 'replace', side_effect=replace):
            writer(path)
        self.assertEqual(events, ['fsync', 'replace'])
        self.assertEqual(json.loads(path.read_bytes()), expected)

    def test_json_atomic_write_fsyncs_flushed_payload_before_replace(self):
        value = {'payload': 'power-loss'}
        self.observe_write(lambda path: adaptive.atomic_write(path, value), value)

    def test_shared_byte_writer_fsyncs_before_replace(self):
        value = {'payload': 'power-loss'}
        self.observe_write(lambda path: adaptive.atomic_write_bytes(path, adaptive.canonical_json(value)), value)

    def test_incident_checkpoint_and_qwen_writers_use_shared_helper(self):
        value = {'payload': 'power-loss'}
        for writer in (incidents._atomic_write_json, process_supervision._write_checkpoint_atomically,
                       qwen_local_runtime._atomic_json):
            with self.subTest(writer=writer.__module__):
                with mock.patch.object(adaptive, 'atomic_write_bytes', wraps=adaptive.atomic_write_bytes) as shared:
                    self.observe_write(lambda path: writer(path, value), value)
                    shared.assert_called_once()

    def test_fleet_transaction_uses_shared_helper_and_durable_order(self):
        value = {'transaction_id': 'qaccept-' + 'a' * 20}
        # Exercise a real Fleet persistence entry point with its actual target.
        expected_dir = self.root / 'quality-gates' / 'acceptance-transactions'
        expected_dir.mkdir(parents=True)
        value_path = expected_dir / (value['transaction_id'] + '.json')
        with mock.patch.object(adaptive, 'atomic_write_bytes', wraps=adaptive.atomic_write_bytes) as shared:
            self.observe_write(lambda path: self.assertEqual(
                fleet.persist_quality_acceptance_transaction(self.root, value), path), value, value_path)
        shared.assert_called_once()

    def test_failed_fsync_does_not_replace_existing_state(self):
        path = self.root / 'stable.json'
        path.write_bytes(b'{"previous": true}')
        with mock.patch.object(adaptive.os, 'fsync', side_effect=OSError('synthetic disk failure')), \
                mock.patch.object(adaptive.os, 'replace') as replaced:
            with self.assertRaises(OSError):
                adaptive.atomic_write(path, {'new': True})
        replaced.assert_not_called()
        self.assertEqual(path.read_bytes(), b'{"previous": true}')


if __name__ == '__main__':
    unittest.main()
