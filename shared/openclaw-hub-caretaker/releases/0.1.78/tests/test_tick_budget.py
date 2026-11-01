import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import maintenance
from caretaker import audit, tick_budget as tb
from caretaker import pubstate
from caretaker.pipeline import _invoke_with_fallback
from tests import test_fleet_child_timeout as fleet_tests
from tests import test_throughput_resume as resume_tests


class BudgetTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / '.test-tmp'
        base.mkdir(exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_frozen_clock_explicit_expiry_and_validated_limit(self):
        for invalid in (0, -1, True, float('nan'), float('inf'), 7201, '1200'):
            with self.assertRaises(ValueError):
                tb.TickBudget(invalid)
        clock = mock.Mock(return_value=123)
        budget = tb.TickBudget(10, clock=clock)
        clock.return_value = 133
        with self.assertRaises(tb.TickBudgetExceeded):
            budget.check()
        clock.return_value = 132
        with self.assertRaises(tb.TickBudgetExceeded):
            budget.timeout(900, minimum=1, margin=5)

    def test_native_timeout_clamps_literal_without_increase(self):
        helper = fleet_tests.FleetChildTimeoutTests()
        with tb.TickBudget(100, clock=lambda: 0).activate():
            seen, _ = helper._invoke(timeout=900, argv=['--agent', 'grok', '--model', 'grok-4.6', '--timeout', '870'])
        self.assertLessEqual(seen['timeout'], 95)
        self.assertLessEqual(int(seen['argv'][seen['argv'].index('--timeout') + 1]), 85)
        with tb.TickBudget(20, clock=lambda: 0).activate(), mock.patch('caretaker.transports.resolve_fleet_py') as resolve:
            with self.assertRaises(tb.TickBudgetExceeded):
                helper._invoke(timeout=900)
            resolve.assert_not_called()

    def test_expiry_between_primary_and_fallback_never_calls_fallback(self):
        from types import SimpleNamespace as S
        clock = mock.Mock(return_value=0)
        budget = tb.TickBudget(10, clock=clock)
        from caretaker.transports import TransportError
        def primary(**kwargs):
            clock.return_value = 10
            raise TransportError('synthetic timeout', reason_code='process_timeout')
        deps = S(primary=S(invoke=primary), fallback=mock.Mock())
        cfg = S(state_dir='synthetic', primary=object(), fallback=object())
        with budget.activate(), mock.patch('caretaker.pipeline._write_run_config', return_value='synthetic'):
            with self.assertRaises(tb.TickBudgetExceeded):
                _invoke_with_fallback(deps, cfg, 'synthetic', state_dir='synthetic', run_id='synthetic')
        deps.fallback.invoke.assert_not_called()

    def test_both_lanes_report_completed_batch_and_stop_before_next(self):
        for lane in ('legacy', 'new'):
            with self.subTest(lane=lane):
                state = self.root / lane
                clock = mock.Mock(return_value=0)
                budget = tb.TickBudget(10, state=state, clock=clock)
                db = sqlite3.connect(':memory:')
                self.addCleanup(db.close)
                policy = {'max_projects_per_tick': 2, 'publication': {'enabled': True, 'max_batches_per_tick': 3}}
                def publish(*args, **kwargs):
                    clock.return_value = 10
                    return {'project': 'p', 'status': 'PUBLISHED', 'batch': 'b', 'model_calls': 0,
                            'proposal_ids': ['prop-a'], 'checkpoint': {'status': 'CHECKPOINTED', 'commit': 'synthetic'}}
                with budget.activate(), mock.patch.object(maintenance, '_audit_summary', return_value={}), \
                     mock.patch.object(maintenance, '_audit_fields', return_value={}), \
                     mock.patch.object(maintenance, 'complete_pending_checkpoints', return_value=[]), \
                     mock.patch.object(audit, 'maybe_start_cycle', return_value={'id':'cycle'}), \
                     mock.patch.object(audit, 'cycle_projects', return_value=['p']), \
                     mock.patch.object(maintenance, '_project_runtime_cfg', return_value=('config', {'project_slug':'p'})), \
                     mock.patch.object(maintenance, 'publication_tick', side_effect=publish) as call, \
                     mock.patch('caretaker.pubstate.reconcile_all_running'), \
                     mock.patch.object(maintenance, '_pending_checkpoint_rows', return_value=[]), \
                     mock.patch.object(maintenance, '_legacy_running_count', return_value=0), \
                     mock.patch.object(maintenance, '_new_epoch_proposal_filter', return_value=lambda _: True):
                    with self.assertRaises(tb.TickBudgetExceeded):
                        maintenance.publication_drain(policy, {'hub':{'vault':'synthetic'}}, db, state, lane=lane, recovery={'status':'NO_LOCK'})
                call.assert_called_once()
                report = json.loads((state/'latest.json').read_text())
                self.assertTrue(report['running'])
                self.assertEqual(report['status'], 'RUNNING')
                self.assertEqual(report['completed_batches'][0]['lane'], lane)
                self.assertEqual(report['completed_batches'][0]['checkpoint']['status'], 'CHECKPOINTED')
                with mock.patch.object(tb, 'utcnow', return_value='real-final-time'):
                    final = budget.finish(budget.report, 'DEFERRED_BUDGET')
                self.assertEqual(final['completed_at'], 'real-final-time')
                self.assertEqual(final['observed_at'], final['completed_at'])
                self.assertFalse(final['running'])
                self.assertEqual(len(final['completed_batches']), 1)

    def test_atomic_report_replacement_and_fatal_path_preserve_progress(self):
        path = self.root/'latest.json'
        tb.atomic_json(path, {'old': True})
        original = path.read_bytes()
        with mock.patch.object(tb.os, 'replace', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                tb.atomic_json(path, {'new': True})
        self.assertEqual(path.read_bytes(), original)
        control = self.root/'control.json'
        control.write_text(json.dumps({'state_dir':str(self.root), 'max_tick_seconds':10}))
        def fail(*args):
            tb.completed('new', {'status':'PUBLISHED', 'batch':'b'})
            raise RuntimeError('synthetic fatal error')
        @contextmanager
        def acquired(*args): yield True
        with mock.patch.object(maintenance, 'caretaker_maintenance_lock', acquired), \
             mock.patch.object(maintenance, '_locked_tick', side_effect=fail):
            with self.assertRaises(RuntimeError): maintenance.tick(control)
        report = json.loads(path.read_text())
        self.assertEqual(report['status'], 'FAILED')
        self.assertEqual(report['completed_batches'][0]['batch'], 'b')
        self.assertFalse(report['running'])

    def test_metadata_budget_exit_without_marking_scan_complete(self):
        proposals = self.root/'proposals'/'synthetic'
        proposals.mkdir(parents=True)
        (proposals/'a.md').write_text('synthetic')
        clock = mock.Mock(return_value=0)
        budget = tb.TickBudget(1, clock=clock)
        clock.return_value = 1
        with budget.activate(), mock.patch.object(audit, 'read_registry', return_value=({}, None)), \
             mock.patch.object(audit, 'accounted_proposal_ids', return_value=set()), \
             mock.patch.object(audit, '_source_session_index', return_value={}):
            with self.assertRaises(tb.TickBudgetExceeded): audit.eligible_proposals(self.root)
        self.assertFalse((self.root/'intake.sqlite').exists())

    def test_deferred_batch_does_not_quarantine_and_is_due_next_tick(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        pubstate.ensure_pub_tables(db)
        pubstate.record_start(db, 'batch', '2026-10-12T12:00:00+00:00', 'fp', {'proposal_ids':['a']})
        before = pubstate.read_row(db, 'batch')
        pubstate.record_budget_deferred(db, 'batch', 'fp')
        row = pubstate.read_row(db, 'batch')
        self.assertEqual(row['attempt'], before['attempt'])
        self.assertEqual(row['failures'], before['failures'])
        self.assertTrue(pubstate.due_to_run(row, '2026-10-12T13:00:00+00:00', 'fp')[0])

    def test_deadline_releases_kernel_lock_and_does_not_replace_busy_tick(self):
        control = self.root/'control.json'
        control.write_text(json.dumps({'state_dir':str(self.root), 'max_tick_seconds':1}))
        with maintenance.caretaker_maintenance_lock(self.root) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(maintenance.tick(control), {'status':'ALREADY_RUNNING'})
            self.assertFalse((self.root/'latest.json').exists())
        with mock.patch.object(maintenance, '_locked_tick', side_effect=tb.TickBudgetExceeded('synthetic')):
            self.assertEqual(maintenance.tick(control)['status'], 'DEFERRED_BUDGET')
        with maintenance.caretaker_maintenance_lock(self.root) as acquired:
            self.assertTrue(acquired)

    def test_deadline_after_canonical_commit_is_pending_checkpoint_not_failed_batch(self):
        import curate_once as curator
        from types import SimpleNamespace as S
        clock = mock.Mock(return_value=0)
        budget = tb.TickBudget(120, clock=clock)
        result = {'proposal_ids': ['prop-a'], 'status': 'REVIEWED_CANDIDATE'}
        @contextmanager
        def lock(*args): yield 'synthetic-owned-lock'
        def commit(*args, **kwargs):
            clock.return_value = 120
            return {'status':'PUBLISHED', 'event_id':'synthetic'}
        cfg = S(hub=S(script='synthetic/knowledge.py', vault=self.root, python_executable='synthetic'))
        with budget.activate(), mock.patch.object(curator, 'curator_lock', lock), \
             mock.patch.object(curator, 'accounted_proposal_ids', return_value=set()), \
             mock.patch.object(curator, 'command', side_effect=commit), \
             mock.patch.object(curator, 'checkpoint_publication', side_effect=tb.TickBudgetExceeded('synthetic')):
            curator._publish_under_lock(cfg, self.root/'manifest', self.root/'review', self.root, result)
        self.assertEqual(result['status'], 'PUBLISHED')
        self.assertEqual(result['checkpoint']['status'], 'PENDING')
        self.assertEqual(result['reason'], 'checkpoint_pending')


class RetainedBudgetTests(unittest.TestCase):
    def setUp(self):
        resume_tests.ResumeTests.setUp(self)

    def test_expiry_preserves_accepted_pointer_and_can_resume(self):
        budget = tb.TickBudget(10, clock=lambda: 0)
        with budget.activate():
            with self.assertRaises(tb.TickBudgetExceeded):
                curator = __import__('curate_once')
                curator._run_with_config(self.cfg, ['proposals/a.md'], True, None, None)
        result, _ = __import__('curate_once')._load_reviewed_candidate(self.cfg, ['proposals/a.md'], self.curation)
        self.assertEqual(result['status'], 'REVIEWED_CANDIDATE')
        self.assertEqual(result.get('model_calls', 0), 0)
