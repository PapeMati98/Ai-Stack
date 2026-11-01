"""Synthetic regression for intake lock isolation and HEAD-gated publication."""
import contextlib
import copy
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import maintenance
from caretaker.audit import policy_from
from caretaker.publication import PublicationError
from caretaker.pubstate import ensure_pub_tables
from tests import test_committed_inputs as committed_fixture


def partial(*, only_busy=False):
    common = dict(action='checkpoint-intake', required=True, mode='local-commit-only',
                  policy_source='git-head', machine='FIXTURE', curator_machine='FIXTURE')
    done = dict(common, proposal_id='prop-unrelated-done', status='CHECKPOINTED',
                commit='a' * 40, path_count=5, lint_error_count=0, lint_warning_count=0)
    already = dict(done, proposal_id='prop-unrelated-already', status='ALREADY_CHECKPOINTED')
    busy = dict(common, proposal_id='prop-unrelated-busy', status='BLOCKED_CHECKPOINT_LOCK')
    return dict(status='PARTIAL', new_sources=0, candidate_count=4,
                results=[busy] if only_busy else [done, already, busy])


@contextlib.contextmanager
def no_lock(*args, **kwargs):
    yield 'fixture-owned-lock'


class StopBeforeModel(RuntimeError):
    pass


class SafePartialCheckpointRecoveryTests(unittest.TestCase):
    def setUp(self):
        fixture = committed_fixture.CommittedInputsTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.root, self.vault = fixture.root, fixture.vault
        self.state = self.root / 'state'
        self.state.mkdir()
        self.base = {'hub': {'vault': str(self.vault), 'script': str(self.root / 'knowledge.py')},
                     'state_dir': str(self.state)}
        self.config = self.root / 'base.json'
        self.config.write_text(json.dumps(self.base), encoding='utf-8')
        self.control = {'base_config': str(self.config), 'sources': [],
                        'publication': {'enabled': True, 'include_existing_hub_backlog': True,
                                        'min_ready_groups': 1, 'max_wait_hours': 24, 'max_batch': 8},
                        'new_event_epoch': {'enabled': False}}
        self.passed = {'status': 'PASS', 'health_status': 'pass', 'lint_status': 'pass'}

    @contextlib.contextmanager
    def tick_environment(self, recovery):
        with contextlib.ExitStack() as stack:
            real_connect = sqlite3.connect
            def tracked_connect(*args, **kwargs):
                connection = real_connect(*args, **kwargs)
                stack.callback(connection.close)
                return connection
            # The positive test deliberately stops at the mocked model boundary;
            # close its tick connection even though normal tick teardown is skipped.
            stack.enter_context(patch('maintenance.sqlite3.connect', side_effect=tracked_connect))
            stack.enter_context(patch('caretaker.transports.resolve_fleet_runtime', return_value={
                'version_text': 'fixture', 'path': 'fixture', 'pinned': True}))
            stack.enter_context(patch('caretaker.registry.read_registry', return_value=(None, None)))
            stack.enter_context(patch('caretaker.roll_forward.recover_stale_curator_lock',
                                      return_value={'status': 'NO_LOCK'}))
            stack.enter_context(patch.object(maintenance, 'drain_intake_checkpoints', return_value=recovery))
            yield stack

    @contextlib.contextmanager
    def batch_environment(self, database):
        member = dict(proposal_id='prop-fixture', path='proposals/one.md',
                      sha256=self.fixture.frozen['inputs'][0]['sha256'], project='fixture-project',
                      producer='codex', episode='', source_ids=['src-fixture'])
        frozen = dict(self.fixture.frozen, proposals=[{
            'id': member['proposal_id'], 'path': member['path'], 'sha256': member['sha256']}])
        policy = self.control['publication']
        summary = dict(trigger='threshold', eligible=[member], raw_eligible=1,
                       group_count=1, ready_group_count=1, oldest_age_hours=24,
                       deferred=0, unsupported=0, policy=policy_from(policy))
        with contextlib.ExitStack() as stack:
            for target, kwargs in (
                ('maintenance._audit_summary', {'return_value': summary}),
                ('caretaker.registry.read_registry', {'return_value': (None, None)}),
                ('caretaker.registry.main_page', {'return_value': 'wiki/projects/fixture-project.md'}),
                ('caretaker.audit.accounted_proposal_ids', {'return_value': set()}),
                ('caretaker.audit.source_metadata_index', {'return_value': {}}),
                ('caretaker.audit.partition_batch_members', {'side_effect': lambda _v, members, *_a, **_k: (members, [])}),
                ('maintenance._live_member_hash', {'return_value': member['sha256']}),
                ('caretaker.publication.safe_file', {'return_value': self.root / 'absent-target.md'}),
                ('caretaker.publication.freeze', {'return_value': frozen}),
                ('caretaker.repaired_retry.repaired_retry_candidates', {'return_value': []}),
            ):
                stack.enter_context(patch(target, **kwargs))
            yield frozen

    def test_allowed_shapes_and_fail_closed_unknown_dirty_lint_role(self):
        self.assertTrue(maintenance._safe_partial_checkpoint_recovery(partial()))
        self.assertTrue(maintenance._safe_partial_checkpoint_recovery(partial(only_busy=True)))
        warning = partial()
        warning['results'][0]['lint_warning_count'] = 1
        self.assertTrue(maintenance._safe_partial_checkpoint_recovery(warning))
        mutations = [('status', 'UNKNOWN'), ('new_sources', 1), ('results', []),
                     ('candidate_count', True)]
        for key, value in mutations:
            with self.subTest(key=key):
                candidate = partial()
                candidate[key] = value
                self.assertFalse(maintenance._safe_partial_checkpoint_recovery(candidate))
        for key, value in [('status', 'BLOCKED_ROLE'), ('lint_error_count', 1),
                           ('lint_warning_count', -1), ('dirty_tree_state', 'dirty'),
                           ('dirty_tree_write_blocked', True), ('exception', 'unknown')]:
            with self.subTest(key=key):
                candidate = partial()
                candidate['results'][-1][key] = value
                self.assertFalse(maintenance._safe_partial_checkpoint_recovery(candidate))
        for malformed in (True, '1', None):
            with self.subTest(lint_warning_count=malformed):
                candidate = partial()
                candidate['results'][0]['lint_warning_count'] = malformed
                self.assertFalse(maintenance._safe_partial_checkpoint_recovery(candidate))

    def test_unrelated_busy_drains_head_verified_batch_before_model(self):
        database = sqlite3.connect(':memory:')
        self.addCleanup(database.close)
        cfg = dict(self.base, project_slug='fixture-project')
        with self.batch_environment(database), \
                patch('caretaker.pubstate.record_start') as start, \
                patch.object(maintenance, '_invoke_curate', side_effect=StopBeforeModel) as invoke:
            def retained_drain(*args, **kwargs):
                self.assertTrue(kwargs['_require_committed_inputs'])
                return maintenance.publication_tick('fixture-config', cfg, self.control, database,
                    _require_committed_inputs=kwargs['_require_committed_inputs'],
                    _hub_preflight_state={'result': self.passed})
            with self.tick_environment(partial()), \
                    patch.object(maintenance, 'publication_drain', side_effect=retained_drain):
                with self.assertRaises(StopBeforeModel):
                    maintenance._locked_tick(copy.deepcopy(self.control), self.state)
            start.assert_called_once()
            invoke.assert_called_once()  # mocked boundary; no model runs

    def test_not_head_frozen_batch_is_rejected_before_record_start(self):
        database = sqlite3.connect(':memory:')
        self.addCleanup(database.close)
        cfg = dict(self.base, project_slug='fixture-project')
        with self.batch_environment(database) as frozen, \
                patch('caretaker.pubstate.record_start') as start, \
                patch.object(maintenance, '_invoke_curate') as invoke:
            body = b'uncheckpointed synthetic change'
            (self.vault / 'raw/original.md').write_bytes(body)
            import hashlib
            frozen['inputs'][2] = dict(path='raw/original.md', bytes=len(body),
                sha256=hashlib.sha256(body).hexdigest())
            result = maintenance.publication_tick('fixture-config', cfg, self.control, database,
                _require_committed_inputs=True, _hub_preflight_state={'result': self.passed})
            self.assertEqual(result['reason'], 'selected_inputs_not_in_head')
            self.assertEqual(result['model_calls'], 0)
            start.assert_not_called()
            invoke.assert_not_called()

    def test_partial_skips_source_discovery_and_backlog_ingest(self):
        self.control['sources'] = [{'kind': 'must-not-discover'}]
        with self.tick_environment(partial()), \
                patch.object(maintenance, 'publication_drain', return_value={'status': 'IDLE', 'model_calls': 0}) as drain, \
                patch('caretaker.backlog.pending_projects', side_effect=AssertionError('ingest backlog touched')):
            result = maintenance._locked_tick(self.control, self.state)
        self.assertEqual(result['new_events'], 0)
        self.assertEqual(result['projects'], [])
        self.assertTrue(drain.call_args.kwargs['_require_committed_inputs'])

    def test_budget_after_dual_lane_drain_keeps_ready_publication_results(self):
        self.control['new_event_epoch'] = {'enabled': True, 'activate': True}
        legacy = {'status': 'PUBLISHED', 'model_calls': 1,
                  'batches': [{'project': 'legacy-project', 'status': 'PUBLISHED'}]}
        new = {'status': 'PUBLISHED', 'model_calls': 1,
               'batches': [{'project': 'new-project', 'status': 'PUBLISHED'}]}
        budget = maintenance.tick_budget.TickBudget(1200, state=self.state)
        normal_check = maintenance.tick_budget.check

        def stop_before_discovery(phase=None, minimum=0):
            if phase == 'source_discovery':
                raise maintenance.tick_budget.TickBudgetExceeded('fixture budget boundary')
            return normal_check(phase, minimum)

        def drain_by_lane(*_args, **kwargs):
            return new if kwargs.get('lane') == 'new' else legacy

        with budget.activate():
            with self.tick_environment({'status': 'COMPLETED', 'new_sources': 0}), \
                    patch.object(maintenance, 'publication_drain', side_effect=drain_by_lane) as drain, \
                    patch('maintenance.tick_budget.check', side_effect=stop_before_discovery):
                with self.assertRaises(maintenance.tick_budget.TickBudgetExceeded):
                    maintenance._locked_tick(copy.deepcopy(self.control), self.state)
            result = budget.finish(budget.report, 'DEFERRED_BUDGET')

        self.assertEqual(result['status'], 'DEFERRED_BUDGET')
        self.assertEqual(result['publication'], legacy)
        self.assertEqual(result['new_lane']['publication'], new)
        self.assertEqual([call.kwargs['lane'] for call in drain.call_args_list],
                         ['legacy', 'new'])

    def test_unknown_partial_recovery_stops_before_drain(self):
        candidate = partial()
        candidate['results'][-1]['status'] = 'UNKNOWN'
        with self.tick_environment(candidate), patch.object(maintenance, 'publication_drain') as drain:
            result = maintenance._locked_tick(self.control, self.state)
        self.assertEqual(result['status'], 'DEFERRED_INTAKE_RECOVERY')
        drain.assert_not_called()

    def test_partial_still_requires_normal_dirty_lint_role_preflight(self):
        for reason in ('hub_lint_failed', 'hub_health_failed', 'hub_role_blocked'):
            with self.subTest(reason=reason), self.tick_environment(partial()), \
                    patch.object(maintenance, '_global_hub_preflight', return_value={
                        'status': 'DEFERRED_HUB_PREFLIGHT', 'reason': reason}), \
                    patch.object(maintenance, '_invoke_curate') as invoke:
                result = maintenance._locked_tick(copy.deepcopy(self.control), self.state)
                self.assertEqual(result['status'], 'DEFERRED_HUB_PREFLIGHT')
                self.assertEqual(result['publication']['model_calls'], 0)
                invoke.assert_not_called()

    def test_own_checkpoint_busy_retains_durable_pending_then_recovers(self):
        database = sqlite3.connect(self.state / 'intake.sqlite')
        ensure_pub_tables(database)
        publication = dict(status='PUBLISHED', event_id='fixture-event')
        value = dict(status='PUBLISHED', publication=publication, checkpoint={
            'status': 'PENDING', 'reason': 'checkpoint_lock_busy', 'receipt_dir': str(self.root)})
        database.execute('INSERT INTO publications(batch,status,failures,result) VALUES(?,?,?,?)',
                         ('fixture-batch', 'PUBLISHED', 0, json.dumps(value)))
        database.commit()
        database.close()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(maintenance, '_global_hub_preflight', return_value=self.passed))
            stack.enter_context(patch.object(maintenance, '_audit_summary', return_value={}))
            stack.enter_context(patch.object(maintenance, '_audit_fields', return_value={}))
            stack.enter_context(patch('curate_once.curator_lock', no_lock))
            stack.enter_context(patch('caretaker.audit.maybe_start_cycle', return_value=None))
            stack.enter_context(patch('caretaker.audit.cycle_projects', return_value=[]))
            stack.enter_context(patch.object(maintenance, '_prepared_priority_candidate', return_value=None))
            stack.enter_context(patch('caretaker.backlog.pending_projects', return_value=[]))
            invoke = stack.enter_context(patch.object(maintenance, '_invoke_curate'))
            checkpoint = stack.enter_context(patch('caretaker.checkpoint.checkpoint_publication', side_effect=[
                PublicationError('checkpoint_lock_busy'),
                {'status': 'CHECKPOINTED', 'head': 'a' * 40}]))
            with self.tick_environment(partial(only_busy=True)):
                first = maintenance._locked_tick(copy.deepcopy(self.control), self.state)
            self.assertEqual(first['publication']['status'], 'CHECKPOINT_PENDING')
            with contextlib.closing(sqlite3.connect(self.state / 'intake.sqlite')) as check:
                stored = json.loads(check.execute('SELECT result FROM publications').fetchone()[0])
                self.assertEqual(stored['checkpoint']['status'], 'PENDING')
            with self.tick_environment({'status': 'COMPLETED', 'new_sources': 0}):
                second = maintenance._locked_tick(copy.deepcopy(self.control), self.state)
            self.assertEqual(second['publication']['status'], 'IDLE')
            with contextlib.closing(sqlite3.connect(self.state / 'intake.sqlite')) as check:
                stored = json.loads(check.execute('SELECT result FROM publications').fetchone()[0])
                self.assertEqual(stored['checkpoint']['status'], 'CHECKPOINTED')
                self.assertTrue(stored['checkpoint']['completed_after_pending'])
            self.assertEqual(checkpoint.call_count, 2)
            for call in checkpoint.call_args_list:
                self.assertEqual(call.args, (publication, self.vault, self.root))
            invoke.assert_not_called()


if __name__ == '__main__':
    unittest.main()
