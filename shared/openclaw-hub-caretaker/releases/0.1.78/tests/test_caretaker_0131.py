"""Regressions for MiniMax intermediate rows, intentional unassignment and Fleet selection."""
import datetime as dt
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import maintenance
from caretaker.connectors import ConnectorCollection, ConnectorIssue, SourceSpec, collect_sources
from caretaker.transports import (RUNTIME_MARKER, TransportError,
                                  resolve_fleet_runtime, resolve_fleet_py)
from . import test_connectors
TASK_TMP = test_connectors.TASK_TMP
from .test_maintenance import _maintenance_fixture


class MiniMaxIntermediateTests(unittest.TestCase):
    def test_completed_steps_are_silent_and_old_incomplete_turn_is_one_info(self):
        TASK_TMP.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            path = Path(directory) / 'minimax.db'
            test_connectors.ConnectorTests()._create_minimax_db(path)
            with sqlite3.connect(path) as db:
                old = 1789647204000
                fresh = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
                for ident, turn, stamp in [(3, 'mm-t1', old), (4, 'mm-open', old),
                                           (5, 'mm-open', old + 1000),
                                           (6, 'mm-fresh', fresh)]:
                    db.execute('INSERT INTO local_runtime_message_rows VALUES (?,?,?,?,?,?,?,?)',
                               (ident, 'mm-s1', 'mm-a' + str(ident), 'assistant', turn,
                                stamp, json.dumps({'role': 'assistant', 'turn_id': turn,
                                                   'msg_content': 'Intermediate',
                                                   'finish_reason': 'toolUse'}), 'api'))
            db.close()
            result = collect_sources([SourceSpec('minimax', path, quiet_minutes=30)])
            self.assertEqual(len(result.events), 1)
            self.assertEqual([(i.status, i.reason) for i in result.issues],
                             [('info', 'turn_not_completed')])


class UnassignedByDesignTests(unittest.TestCase):
    def test_info_issue_is_counted_without_degrading_tick(self):
        TASK_TMP.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            root = Path(directory)
            control, _, _ = _maintenance_fixture(root)
            control['max_projects_per_tick'] = 0
            state = Path(control['state_dir'])
            state.mkdir(parents=True)
            issue = ConnectorIssue('synthetic', 'minimax', 'info', 'turn_not_completed')
            with patch('maintenance.collect_sources', return_value=ConnectorCollection(issues=(issue,))):
                report = maintenance._locked_tick(control, state)
            self.assertEqual(report['source_issue_count'], 1)
            self.assertEqual(report['status'], 'OK')

    def test_prefix_is_case_insensitive_bounded_and_missing_key_keeps_degraded(self):
        TASK_TMP.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            root = Path(directory)
            control, _, _ = _maintenance_fixture(root)
            control.update(sources=[], max_projects_per_tick=0,
                           unassigned_by_design_workspace_prefixes=[
                               'C:/Users/Example/AppData/Roaming/Claude/scratch-workspaces'])
            state = Path(control['state_dir'])
            state.mkdir(parents=True)
            maintenance._locked_tick(control, state)
            db = sqlite3.connect(state / 'intake.sqlite')
            event = {'tool': 'claude', 'native_session_id': 'chat-one', 'cursor': 'turn-one',
                     'cwd': r'c:\USERS\example\AppData\Roaming\Claude\scratch-workspaces\chat',
                     'completed_at': dt.datetime.now(dt.timezone.utc).isoformat()}
            db.execute('INSERT INTO queue(id,project,payload) VALUES(?,NULL,?)',
                       (maintenance.event_id(event), json.dumps(event)))
            db.commit()
            db.close()
            report = maintenance._locked_tick(control, state)
            self.assertIn('version', report['fleet_runtime'])
            self.assertIn('pinned', report['fleet_runtime'])
            self.assertEqual((report['unassigned_by_design'], report['unassigned_events']), (1, 0))
            self.assertEqual(report['status'], 'OK')
            db = sqlite3.connect(state / 'intake.sqlite')
            self.assertEqual(db.execute('SELECT forwarded FROM queue').fetchone()[0], 0)
            db.close()
            missing = maintenance._locked_tick({k: v for k, v in control.items()
                                                if k != 'unassigned_by_design_workspace_prefixes'}, state)
            self.assertEqual(missing['unassigned_events'], 1)
            self.assertEqual(missing['status'], 'DEGRADED')
            self.assertFalse(maintenance._unassigned_by_design(
                event | {'cwd': event['cwd'].replace('\\chat', '-other\\chat')}, control))
            assigned = event | {'native_session_id': 'chat-assigned', 'project': 'one'}
            db = sqlite3.connect(state / 'intake.sqlite')
            db.execute('INSERT INTO queue(id,project,payload) VALUES(?,?,?)',
                       (maintenance.event_id(assigned), 'one', json.dumps(assigned)))
            db.commit()
            db.close()
            reclassified = maintenance._locked_tick(control, state)
            self.assertEqual(reclassified['unassigned_by_design'], 2)
            db = sqlite3.connect(state / 'intake.sqlite')
            self.assertEqual(db.execute('SELECT project,forwarded FROM queue WHERE id=?',
                                        (maintenance.event_id(assigned),)).fetchone(),
                             (None, 0))
            db.close()

    def test_new_lane_counts_design_without_degradation(self):
        TASK_TMP.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            root = Path(directory)
            control, _, _ = _maintenance_fixture(root)
            control.update(sources=[], max_projects_per_tick=0,
                           new_event_epoch={'enabled': True, 'activate': True},
                           unassigned_by_design_workspace_prefixes=['C:/scratch'])
            state = Path(control['state_dir'])
            state.mkdir(parents=True)
            maintenance._locked_tick(control, state)
            db = sqlite3.connect(state / 'intake.sqlite')
            event = {'tool': 'claude', 'native_session_id': 'chat-two', 'cursor': 'turn-two',
                     'cwd': r'c:\SCRATCH\chat',
                     'completed_at': dt.datetime.now(dt.timezone.utc).isoformat()}
            db.execute('INSERT INTO queue(id,project,payload) VALUES(?,NULL,?)',
                       (maintenance.event_id(event), json.dumps(event)))
            db.commit()
            db.close()
            report = maintenance._locked_tick(control, state)
            self.assertEqual(report['new_lane']['unassigned_by_design'], 1)
            self.assertEqual(report['new_lane']['unassigned_events'], 0)
            self.assertEqual(report['unassigned_events'], 0)
            self.assertNotEqual(report['status'], 'DEGRADED')


class FleetRuntimeSelectionTests(unittest.TestCase):
    @staticmethod
    def runtime(parent, name, version, *, marker=True, partial=False):
        root = parent / name
        entry = root / 'agent-fleet' / 'scripts' / 'fleet.py'
        entry.parent.mkdir(parents=True)
        entry.write_text('# synthetic fleet\n', encoding='utf-8')
        if marker:
            (root / RUNTIME_MARKER).write_text(json.dumps({
                'package_id': 'agent-fleet-suite',
                'managed_by': 'agent-fleet-suite-v2-installer',
                'package_fingerprint': 'synthetic', 'payload_fingerprint': 'synthetic',
                'transaction_id': 'synthetic', 'package_version': version,
                'payload_file_count': 2 if partial else 1,
            }), encoding='utf-8')
        return root

    def test_newer_attested_sibling_wins_and_partial_unattested_ignored(self):
        TASK_TMP.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            parent = Path(directory)
            old = self.runtime(parent, 'old', '0.9.20')
            newer = self.runtime(parent, 'newer', '0.9.37')
            self.runtime(parent, 'unattested', '0.9.99', marker=False)
            self.runtime(parent, 'partial', '0.9.98', partial=True)
            with patch.dict(os.environ, {'AGENT_FLEET_SUITE_ROOT': str(old),
                                         'AGENT_FLEET_SUITE_PIN': 'false'}):
                self.assertEqual(resolve_fleet_runtime()['path'], newer)
                self.assertEqual(resolve_fleet_py(), newer / 'agent-fleet/scripts/fleet.py')

    def test_pin_honoured_and_unusable_pin_fails_closed(self):
        TASK_TMP.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            parent = Path(directory)
            old = self.runtime(parent, 'old', '0.9.20')
            self.runtime(parent, 'newer', '0.9.37')
            with patch.dict(os.environ, {'AGENT_FLEET_SUITE_ROOT': str(old),
                                         'AGENT_FLEET_SUITE_PIN': 'yes'}):
                self.assertEqual(resolve_fleet_runtime()['path'], old)
                self.assertTrue(resolve_fleet_runtime()['pinned'])
            with patch.dict(os.environ, {'AGENT_FLEET_SUITE_ROOT': str(parent / 'missing'),
                                         'AGENT_FLEET_SUITE_PIN': 'on'}):
                with self.assertRaises(TransportError):
                    resolve_fleet_py()


if __name__ == '__main__':
    unittest.main()
