"""Normal ticks rotate their full budget across publication and intake families."""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import maintenance
from caretaker import tick_budget


TEST_TEMP = Path(os.environ.get(
    'KH_PRIVATE_TEST_TEMP',
    str(Path(__file__).resolve().parents[1] / '.test-tmp'),
))
TEST_TEMP.mkdir(parents=True, exist_ok=True)


class MaintenanceFamilyRotationTests(unittest.TestCase):
    def _root(self):
        return Path(tempfile.mkdtemp(prefix='family-rotation-', dir=TEST_TEMP))

    def test_three_normal_ticks_rotate_full_budget_without_editing_control(self):
        root = self._root()
        state = root / 'state'
        control = {
            'state_dir': str(state),
            'max_tick_seconds': 1200,
            'sources': [],
            'new_event_epoch': {'enabled': True, 'activate': False,
                                'source_ids': ['synthetic-source']},
            'publication': {'enabled': True,
                            'include_existing_hub_backlog': True,
                            'allowed_projects': ['synthetic-project']},
        }
        path = root / 'control.json'
        original_bytes = json.dumps(control, ensure_ascii=False, indent=2).encode('utf-8')
        path.write_bytes(original_bytes)
        original = json.loads(json.dumps(control))
        observed = []

        def simulated_locked_tick(runtime_control, runtime_state):
            runtime = json.loads(json.dumps(runtime_control))
            observed.append(runtime)
            publication = runtime['publication']
            if not publication['enabled']:
                families = ['source_intake']
            elif publication['include_existing_hub_backlog']:
                # Model an immutable legacy backlog consuming the whole tick.
                raise tick_budget.TickBudgetExceeded('synthetic_legacy_budget_exhausted')
            else:
                families = ['new_publication', 'source_intake']
            return {'status': 'OK', 'families': families}

        with patch('maintenance._locked_tick', side_effect=simulated_locked_tick):
            first = maintenance.tick(path)
            second = maintenance.tick(path)
            third = maintenance.tick(path)

        self.assertEqual(first['status'], 'DEFERRED_BUDGET')
        self.assertEqual(first['work_stage'], 'publication_family:legacy_publication')
        self.assertEqual(first['runtime_deferred'], ['new_publication', 'source_intake'])
        self.assertEqual(second['work_stage'], 'publication_family:new_publication')
        self.assertEqual(second['families'], ['new_publication', 'source_intake'])
        self.assertEqual(second['runtime_deferred'], ['legacy_publication', 'source_intake'])
        self.assertEqual(third['work_stage'], 'publication_family:source_intake')
        self.assertEqual(third['families'], ['source_intake'])
        self.assertEqual(third['runtime_deferred'], ['legacy_publication', 'new_publication'])

        expected_legacy = json.loads(json.dumps(original))
        expected_legacy['_runtime_work_family'] = 'legacy_publication'
        self.assertEqual(observed[0], expected_legacy)
        expected_new = json.loads(json.dumps(original))
        expected_new['publication']['include_existing_hub_backlog'] = False
        expected_new['_runtime_work_family'] = 'new_publication'
        self.assertEqual(observed[1], expected_new)
        expected_source = json.loads(json.dumps(original))
        expected_source['publication']['enabled'] = False
        expected_source['_runtime_work_family'] = 'source_intake'
        self.assertEqual(observed[2], expected_source)
        self.assertEqual(path.read_bytes(), original_bytes)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['new_event_epoch'],
                         original['new_event_epoch'])
        self.assertFalse(any('owner_held' in report for report in (first, second, third)))
        cursor = json.loads((state / 'publication_family_rotation.json').read_text(encoding='utf-8'))
        self.assertEqual(cursor, {'schema': 1, 'next_slot': 0})

    def test_rotation_stays_off_when_either_lane_is_disabled(self):
        for disabled in ('publication', 'no_active_publication_family'):
            with self.subTest(disabled=disabled):
                root = self._root()
                state = root / 'state'
                control = {
                    'state_dir': str(state),
                    'max_tick_seconds': 1200,
                    'sources': [],
                    'new_event_epoch': {'enabled': disabled == 'publication'},
                    'publication': {
                        'enabled': disabled != 'publication',
                        'include_existing_hub_backlog': disabled != 'no_active_publication_family',
                    },
                }
                path = root / 'control.json'
                original_bytes = json.dumps(control, indent=2).encode('utf-8')
                path.write_bytes(original_bytes)
                observed = []
                with patch('maintenance._locked_tick',
                           side_effect=lambda c, _s: observed.append(json.loads(json.dumps(c))) or {'status': 'OK'}):
                    report = maintenance.tick(path)
                self.assertNotIn('work_stage', report)
                self.assertEqual(observed, [control])
                self.assertEqual(path.read_bytes(), original_bytes)
                self.assertFalse((state / 'publication_family_rotation.json').exists())

    def test_legacy_only_control_rotates_to_source_intake_after_one_budget(self):
        root = self._root()
        state = root / 'state'
        control = {
            'state_dir': str(state), 'max_tick_seconds': 1200, 'sources': [],
            'new_event_epoch': {'enabled': False},
            'publication': {'enabled': True, 'include_existing_hub_backlog': True},
        }
        path = root / 'control.json'
        original_bytes = json.dumps(control, indent=2).encode('utf-8')
        path.write_bytes(original_bytes)
        observed = []

        def simulate(runtime_control, _state):
            observed.append(json.loads(json.dumps(runtime_control)))
            if runtime_control['publication']['enabled']:
                raise tick_budget.TickBudgetExceeded('synthetic_legacy_budget_exhausted')
            return {'status': 'OK', 'families': ['source_intake']}

        with patch('maintenance._locked_tick', side_effect=simulate):
            first = maintenance.tick(path)
            second = maintenance.tick(path)
        self.assertEqual(first['work_stage'], 'publication_family:legacy_publication')
        self.assertEqual(first['status'], 'DEFERRED_BUDGET')
        self.assertEqual(second['work_stage'], 'publication_family:source_intake')
        self.assertEqual(second['families'], ['source_intake'])
        expected = json.loads(json.dumps(control))
        expected['_runtime_work_family'] = 'source_intake'
        expected['publication']['enabled'] = False
        self.assertEqual(observed[1], expected)
        self.assertEqual(path.read_bytes(), original_bytes)
        cursor = json.loads((state / 'publication_family_rotation.json').read_text(encoding='utf-8'))
        self.assertEqual(cursor, {'schema': 1, 'next_slot': 0})

    def test_scheduled_legacy_deferral_is_narrow_and_keeps_real_errors(self):
        held = {'status': 'DEGRADED', 'reason': 'legacy_backlog_held',
                'raw_eligible': 3, 'pending_queue': 2}
        scheduled = maintenance._scheduled_legacy_publication(held, 'new_publication')
        self.assertEqual(scheduled['status'], 'DEFERRED_SCHEDULED')
        self.assertEqual(scheduled['reason'], 'legacy_publication_deferred_by_family_rotation')
        self.assertTrue(scheduled['scheduled'])
        self.assertEqual(held['status'], 'DEGRADED')
        actual_error = {'status': 'DEGRADED', 'reason': 'legacy_audit_failed'}
        self.assertEqual(maintenance._scheduled_legacy_publication(
            actual_error, 'new_publication'), actual_error)
        owner_hold = {'status': 'DEGRADED', 'reason': 'legacy_backlog_held'}
        self.assertEqual(maintenance._scheduled_legacy_publication(
            owner_hold, None), owner_hold)

    def test_prepared_priority_is_one_attempt_per_exact_phase_and_scope(self):
        database = sqlite3.connect(':memory:')
        self.addCleanup(database.close)
        database.execute('CREATE TABLE publications(batch TEXT,status TEXT,result TEXT,next_due TEXT,updated_at TEXT)')
        member = {'proposal_id': 'prop-one', 'path': 'proposals/one.md',
                  'sha256': 'b' * 64, 'project': 'allowed-project', 'status': 'pending'}
        cycle = {'id': 'cycle-one', 'status': 'ACTIVE', 'members': [member]}
        result = {
            'status': 'PREPARED_DEFERRED', 'pre_effect': True,
            'cycle_id': 'cycle-one', 'batch': 'batch-one',
            'project': 'allowed-project', 'phase': 'PREPARED',
            'manifest_sha256': 'a' * 64, 'maker_calls': 1, 'review_calls': 0,
            'proposal_ids': ['prop-one'], 'paths': ['proposals/one.md'],
            'hashes': ['b' * 64],
        }

        def insert(batch, status='PREPARED_DEFERRED', payload=None, next_due=''):
            payload = dict(payload or result, batch=batch)
            database.execute('INSERT INTO publications VALUES(?,?,?,?,?)',
                             (batch, status, json.dumps(payload), next_due, '2026-10-22T00:00:00+00:00'))

        insert('batch-one')
        policy = {'projects': ['allowed-project'], 'held_projects': []}
        expected = 'PREPARED|' + ('a' * 64) + '|1|0'
        self.assertEqual(
            maintenance._prepared_priority_candidate(
                database, cycle, ['allowed-project'], policy, '2026-10-22T12:00:00+00:00'),
            ('allowed-project', expected),
        )

        # A seen phase, changed cycle, disallowed scope, malformed pre-effect
        # result, or cooldown must not receive another priority attempt.
        seen = dict(result, prepare_priority_seen_key=expected)
        database.execute('UPDATE publications SET result=? WHERE batch=?',
                         (json.dumps(seen), 'batch-one'))
        insert('seen', payload=seen)
        wrong_cycle = dict(result, cycle_id='other-cycle')
        insert('wrong-cycle', payload=wrong_cycle)
        insert('cooldown', payload=dict(result), next_due='2026-10-23T00:00:00+00:00')
        self.assertIsNone(maintenance._prepared_priority_candidate(
            database, cycle, ['allowed-project'], policy, '2026-10-22T12:00:00+00:00'))
        self.assertIsNone(maintenance._prepared_priority_candidate(
            database, cycle, ['allowed-project'], {'projects': ['other-project']},
            '2026-10-22T12:00:00+00:00'))
        wrong_status = dict(result, status='UNKNOWN')
        insert('malformed', payload=wrong_status)
        self.assertIsNone(maintenance._prepared_priority_candidate(
            database, cycle, ['allowed-project'], policy, '2026-10-22T12:00:00+00:00'))

    def _prepared_drain(self, *, no_progress=False, reject=None, repeat_budget=False, repair=False):
        import datetime as dt
        from caretaker.audit import active_cycle, policy_from
        from caretaker.publication import sha
        from caretaker.pubstate import ensure_pub_tables
        from tests.test_phase_resume import PhaseResumeTests, Reviewer

        fixture = PhaseResumeTests('test_prepared_candidate_resumes_at_review_and_publishes_without_second_maker')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.cfg.project_slug = 'new-slug'
        fixture.reviewer = Reviewer(([('REPAIR', [{'class': 'unsupported_claim', 'weight': 'blocking', 'text': 'Synthetic claim needs repair.'}])] if repair else []) + ['timeout', 'PASS'])
        expected_makers = 2 if repair else 1
        reviews_before = 2 if repair else 1
        state_root = fixture.state
        fixture.state = state_root / 'projects' / 'new-slug' / 'state'
        fixture.state.mkdir(parents=True, exist_ok=True)
        fixture.cfg.state_dir = fixture.state

        # A second complete independent project stays behind the first in the
        # frozen cycle, so the drain's ordinary rotation points there next.
        source_dir = fixture.vault / 'raw/sources/2026/09/src-two'
        source_dir.mkdir(parents=True)
        source_bytes = b'Second synthetic independent source.'
        (source_dir / 'original.md').write_bytes(source_bytes)
        (source_dir / 'source.json').write_text(json.dumps({
            'source_id': 'src-two', 'classification': 'internal',
            'local_only': False, 'original_file': 'original.md',
            'sha256': sha(source_bytes), 'byte_length': len(source_bytes),
        }), encoding='utf-8')
        second_path = 'proposals/agent/two.md'
        second_proposal = fixture.vault / second_path
        second_proposal.parent.mkdir(parents=True, exist_ok=True)
        second_proposal.write_text(
            '---\nid: prop-two\nproject_slug: z-other\nclassification: internal\n'
            'source_ids:\n  - src-two\ntarget_pages:\n  - wiki/projects/z-other.md\n'
            '---\nIndependent second proposal.\n', encoding='utf-8')
        registry = fixture.vault / 'wiki/registry.md'
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text('```json\n' + json.dumps({
            'schema_version': 1,
            'projects': {
                'new-slug': {'main_page': 'wiki/projects/new-slug.md'},
                'z-other': {'main_page': 'wiki/projects/z-other.md'},
            },
            'aliases': {},
        }) + '\n```\n', encoding='utf-8')

        first_bytes = (fixture.vault / fixture.paths[0]).read_bytes()
        members = [
            {'proposal_id': 'prop-one', 'path': fixture.paths[0],
             'sha256': sha(first_bytes), 'project': 'new-slug',
             'producer': 'codex', 'episode': '', 'source_ids': ['src-one']},
            {'proposal_id': 'prop-two', 'path': second_path,
             'sha256': sha(second_proposal.read_bytes()), 'project': 'z-other',
             'producer': 'codex', 'episode': '', 'source_ids': ['src-two']},
        ]
        policy = {
            'enabled': True, 'include_existing_hub_backlog': True,
            'projects': ['new-slug', 'z-other'], 'held_projects': [],
            'min_ready_groups': 1, 'max_wait_hours': 24, 'max_batch': 8,
            'max_batches_per_tick': 1,
        }
        summary = {
            'trigger': 'threshold', 'eligible': members,
            'raw_eligible': 2, 'group_count': 2, 'ready_group_count': 2,
            'oldest_age_hours': 48, 'deferred': 0, 'unsupported': 0,
            'policy': policy_from(policy),
        }
        database = sqlite3.connect(':memory:')
        self.addCleanup(database.close)
        ensure_pub_tables(database)
        now = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.timezone.utc)
        base = {'hub': {'vault': str(fixture.vault)}, 'project_slug': 'new-slug'}
        runtime_cfg = {'hub': {'vault': str(fixture.vault)},
                       'project_slug': 'new-slug', 'state_dir': str(fixture.state)}

        visited = []
        def project_cfg(_base, project, _state, **_kwargs):
            visited.append(project)
            config = dict(runtime_cfg, project_slug=project)
            return 'synthetic-config', config

        defer_again = [False]
        def invoke(_config, _paths):
            if visited[-1] != 'new-slug':
                return {'status': 'BLOCKED', 'reason': 'synthetic_unrelated_block', 'model_calls': 0}
            if defer_again[0]:
                with patch('curate_once._phase_timeout', side_effect=tick_budget.TickBudgetExceeded('tick_time_budget_exhausted')):
                    return fixture._run()
            from caretaker.publication import PublicationError
            try:
                return fixture._run()
            except PublicationError as exc:
                return {'status': 'BLOCKED', 'reason': str(exc), 'model_calls': 0}

        with patch.object(maintenance, '_audit_summary', return_value=summary), \
             patch.object(maintenance, '_project_runtime_cfg', side_effect=project_cfg), \
             patch.object(maintenance, '_invoke_curate',
                          side_effect=invoke):
            first = maintenance.publication_drain(
                {'publication': policy, 'known_projects': ['new-slug', 'z-other'],
                 'max_projects_per_tick': 1}, base, database, state_root,
                now=now, recovery={'status': 'NO_LOCK'},
            )
            cycle = active_cycle(database)
            self.assertIsNotNone(cycle)
            self.assertEqual(first['batches'][0]['project'], 'new-slug')
            self.assertEqual(first['batches'][0]['status'], 'DEFERRED_PREPARED')
            self.assertEqual(fixture.maker.calls, expected_makers)
            current_cycle = active_cycle(database)
            priority_row = database.execute(
                "SELECT batch,status,result,next_due,updated_at FROM publications WHERE status='PREPARED_DEFERRED'"
            ).fetchall()
            selected_priority = maintenance._prepared_priority_candidate(
                database, current_cycle, ['new-slug', 'z-other'], policy,
                (now + dt.timedelta(seconds=1)).isoformat(),
                state_root,
            )
            self.assertIsNotNone(selected_priority,
                                 msg=json.dumps({'cycle': current_cycle, 'rows': priority_row}, default=str))
            phase_path = fixture._phase_path()
            original_phase_bytes = phase_path.read_bytes()
            phase_record = json.loads(original_phase_bytes)
            self.assertEqual(selected_priority[1],
                             maintenance._prepared_priority_phase_key(dict(phase_record['progress'], manifest_sha256=phase_record['manifest_sha256'])))

            # This cache was written through the real curate_once phase writer.
            # Tampering its integrity digest must remove priority eligibility.
            phase_record['manifest_sha256'] = 'c' * 64
            phase_path.write_text(json.dumps(phase_record), encoding='utf-8')
            self.assertIsNone(maintenance._prepared_priority_candidate(
                database, current_cycle, ['new-slug', 'z-other'], policy,
                (now + dt.timedelta(seconds=1)).isoformat(), state_root,
            ))
            phase_path.write_bytes(original_phase_bytes)

            if no_progress:
                # Real curator catches a cooperative pre-review budget stop;
                # the retained manifest and maker/reviewer counts do not move.
                with patch('curate_once._phase_timeout', side_effect=tick_budget.TickBudgetExceeded('tick_time_budget_exhausted')):
                    deferred = maintenance.publication_drain(
                        {'publication': policy, 'known_projects': ['new-slug', 'z-other'],
                         'max_projects_per_tick': 1}, base, database, state_root,
                        now=now + dt.timedelta(seconds=1), recovery={'status': 'NO_LOCK'})
                self.assertEqual(deferred['batches'][0]['status'], 'DEFERRED_PREPARED', deferred)
                raw = database.execute("SELECT batch,result FROM publications WHERE status='PREPARED_DEFERRED'").fetchone()
                stored = json.loads(raw[1])
                self.assertEqual(stored['reason'], 'tick_time_budget_exhausted')
                self.assertEqual(stored['model_calls'], 0)
                self.assertEqual(stored['prepare_priority_seen_key'], selected_priority[1])
                self.assertEqual(phase_path.read_bytes(), original_phase_bytes)
                self.assertEqual(fixture.maker.calls, expected_makers)
                self.assertEqual(fixture.reviewer.calls, reviews_before)
                if reject:
                    if reject in ('cache_changed', 'native_started'):
                        record = json.loads(phase_path.read_text())
                        if reject == 'cache_changed':
                            record['manifest_sha256'] = 'c' * 64
                        else:
                            import curate_once
                            record['progress']['status'] = 'RUNNING'
                            record['cache_sha256'] = curate_once._phase_digest(record)
                        phase_path.write_text(json.dumps(record))
                    elif reject == 'source_changed':
                        source = next((fixture.vault / 'raw/sources').rglob('original.md'))
                        source.write_bytes(source.read_bytes() + b' changed')
                    elif reject == 'target_changed':
                        target = fixture.vault / 'wiki/projects/new-slug.md'
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text('Changed target preimage.', encoding='utf-8')
                    else:
                        key, value = reject
                        stored[key] = value
                        database.execute('UPDATE publications SET result=? WHERE batch=?', (json.dumps(stored), raw[0]))
                        database.commit()

            # The ordinary rotation now points to z-other. The exact
            # pre-effect phase gets one priority resume ahead of that slot.
            defer_again[0] = repeat_budget
            second = maintenance.publication_drain(
                {'publication': policy, 'known_projects': ['new-slug', 'z-other'],
                 'max_projects_per_tick': 1}, base, database, state_root,
                now=now + dt.timedelta(seconds=1), recovery={'status': 'NO_LOCK'},
            )
            if repeat_budget:
                self.assertEqual(second['batches'][0]['project'], 'new-slug')
                self.assertEqual(second['batches'][0]['status'], 'DEFERRED_PREPARED')
                self.assertEqual(phase_path.read_bytes(), original_phase_bytes)
                defer_again[0] = False
                ordinary = maintenance.publication_drain(
                    {'publication': policy, 'known_projects': ['new-slug', 'z-other'],
                     'max_projects_per_tick': 1}, base, database, state_root,
                    now=now + dt.timedelta(seconds=2), recovery={'status': 'NO_LOCK'})
                self.assertEqual(ordinary['batches'][0]['project'], 'z-other', ordinary)
                self.assertEqual(fixture.maker.calls, expected_makers)
                self.assertEqual(fixture.reviewer.calls, reviews_before)
                second = maintenance.publication_drain(
                    {'publication': policy, 'known_projects': ['new-slug', 'z-other'],
                     'max_projects_per_tick': 1}, base, database, state_root,
                    now=now + dt.timedelta(seconds=3), recovery={'status': 'NO_LOCK'})

        if reject:
            if reject in ('source_changed', 'target_changed'):
                self.assertFalse(any(b['status'] == 'PUBLISHED' for b in second['batches']), second)
            else:
                self.assertEqual(second['batches'][0]['project'], 'z-other', second)
            self.assertEqual(fixture.maker.calls, expected_makers)
            self.assertEqual(fixture.reviewer.calls, reviews_before)
            return
        self.assertEqual(second['batches'][0]['project'], 'new-slug')
        self.assertEqual(second['batches'][0]['status'], 'PUBLISHED')
        self.assertEqual(fixture.maker.calls, expected_makers)
        self.assertEqual(fixture.reviewer.calls, reviews_before + 1)
        cycle = active_cycle(database)
        if repeat_budget:
            self.assertIsNone(cycle)  # ordinary blocked member plus published prepared member drained it
            return
        self.assertIsNotNone(cycle)
        statuses = {member['proposal_id']: member['status'] for member in cycle['members']}
        self.assertEqual(statuses, {'prop-one': 'published', 'prop-two': 'pending'})


    def test_publication_drain_resumes_prepared_project_before_rotated_fresh_project(self):
        self._prepared_drain()

    def test_budget_no_progress_does_not_spend_retained_priority(self):
        self._prepared_drain(no_progress=True)

    def test_repair_prepared_budget_no_progress_reviews_without_third_maker(self):
        self._prepared_drain(no_progress=True, repair=True)

    def test_repeated_budget_only_priority_yields_alternate_single_slot(self):
        self._prepared_drain(no_progress=True, repeat_budget=True)

    def test_budget_priority_alternates_without_starving_either_ordinary_pool(self):
        from tests.test_frozen_project_scheduling import FrozenProjectSchedulingTests
        fixture = FrozenProjectSchedulingTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.seed(['frozen-a', 'frozen-b'], ['future-a', 'future-b'],
                     rotation=0, cap=1, batches=1, blocked=['frozen-a', 'frozen-b'])
        key = 'PREPARED|' + 'a' * 64 + '|1|0'
        def priority(*args, **kwargs):
            return ('frozen-b', key) if kwargs.get('allow_budget_retry') else None
        with patch.object(maintenance, '_prepared_priority_candidate', side_effect=priority), \
             patch.object(maintenance, 'publication_tick', wraps=maintenance.publication_tick) as tick, \
             patch.object(maintenance, '_invoke_curate') as model:
            for _ in range(8):
                before = len(tick.call_args_list)
                fixture.drain()
                self.assertEqual(len(tick.call_args_list) - before, 1)
        visited = [call.args[1]['project_slug'] for call in tick.call_args_list]
        self.assertEqual(visited, ['frozen-a', 'frozen-b', 'future-a', 'frozen-b',
                                  'frozen-b', 'frozen-b', 'future-b', 'frozen-b'])
        model.assert_not_called()

    def test_seen_budget_priority_rejects_unknown_progress_effects_and_bad_cache(self):
        for change in (
            ('reason', 'native_timeout'), ('reason', 'runtime_outcome_unknown'),
            ('reason', 'generic_failure'), ('model_calls', 1), ('model_calls', None),
            ('model_calls', False), ('pre_effect', False), ('publication', {'status': 'PUBLISHED'}),
            ('checkpoint', {'status': 'PENDING'}), ('phase', 'REPAIR_STARTED'),
            ('prepare_priority_seen_key', 'malformed'), 'cache_changed', 'native_started',
            'source_changed', 'target_changed',
        ):
            with self.subTest(change=change):
                self._prepared_drain(no_progress=True, reject=change)


if __name__ == '__main__':
    unittest.main()
