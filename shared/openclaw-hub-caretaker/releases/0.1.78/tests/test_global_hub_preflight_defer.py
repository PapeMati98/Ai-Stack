import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import maintenance
from caretaker.hub import CanonicalKnowledgeGateway, HealthResult, LintResult
from caretaker.audit import policy_from
from caretaker.pubstate import read_row


class GlobalHubPreflightTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / '.test-tmp'
        root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.database = sqlite3.connect(':memory:')
        self.addCleanup(self.database.close)
        self.database.execute(
            'CREATE TABLE intake_inputs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)'
        )
        self.database.execute(
            'INSERT INTO intake_inputs VALUES(?,?)', ('input-a', 'retained')
        )
        self.base = {'hub': {
            'python_executable': 'python',
            'script': str(self.root / 'knowledge.py'),
            'vault': str(self.root / 'vault'),
            'context_receipt': '',
        }}
        self.control = {'max_projects_per_tick': 2, 'publication': {
            'enabled': True,
            'include_existing_hub_backlog': True,
            'max_batches_per_tick': 1,
            'held_projects': [],
        }}

    def preflight(self, *, health_payload=None, lint_payload=None):
        health_payload = health_payload or {
            'action': 'status', 'dirty_tree_state': 'clean',
            'dirty_tree_write_blocked': False,
        }
        lint_payload = lint_payload or {'error_count': 0, 'warning_count': 0}

        def runner(*, argv, cwd, timeout):
            action = argv[4]
            payload = health_payload if action == 'status' else lint_payload
            return 0, json.dumps(payload), ''

        gateway = CanonicalKnowledgeGateway(
            python_executable='python', script=self.base['hub']['script'],
            vault=self.base['hub']['vault'], run_callable=runner,
        )
        with mock.patch('caretaker.hub.CanonicalKnowledgeGateway',
                        return_value=gateway):
            return maintenance._global_hub_preflight(self.base)

    def test_global_lint_failure_defers_then_pass_uses_normal_drain(self):
        failed_gate = self.preflight(lint_payload={'warning_count': 0})
        self.assertEqual(failed_gate['status'], 'DEFERRED_HUB_PREFLIGHT')
        self.assertEqual(failed_gate['reason'], 'hub_lint_failed')

        before_changes = self.database.total_changes
        first = maintenance.publication_drain(
            self.control, self.base, self.database, self.root,
            _hub_preflight=failed_gate, recovery={'status': 'NO_LOCK'},
        )
        self.assertEqual(first['status'], 'DEFERRED_HUB_PREFLIGHT')
        self.assertEqual(first['model_calls'], 0)
        self.assertEqual(first['batches'], [])
        self.assertEqual(self.database.total_changes, before_changes)
        self.assertEqual(self.database.execute(
            'SELECT payload FROM intake_inputs WHERE id=?', ('input-a',),
        ).fetchone(), ('retained',))
        self.assertEqual(self.database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
        ).fetchall(), [('intake_inputs',)])

        passed_gate = self.preflight()
        import caretaker.audit as audit
        cycle = {'id': 'cycle-a', 'members': []}
        publication_calls = []

        def publish(*args, **kwargs):
            publication_calls.append(kwargs)
            return {'status': 'PUBLISHED', 'model_calls': 1, 'batch': 'batch-a'}

        with mock.patch.object(maintenance, '_audit_summary', return_value={}), \
                mock.patch.object(maintenance, '_audit_fields', return_value={}), \
                mock.patch.object(maintenance, 'complete_pending_checkpoints', return_value=[]), \
                mock.patch.object(audit, 'maybe_start_cycle', return_value=cycle), \
                mock.patch.object(audit, 'cycle_projects', return_value=['project-a']), \
                mock.patch.object(maintenance, '_prepared_priority_candidate', return_value=None), \
                mock.patch.object(maintenance, '_project_runtime_cfg',
                                  return_value=('config', {'project_slug': 'project-a'})), \
                mock.patch.object(maintenance, 'publication_tick', side_effect=publish), \
                mock.patch('caretaker.pubstate.reconcile_all_running'):
            second = maintenance.publication_drain(
                self.control, self.base, self.database, self.root,
                _hub_preflight=passed_gate, recovery={'status': 'NO_LOCK'},
            )

        self.assertEqual(second['status'], 'OK')
        self.assertEqual(second['model_calls'], 1)
        self.assertEqual(len(publication_calls), 1)
        self.assertEqual(self.database.execute(
            'SELECT payload FROM intake_inputs WHERE id=?', ('input-a',),
        ).fetchone(), ('retained',))

    def test_late_curator_preflight_failure_is_cached_without_charging_batch(self):
        vault = self.root / 'late-gate-vault'
        vault.mkdir()
        state = self.root / 'late-gate-state'
        state.mkdir()
        target = self.root / 'missing-target.md'
        member = {
            'proposal_id': 'prop-a', 'path': 'proposals/agent/prop-a.md',
            'sha256': 'a' * 64, 'project': 'project-a', 'producer': 'codex',
            'episode': '', 'source_ids': ['src-a'],
        }
        policy = {
            'enabled': True, 'include_existing_hub_backlog': True,
            'min_ready_groups': 1, 'max_wait_hours': 24, 'max_batch': 8,
        }
        control = {'publication': policy}
        cfg = {
            'hub': {'vault': str(vault)}, 'project_slug': 'project-a',
            'state_dir': str(state),
        }
        summary = {
            'trigger': 'threshold', 'eligible': [member], 'raw_eligible': 1,
            'group_count': 1, 'ready_group_count': 1, 'oldest_age_hours': 24,
            'deferred': 0, 'unsupported': 0,
            'policy': policy_from(policy),
        }
        passed = {
            'status': 'PASS', 'health_status': 'pass', 'lint_status': 'pass',
        }
        preflight_state = {'result': dict(passed)}
        calls = []

        def invoke(*_args):
            calls.append(True)
            if len(calls) == 1:
                return {'status': 'BLOCKED', 'reason': 'hub_preflight_failed'}
            return {'status': 'PUBLISHED'}

        with mock.patch.object(maintenance, '_audit_summary', return_value=summary), \
                mock.patch('caretaker.registry.read_registry', return_value=(None, None)), \
                mock.patch('caretaker.registry.main_page', return_value='wiki/projects/project-a.md'), \
                mock.patch('caretaker.audit.accounted_proposal_ids', return_value=set()), \
                mock.patch('caretaker.audit.source_metadata_index', return_value={}), \
                mock.patch('caretaker.audit.partition_batch_members',
                           side_effect=lambda _v, members, *_a, **_kw: (members, [])), \
                mock.patch.object(maintenance, '_live_member_hash', return_value=member['sha256']), \
                mock.patch('caretaker.publication.safe_file', return_value=target), \
                mock.patch('caretaker.repaired_retry.repaired_retry_candidates', return_value=[]), \
                mock.patch.object(maintenance, '_invoke_curate', side_effect=invoke) as curator:
            first = maintenance.publication_tick(
                'config', cfg, control, self.database,
                _hub_preflight_state=preflight_state,
            )
            first_row = read_row(self.database, first['batch'])
            self.assertEqual(first['status'], 'DEFERRED_HUB_PREFLIGHT')
            self.assertEqual(first['reason'], 'hub_preflight_failed')
            self.assertEqual((first_row['status'], first_row['failures'], first_row['attempt']),
                             ('DEFERRED_BUDGET', 0, 0))
            self.assertEqual(self.database.execute(
                "SELECT status FROM audit_members WHERE proposal_id='prop-a'"
            ).fetchone(), ('pending',))

            changes_after_first = self.database.total_changes
            same_tick = maintenance.publication_tick(
                'config', cfg, control, self.database,
                _hub_preflight_state=preflight_state,
            )
            self.assertEqual(same_tick['status'], 'DEFERRED_HUB_PREFLIGHT')
            self.assertEqual(self.database.total_changes, changes_after_first)
            curator.assert_called_once()

            next_tick = maintenance.publication_tick(
                'config', cfg, control, self.database,
                _hub_preflight_state={'result': dict(passed)},
            )

        self.assertEqual(next_tick['status'], 'PUBLISHED')
        self.assertEqual(len(calls), 2)
        final_row = read_row(self.database, next_tick['batch'])
        self.assertEqual((final_row['status'], final_row['failures'], final_row['attempt']),
                         ('PUBLISHED', 0, 1))

    def test_missing_health_or_lint_counts_fail_closed(self):
        bad_health = self.preflight(health_payload={'action': 'status'})
        self.assertEqual(bad_health, {
            'status': 'DEFERRED_HUB_PREFLIGHT', 'reason': 'hub_health_failed',
        })
        bad_lint = self.preflight(lint_payload={'warning_count': 0})
        self.assertEqual(bad_lint, {
            'status': 'DEFERRED_HUB_PREFLIGHT', 'reason': 'hub_lint_failed',
        })

    def test_preflight_requires_canonical_result_types_and_nonempty_status_evidence(self):
        self.assertFalse(maintenance._valid_hub_preflight({'status': 'PASS'}))
        gateway = mock.Mock()
        gateway.health.return_value = mock.Mock(status='pass', notes='looks healthy')
        gateway.lint.return_value = LintResult(status='pass', notes='lint clean')
        with mock.patch('caretaker.hub.CanonicalKnowledgeGateway', return_value=gateway):
            impostor = maintenance._global_hub_preflight(self.base)
        self.assertEqual(impostor['reason'], 'hub_health_failed')
        gateway.lint.assert_not_called()

        gateway.health.return_value = HealthResult(status='pass', notes='')
        with mock.patch('caretaker.hub.CanonicalKnowledgeGateway', return_value=gateway):
            empty_evidence = maintenance._global_hub_preflight(self.base)
        self.assertEqual(empty_evidence['reason'], 'hub_health_failed')

        gateway.health.return_value = HealthResult(status='pass', notes='status PASS')
        gateway.lint.return_value = LintResult(status='pass', notes='')
        with mock.patch('caretaker.hub.CanonicalKnowledgeGateway', return_value=gateway):
            empty_lint_evidence = maintenance._global_hub_preflight(self.base)
        self.assertEqual(empty_lint_evidence['reason'], 'hub_lint_failed')

    def test_missing_global_gate_configuration_fails_closed(self):
        self.assertEqual(maintenance._global_hub_preflight({}), {
            'status': 'DEFERRED_HUB_PREFLIGHT',
            'reason': 'hub_preflight_config_invalid',
        })

    def test_maintenance_tick_recovers_intake_before_global_gate_failure(self):
        state = self.root / 'state'
        state.mkdir()
        intake = sqlite3.connect(state / 'intake.sqlite')
        intake.execute(
            'CREATE TABLE queue (id TEXT PRIMARY KEY, project TEXT, payload TEXT NOT NULL, '
            'forwarded INTEGER NOT NULL DEFAULT 0)'
        )
        intake.execute(
            'INSERT INTO queue VALUES(?,?,?,?)',
            ('input-a', 'project-a', '{"synthetic":true}', 0),
        )
        intake.commit()
        intake.close()
        base_config = self.root / 'base-config.json'
        base_config.write_text(json.dumps({
            'hub': self.base['hub'], 'state_dir': str(state),
        }), encoding='utf-8')
        control = {
            'state_dir': str(state), 'base_config': str(base_config),
            'sources': [], 'publication': {
                'enabled': True, 'include_existing_hub_backlog': True,
            },
            'new_event_epoch': {'enabled': False},
        }
        failure = {
            'status': 'DEFERRED_HUB_PREFLIGHT', 'reason': 'hub_lint_failed',
        }
        call_order = []
        with mock.patch('caretaker.transports.resolve_fleet_runtime', return_value={
                    'version_text': 'synthetic', 'path': 'synthetic', 'pinned': True,
                }), \
                mock.patch('caretaker.registry.read_registry', return_value=(None, None)), \
                mock.patch('caretaker.roll_forward.recover_stale_curator_lock',
                           return_value={'status': 'NO_LOCK'}), \
                mock.patch.object(maintenance, '_global_hub_preflight',
                                  side_effect=lambda _base: (call_order.append('hub-gate'), failure)[1]) as gate, \
                mock.patch.object(maintenance, 'drain_intake_checkpoints',
                                  side_effect=lambda _base: (call_order.append('intake-recovery'),
                                                             {'status': 'COMPLETED', 'new_sources': 0})[1]), \
                mock.patch.object(maintenance, 'publication_drain',
                                  wraps=maintenance.publication_drain) as pub_drain:
            report = maintenance._locked_tick(control, state)

        self.assertEqual(report['status'], 'DEFERRED_HUB_PREFLIGHT')
        self.assertEqual(report['publication']['model_calls'], 0)
        self.assertEqual(report['publication']['batches'], [])
        self.assertEqual(call_order, ['intake-recovery', 'hub-gate'])
        gate.assert_called_once_with(json.loads(base_config.read_text(encoding='utf-8')))
        pub_drain.assert_called_once()
        check = sqlite3.connect(state / 'intake.sqlite')
        self.addCleanup(check.close)
        self.assertEqual(check.execute(
            'SELECT id, forwarded FROM queue WHERE id=?', ('input-a',),
        ).fetchone(), ('input-a', 0))
        self.assertEqual(check.execute(
            'SELECT count(*) FROM pipeline_failures',
        ).fetchone(), (0,))

    def test_incomplete_intake_recovery_fails_closed_before_gate_or_publication(self):
        state = self.root / 'state-recovery'
        state.mkdir()
        intake = sqlite3.connect(state / 'intake.sqlite')
        intake.execute(
            'CREATE TABLE queue (id TEXT PRIMARY KEY, project TEXT, payload TEXT NOT NULL, '
            'forwarded INTEGER NOT NULL DEFAULT 0)'
        )
        intake.execute(
            'INSERT INTO queue VALUES(?,?,?,?)',
            ('input-b', 'project-b', '{"synthetic":true}', 0),
        )
        intake.commit()
        intake.close()
        base_config = self.root / 'base-config-recovery.json'
        base_config.write_text(json.dumps({
            'hub': self.base['hub'], 'state_dir': str(state),
        }), encoding='utf-8')
        control = {
            'state_dir': str(state), 'base_config': str(base_config),
            'sources': [], 'publication': {
                'enabled': True, 'include_existing_hub_backlog': True,
            },
            'new_event_epoch': {'enabled': False},
        }
        with mock.patch('caretaker.transports.resolve_fleet_runtime', return_value={
                    'version_text': 'synthetic', 'path': 'synthetic', 'pinned': True,
                }), \
                mock.patch('caretaker.registry.read_registry', return_value=(None, None)), \
                mock.patch('caretaker.roll_forward.recover_stale_curator_lock',
                           return_value={'status': 'NO_LOCK'}), \
                mock.patch.object(maintenance, 'drain_intake_checkpoints',
                                  return_value={'status': 'UNAVAILABLE', 'new_sources': 0}), \
                mock.patch.object(maintenance, '_global_hub_preflight') as gate, \
                mock.patch.object(maintenance, 'publication_drain') as pub_drain:
            report = maintenance._locked_tick(control, state)

        self.assertEqual(report['status'], 'DEFERRED_INTAKE_RECOVERY')
        self.assertEqual(report['pending_intake_recovery']['status'], 'UNAVAILABLE')
        self.assertEqual(report['publication']['model_calls'], 0)
        gate.assert_not_called()
        pub_drain.assert_not_called()
        check = sqlite3.connect(state / 'intake.sqlite')
        self.addCleanup(check.close)
        self.assertEqual(check.execute(
            'SELECT id, forwarded FROM queue WHERE id=?', ('input-b',),
        ).fetchone(), ('input-b', 0))
        self.assertEqual(check.execute(
            'SELECT count(*) FROM pipeline_failures',
        ).fetchone(), (0,))


if __name__ == '__main__':
    unittest.main()
