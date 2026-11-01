"""A ready publication must not wait behind source/model work in the same tick."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import maintenance
from caretaker.connectors import ConnectorCollection
from .test_maintenance import _maintenance_fixture, _Event


class PublicationPriorityTests(unittest.TestCase):
    def run_tick(self, *, epoch=False, hold_legacy=False, pipeline=False,
                 runtime_family=None, held_result=None):
        tmp_root = Path(__file__).resolve().parents[1] / '.test-tmp'
        tmp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as directory:
            root = Path(directory)
            control, _, _ = _maintenance_fixture(root)
            control['publication'] = {'enabled': True,
                                      'include_existing_hub_backlog': not hold_legacy}
            if epoch:
                control['new_event_epoch'] = {'enabled': True, 'activate': True}
            if runtime_family:
                control['_runtime_work_family'] = runtime_family
            state = Path(control['state_dir'])
            state.mkdir()
            (root / 'vault/events').mkdir(parents=True)
            (root / 'vault/proposals').mkdir()
            if pipeline:
                with closing(sqlite3.connect(state / 'intake.sqlite')) as db:
                    db.execute('CREATE TABLE queue(id TEXT PRIMARY KEY,project TEXT,payload TEXT NOT NULL,forwarded INTEGER NOT NULL DEFAULT 0)')
                    event = _Event().to_dict()
                    db.execute('INSERT INTO queue VALUES(?,?,?,0)',
                               (maintenance.event_id(event), 'one', json.dumps(event)))
                    db.commit()
            order = []

            def drain(*args, **kwargs):
                order.append('publish:' + kwargs.get('lane', 'legacy'))
                return {'status': 'OK', 'batches': [], 'model_calls': 0}

            def collect(*args, **kwargs):
                order.append('collect')
                return ConnectorCollection()

            def run(*args, **kwargs):
                order.append('pipeline')
                return SimpleNamespace(returncode=1, stdout=json.dumps(
                    {'status': 'BLOCKED', 'incidents': ['synthetic_failure']}))

            with patch('caretaker.transports.resolve_fleet_runtime', return_value={
                    'version_text': 'synthetic', 'path': root, 'pinned': True}), \
                 patch('caretaker.registry.read_registry', return_value=({}, None)), \
                 patch('caretaker.roll_forward.recover_stale_curator_lock', return_value={'status': 'NO_LOCK'}), \
                 patch('maintenance.drain_intake_checkpoints', return_value={'status': 'IDLE'}), \
                 patch('maintenance.publication_drain', side_effect=drain), \
                 patch('maintenance._held_legacy_publication', return_value=held_result or {'status': 'DEGRADED', 'reason': 'legacy_backlog_held'}), \
                 patch('maintenance.collect_sources', side_effect=collect), \
                 patch('caretaker.backlog.pending_projects', return_value=[]), \
                 patch('maintenance.subprocess.run', side_effect=run):
                report = maintenance._locked_tick(control, state)
            return order, report

    def test_legacy_publication_precedes_collector_and_runs_once(self):
        order, report = self.run_tick()
        self.assertEqual(order, ['publish:legacy', 'collect'])
        self.assertEqual(report['publication']['status'], 'OK')

    def test_pipeline_failure_cannot_delay_ready_publication(self):
        order, report = self.run_tick(pipeline=True)
        self.assertEqual(order, ['publish:legacy', 'collect', 'pipeline'])
        self.assertEqual(report['projects'][0]['pipeline']['status'], 'BLOCKED')
        self.assertEqual(report['publication']['status'], 'OK')

    def test_both_lanes_publish_once_before_collection(self):
        order, report = self.run_tick(epoch=True)
        self.assertEqual(order, ['publish:legacy', 'publish:new', 'collect'])
        self.assertEqual(report['publication']['status'], 'OK')
        self.assertEqual(report['new_lane']['publication']['status'], 'OK')

    def test_held_legacy_does_not_run_or_block_new_lane(self):
        order, report = self.run_tick(epoch=True, hold_legacy=True)
        self.assertEqual(order, ['publish:new', 'collect'])
        self.assertEqual(report['publication']['reason'], 'legacy_backlog_held')
        self.assertEqual(report['publication']['status'], 'DEGRADED')

    def test_rotation_slot_reports_only_intentional_legacy_deferral(self):
        order, scheduled = self.run_tick(
            epoch=True, hold_legacy=True, runtime_family='new_publication',
        )
        self.assertEqual(order, ['publish:new', 'collect'])
        self.assertEqual(scheduled['publication']['status'], 'DEFERRED_SCHEDULED')
        self.assertEqual(scheduled['publication']['reason'],
                         'legacy_publication_deferred_by_family_rotation')
        self.assertNotEqual(scheduled['status'], 'DEGRADED')

        _, actual_error = self.run_tick(
            epoch=True, hold_legacy=True, runtime_family='new_publication',
            held_result={'status': 'DEGRADED', 'reason': 'legacy_audit_failed'},
        )
        self.assertEqual(actual_error['publication']['status'], 'DEGRADED')
        self.assertEqual(actual_error['publication']['reason'], 'legacy_audit_failed')
        self.assertEqual(actual_error['status'], 'DEGRADED')


if __name__ == '__main__':
    unittest.main()
