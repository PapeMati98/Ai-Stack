import concurrent.futures
import json
import os
import multiprocessing
from pathlib import Path
import tempfile
import time
import tomllib
import unittest
from unittest.mock import patch

import fleet
import grok_availability
import kimi_usage
import quota_availability as qa
import routing_v2


def process_check(root, counter):
    def probe():
        with counter.get_lock():
            counter.value += 1
        time.sleep(.1)
        return {'status': 'available', 'source': 'native-cli-check'}
    qa.check(Path(root), 'process-pool', {}, probe=probe, lock_factory=fleet.ReleasableKernelLock)


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = 2000000000.0
        self.prior = {'status': 'stale', 'authority': 'authoritative', 'reported_headroom': 0.0,
                      'headroom': None, 'observed_at': qa.stamp(self.now - 3 * 86400)}

    def tearDown(self):
        self.temp.cleanup()

    def check(self, **kwargs):
        return qa.check(self.root, 'pool', self.prior, lock_factory=fleet.ReleasableKernelLock,
                        now=kwargs.pop('now', self.now), **kwargs)

    def test_stale_exhaustion_does_not_allow_content(self):
        # Stale numeric evidence blocks only for the fixed no-probe backoff.
        self.prior['observed_at'] = qa.stamp(self.now - 10)
        state = self.check()
        row = qa.overlay(self.prior, state)
        gates = routing_v2.quota_hard_gates({}, row)
        self.assertFalse(next(g['passed'] for g in gates if g['code'] == 'quota-not-exhausted'))

    def test_unknown_is_not_exhausted_for_uninstrumented_providers(self):
        self.prior = {'status': 'unknown'}
        self.assertFalse(qa.overlay(self.prior, self.check())['availability_blocked'])

    def test_verified_check_after_reset_clears_old_exhaustion(self):
        state = self.check(probe=lambda: {'status': 'available', 'source': 'native-cli-check'})
        row = qa.overlay(self.prior, state)
        self.assertFalse(row['availability_blocked'])
        self.assertTrue(all(g['passed'] for g in routing_v2.quota_hard_gates({}, row)))
        self.assertIsNone(row['headroom'])

    def test_expired_success_cannot_authorize_task_when_check_fails(self):
        self.check(probe=lambda: {'status': 'available', 'source': 'native-cli-check'})
        def fail():
            raise OSError('SECRET never persist')
        state = self.check(probe=fail, now=self.now + 301)
        self.assertTrue(qa.overlay(self.prior, state, require_check=True)['availability_blocked'])
        self.assertNotIn('SECRET', qa.cache_path(self.root, 'pool').read_text())

    def test_parallel_checks_are_coalesced(self):
        calls = []
        def probe():
            calls.append(1)
            time.sleep(.05)
            return {'status': 'exhausted', 'scope': 'monthly', 'source': 'native-provider-error'}
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            values = list(pool.map(lambda _: self.check(probe=probe), range(12)))
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(x['status'] == 'exhausted' for x in values))

    def test_denial_blocks_without_another_probe_until_retry(self):
        denial = qa.rejection('kimi', "error: failed to run prompt: provider.api_error: 403 You've reached your monthly usage limit", 1)
        qa.record_rejection(self.root, 'pool', denial, lock_factory=fleet.ReleasableKernelLock, now=self.now)
        def forbidden():
            self.fail('Do not query each task')
        self.assertEqual(self.check(probe=forbidden, now=self.now + 10)['status'], 'exhausted')

    def test_separate_cli_processes_share_one_check(self):
        context = multiprocessing.get_context('spawn')
        counter = context.Value('i', 0)
        processes = [context.Process(target=process_check, args=(str(self.root), counter)) for _ in range(4)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(15)
            if process.is_alive():
                process.terminate()
                process.join()
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(counter.value, 1)

    def test_live_view_excludes_kimi_but_keeps_alternative(self):
        self.prior['observed_at'] = qa.stamp(time.time() - 3 * 86400)
        profiles = {'kimi': {'adapter': 'kimi', 'quota_pool_id': 'kimi'}, 'grok': {'adapter': 'grok'}}
        with patch.object(fleet.adaptive, 'latest_quotas', return_value={'kimi': self.prior}), \
                patch.object(fleet.quota_collectors, 'collect_profile_quotas', return_value=({}, {})), \
                patch.object(kimi_usage, 'query', return_value={'status': 'exhausted', 'scope': 'monthly'}) as probe, \
                patch.object(kimi_usage, 'native_probe') as fallback:
            for _ in range(2):
                views, _ = fleet.current_quota_views(self.root, profiles)
                self.assertEqual(views['kimi']['availability']['scope'], 'monthly')
                eligible = [name for name in profiles if all(g['passed'] for g in routing_v2.quota_hard_gates(profiles[name], views[name]))]
                self.assertEqual(eligible, ['grok'])
            self.assertEqual(probe.call_count, 1)
            fallback.assert_not_called()

    def test_newer_available_snapshot_clears_generic_exhaustion(self):
        self.check()
        self.prior.update(status='fresh', headroom=.75, reported_headroom=.75, observed_at=qa.stamp(self.now + 1))
        state = self.check(now=self.now + 1)
        self.assertEqual(state['status'], 'available')
        self.assertEqual(qa.read_cache(qa.cache_path(self.root, 'pool'))['status'], 'available')
        self.prior.update(status='stale', headroom=None)
        state = self.check(now=self.now + 100)
        self.assertFalse(qa.overlay(self.prior, state)['availability_blocked'])

    def test_weekly_counter_cannot_clear_monthly_rejection(self):
        qa.record_rejection(self.root, 'pool', {'status': 'exhausted', 'scope': 'monthly'},
                            lock_factory=fleet.ReleasableKernelLock, now=self.now)
        state = self.check(now=self.now + 301, probe=lambda: {'status': 'available', 'covered_scopes': ['weekly']})
        self.assertEqual(state['status'], 'recheck-required')

    def test_uncovered_window_uses_one_native_fallback(self):
        calls = []
        def native():
            calls.append(1)
            return {'status': 'available', 'source': 'native-cli-check'}
        state = self.check(probe=lambda: {'status': 'available', 'covered_scopes': ['weekly']}, fallback=native)
        self.assertEqual(state['status'], 'available')
        self.assertEqual(calls, [1])

    def test_other_403_is_not_a_quota_rejection(self):
        for text in ('403 forbidden', 'error: failed to run prompt: provider.api_error: 403 unauthorized',
                     'Model says monthly usage limit', 'error: failed to run prompt: provider.api_error: 429 requests per minute'):
            self.assertIsNone(qa.rejection('kimi', text, 1))

    def test_usage_schema_and_windows(self):
        payload = {'usage': {'used': '25', 'limit': '100'},
                   'limits': [{'window': {'duration': 300, 'timeUnit': 'TIME_UNIT_MINUTE'},
                               'detail': {'used': '0', 'limit': '10'}}]}
        row = kimi_usage.parse_usage(payload)
        self.assertEqual(row['status'], 'available')
        self.assertEqual(row['covered_scopes'], ['short-window', 'weekly'])
        payload['limits'][0]['detail']['used'] = '10'
        self.assertEqual(kimi_usage.parse_usage(payload)['status'], 'exhausted')
        for bad in ({}, {'usage': {'used': 1}}, {'usage': {'used': 'NaN', 'limit': '100'}}):
            with self.assertRaises((ValueError, KeyError, TypeError)):
                kimi_usage.parse_usage(bad)

    def test_native_probe_is_isolated_and_has_no_task_content(self):
        def prepare(folder, **kwargs):
            home = folder / 'kimi-home'
            home.mkdir()
            (home / 'config.toml').write_text('default_model="k3"\n[models.k3]\nmodel="test"\n[background]\n')
            return home
        seen = []
        def run(command, **kwargs):
            seen.append(command)
            config = (Path(kwargs['env']['KIMI_CODE_HOME']) / 'config.toml').read_text()
            parsed = tomllib.loads(config)
            # Updated 2026-09-24: Kimi k3 reasoning now requires at least two steps
            # (one for chain-of-thought, one for the trivial reply). The probe still
            # denies every tool pattern, so a larger budget grants no new capability.
            self.assertGreaterEqual(parsed['loop_control']['max_steps_per_turn'], 2)
            # Updated 2026-09-24: this assertion used to pin the output budget to 16,
            # i.e. it locked in the bug. Measured live on Kimi CLI 0.43.1: 16 tokens
            # are spent entirely on reasoning (exit 1, "only thinking content"), while
            # 64 and above return the reply. The budget must clear that floor.
            self.assertGreaterEqual(parsed['models']['k3']['max_output_size'], 64)
            # `max_retries_per_step` is deprecated and ignored by CLI 0.43.1; only the
            # current key actually limits attempts.
            self.assertNotIn('max_retries_per_step', parsed['loop_control'])
            self.assertEqual(parsed['loop_control']['max_attempts_per_step'], 1)
            self.assertIn('pattern = "*"', config)
            self.assertIn('--skills-dir', command)
            self.assertEqual(kwargs['cwd'].parent, self.root / 'availability-probes')
            return {'exit_code': 1, 'stderr': "error: failed to run prompt: provider.api_error: 403 You've reached your monthly usage limit"}
        with patch.object(fleet, 'prepare_guarded_kimi_home', prepare), patch.object(fleet, 'run_bounded_child', run), patch.object(fleet, 'resolve_executable', return_value='kimi.exe'):
            row = kimi_usage.native_probe(fleet, {'models': {'low': 'k3'}}, self.root)
        self.assertEqual(row['scope'], 'monthly')
        self.assertEqual(len(seen), 1)

    def test_native_success_clears_without_inventing_remaining_quota(self):
        def prepare(folder, **kwargs):
            home = folder / 'kimi-home'
            home.mkdir()
            (home / 'config.toml').write_text('default_model="k3"\n[models.k3]\nmodel="test"\n[background]\n')
            return home
        result = {'exit_code': 0, 'timed_out': False,
                  'stdout': json.dumps({'type': 'usage.record', 'usage': {'input_tokens': 100, 'output_tokens': 1}})}
        with patch.object(fleet, 'prepare_guarded_kimi_home', prepare), \
                patch.object(fleet, 'run_bounded_child', return_value=result), \
                patch.object(fleet, 'resolve_executable', return_value='kimi.exe'):
            row = kimi_usage.native_probe(fleet, {'models': {'low': 'k3'}}, self.root)
        self.assertEqual(row['status'], 'available')
        self.assertEqual(row['probe_tokens'], 101)
        self.assertNotIn('headroom', row)

    @patch.dict(os.environ, {'AGENT_FLEET_DEPTH': '0'})
    def test_stale_negative_blocks_dispatch_before_materializing_context(self):
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        self.prior['observed_at'] = qa.stamp(time.time() - 3 * 86400)
        execution = routing_v2.resolve_execution_profile('kimi', profiles['kimi'], complexity='low', mode='review', required_tools=[])
        with patch.object(fleet, 'resolve_executable', return_value='kimi.exe'), \
                patch.object(fleet, 'probe_command', return_value={'launchable': True, 'exit_code': 0, 'output': 'ok'}), \
                patch.object(fleet, 'profile_credential_status', return_value={'ready': True}), \
                patch.object(routing_v2, 'readiness_from_probes', return_value={'ready': True, 'reasons': []}), \
                patch.object(fleet, 'merge_credential_readiness', return_value={'ready': True, 'reasons': []}), \
                patch.object(routing_v2, 'runtime_model_visible', return_value=(True, 'visible')), \
                patch.object(fleet.adaptive, 'latest_quotas', return_value={'kimi': self.prior}), \
                patch.object(fleet.quota_collectors, 'collect_profile_quotas', return_value=({}, {})), \
                patch.object(kimi_usage, 'query', return_value={'status': 'exhausted', 'scope': 'monthly'}), \
                patch.object(fleet, 'create_run_dir') as create_run, \
                patch.object(fleet, 'prepare_execution_workspace') as prepare_workspace, \
                patch.object(fleet, 'run_bounded_child') as child:
            with self.assertRaisesRegex(fleet.FleetError, 'quota-not-exhausted'):
                fleet.execute_single('PRIVATE TASK MUST NOT BE SENT', profiles=profiles, agent_id='kimi', mode='review',
                                     kind='review', complexity='low', model_override=None, workspace=self.root,
                                     timeout=60, allow_write=False, native_subagent_limit=0, enable_web=False,
                                     inherit_api_keys=False, dry_run=False, output_root=self.root / 'evidence',
                                     root=self.root, execution_profile=execution, knowledge_mode='none')
            create_run.assert_not_called()
            prepare_workspace.assert_not_called()
            child.assert_not_called()


CODEBUDDY_429 = (
    '429 Credits exhausted. Please visit the link below to purchase add-on packs '
    'and get more credits: https://www.codebuddy.ai/profile/usage  (run/attempt)'
)


class CodebuddyExhaustionTests(unittest.TestCase):
    """CodeBuddy exits 0 and prints the 429 on stderr, so exit code cannot gate it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def profile(self):
        return {
            'adapter': 'codebuddy',
            'quota_pool_id': 'codebuddy-pro',
            'version_args': ['--version'],
            'deep_probe_args': ['--model', '__fleet_readiness_probe__', '-p', 'probe'],
            'readiness': {
                'require_deep_probe': True,
                'minimum_version': '2.155.0',
                'required_deep_probe_substrings': ['hy4-preview'],
                'deny_patterns': ['not authenticated', '429 credits exhausted'],
            },
        }

    def test_zero_exit_credit_refusal_is_an_exhaustion(self):
        result = qa.rejection('codebuddy', CODEBUDDY_429, 0)
        self.assertEqual(result['status'], 'exhausted')
        self.assertEqual(result['scope'], 'credits')
        self.assertEqual(result['reason'], 'plan-credits-exhausted')

    def test_healthy_probe_output_is_not_an_exhaustion(self):
        self.assertIsNone(qa.rejection('codebuddy', 'available models: hy4-preview', 0))

    def test_model_prose_mentioning_credits_is_not_an_exhaustion(self):
        prose = 'The user said their 429 Credits exhausted message was confusing.'
        self.assertIsNone(qa.rejection('codebuddy', prose, 0))

    def test_other_adapters_keep_their_exit_code_gate(self):
        self.assertIsNone(qa.rejection('kimi', CODEBUDDY_429, 0))
        self.assertIsNone(qa.rejection('claude', CODEBUDDY_429, 0))

    def test_refusal_denies_readiness_with_an_entitlement_reason(self):
        row = routing_v2.readiness_from_probes(
            self.profile(),
            executable='codebuddy.cmd',
            version_probe={'launchable': True, 'exit_code': 0, 'output': '2.155.0'},
            deep_probe={'launchable': True, 'exit_code': 0, 'output': CODEBUDDY_429},
        )
        self.assertFalse(row['ready'])
        self.assertIn('authentication-or-entitlement-denied', row['reasons'])

    def test_readiness_probe_persists_availability_with_a_recheck(self):
        profiles = {'codebuddy': self.profile()}
        with patch.object(fleet, 'resolve_executable', return_value='codebuddy.cmd'), \
\
                patch.object(fleet, 'profile_probe_environment', return_value={}), \
                patch.object(fleet, 'version_probe_environment', return_value={}), \
                patch.object(fleet, 'probe_command', side_effect=[
                    {'launchable': True, 'exit_code': 0, 'output': '2.155.0'},
                    {'launchable': True, 'exit_code': 0, 'output': CODEBUDDY_429},
                ]):
            readiness, _ = fleet.live_readiness(profiles, cwd=self.root)
        self.assertFalse(readiness['codebuddy']['ready'])
        self.assertEqual(
            readiness['codebuddy']['provider_exhaustion']['status'], 'exhausted'
        )
        fleet.persist_readiness_exhaustion(self.root, profiles, readiness)
        cached = qa.read_cache(qa.cache_path(self.root, 'codebuddy-pro'))
        self.assertEqual(cached['status'], 'exhausted')
        self.assertEqual(cached['scope'], 'credits')
        self.assertTrue(cached['recheck_at'])

    def test_ready_provider_writes_no_availability_record(self):
        profiles = {'codebuddy': self.profile()}
        with patch.object(fleet, 'resolve_executable', return_value='codebuddy.cmd'), \
\
                patch.object(fleet, 'profile_probe_environment', return_value={}), \
                patch.object(fleet, 'version_probe_environment', return_value={}), \
                patch.object(fleet, 'probe_command', side_effect=[
                    {'launchable': True, 'exit_code': 0, 'output': '2.155.0'},
                    {'launchable': True, 'exit_code': 0, 'output': 'models: hy4-preview'},
                ]):
            readiness, _ = fleet.live_readiness(profiles, cwd=self.root)
        self.assertTrue(readiness['codebuddy']['ready'])
        self.assertNotIn('provider_exhaustion', readiness['codebuddy'])
        fleet.persist_readiness_exhaustion(self.root, profiles, readiness)
        self.assertFalse(qa.cache_path(self.root, 'codebuddy-pro').exists())


class GrokAvailabilityWiringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profile = {
            'adapter': 'grok',
            'quota_pool_id': 'grok-pool',
            'models': {'low': 'grok-4.7'},
        }
        self.profiles = {'grok': self.profile}

    def tearDown(self):
        self.temp.cleanup()

    def test_never_blocked_fresh_grok_makes_no_probe(self):
        with patch.object(fleet.adaptive, 'latest_quotas', return_value={'grok': {'status': 'fresh', 'headroom': 0.8}}), \
                patch.object(fleet.quota_collectors, 'collect_profile_quotas', return_value=({}, {})), \
                patch.object(grok_availability, 'native_probe') as probe:
            views, _ = fleet.current_quota_views(self.root, self.profiles)
            probe.assert_not_called()
            self.assertFalse(views['grok'].get('availability_blocked', False))

    def test_never_blocked_unknown_grok_makes_no_probe(self):
        with patch.object(fleet.adaptive, 'latest_quotas', return_value={'grok': {'status': 'unknown', 'headroom': None, 'confidence': 0.0}}), \
                patch.object(fleet.quota_collectors, 'collect_profile_quotas', return_value=({}, {})), \
                patch.object(grok_availability, 'native_probe') as probe:
            views, _ = fleet.current_quota_views(self.root, self.profiles)
            probe.assert_not_called()
            self.assertFalse(views['grok'].get('availability_blocked', False))

    def test_blocked_grok_rechecks_via_native_probe(self):
        qa.record_rejection(
            self.root, 'grok-pool',
            {'status': 'exhausted', 'scope': 'credits', 'source': 'native-provider-error'},
            lock_factory=fleet.ReleasableKernelLock, now=time.time() - 1000
        )
        with patch.object(fleet.adaptive, 'latest_quotas', return_value={'grok': {'status': 'unknown', 'headroom': None}}), \
                patch.object(fleet.quota_collectors, 'collect_profile_quotas', return_value=({}, {})), \
                patch.object(grok_availability, 'native_probe', return_value={
                    'status': 'available', 'source': 'native-cli-check', 'covered_scopes': ['credits']
                }) as probe:
            views, _ = fleet.current_quota_views(self.root, self.profiles)
            probe.assert_called_once()
            self.assertEqual(views['grok']['availability']['status'], 'available')
            self.assertFalse(views['grok']['availability_blocked'])

    def test_failed_grok_recheck_retains_block_without_native_proof(self):
        qa.record_rejection(
            self.root, 'grok-pool',
            {'status': 'exhausted', 'scope': 'credits', 'source': 'native-provider-error'},
            lock_factory=fleet.ReleasableKernelLock, now=time.time() - 1000
        )
        with patch.object(fleet.adaptive, 'latest_quotas', return_value={'grok': {'status': 'unknown', 'headroom': None}}), \
                patch.object(fleet.quota_collectors, 'collect_profile_quotas', return_value=({}, {})), \
                patch.object(grok_availability, 'native_probe', side_effect=ValueError('Grok probe failed')) as probe:
            views, _ = fleet.current_quota_views(self.root, self.profiles)
            probe.assert_called_once()
            # Expiry schedules a recheck; an ambiguous probe cannot restore
            # availability after an actual native provider refusal.
            self.assertTrue(views['grok']['availability_blocked'])
            self.assertEqual(views['grok']['availability']['status'], 'recheck-required')
            self.assertEqual(views['grok']['availability']['reason'], 'check-unavailable')

    def test_repeated_cached_grok_checks_coalesce(self):
        qa.record_rejection(
            self.root, 'grok-pool',
            {'status': 'exhausted', 'scope': 'credits', 'source': 'native-provider-error'},
            lock_factory=fleet.ReleasableKernelLock, now=time.time() - 1000
        )
        with patch.object(fleet.adaptive, 'latest_quotas', return_value={'grok': {'status': 'unknown', 'headroom': None}}), \
                patch.object(fleet.quota_collectors, 'collect_profile_quotas', return_value=({}, {})), \
                patch.object(grok_availability, 'native_probe', return_value={
                    'status': 'available', 'source': 'native-cli-check', 'covered_scopes': ['credits']
                }) as probe:
            views1, _ = fleet.current_quota_views(self.root, self.profiles)
            views2, _ = fleet.current_quota_views(self.root, self.profiles)
            probe.assert_called_once()
            self.assertEqual(views1['grok']['availability']['status'], 'available')
            self.assertEqual(views2['grok']['availability']['status'], 'available')


if __name__ == '__main__':
    unittest.main()
