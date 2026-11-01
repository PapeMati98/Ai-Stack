"""B2-B5 synthetic routing fixtures and explicit canaries. Providers are never called."""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import fleet
import native_availability as native
import quota_availability as qa
import quota_collectors
import routing_v2

INPUTS = Path(__file__).resolve().parent.parent / 'references/synthetic-routing-witnesses'

# Preserve inherited sandbox ACL for Python 3.13 synthetic temp directories.
if os.name == 'nt' and not getattr(os.mkdir, '_routing_fixture_acl', False):
    _mkdir = os.mkdir
    def fixture_mkdir(path, mode=0o777, *, dir_fd=None):
        if mode == 0o700 and Path(path).resolve().is_relative_to(Path(os.environ['TEAM_TEST_ROOT']).resolve()):
            mode = 0o777
        return _mkdir(path, mode, dir_fd=dir_fd)
    fixture_mkdir._routing_fixture_acl = True
    os.mkdir = fixture_mkdir


class RoutingAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='availability-b-', dir=os.environ['TEAM_TEST_ROOT']))
        self.now = int(time.time())
        self.profile = {'adapter': 'antigravity', 'quota_pool_id': 'antigravity'}
        self.profiles = {'antigravity': self.profile}

    def failure(self, age=10):
        return {'agent': 'antigravity', 'adapter': 'antigravity', 'model': 'gemini-3.8-flash-high',
                'run_status': 'FAILED', 'exit_code': 3, 'runtime_attestation': {'status': 'VERIFIED'},
                'started_at': qa.stamp(self.now - age - 2), 'completed_at': qa.stamp(self.now - age),
                'token_usage': {'status': 'known', 'total_tokens': 0, 'adapter': 'antigravity'},
                'provider_attempts': [{'exit_code': 3, 'terminal_reason': 'error',
                                       'transport_outcome': {'status': 'failed'},
                                       'token_usage': {'status': 'known', 'total_tokens': 0}}]}

    def success(self, age=10):
        row = self.failure(age)
        row.update(run_status='COMPLETED', exit_code=0,
                   model_identity={'status': 'verified', 'expected_model': row['model']})
        return row

    def import_rows(self, rows):
        qa.import_native_observations(self.root, self.profiles, rows,
                                      lock_factory=fleet.ReleasableKernelLock, now=self.now)
        return qa.read_cache(qa.cache_path(self.root, 'antigravity'))

    def test_B2_both_synthetic_failures_block_with_recheck_timestamps(self):
        rows = [json.loads((INPUTS / name).read_text(encoding='utf-8')) for name in (
            'failure-1.json', 'failure-2.json')]
        for index, row in enumerate(rows):
            age = 20 - index * 10
            row['started_at'] = qa.stamp(self.now - age - 2)
            row['completed_at'] = qa.stamp(self.now - age)
        original = copy.deepcopy(rows)
        state = self.import_rows(rows)
        self.assertEqual(state['status'], 'recheck-required')
        self.assertEqual(state['reason'], 'native-zero-token-failure')
        self.assertEqual(state['observed_at'], rows[1]['completed_at'])
        self.assertGreater(qa.seconds(state['recheck_at']), qa.seconds(state['observed_at']))
        self.assertIn('expires_at', state)
        self.assertEqual(rows, original)
        view = qa.overlay({'status': 'unknown'}, state)
        self.assertTrue(view['availability_blocked'])
        self.assertFalse(next(g['passed'] for g in routing_v2.quota_hard_gates({}, view)
                              if g['code'] == 'quota-not-exhausted'))

    def test_B2_only_known_zero_actual_native_failure_is_a_block(self):
        base = self.failure()
        variants = [{'provider_attempts': []}, {'token_usage': {'status': 'unknown', 'total_tokens': 0}},
                    {'token_usage': {'status': 'known', 'total_tokens': None}},
                    {'token_usage': {'status': 'known', 'total_tokens': 40}},
                    {'runtime_attestation': {'status': 'UNVERIFIED'}}, {'exit_code': 0},
                    {'run_status': 'BLOCKED'}, {'adapter': 'wrong'}, {'quota_pool_id': 'other'}]
        for changes in variants:
            with self.subTest(changes=changes):
                self.assertIsNone(qa.native_run_observation(dict(base, **changes), 'antigravity',
                                                          self.profile, now=self.now))
        self.assertEqual(qa.native_run_observation(base, 'antigravity', self.profile,
                                                  now=self.now)['status'], 'recheck-required')

    def test_B2_current_view_imports_existing_receipt_once_and_does_not_probe(self):
        row = self.failure()
        with patch.object(fleet, 'read_usage', return_value=[row]) as ledger, \
                patch.object(fleet.adaptive, 'latest_quotas', return_value={}), \
                patch.object(quota_collectors, 'collect_profile_quotas', return_value=({}, {})), \
                patch.object(native, 'native_probe') as probe:
            views, _ = fleet.current_quota_views(self.root, self.profiles)
            self.assertTrue(views['antigravity']['availability_blocked'])
            ledger.assert_called_once_with(self.root, days=35)
            probe.assert_not_called()
        state = qa.read_cache(qa.cache_path(self.root, 'antigravity'))
        self.assertEqual(state['reason'], 'native-zero-token-failure')
        before = qa.cache_path(self.root, 'antigravity').read_bytes()
        self.import_rows([row])
        self.assertEqual(qa.cache_path(self.root, 'antigravity').read_bytes(), before)

    def test_B2_no_probe_expiry_opens_after_fixed_backoff(self):
        state = self.import_rows([self.failure(age=1000)])
        result = qa.check(self.root, 'antigravity', {}, now=self.now,
                          lock_factory=fleet.ReleasableKernelLock)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['reason'], 'half-open-after-backoff')
        self.assertFalse(qa.overlay({}, result)['availability_blocked'])
        self.assertNotEqual(state['status'], 'exhausted')

    def test_B4_verified_success_survives_numeric_staleness_without_fake_quota(self):
        with patch.object(fleet, 'read_usage', return_value=[self.success()]), \
                patch.object(fleet.adaptive, 'latest_quotas', return_value={'antigravity': {'status': 'stale', 'headroom': None}}), \
                patch.object(quota_collectors, 'collect_profile_quotas', return_value=({}, {})):
            views, _ = fleet.current_quota_views(self.root, self.profiles)
        self.assertTrue(views['antigravity']['availability_verified'])
        self.assertEqual(views['antigravity']['availability']['source'], 'native-run')
        self.assertIsNone(views['antigravity']['headroom'])
        self.assertFalse(views['antigravity']['availability_blocked'])

    def test_B4_existing_protected_native_clients_count_without_faking_identity(self):
        for agent, model, provider in [('codex', 'gpt-6.1-sol', 'openai'), ('kimi', 'k3', 'moonshot')]:
            with self.subTest(agent=agent):
                profile = {'adapter': agent, 'quota_pool_id': agent}
                row = self.success()
                row.update(agent=agent, adapter=agent, model=model, model_provider_id=provider,
                           token_usage={'status': 'known', 'adapter': agent, 'total_tokens': 10},
                           provider_attempts=[], model_identity={'status': 'not-applicable',
                           'expected_model': model, 'observed_models': []})
                original = copy.deepcopy(row)
                observation = qa.native_run_observation(row, agent, profile, now=self.now)
                self.assertEqual(observation['status'], 'available')
                self.assertEqual(row, original)
                for changes in [
                    {'model_provider_id': 'wrong-provider'},
                    {'runtime_attestation': {'status': 'UNVERIFIED'}},
                    {'run_status': 'PARTIAL'},
                    {'model_identity': {'status': 'unavailable', 'expected_model': model}},
                    {'model_identity': {'status': 'not-applicable', 'expected_model': model, 'error': 'identity-error'}},
                    {'model_identity': {'status': 'not-applicable', 'expected_model': 'wrong'}},
                    {'model_identity': {'status': 'not-applicable', 'expected_model': model, 'observed_models': ['wrong']}},
                    {'model_identity': {'status': 'not-applicable', 'expected_model': model, 'observed_runtime_models': ['wrong']}},
                    {'started_at': qa.stamp(self.now - 86402), 'completed_at': qa.stamp(self.now - 86400)},
                ]:
                    self.assertIsNone(qa.native_run_observation(dict(row, **changes), agent, profile, now=self.now))
                wrong_adapter = dict(row, agent='grok', adapter='grok', model_provider_id='openai')
                self.assertIsNone(qa.native_run_observation(wrong_adapter, 'grok', {'adapter': 'grok'}, now=self.now))

    def test_B4_exact_24h_boundary_identity_and_pool_are_checked(self):
        for changes in ({'completed_at': qa.stamp(self.now - 86400), 'started_at': qa.stamp(self.now - 86402)},
                        {'model_identity': {'status': 'unavailable'}},
                        {'model_identity': {'status': 'verified', 'expected_model': 'wrong'}},
                        {'runtime_attestation': {}}, {'agent': 'other'}, {'quota_pool_id': 'other'}):
            self.assertIsNone(qa.native_run_observation(dict(self.success(), **changes), 'antigravity',
                                                      self.profile, now=self.now))
        self.import_rows([self.success(age=86399)])
        result = qa.check(self.root, 'antigravity', {}, now=self.now + 1,
                          lock_factory=fleet.ReleasableKernelLock)
        self.assertEqual(result['status'], 'unknown')

    def test_B4_concurrent_refusal_beats_later_completion_of_older_run(self):
        success = self.success(age=2)
        success['started_at'] = qa.stamp(self.now - 100)
        result = self.import_rows([self.failure(age=10), success])
        self.assertEqual(result['status'], 'recheck-required')
        qa.record_native_success(self.root, 'antigravity', lock_factory=fleet.ReleasableKernelLock,
                                 now=self.now, run_started_at=qa.stamp(self.now - 100))
        self.assertEqual(qa.read_cache(qa.cache_path(self.root, 'antigravity'))['status'], 'recheck-required')
        self.assertEqual(self.import_rows([self.success(age=1)])['status'], 'available')

    def test_B4_shared_pool_import_is_independent_of_profile_order(self):
        success = dict(self.success(age=2), agent='sibling')
        success['started_at'] = qa.stamp(self.now - 100)
        failure = self.failure(age=10)
        for agents in (('sibling', 'antigravity'), ('antigravity', 'sibling')):
            root = self.root / agents[0]
            qa.import_native_observations(root, {a: self.profile for a in agents},
                                          [success, failure], now=self.now,
                                          lock_factory=fleet.ReleasableKernelLock)
            self.assertEqual(qa.read_cache(qa.cache_path(root, 'antigravity'))['status'],
                             'recheck-required')

    def test_B4_delayed_refusal_persistence_beats_concurrent_success(self):
        qa.record_native_success(self.root, 'antigravity', now=self.now,
                                 run_started_at=qa.stamp(self.now - 100),
                                 lock_factory=fleet.ReleasableKernelLock)
        qa.record_rejection(self.root, 'antigravity', {
            'status': 'recheck-required', 'source': 'native-provider-attempt',
            'reason': 'native-zero-token-failure'}, now=self.now - 10,
            lock_factory=fleet.ReleasableKernelLock)
        self.assertEqual(qa.read_cache(qa.cache_path(self.root, 'antigravity'))['status'],
                         'recheck-required')

    def test_B4_legacy_cache_is_reconciled_with_exact_concurrent_ledger_proof(self):
        success = self.success(age=2)
        success['started_at'] = qa.stamp(self.now - 100)
        fleet.adaptive.atomic_write(qa.cache_path(self.root, 'antigravity'), {
            'schema_version': 1, 'status': 'available', 'source': 'native-run',
            'observed_at': success['completed_at'], 'recheck_at': qa.stamp(self.now + 900)})
        self.assertEqual(self.import_rows([success, self.failure(age=10)])['status'],
                         'recheck-required')

    def test_B2_malformed_metadata_is_not_native_evidence(self):
        for changes in ({'runtime_attestation': []}, {'token_usage': 'invalid'},
                        {'provider_attempts': [None]}, {'provider_attempts': [{'token_usage': []}]}):
            with self.subTest(changes=changes):
                self.assertIsNone(qa.native_run_observation(dict(self.failure(), **changes),
                                                          'antigravity', self.profile, now=self.now))
        self.assertIsNone(qa.native_run_observation(dict(self.success(), exit_code=False),
                                                  'antigravity', self.profile, now=self.now))

    def test_B5_positive_forged_ledger_cannot_clear_native_refusal(self):
        qa.record_rejection(self.root, 'pool', {'status': 'exhausted', 'scope': 'credits',
                                               'source': 'native-provider-error'},
                            lock_factory=fleet.ReleasableKernelLock, now=self.now)
        prior = {'source': 'local-token-ledger', 'status': 'fresh', 'authority': 'authoritative',
                 'headroom': 1.0, 'credits_remaining': 100, 'observed_at': qa.stamp(self.now + 1)}
        state = qa.check(self.root, 'pool', prior, now=self.now + 2,
                         lock_factory=fleet.ReleasableKernelLock)
        self.assertTrue(qa.overlay(prior, state)['availability_blocked'])
        self.assertEqual(state['source'], 'native-provider-error')

    def test_B5_local_prior_hard_block_is_advisory_but_real_cache_wins(self):
        prior = {'source': 'local-token-ledger', 'hard_blocking': True,
                 'headroom': 0, 'remaining_tokens': 0, 'authority': 'authoritative'}
        view = qa.overlay(prior, {'status': 'unknown'})
        self.assertFalse(view['hard_blocking'])
        self.assertIsNone(view['headroom'])
        self.assertEqual(view['estimated_remaining_tokens'], 0)
        view = qa.overlay(prior, {'status': 'exhausted', 'source': 'native-provider-error'})
        self.assertTrue(view['availability_blocked'])

    def test_B3_failed_recheck_has_complete_deadline_metadata(self):
        state = qa.check(self.root, 'pool', {}, force_recheck=True,
                         probe=lambda: {'status': 'unknown'}, now=self.now,
                         lock_factory=fleet.ReleasableKernelLock)
        self.assertEqual(state['status'], 'recheck-required')
        self.assertEqual(state['observed_at'], qa.stamp(self.now))
        self.assertEqual(state['expires_at'], state['recheck_at'])

    def test_B4_old_canary_is_unknown_without_another_model_call(self):
        path = qa.cache_path(self.root, 'pool')
        fleet.adaptive.atomic_write(path, {'schema_version': 1, 'status': 'available',
                                          'source': 'native-cli-check',
                                          'observed_at': qa.stamp(self.now - 10800),
                                          'recheck_at': qa.stamp(self.now - 10500)})
        state = qa.check(self.root, 'pool', {}, now=self.now, costly_probe=True,
                         probe=lambda: self.fail('healthy pool must not probe'),
                         lock_factory=fleet.ReleasableKernelLock)
        self.assertEqual(state['status'], 'unknown')

    def test_B5_every_local_ledger_is_advisory_even_forged_metadata(self):
        for metadata in ({}, {'capacity_source': 'inferred-guess'}, {'capacity_source': 'authoritative-provider'}):
            row = dict(source='local-token-ledger', status='fresh', authority='authoritative',
                       hard_blocking=True, availability_blocked=True, remaining_tokens=0,
                       headroom=0, reported_headroom=0, weekly_remaining_fraction=0, **metadata)
            normalized = quota_collectors.normalize_inferred_bailian_ledger(row)
            self.assertEqual(normalized['authority'], 'advisory')
            self.assertEqual(normalized['estimated_remaining_tokens'], 0)
            self.assertFalse(normalized['hard_blocking'])
            self.assertIsNone(normalized['headroom'])
            self.assertFalse(qa.is_exhaustion(row))
            self.assertFalse(qa.overlay({}, {'status': 'exhausted', 'source': 'local-token-ledger'})['availability_blocked'])

    def test_B5_cache_contamination_cannot_block_real_native_refusal_still_can(self):
        path = qa.cache_path(self.root, 'pool')
        fleet.adaptive.atomic_write(path, {'schema_version': 1, 'status': 'exhausted',
                                          'source': 'local-token-ledger', 'observed_at': qa.stamp(self.now)})
        result = qa.check(self.root, 'pool', {}, lock_factory=fleet.ReleasableKernelLock, now=self.now)
        self.assertFalse(qa.overlay({}, result)['availability_blocked'])
        qa.record_rejection(self.root, 'pool', {'status': 'exhausted', 'source': 'native-provider-error'},
                            lock_factory=fleet.ReleasableKernelLock, now=self.now + 1)
        result = qa.check(self.root, 'pool', {'source': 'local-token-ledger', 'authority': 'authoritative',
                                             'headroom': 0}, lock_factory=fleet.ReleasableKernelLock, now=self.now + 2)
        self.assertTrue(qa.overlay({}, result)['availability_blocked'])

    def codebuddy_output(self):
        return json.dumps([{'type': 'message', 'role': 'assistant',
                            'providerData': {'model': 'hy4-preview', 'requestModelId': 'hy4-preview'}},
                           {'type': 'result', 'subtype': 'success', 'result': 'OK'}])

    def muse_output(self):
        return '\n'.join(json.dumps(e) for e in [
            {'payload_type': 'run.lifecycle.started', 'payload': {'run_stream': 'probe'}},
            {'payload_type': 'run.model.configured', 'payload': {'model_id': 'muse-spark-1.3', 'provider_id': 'meta'}},
            {'payload_type': 'run.terminal.completed', 'payload': {'run_stream': 'probe', 'terminal': 'completed', 'text': 'OK'}}])

    def test_B3_direct_launch_one_call_zero_tools_and_exact_model(self):
        for adapter, model, raw in (('muse', 'muse-spark-1.3', self.muse_output()),
                                    ('codebuddy', 'hy4-preview', self.codebuddy_output())):
            profile = {'adapter': adapter, 'models': {'low': model}}
            with patch.object(fleet, 'resolve_executable', return_value='native.exe'), \
                    patch.object(fleet, 'require_muse_native_executable'), \
                    patch.object(fleet, 'profile_runtime_environment', return_value={}), \
                    patch.object(native.process_supervision, 'run', return_value={
                        'exit_code': 0, 'stdout': raw, 'stderr': '', 'model_tool_calls': 0}) as run:
                result = native.native_probe(fleet, profile, self.root)
            self.assertEqual(result['status'], 'available')
            run.assert_called_once()
            command = run.call_args.args[0]
            args = run.call_args.kwargs
            self.assertEqual(args['tool_limit'], 0)
            self.assertLessEqual(args['timeout'], 30)
            self.assertTrue(args['cwd'].is_relative_to(self.root))
            self.assertEqual(list(args['cwd'].glob('*')), [])
            self.assertEqual(command[command.index('--model') + 1], model)
            if adapter == 'codebuddy':
                self.assertEqual(command[command.index('--tools') + 1], '')
                self.assertIn('--strict-mcp-config', command)
            else:
                self.assertIn('--no-foreign-personal-context', command)
                self.assertIn('--disable-shell', command)
                self.assertEqual(command[command.index('--max-model-steps') + 1], '1')

    def test_B3_ambiguous_failure_and_tool_use_never_prove_available(self):
        raw = self.codebuddy_output()
        for changes in ({'exit_code': 3}, {'timed_out': True}, {'model_tool_calls': None},
                        {'model_tool_calls': False},
                        {'model_tool_calls': 1}, {'stdout': raw.replace('hy4-preview', 'other')},
                        {'stdout': raw.replace('OK', 'not OK')}, {'stdout': '{}'},
                        {'stdout': raw.replace('"role": "assistant"', '"role": "assistant", "tool_calls": [1]')}):
            with self.assertRaises((ValueError, native.process_supervision.ToolEventUnreadable)):
                native.completion(fleet, 'codebuddy', 'hy4-preview', dict(
                    {'exit_code': 0, 'stdout': raw, 'model_tool_calls': 0}, **changes))

    def test_B3_muse_run_tool_events_and_unknown_scan_do_not_clear_block(self):
        for kind in ('run.tool.started', 'run.tool.call.completed', 'run.permission.requested'):
            raw = self.muse_output() + '\n' + json.dumps({'payload_type': kind, 'payload': {}})
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                native.completion(fleet, 'muse', 'muse-spark-1.3',
                                  {'exit_code': 0, 'stdout': raw, 'model_tool_calls': 0})

    def test_B3_explicit_recheck_recovers_only_on_native_proof(self):
        qa.record_rejection(self.root, 'pool', {'status': 'recheck-required', 'reason': 'native-zero-token-failure',
                                               'clear_requires': 'native-cli-check'},
                            lock_factory=fleet.ReleasableKernelLock, now=self.now)
        result = qa.check(self.root, 'pool', {}, force_recheck=True,
                          probe=lambda: {'status': 'unknown'}, lock_factory=fleet.ReleasableKernelLock, now=self.now + 1)
        self.assertEqual(result['status'], 'recheck-required')
        self.assertGreater(qa.seconds(result['recheck_at']), self.now)
        result = qa.check(self.root, 'pool', {}, force_recheck=True,
                          probe=lambda: {'status': 'available', 'source': 'native-cli-check'},
                          lock_factory=fleet.ReleasableKernelLock, now=self.now + 2)
        self.assertEqual(result['status'], 'available')
        self.assertIsNone(qa.rejection('muse', 'native internal error', 1))
        self.assertIsNone(qa.rejection('codebuddy', 'native internal error', 3))
        self.assertEqual(qa.rejection('codebuddy', '429 Credits exhausted', 0)['status'], 'exhausted')

    def test_B3_healthy_muse_and_codebuddy_preflight_never_runs_a_probe(self):
        profiles = {'muse': {'adapter': 'muse'}, 'codebuddy': {'adapter': 'codebuddy'}}
        with patch.object(fleet, 'read_usage', return_value=[]), \
                patch.object(fleet.adaptive, 'latest_quotas', return_value={}), \
                patch.object(quota_collectors, 'collect_profile_quotas', return_value=({}, {})), \
                patch.object(native, 'native_probe') as probe:
            fleet.current_quota_views(self.root, profiles)
        probe.assert_not_called()
        self.assertEqual(fleet.build_parser().parse_args(['availability-recheck', '--agent', 'muse']).action,
                         'availability-recheck')

    def test_B3_explicit_Fleet_CLI_is_wired_without_execute_or_preflight(self):
        profile = {'adapter': 'muse', 'quota_pool_id': 'muse', 'models': {'low': 'muse-spark-1.3'}}
        with patch.object(fleet, 'state_root', return_value=self.root), \
                patch.object(fleet, 'load_profiles', return_value={'muse': profile}), \
                patch.object(native, 'native_probe', return_value={'status': 'available', 'source': 'native-cli-check'}) as probe, \
                patch.object(fleet, 'execute_single') as dispatch, patch.object(fleet, 'live_readiness') as ready, \
                patch.object(fleet, 'current_quota_views') as views:
            self.assertEqual(fleet.main(['availability-recheck', '--agent', 'muse']), 0)
        probe.assert_called_once()
        dispatch.assert_not_called()
        ready.assert_not_called()
        views.assert_not_called()


if __name__ == '__main__':
    unittest.main()
