import copy
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import routing_advisor as advisor
from jev_client import JevError, PRICING_BASIS
from jev_policy import recommend


class NativeContextTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 26, 20, tzinfo=timezone.utc)
        self.row = dict(slug='gpt-6-sol', context_window=272000,
                        max_context_window=872000, effective_context_window_percent=95)
        self.cache = dict(fetched_at=self.now.isoformat(), models=[self.row], identity='must-stay-local')

    def test_effective_default_used_not_opt_in_maximum(self):
        self.assertEqual(advisor.codex_context_windows(self.cache, now=self.now), {'gpt-6-sol':258400})

    def test_stale_future_malformed_or_missing_evidence_stays_unknown(self):
        for stamp in ((self.now-timedelta(days=2)).isoformat(),
                      (self.now+timedelta(seconds=1)).isoformat(), 'invalid', None):
            self.assertEqual(advisor.codex_context_windows({**self.cache,'fetched_at':stamp}, now=self.now), {})
        for row in ({'slug':'gpt-6-sol'}, {**self.row,'context_window':True},
                    {**self.row,'effective_context_window_percent':0}):
            self.assertEqual(advisor.codex_context_windows({**self.cache,'models':[row]}, now=self.now), {})

    def test_duplicate_model_metadata_cannot_lend_larger_window(self):
        self.assertEqual(advisor.codex_context_windows({**self.cache,'models':
                         [self.row,{**self.row,'context_window':872000}]}, now=self.now), {})

    def test_missing_file_is_fail_open_without_guessing(self):
        with patch.object(advisor, 'load', side_effect=OSError):
            self.assertEqual(advisor.local_context_windows({'codex':{}}), {})
        with patch.object(advisor, 'load', side_effect=AssertionError('unneeded read')):
            self.assertEqual(advisor.local_context_windows({'claude':{}}), {})

    def test_snapshot_fills_only_missing_codex_context_and_sends_no_cache_identity(self):
        execution=dict(model_provider_id='openai', lifecycle='ACTIVE',tools=['filesystem-read'],
                       context_window_tokens=None)
        fleet=SimpleNamespace(routing_v2=Router)
        profile=dict(routing_enabled=True,advisor_billing='subscription',models={'high':'gpt-6-sol'},fit={})
        with patch.object(Router, 'resolve_execution_profile', return_value=execution):
            value=advisor.make_snapshot(fleet,{'codex':profile},{},{},{'mode':'advisory'},
                                         context_windows=advisor.codex_context_windows(self.cache,now=self.now))
            self.assertEqual(value['candidates'][0]['context_window_tokens'],258400)
            execution['context_window_tokens']=50000
            value=advisor.make_snapshot(fleet,{'codex':profile},{},{},{'mode':'advisory'},context_windows={'gpt-6-sol':258400})
            self.assertEqual(value['candidates'][0]['context_window_tokens'],50000)
        self.assertNotIn('must-stay-local',json.dumps(value))


class Router:
    class RoutingV2Error(ValueError):
        pass

    @staticmethod
    def quota_hard_gates(profile, quota):
        return [{'code': 'quota-not-exhausted', 'passed': not quota.get('availability_blocked', False)},
                {'code': 'pace-brake-clear', 'passed': not quota.get('pace_brake', {}).get('active', False)}]

    @staticmethod
    def resolve_execution_profile(agent, profile, **kwargs):
        return dict(model_provider_id='moonshot', lifecycle='ACTIVE',
                    tools=['filesystem-read', 'filesystem-write'], context_window_tokens=100000)


_ISOLATION = None
_TMP = None


def setUpModule():
    global _ISOLATION
    import os
    import tempfile
    from unittest import mock
    global _TMP
    _TMP = tempfile.TemporaryDirectory(prefix='routing-advisor-tests-')
    tmp = _TMP.name
    _ISOLATION = mock.patch.dict(os.environ, {
        'AGENT_FLEET_RUNS_DIR': os.path.join(tmp, 'runs'),
        'AGENT_MODEL_TIERS_INDEX': os.path.join(tmp, 'usage-index.json'),
        'AGENT_MODEL_TIERS_PICKS': os.path.join(tmp, 'picks.json')})
    _ISOLATION.start()


def tearDownModule():
    _ISOLATION.stop()
    _TMP.cleanup()


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.fleet = SimpleNamespace(routing_v2=Router)
        self.profiles = {'kimi': {'routing_enabled': True, 'advisor_billing': 'subscription',
                                 'models': {'low': 'k3', 'medium': 'k3'}, 'fit': {'implementation': 95}}}
        self.task = {'mode': 'implement', 'required_tools': ['filesystem-write']}

    def snapshot(self, quota=None, readiness=None):
        return advisor.make_snapshot(self.fleet, self.profiles, {'kimi': quota or {}}, readiness or {}, self.task)

    def test_tier_metadata_follows_rollback_switch_and_degrades_visibly(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {'AGENT_GRAPH_TIER_ROUTING': '0'}):
            row = self.snapshot()['candidates'][0]
            self.assertNotIn('tier', row)
        with mock.patch.dict(os.environ, {'AGENT_GRAPH_TIER_ROUTING': '1',
                                          'AGENT_MODEL_CATALOG': os.devnull + '-missing-catalog.json'}):
            result = self.snapshot()
            self.assertNotIn('tier', result['candidates'][0])
            self.assertTrue(any(str(d.get('reason', '')).startswith('MODEL_CATALOG_UNAVAILABLE')
                                for d in result['local_diagnostics']))

    def test_unknown_is_not_full_or_exhausted_and_modes_merge(self):
        result = self.snapshot()
        self.assertEqual(len(result['candidates']), 1)
        row = result['candidates'][0]
        self.assertIsNone(row['quota_remaining'])
        self.assertEqual(row['status'], 'unknown')
        self.assertEqual(set(row['modes']), {'implement', 'review', 'advisory'})
        self.assertAlmostEqual(row['fit']['implementation'], .95)

    def test_authoritative_exhaustion_survives_stale_numbers(self):
        row = self.snapshot({'availability_blocked': True, 'status': 'stale', 'weekly_remaining_fraction': 1})['candidates'][0]
        self.assertEqual(row['status'], 'exhausted')
        self.assertFalse(row['enabled'])
        self.assertIsNone(row['quota_remaining'])

    def test_brake_and_runtime_failure_disable_candidate(self):
        self.assertFalse(self.snapshot({'pace_brake': {'active': True}})['candidates'][0]['enabled'])
        self.assertFalse(self.snapshot(readiness={'kimi': {'ready': False}})['candidates'][0]['enabled'])

    def test_unknown_funding_not_assumed_subscription(self):
        self.profiles['kimi'].pop('advisor_billing')
        self.assertEqual(self.snapshot()['candidates'], [])

    def test_no_config_secret_notes_reach_snapshot(self):
        self.profiles['kimi']['credential_source'] = 'private-token'
        self.profiles['kimi']['fit']['note'] = 'customer-data'
        text = str(self.snapshot())
        self.assertNotIn('private-token', text)
        self.assertNotIn('customer-data', text)

    def test_no_invented_remaining_from_stale_quota(self):
        row = self.snapshot({'status': 'fresh', 'current': False, 'weekly_remaining_fraction': .9})['candidates'][0]
        self.assertIsNone(row['quota_remaining'])

    def test_candidate_identity_does_not_rotate_with_quota_observation(self):
        first = self.snapshot()['candidates'][0]['id']
        now = datetime.now(timezone.utc).isoformat()
        second = self.snapshot({'current': True, 'status': 'fresh',
                                'weekly_remaining_fraction': .5, 'observed_at': now})['candidates'][0]['id']
        self.assertEqual(first, second)

    def test_native_kimi_windows_are_not_lost_to_legacy_status(self):
        now = datetime.now(timezone.utc)
        quota = {'current': False, 'status': 'unknown', 'availability_verified': True,
                 'availability': {'observed_at': now.isoformat(), 'recheck_at': (now+timedelta(seconds=200)).isoformat(),
                                  'windows': [{'scope':'weekly','used':70,'limit':100}, {'scope':'short-window','used':20,'limit':100}]}}
        row = self.snapshot(quota)['candidates'][0]
        self.assertAlmostEqual(row['quota_remaining'], .3)
        quota['availability']['observed_at'] = (now-timedelta(seconds=600)).isoformat()
        self.assertIsNone(self.snapshot(quota)['candidates'][0]['quota_remaining'])

    def test_material_execution_profiles_get_distinct_ids_without_extra_architects(self):
        class DistinctRouter(Router):
            @staticmethod
            def resolve_execution_profile(agent, profile, **kwargs):
                mode = kwargs['mode']
                return dict(model_provider_id='moonshot', lifecycle='ACTIVE',
                            tools=['filesystem-read', 'filesystem-write'],
                            context_window_tokens=100000,
                            execution_profile_id='worker-' + mode,
                            runtime_model_id='k3', reasoning_level='medium',
                            model_class='luna', coding_rank=3)
        self.fleet.routing_v2 = DistinctRouter
        self.profiles['kimi']['execution_profiles'] = {'extra-architect': {'model': 'mythos-model'}}
        rows = self.snapshot()['candidates']
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({row['id'] for row in rows}), 3)
        self.assertEqual({row['model'] for row in rows}, {'k3'})
        self.assertEqual({row['model_class'] for row in rows}, {'luna'})
        self.assertEqual({row['coding_rank'] for row in rows}, {3})
        self.assertTrue(all('_execution_identity' not in row for row in rows))

    def test_invalid_native_metadata_stays_unknown(self):
        class InvalidMetadataRouter(Router):
            @staticmethod
            def resolve_execution_profile(agent, profile, **kwargs):
                return {**Router.resolve_execution_profile(agent, profile, **kwargs),
                        'model_class': 'made-up', 'coding_rank': True}
        self.fleet.routing_v2 = InvalidMetadataRouter
        row = self.snapshot()['candidates'][0]
        self.assertEqual(row['model_class'], 'unknown')
        self.assertIsNone(row['coding_rank'])


class BoundReportTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.created = (self.now - timedelta(seconds=1)).isoformat().replace('+00:00', 'Z')
        self.plan = {
            'version': 1, 'goal': 'Bound routing', 'planner': {'model': 'parent', 'source': 'task'},
            'workspace': str(Path.cwd().resolve()), 'max_parallel': 2,
            'requirements': ['one'], 'constraints': ['no deploy'],
            'tasks': [{'id': 'x', 'text': 'Implement task-bound behavior', 'covers': ['one'],
                       'scope': ['src/module.py'], 'resources': ['module-lock'],
                       'done_when': ['regression passes'], 'depends_on': [],
                       'maker': [{'agent': 'old', 'model': 'old', 'score': 50, 'reason': 'old'}],
                       'reviewer': [{'agent': 'other', 'model': 'other', 'score': 50, 'reason': 'old'}],
                       'checks': [['python', '-V']]}]}
        self.task = {'schema_version': 1, 'brief': 'Implement task-bound behavior',
                     'mode': 'implement', 'required_tools': ['filesystem-write'],
                     'scope': ['src/module.py'], 'resources': ['module-lock'], 'depends_on': [],
                     'estimated_context_tokens': 1000, 'domain': 'coding', 'high_risk': False}
        candidates = []
        for cid, lineage in (('a', 'vendor-a'), ('b', 'vendor-b')):
            candidates.append({'id': cid, 'agent': 'agent-' + cid, 'model': 'model-' + cid,
                               'lineage': lineage, 'enabled': True, 'training_allowed': False,
                               'billing': 'subscription', 'tools': ['filesystem-read', 'filesystem-write'],
                               'modes': ['implement', 'review'], 'fit': {'coding': .8},
                               'status': 'unknown', 'quota_remaining': None,
                               'quota_observed_at': None, 'quota_reset_at': None,
                               'context_window_tokens': 10000, 'capacity_remaining': 1})
        self.snapshot = {'schema_version': 1,
                         'captured_at': (self.now - timedelta(seconds=2)).isoformat().replace('+00:00', 'Z'),
                         'candidates': candidates, 'active_assignments': [], 'capacity': 2}

    def report(self, *, available=True):
        created_dt = datetime.fromisoformat(self.created.replace('Z', '+00:00'))
        if available:
            decision = {'model': 'typesafe/jev-1.13', 'provider': 'TypeSafe',
                        'decision_id': 'synthetic', 'difficulty': {'value': 'medium', 'confidence': .9},
                        'parallelism': {'value': 'sequential', 'confidence': .9},
                        'consultation': {'value': 'none', 'confidence': .9},
                        'fit': {cid: {'score': 3.5, 'confidence': .9} for cid in ('a', 'b')},
                        'usage': {'input_tokens': 10, 'output_tokens': 1, 'cost_usd': None},
                        'pricing_basis': dict(PRICING_BASIS)}
            recommendation = recommend(self.task, self.snapshot, decision, now=created_dt)
        else:
            decision = None
            recommendation = advisor.unavailable_recommendation(self.task, self.snapshot, now=created_dt)
        return {'schema_version': 1, 'created_at': self.created, 'decision': decision,
                'binding': advisor.binding_for(self.plan, 'x', self.task, self.snapshot),
                'consultation_status': 'AVAILABLE' if available else 'UNAVAILABLE',
                'consultation_error_code': None if available else 'transport_error',
                'recommendation': recommendation, 'execution': 'NOT_DISPATCHED'}

    def test_full_plan_task_and_candidate_binding(self):
        report = self.report()
        self.assertEqual(advisor.verify_report(report, self.plan, 'x', self.task,
                                               self.snapshot, now=self.now), report['recommendation'])
        plan_changes = [
            ('goal', 'another goal'), ('constraints', ['production allowed']),
            ('max_parallel', 3), ('workspace', str(Path.cwd().parent.resolve())),
        ]
        for key, value in plan_changes:
            changed = copy.deepcopy(self.plan)
            changed[key] = value
            with self.subTest(field=key), self.assertRaises(ValueError):
                advisor.verify_report(report, changed, 'x', self.task, self.snapshot, now=self.now)
        for key, value in (('text', 'Different work'), ('checks', [['python', '-m', 'pytest']]),
                           ('done_when', ['different acceptance']), ('scope', ['src/other.py']),
                           ('resources', ['other-lock'])):
            changed = copy.deepcopy(self.plan)
            changed['tasks'][0][key] = value
            with self.subTest(node=key), self.assertRaises(ValueError):
                advisor.verify_report(report, changed, 'x', self.task, self.snapshot, now=self.now)
        changed_task = {**self.task, 'brief': 'Different minimized intent'}
        with self.assertRaises(ValueError):
            advisor.verify_report(report, self.plan, 'x', changed_task, self.snapshot, now=self.now)
        changed_snapshot = copy.deepcopy(self.snapshot)
        changed_snapshot['candidates'][0]['training_allowed'] = True
        with self.assertRaises(ValueError):
            advisor.verify_report(report, self.plan, 'x', self.task, changed_snapshot, now=self.now)

    def test_legacy_and_edited_recommendations_are_rejected(self):
        report = self.report()
        for edit in ('binding', 'maker', 'reviewer', 'decision'):
            changed = copy.deepcopy(report)
            if edit == 'binding':
                changed.pop('binding')
            elif edit == 'decision':
                changed['decision']['fit']['a']['score'] = 0
            else:
                changed['recommendation'][edit][0]['agent'] = 'forged'
            with self.subTest(edit=edit), self.assertRaises((ValueError, JevError)):
                advisor.verify_report(changed, self.plan, 'x', self.task, self.snapshot, now=self.now)

    def test_old_and_future_report_or_snapshot_refused(self):
        for field, offset in (('created_at', -301), ('created_at', 1),
                              ('captured_at', -301), ('captured_at', 1)):
            changed = self.report()
            if field == 'created_at':
                changed[field] = (self.now + timedelta(seconds=offset)).isoformat().replace('+00:00', 'Z')
            else:
                changed_snapshot = copy.deepcopy(self.snapshot)
                changed_snapshot[field] = (self.now + timedelta(seconds=offset)).isoformat().replace('+00:00', 'Z')
                changed['binding'] = advisor.binding_for(self.plan, 'x', self.task, changed_snapshot)
            with self.subTest(field=field, offset=offset), self.assertRaises(ValueError):
                advisor.verify_report(changed, self.plan, 'x', self.task,
                                      changed_snapshot if field == 'captured_at' else self.snapshot,
                                      now=self.now)

    def test_cli_reuse_is_call_free_and_exactly_bound(self):
        report = self.report()
        sources = {'task': self.task, 'snapshot': self.snapshot, 'todo': self.plan, 'report': report}
        output = io.StringIO()
        with patch.object(advisor, 'load', side_effect=sources.__getitem__), \
             patch.object(advisor, 'write_new') as write, \
             patch('jev_client.request_decision') as request, \
             redirect_stdout(output):
            rc = advisor.main(['advise', '--task', 'task', '--snapshot', 'snapshot',
                               '--todo', 'todo', '--task-id', 'x', '--reuse-report', 'report',
                               '--output', 'routing-test-output.json'])
        self.assertEqual(rc, 0)
        request.assert_not_called()
        write.assert_called_once_with('routing-test-output.json', report)
        self.assertEqual(json.loads(output.getvalue())['status'], 'WRITTEN')

    def test_cli_transport_failure_writes_explicit_unavailable_report(self):
        sources = {'task': self.task, 'snapshot': self.snapshot, 'todo': self.plan}
        output = io.StringIO()
        with patch.object(advisor, 'load', side_effect=sources.__getitem__), \
             patch.object(advisor, 'write_new') as write, \
             patch('jev_client.request_decision', side_effect=JevError('transport_error')) as request, \
             patch.object(advisor.os.environ, 'get', return_value='synthetic'), \
             redirect_stdout(output):
            rc = advisor.main(['advise', '--task', 'task', '--snapshot', 'snapshot',
                               '--todo', 'todo', '--task-id', 'x', '--max-cost-usd', '0.002',
                               '--output', 'routing-test-output.json'])
        self.assertEqual(rc, 1)
        request.assert_called_once()
        result = write.call_args.args[1]
        self.assertEqual(result['consultation_status'], 'UNAVAILABLE')
        self.assertTrue(result['recommendation']['requires_parent_review'])
        self.assertIn('JEV_UNAVAILABLE', result['recommendation']['requires_parent_review_reasons'])
        self.assertIsNone(result['decision'])
        self.assertEqual(json.loads(output.getvalue())['status'], 'UNAVAILABLE')

    def test_cli_malformed_model_result_cannot_be_silent_success(self):
        sources = {'task': self.task, 'snapshot': self.snapshot, 'todo': self.plan}
        with patch.object(advisor, 'load', side_effect=sources.__getitem__), \
             patch.object(advisor, 'write_new') as write, \
             patch('jev_client.request_decision', return_value={}), \
             patch.object(advisor.os.environ, 'get', return_value='synthetic'), \
             redirect_stdout(io.StringIO()):
            rc = advisor.main(['advise', '--task', 'task', '--snapshot', 'snapshot',
                               '--todo', 'todo', '--task-id', 'x', '--max-cost-usd', '0.002',
                               '--output', 'routing-test-output.json'])
        self.assertEqual(rc, 1)
        self.assertEqual(write.call_args.args[1]['consultation_error_code'], 'invalid_response')

    def test_cli_draft_preserves_intent_and_rejects_stale_even_with_review_note(self):
        report = self.report()
        sources = {'task': self.task, 'snapshot': self.snapshot, 'todo': self.plan, 'report': report}
        output = io.StringIO()
        with patch.object(advisor, 'load', side_effect=sources.__getitem__), \
             patch.object(advisor, 'write_new') as write, redirect_stdout(output):
            rc = advisor.main(['draft-todo', '--task', 'task', '--snapshot', 'snapshot',
                               '--todo', 'todo', '--task-id', 'x', '--report', 'report',
                               '--output', 'routing-test-output.json'])
        self.assertEqual(rc, 0)
        draft = write.call_args.args[1]
        self.assertEqual(draft['goal'], self.plan['goal'])
        self.assertEqual(draft['constraints'], self.plan['constraints'])
        self.assertEqual(draft['max_parallel'], self.plan['max_parallel'])
        for field in ('text', 'checks', 'scope', 'resources', 'depends_on', 'done_when'):
            self.assertEqual(draft['tasks'][0][field], self.plan['tasks'][0][field])

        stale = {**report, 'created_at': (self.now - timedelta(seconds=301)).isoformat().replace('+00:00', 'Z')}
        sources['report'] = stale
        with patch.object(advisor, 'load', side_effect=sources.__getitem__), \
             patch.object(advisor, 'write_new') as write, redirect_stdout(io.StringIO()):
            rc = advisor.main(['draft-todo', '--task', 'task', '--snapshot', 'snapshot',
                               '--todo', 'todo', '--task-id', 'x', '--report', 'report',
                               '--parent-review-note', 'I reviewed this',
                               '--output', 'routing-test-output.json'])
        self.assertEqual(rc, 2)
        write.assert_not_called()

    def test_cli_reuse_refuses_changed_inputs_without_a_call(self):
        report = self.report()
        cases = [
            {'todo': {**self.plan, 'goal': 'changed'}},
            {'task': {**self.task, 'brief': 'changed'}},
            {'snapshot': {**self.snapshot, 'capacity': 3}},
            {'report': {**report, 'created_at': (self.now - timedelta(seconds=301)).isoformat().replace('+00:00', 'Z')}},
        ]
        for changed in cases:
            sources = {'task': self.task, 'snapshot': self.snapshot, 'todo': self.plan, 'report': report}
            sources.update(changed)
            with self.subTest(changed=list(changed)), \
                 patch.object(advisor, 'load', side_effect=sources.__getitem__), \
                 patch.object(advisor, 'write_new') as write, \
                 patch('jev_client.request_decision') as request, \
                 redirect_stdout(io.StringIO()):
                rc = advisor.main(['advise', '--task', 'task', '--snapshot', 'snapshot',
                                   '--todo', 'todo', '--task-id', 'x', '--reuse-report', 'report',
                                   '--output', 'routing-test-output.json'])
                self.assertEqual(rc, 2)
                write.assert_not_called()
                request.assert_not_called()


class ApplyTests(unittest.TestCase):
    def test_only_assignments_change(self):
        plan = {'max_parallel': 1, 'tasks': [{'id': 'x', 'scope': ['a'], 'depends_on': ['y'], 'checks': [['test']], 'approvals': ['owner'], 'text': 'original'}]}
        original = copy.deepcopy(plan)
        c = {'agent': 'kimi', 'model': 'k3', 'score': 90, 'reason': 'FIT', 'lineage': 'moonshot'}
        report = {'requires_parent_review': False, 'maker': [c], 'reviewer': [{**c, 'agent': 'opencode', 'model': 'glm', 'lineage': 'zai'}]}
        result = advisor.apply_advice(plan, 'x', report)
        self.assertEqual(plan, original)
        self.assertEqual(result['max_parallel'], 1)
        for key, value in original['tasks'][0].items():
            self.assertEqual(result['tasks'][0][key], value)

    def test_low_confidence_cannot_apply(self):
        with self.assertRaisesRegex(ValueError, 'PARENT_REVIEW_REQUIRED'):
            advisor.apply_advice({}, 'x', {'requires_parent_review': True})

    def test_secret_and_absolute_path_guard(self):
        for text in ['Bearer ' + 'x'*30, 'api_key='+'x'*30, r'C:\Users\private\data.txt']:
            with self.assertRaises(ValueError):
                advisor.guard_metadata({'brief': text})
        advisor.guard_metadata({'brief': 'Explain how authentication works; no keys provided.'})



class SnapshotPoolTests(unittest.TestCase):
    PROFILES = {
        'codex': {'routing_enabled': True}, 'claude': {'routing_enabled': True},
        'muse': {'routing_enabled': True}, 'grok': {'routing_enabled': True},
        'qwen-local': {'routing_enabled': False},
    }

    def test_default_pool_is_every_routing_enabled_profile(self):
        for requested in (None, [], ['all']):
            chosen, omitted = advisor.select_profiles(self.PROFILES, requested)
            self.assertEqual(chosen, ['claude', 'codex', 'grok', 'muse'])
            self.assertEqual(omitted, [])

    def test_parent_subset_records_each_omitted_enabled_profile(self):
        chosen, omitted = advisor.select_profiles(self.PROFILES, ['codex', 'claude', 'codex'])
        self.assertEqual(chosen, ['codex', 'claude'])
        self.assertEqual(omitted, [{'agent': 'grok', 'reason': 'NOT_SELECTED_BY_PARENT'},
                                   {'agent': 'muse', 'reason': 'NOT_SELECTED_BY_PARENT'}])

    def test_disabled_profile_is_never_reported_as_omitted_but_may_be_explicit(self):
        chosen, omitted = advisor.select_profiles(self.PROFILES, ['qwen-local'])
        self.assertEqual(chosen, ['qwen-local'])
        self.assertNotIn('qwen-local', [row['agent'] for row in omitted])

    def test_unknown_or_oversized_selection_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'INVALID_AGENT_SELECTION'):
            advisor.select_profiles(self.PROFILES, ['nope'])
        many = {f'a{i}': {'routing_enabled': True} for i in range(advisor.MAX_SNAPSHOT_AGENTS + 1)}
        with self.assertRaisesRegex(ValueError, 'INVALID_AGENT_SELECTION'):
            advisor.select_profiles(many, None)
        with self.assertRaisesRegex(ValueError, 'INVALID_AGENT_SELECTION'):
            advisor.select_profiles({'x': {'routing_enabled': False}}, None)

    def test_omission_diagnostics_never_reach_the_public_snapshot(self):
        snapshot = {'captured_at': 'now', 'candidates': [], 'active_assignments': [], 'capacity': 1,
                    'local_diagnostics': [{'agent': 'muse', 'reason': 'NOT_SELECTED_BY_PARENT'}]}
        self.assertNotIn('local_diagnostics', advisor.public_snapshot(snapshot))


class SnapshotCliBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.allowed = Path.cwd() / 'allowed-beam-test-workspace'
        self.outside = Path.cwd() / 'outside-beam-test-workspace'
        self.task_path = 'routing-advisor-test-task.json'
        self.task = {'mode': 'implement', 'required_tools': ['filesystem-write']}
        self.funding_path = Path(__file__).resolve().parents[1] / 'references' / 'jev-funding.json'
        self.output_number = 0
        self.funding = json.loads(self.funding_path.read_text(encoding='utf-8'))
        self.assertEqual(self.funding.get('opencode-beam'), 'api')

    def run_snapshot(self, workspace, *, trial_status='active'):
        boundary = {'synthetic_only': True, 'workspace_root': str(self.allowed)}
        profiles = {
            'opencode-beam': {'routing_enabled': True, 'adapter': 'opencode',
                              'enabled_providers': ['reflection'], 'trial_status': trial_status,
                              'data_boundary': boundary, 'models': {'high': 'beam-model'}, 'fit': {}},
            'kimi': {'routing_enabled': True, 'models': {'low': 'k3'}, 'fit': {}},
        }
        fleet_state = {'quota_agents': None, 'readiness_calls': 0, 'boundary_calls': []}

        class SnapshotFleet:
            DEFAULT_PROFILES = {}
            routing_v2 = Router
            FleetError = RuntimeError

            @staticmethod
            def load_profiles(_defaults):
                return profiles

            @staticmethod
            def is_beam_profile(profile):
                return profile.get('adapter') == 'opencode'

            @staticmethod
            def qualify_beam_workspace(agent, profile, candidate_workspace):
                if profile.get('trial_status') != 'active':
                    raise RuntimeError('expired trial')
                SnapshotFleet.validate_profile_workspace_boundary(profile, candidate_workspace)

            @staticmethod
            def validate_profile_workspace_boundary(profile, candidate_workspace):
                fleet_state['boundary_calls'].append(profile.get('adapter'))
                if Path(candidate_workspace).resolve() != self.allowed.resolve():
                    raise RuntimeError('outside workspace')
                return Path(candidate_workspace).resolve()

            @staticmethod
            def state_root(_value):
                return Path.cwd()

            @staticmethod
            def current_quota_views(_root, selected_profiles):
                fleet_state['quota_agents'] = set(selected_profiles)
                return {}, None

            @staticmethod
            def live_readiness(selected_profiles, *, cwd):
                fleet_state['readiness_calls'] += 1
                return {}, None

        self.output_number += 1
        output_path = f'routing-advisor-test-snapshot-{self.output_number}.json'
        sources = {str(self.task_path): self.task, str(self.funding_path): self.funding}
        with patch.dict('sys.modules', {'todo_fleet': SimpleNamespace(load_fleet=lambda: SnapshotFleet)}), \
             patch.dict(os.environ, {'AGENT_GRAPH_TIER_ROUTING': '0'}), \
             patch.object(advisor.Path, 'exists', return_value=False), \
             patch.object(advisor, 'load', side_effect=lambda path: sources[str(path)]), \
             patch.object(advisor, 'write_new') as write, \
             redirect_stdout(io.StringIO()):
            rc = advisor.main(['snapshot', '--task', str(self.task_path),
                               '--funding', str(self.funding_path),
                               '--workspace', str(workspace), '--output', str(output_path)])
        self.assertEqual(rc, 0)
        return write.call_args.args[1], fleet_state

    def test_contextual_snapshot_admits_beam_in_workspace_without_readiness_and_keeps_other_profiles(self):
        result, state = self.run_snapshot(self.allowed)
        rows = result['candidates']
        self.assertTrue(any(row['agent'] == 'opencode-beam' and row['enabled'] for row in rows))
        self.assertTrue(any(row['agent'] == 'kimi' and row['enabled'] for row in rows))
        self.assertEqual(state['quota_agents'], {'opencode-beam', 'kimi'})
        self.assertEqual(state['readiness_calls'], 0)
        self.assertEqual(state['boundary_calls'], ['opencode'])

    def test_contextual_snapshot_omits_outside_expired_or_malformed_beam_before_quota_and_readiness(self):
        for workspace, trial_status in ((self.outside, 'active'), (self.allowed, 'expired'),
                                        (self.allowed, 'malformed')):
            with self.subTest(workspace=workspace.name, trial_status=trial_status):
                result, state = self.run_snapshot(workspace, trial_status=trial_status)
                self.assertFalse(any(row['agent'] == 'opencode-beam' for row in result['candidates']))
                self.assertTrue(any(row.get('agent') == 'opencode-beam'
                                    and row.get('reason') == 'WORKSPACE_OR_TRIAL_NOT_ADMITTED'
                                    for row in result['local_diagnostics']))
                self.assertTrue(any(row['agent'] == 'kimi' and row['enabled']
                                    for row in result['candidates']))
                self.assertEqual(state['quota_agents'], {'kimi'})
                self.assertEqual(state['readiness_calls'], 0)


if __name__ == '__main__':
    unittest.main()
