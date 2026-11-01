"""Synthetic retained-candidate recovery and native Windows identity guards."""
import ctypes
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_todo_graph_recovery as fixtures

graph = fixtures.todo_graph
RECORDED = 1792230822.87037
CREATED = 1792232394.8051815


def sample(created=CREATED, alive=True):
    return {'alive': alive, 'created_at': created,
            'creation_filetime': round((created + 11_644_473_600) * 10_000_000),
            'method': 'win32-getprocesstimes'}


class PidReuseRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.RecoveryTests('runTest')
        self.fx.fixtures_root = Path(os.environ.get('TEAM_TEST_ROOT', tempfile.gettempdir())) / 'pid-reuse'
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)

    def evidence(self, *, started=RECORDED, created=CREATED, alive=True, reuse=True):
        self.fx.make_process_evidence(started=started)
        # Patch points also exist in the old implementation: regression checks
        # exercise its real aggregation/gate, not just a missing new helper.
        with patch.object(graph, '_windows_process_probe', return_value=sample(created, alive), create=True), \
             patch.object(graph, '_process_alive', return_value=alive), \
             patch.object(graph, '_pid_reused', return_value=reuse):
            return graph._node_process_evidence(self.fx.run_dir / 'task-a')

    @unittest.skipUnless(os.name == 'nt', 'Windows identity fixture')
    def test_newer_live_pid_does_not_keep_original_check_alive(self):
        evidence = self.evidence()
        self.assertFalse(evidence['alive'])
        self.assertTrue(evidence['pid_reused'])
        self.assertTrue(evidence['all_processes'][0]['pid_alive'])
        self.assertEqual(graph._process_evidence_ok(evidence), (True, ''))

    @unittest.skipUnless(os.name == 'nt', 'Windows identity fixture')
    def test_retained_candidate_checks_and_review_run_without_maker_replay(self):
        plan = self.fx.full_fixture()
        self.fx.make_process_evidence(started=RECORDED)
        adapter = fixtures.FakeAdapter()
        with patch.object(graph, '_windows_process_probe', return_value=sample(), create=True), \
             patch.object(graph, '_process_alive', return_value=True), \
             patch.object(graph, '_pid_reused', return_value=True):
            inspected = graph.inspect_run(self.fx.run_dir, plan)
            result = graph.resolve_interrupted(self.fx.run_dir, plan, inspected, adapter=adapter)
        self.assertEqual(result['status'], 'ACCEPTED_CANDIDATE')
        self.assertEqual([call[1] for call in adapter.calls], ['reviewer'])
        final = graph.read(self.fx.run_dir / 'state.json')
        self.assertEqual(final['nodes']['task-a']['status'], 'ACCEPTED')

    @unittest.skipUnless(os.name == 'nt', 'Windows identity fixture')
    def test_duplicate_pid_records_use_one_native_sample_per_scan(self):
        self.fx.make_process_evidence(started=RECORDED)
        self.fx.make_native_checkpoint(started_at=RECORDED)
        with patch.object(graph, '_windows_process_probe', return_value=sample(), create=True) as probe:
            first = graph._node_process_evidence(self.fx.run_dir / 'task-a')
            self.assertEqual(probe.call_count, 1)
            self.assertEqual(len(first['all_processes']), 2)
            graph._node_process_evidence(self.fx.run_dir / 'task-a')
            self.assertEqual(probe.call_count, 2)  # no cache survives a scan

    def test_reuse_fact_not_lost_when_raw_pid_is_live(self):
        self.assertTrue(self.evidence()['pid_reused'])

    def test_earlier_creation_is_not_reuse_proof(self):
        self.assertFalse(graph._process_evidence_ok(self.evidence(created=RECORDED - 3600))[0])

    def test_subsecond_creation_difference_keeps_live_instance_blocked(self):
        self.assertFalse(graph._process_evidence_ok(self.evidence(created=RECORDED + .05))[0])

    def test_same_live_instance_still_blocks(self):
        self.assertFalse(graph._process_evidence_ok(self.evidence(reuse=False))[0])

    def test_unknown_process_still_blocks(self):
        self.assertFalse(graph._process_evidence_ok(self.evidence(alive=None, reuse=None))[0])

    def test_bad_start_never_proves_a_newer_instance(self):
        for start in (None, True, False, float('nan'), float('inf'), 'NaN', 'bad', {}, -1):
            with self.subTest(start=repr(start)):
                self.assertFalse(graph._process_evidence_ok(self.evidence(started=start))[0])

    def test_mixed_reused_check_and_live_original_child_blocks(self):
        self.fx.make_process_evidence(started=RECORDED)
        folder = self.fx.run_dir / 'task-a/1/reviewer'
        graph.write(folder / 'process.json', {'pid': 88888, 'started': RECORDED})
        with patch.object(graph, '_windows_process_probe', side_effect=lambda pid: sample(
                CREATED if pid == 99999 else RECORDED - .02), create=True):
            evidence = graph._node_process_evidence(self.fx.run_dir / 'task-a')
        # On POSIX synthetic PIDs may be absent; the native mixed test is Windows.
        if os.name == 'nt':
            self.assertTrue(evidence['alive'])
            self.assertTrue(evidence['pid_reused'])
            self.assertFalse(graph._process_evidence_ok(evidence)[0])

    def test_top_level_reused_claim_without_details_still_blocks(self):
        self.assertFalse(graph._process_evidence_ok(
            {'pid': 99999, 'alive': False, 'pid_reused': True})[0])

    def test_invalid_pids_are_not_sampled_or_truncated(self):
        for pid in (True, False, 0, -1, 1.2, '12', 2 ** 32 + 12):
            with self.subTest(pid=pid):
                self.assertIsNone(graph._process_alive(pid))
                self.assertIsNone(graph._pid_reused(pid, RECORDED))

    def test_directional_epoch_comparison(self):
        self.assertTrue(graph._pid_reused(99999, RECORDED, alive=True, current_start=CREATED))
        for created in (RECORDED - 1000, RECORDED - .001, RECORDED, RECORDED + .5):
            self.assertFalse(graph._pid_reused(99999, RECORDED, alive=True, current_start=created))

    def test_iso_checkpoint_timestamp_is_supported(self):
        iso = datetime.fromtimestamp(RECORDED, timezone.utc).isoformat()
        self.assertTrue(graph._pid_reused(99999, iso, alive=True, current_start=CREATED))
        self.assertIsNone(graph._pid_reused(99999, '2026-10-17T10:00:00', alive=True, current_start=CREATED))

    def test_missing_or_nonfinite_creation_stays_unknown(self):
        for created in (None, 'NaN', float('inf'), True):
            self.assertIsNone(graph._pid_reused(99999, RECORDED, alive=True, current_start=created))

    @unittest.skipUnless(os.name == 'nt', 'Actual read-only Windows probe')
    def test_real_self_process_probe_never_spawns_a_query_process(self):
        with patch.object(graph.subprocess, 'run', side_effect=AssertionError('external query spawned')):
            self.assertIs(graph._process_alive(os.getpid()), True)
            created = graph._process_start_time(os.getpid())
            self.assertIsInstance(created, float)
            self.assertFalse(graph._pid_reused(os.getpid(), created))

    @unittest.skipUnless(os.name == 'nt', 'Windows native failure handling')
    def test_native_api_unavailable_is_unknown(self):
        with patch.object(ctypes, 'WinDLL', side_effect=OSError('unavailable')):
            self.assertIsNone(graph._process_alive(99999))

    @unittest.skipUnless(os.name == 'nt', 'Windows native failure handling')
    def test_access_denied_is_unknown_absent_pid_is_dead(self):
        class Function:
            def __call__(self, *args):
                return 0
        class Kernel:
            OpenProcess = Function()
            GetProcessTimes = Function()
            GetExitCodeProcess = Function()
            CloseHandle = Function()
        for error, expected in ((5, None), (87, False), (6, None)):
            with self.subTest(error=error), patch.object(ctypes, 'WinDLL', return_value=Kernel()), \
                 patch.object(ctypes, 'get_last_error', return_value=error):
                self.assertIs(graph._process_alive(99999), expected)


if __name__ == '__main__':
    unittest.main()
