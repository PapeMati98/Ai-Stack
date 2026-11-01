"""0.9.67: native reset evidence and bounded no-probe recovery, no providers."""
import copy
import contextlib
import json
import os
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch

import fleet
import quota_availability as qa
import routing_v2


class NoProbeExpiryTests(unittest.TestCase):
    def setUp(self):
        # Retain owned fixtures; normal mkdir also preserves inherited Windows
        # sandbox ACLs without depending on another test module's mkdir hook.
        self.root = Path(os.environ['TEAM_TEST_ROOT']) / ('no-probe-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.now = qa.seconds('2026-10-11T06:34:31Z')
        self.profile = {'adapter': 'antigravity', 'quota_pool_id': 'antigravity'}

    def stream(self, message, *, status='ERROR', field='error'):
        terminal = {'status': status, 'response': '', 'usage': {'total_tokens': 0}}
        terminal[field] = message
        return json.dumps({'event': 'result', 'result': terminal})

    def failure(self, stdout, *, stderr='', completed=None):
        completed = self.now if completed is None else completed
        projection = fleet.project_provider_handoff('antigravity', stdout, **({'stderr': stderr} if stderr else {}))
        return {'agent': 'antigravity', 'adapter': 'antigravity', 'model': 'gemini-3.8-flash-high',
                'run_status': 'FAILED', 'exit_code': 3, 'runtime_attestation': {'status': 'VERIFIED'},
                'started_at': qa.stamp(completed - 2), 'completed_at': qa.stamp(completed),
                'token_usage': {'status': 'known', 'total_tokens': 0},
                'provider_attempts': [{'exit_code': 3, 'terminal_reason': projection['terminal_reason'],
                                       'transport_outcome': {'status': 'failed'},
                                       'token_usage': {'status': 'known', 'total_tokens': 0},
                                       'quota_rejection': projection.get('quota_rejection')}]}

    def observe(self, row):
        return qa.native_run_observation(row, 'antigravity', self.profile, now=self.now)

    def check(self, when, *, prior=None, **kwargs):
        return qa.check(self.root, 'antigravity', prior or {}, now=when,
                        lock_factory=fleet.ReleasableKernelLock, **kwargs)

    def reject(self, *, observed=None, step=None, reset=None):
        observed = self.now if observed is None else observed
        result = {'status': 'recheck-required', 'source': 'native-provider-attempt',
                  'reason': 'native-zero-token-failure', 'scope': 'native',
                  'clear_requires': 'native-cli-check'}
        if reset is not None:
            result.update(status='exhausted', reason='usage-limit', scope='subscription',
                          reset_at=qa.stamp(reset))
            result.pop('clear_requires')
        value = qa.record_rejection(self.root, 'antigravity', result, now=observed,
                                    lock_factory=fleet.ReleasableKernelLock)
        if step is not None:
            value['block_step'] = step
            fleet.adaptive.atomic_write(qa.cache_path(self.root, 'antigravity'), value)
        return value

    def test_terminal_limit_duration_variants_use_run_completion(self):
        for duration, delta in [('14h11m56s', 51116), ('45m', 2700), ('2h', 7200),
                                ('1d3h', 97200), ('7s', 7), ('1d2h3m4s', 93784)]:
            for field in ('error', 'response'):
                with self.subTest(duration=duration, field=field):
                    row = self.failure(self.stream('Individual quota reached. Resets in ' + duration + '.', field=field))
                    original = copy.deepcopy(row)
                    observation = self.observe(row)
                    self.assertEqual(observation['status'], 'exhausted')
                    self.assertEqual(observation['reason'], 'usage-limit')
                    self.assertEqual(observation['scope'], 'subscription')
                    self.assertEqual(observation['source'], 'native-provider-attempt')
                    self.assertEqual(observation['reset_at'], qa.stamp(self.now + delta))
                    self.assertNotIn('clear_requires', observation)
                    self.assertEqual(row, original)

    def test_stderr_native_error_and_agy_error_use_same_strict_parser(self):
        message = 'Individual quota reached. Please upgrade your subscription. Resets in 14h11m56s.'
        structured = 'AGY_ERROR: ' + json.dumps({'short_error': 'RESOURCE_EXHAUSTED (code 429): ' + message,
                                                'status': 'RESOURCE_EXHAUSTED', 'error_code': 429})
        for stderr in ('Error: ' + message, structured, 'Error: ' + message + '\n' + structured):
            with self.subTest(stderr=stderr):
                observation = self.observe(self.failure(self.stream('Provider error'), stderr=stderr))
                self.assertEqual(observation['status'], 'exhausted')
                self.assertEqual(observation['reset_at'], qa.stamp(self.now + 51116))

    def test_stderr_only_terminal_refusal(self):
        row = self.failure('', stderr='Error: RESOURCE_EXHAUSTED (code 429): Resets in 45m.')
        self.assertEqual(self.observe(row)['reset_at'], qa.stamp(self.now + 2700))

    def test_conflicting_native_resets_do_not_invent_a_deadline(self):
        row = self.failure(self.stream('quota reached. Resets in 2h.'),
                           stderr='Error: quota reached. Resets in 45m.')
        self.assertEqual(self.observe(row)['status'], 'recheck-required')

    def test_no_reset_and_invalid_duration_keep_native_failure_semantics(self):
        for message in ('Individual quota reached.', 'HTTP 429: Resets in 2h.',
                        'RESOURCE_EXHAUSTED: Resets in 45m.'):
            with self.subTest(message=message):
                observation = self.observe(self.failure(self.stream(message)))
                self.assertEqual(observation['status'], 'recheck-required' if 'Resets in' not in message else 'exhausted')
        for duration in ('', '-2h', '1.5h', '2hgarbage', '45m2h', '2h 5m', '0s', '1w', '9' * 400 + 'd'):
            with self.subTest(duration=duration):
                observation = self.observe(self.failure(self.stream('Individual quota reached. Resets in ' + duration + '.')))
                self.assertEqual(observation['status'], 'recheck-required')
                self.assertEqual(observation['reason'], 'native-zero-token-failure')
                self.assertNotIn('reset_at', observation)

    def test_unrelated_or_successful_prose_cannot_supply_a_reset(self):
        for stdout, stderr in ((self.stream('Transport failed. Resets in 2h.'), ''),
                               (self.stream('quota reached'), 'Resets in 2h.'),
                               (self.stream('Provider error'), 'model says quota reached. Resets in 2h.'),
                               (self.stream('quota reached. Resets in 2h.', status='SUCCESS'), ''),
                               (json.dumps({'event': 'tool', 'error': 'quota reached. Resets in 2h.'}) + '\n'
                                + self.stream('Provider error'), ''),
                               (self.stream('Provider error'), 'AGY_ERROR: broken JSON')):
            with self.subTest(stdout=stdout, stderr=stderr):
                projection = fleet.project_provider_handoff('antigravity', stdout, stderr=stderr)
                self.assertIsNone(projection.get('quota_rejection'))

    def test_quota_metadata_needs_verified_zero_token_native_failure(self):
        row = self.failure(self.stream('quota reached. Resets in 2h.'))
        for changes in ({'token_usage': {'status': 'known', 'total_tokens': 1}},
                        {'token_usage': {'status': 'unknown', 'total_tokens': 0}},
                        {'runtime_attestation': {'status': 'UNVERIFIED'}},
                        {'exit_code': 0}, {'run_status': 'BLOCKED'}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.observe(dict(row, **changes)))

    def test_execute_single_persists_reset_from_native_stderr_in_run_and_cache(self):
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        message = 'Individual quota reached. Resets in 14h11m56s.'
        child = {'exit_code': 3, 'timed_out': False, 'stdout': self.stream('Provider error'),
                 'stderr': 'Error: ' + message, 'model_tool_calls': 0, 'tool_budget_exceeded': False}
        patches = [
            patch.dict(os.environ, {'AGENT_FLEET_DEPTH': '0', 'AGENT_FLEET_OFFICE': '0',
                                    'AGENT_FLEET_BILLING_MODE_PATH': str(self.root / 'missing.json')}),
            patch.object(fleet, 'resolve_executable', return_value='synthetic-agy.exe'),
            patch.object(fleet, 'probe_command', return_value={'launchable': True, 'exit_code': 0, 'output': 'ok'}),
            patch.object(fleet, 'profile_runtime_environment', return_value={}),
            patch.object(fleet, 'profile_credential_status', return_value={'ready': True}),
            patch.object(fleet, 'merge_credential_readiness', return_value={'ready': True, 'reasons': []}),
            patch.object(routing_v2, 'readiness_from_probes', return_value={'ready': True, 'reasons': []}),
            patch.object(routing_v2, 'runtime_model_visible', return_value=(True, 'visible')),
            patch.object(fleet, 'fleet_runtime_attestation', return_value={'status': 'VERIFIED'}),
            patch.object(fleet, 'read_usage', return_value=[]),
            patch.object(fleet.adaptive, 'latest_quotas', return_value={}),
            patch.object(fleet.quota_collectors, 'collect_profile_quotas', return_value=({}, {})),
            patch.object(fleet, 'utc_now', return_value=qa.stamp(self.now)),
            patch.object(qa.time, 'time', return_value=self.now),
        ]
        with contextlib.ExitStack() as stack:
            for mocked in patches:
                stack.enter_context(mocked)
            run = stack.enter_context(patch.object(fleet, 'run_provider_child', return_value={'child': child}))
            result = fleet.execute_single(
                'Synthetic quota refusal fixture.', profiles=profiles, agent_id='antigravity',
                mode='review', kind='review', complexity='low', model_override='gemini-3.8-flash-high',
                workspace=self.root, timeout=5, allow_write=False, native_subagent_limit=0, enable_web=False,
                inherit_api_keys=False, dry_run=False, output_root=self.root / 'evidence',
                root=self.root, knowledge_mode='none')
        run.assert_called_once()
        row = json.loads(Path(result['usage_record']).read_text(encoding='utf-8'))
        self.assertEqual(row['run_status'], 'FAILED')
        self.assertEqual(row['provider_attempts'][0]['quota_rejection']['reset_after_seconds'], 51116)
        observation = self.observe(row)
        self.assertEqual(observation['status'], 'exhausted')
        self.assertEqual(observation['reset_at'], qa.stamp(self.now + 51116))
        cache = qa.read_cache(qa.cache_path(self.root, str(profiles['antigravity'].get('quota_pool_id') or 'antigravity')))
        self.assertEqual(cache['status'], 'exhausted')
        self.assertEqual(cache['reset_at'], observation['reset_at'])

    def test_known_reset_stays_blocked_until_exact_reset_even_with_longer_legacy_deadline(self):
        reset = self.now + 2700
        value = self.reject(reset=reset, step=3)
        value['recheck_at'] = qa.stamp(reset + 60)
        fleet.adaptive.atomic_write(qa.cache_path(self.root, 'antigravity'), value)
        for when in (self.now, reset - 1):
            state = self.check(when)
            self.assertEqual(state['status'], 'exhausted')
            self.assertTrue(qa.overlay({}, state)['availability_blocked'])
            self.assertEqual(state['recheck_at'], qa.stamp(reset))
        state = self.check(reset)
        self.assertEqual((state['status'], state['reason']), ('unknown', 'reset-passed-no-probe'))
        self.assertFalse(qa.overlay({}, state)['availability_blocked'])
        self.assertEqual(qa.read_cache(qa.cache_path(self.root, 'antigravity')), state)
        self.assertNotIn('clear_requires', state)

    def test_six_day_legacy_cache_opens_on_first_check_without_trusting_renewed_deadline(self):
        value = self.reject(observed=self.now - 6 * 86400, step=3)
        value.update(recheck_at=qa.stamp(self.now + 60), expires_at=qa.stamp(self.now + 60))
        fleet.adaptive.atomic_write(qa.cache_path(self.root, 'antigravity'), value)
        state = self.check(self.now)
        self.assertEqual((state['status'], state['reason']), ('unknown', 'half-open-after-backoff'))
        self.assertEqual(state['block_step'], 3)
        self.assertEqual(state['observed_at'], value['observed_at'])
        self.assertFalse(qa.overlay({}, state)['availability_blocked'])
        self.assertEqual(qa.read_cache(qa.cache_path(self.root, 'antigravity')), state)

    def test_checks_never_extend_backoff_and_each_step_opens_at_its_deadline(self):
        for step, backoff in enumerate(qa.COSTLY_BACKOFF_S):
            with self.subTest(step=step):
                observed = self.now + step * 10 * 86400
                self.reject(observed=observed, step=step)
                for offset in (0, 60, 300, backoff - 1):
                    state = self.check(observed + offset)
                    self.assertEqual(state['status'], 'recheck-required')
                    self.assertEqual(state['recheck_at'], qa.stamp(observed + backoff))
                    self.assertEqual(state['expires_at'], state['recheck_at'])
                    self.assertEqual(state['observed_at'], qa.stamp(observed))
                state = self.check(observed + backoff)
                self.assertEqual((state['status'], state['reason']), ('unknown', 'half-open-after-backoff'))
                self.assertEqual(self.check(observed + backoff + 86400), state)

    def test_half_open_refusal_escalates_success_clears_and_reimport_is_idempotent(self):
        row = self.failure(self.stream('Provider error'), completed=self.now - 901)
        qa.import_native_observations(self.root, {'antigravity': self.profile}, [row],
                                      now=self.now, lock_factory=fleet.ReleasableKernelLock)
        opened = self.check(self.now)
        self.assertEqual(opened['status'], 'unknown')
        qa.import_native_observations(self.root, {'antigravity': self.profile}, [row],
                                      now=self.now + 1, lock_factory=fleet.ReleasableKernelLock)
        self.assertEqual(self.check(self.now + 1), opened)
        rejected = self.reject(observed=self.now + 2)
        self.assertEqual(rejected['block_step'], 1)
        self.assertEqual(self.check(self.now + 3)['status'], 'recheck-required')
        self.assertEqual(self.check(self.now + 2 + qa.COSTLY_BACKOFF_S[1])['status'], 'unknown')
        success = qa.record_native_success(self.root, 'antigravity', now=self.now + 4000,
                                          lock_factory=fleet.ReleasableKernelLock,
                                          run_started_at=qa.stamp(self.now + 3900))
        self.assertEqual(success['status'], 'available')
        self.assertEqual(success['block_step'], 0)
        self.assertEqual(self.check(self.now + 4001)['status'], 'available')

    def test_expired_reset_reimport_never_reblocks_or_changes_deadline(self):
        row = self.failure(self.stream('quota reached. Resets in 45m.'), completed=self.now - 3000)
        for offset in (0, 1, 61, 900):
            qa.import_native_observations(self.root, {'antigravity': self.profile}, [row],
                                          now=self.now + offset, lock_factory=fleet.ReleasableKernelLock)
            state = self.check(self.now + offset)
            self.assertEqual((state['status'], state['reason']), ('unknown', 'reset-passed-no-probe'))
            self.assertEqual(state['block_step'], 0)
            self.assertEqual(state['recheck_at'], qa.stamp(self.now - 300))

    def test_legacy_half_open_retry_preserves_escalation_after_long_idle(self):
        self.reject(observed=self.now - 6 * 86400, step=3)
        self.assertEqual(self.check(self.now)['reason'], 'half-open-after-backoff')
        self.assertEqual(self.reject(observed=self.now + 1)['block_step'], 3)
        self.assertEqual(self.check(self.now + 1)['recheck_at'], qa.stamp(self.now + 1 + 86400))

    def test_reset_refusal_beats_concurrent_success_and_latest_attempt_owns_reset(self):
        refusal = self.failure(self.stream('quota reached. Resets in 2h.'), completed=self.now - 10)
        success = dict(refusal, run_status='COMPLETED', exit_code=0, completed_at=qa.stamp(self.now),
                       model_identity={'status': 'verified', 'expected_model': refusal['model']})
        success['started_at'] = qa.stamp(self.now - 100)
        qa.import_native_observations(self.root, {'antigravity': self.profile}, [success, refusal],
                                      now=self.now, lock_factory=fleet.ReleasableKernelLock)
        self.assertEqual(self.check(self.now)['status'], 'exhausted')
        later_attempt = self.failure(self.stream('Provider error'))['provider_attempts'][0]
        refusal['provider_attempts'].append(later_attempt)
        self.assertEqual(self.observe(refusal)['status'], 'recheck-required')

    def test_pools_with_probe_preserve_native_only_semantics(self):
        self.reject(observed=self.now - 6 * 86400, step=3)
        state = self.check(self.now, probe=lambda: {'status': 'available', 'source': 'quota-check',
                                                   'covered_scopes': ['native']})
        self.assertEqual(state['status'], 'recheck-required')
        self.assertEqual(state['clear_requires'], 'native-cli-check')
        state = self.check(self.now + 61, probe=lambda: {'status': 'available', 'source': 'native-cli-check'})
        self.assertEqual(state['status'], 'available')

    def test_explicit_native_probe_pools_keep_refusal_without_sliding_deadlines(self):
        for adapter, stderr in (
                ('muse', 'run ended with Failed: API error 429: Subscription quota exhausted. '
                         'Your usage window resets at ' + qa.stamp(self.now + 3600) + '. (rate_limit_error)'),
                ('codebuddy', '429 Credits exhausted')):
            with self.subTest(adapter=adapter):
                denial = qa.rejection(adapter, stderr, 3)
                # Replay the legacy persisted shape as well as new refusals.
                denial.pop('recheck_adapter', None)
                qa.record_rejection(self.root, adapter, denial, now=self.now,
                                    lock_factory=fleet.ReleasableKernelLock)
                deadline = None
                for when in (self.now + 3601, self.now + 86400, self.now + 6 * 86400):
                    state = qa.check(self.root, adapter, {}, now=when, lock_factory=fleet.ReleasableKernelLock)
                    self.assertEqual(state['status'], 'recheck-required')
                    self.assertTrue(qa.overlay({}, state)['availability_blocked'])
                    if deadline is None:
                        deadline = state['recheck_at']
                    self.assertEqual(state['recheck_at'], deadline)
                    self.assertEqual(state['expires_at'], deadline)

    def test_manual_probe_pools_preserve_generic_native_refusal_in_passive_preflight(self):
        for adapter in ('muse', 'codebuddy'):
            with self.subTest(adapter=adapter):
                row = self.failure(self.stream('Provider error'), completed=self.now - 6 * 86400)
                row.update(agent=adapter, adapter=adapter)
                profile = {'adapter': adapter, 'quota_pool_id': adapter + '-fixture'}
                deadline = None
                for when in (self.now, self.now + 60):
                    with patch.object(fleet, 'read_usage', return_value=[row]), \
                            patch.object(fleet.adaptive, 'latest_quotas', return_value={}), \
                            patch.object(fleet.quota_collectors, 'collect_profile_quotas', return_value=({}, {})), \
                            patch.object(qa.time, 'time', return_value=when):
                        views, _ = fleet.current_quota_views(self.root, {adapter: profile})
                    state = views[adapter]['availability']
                    self.assertEqual(state['status'], 'recheck-required')
                    self.assertEqual(state['clear_requires'], 'native-cli-check')
                    self.assertTrue(views[adapter]['availability_blocked'])
                    if deadline is None:
                        deadline = state['recheck_at']
                    self.assertEqual(state['recheck_at'], deadline)

    def test_recovered_antigravity_is_a_routing_candidate_and_live_view_does_not_probe(self):
        for reset in (None, self.now - 86400):
            with self.subTest(reset=reset):
                self.reject(observed=self.now - 6 * 86400 + (1 if reset is not None else 0), step=3, reset=reset)
                with patch.object(fleet, 'read_usage', return_value=[]), \
                        patch.object(fleet.adaptive, 'latest_quotas', return_value={}), \
                        patch.object(fleet.quota_collectors, 'collect_profile_quotas', return_value=({}, {})), \
                        patch.object(qa.time, 'time', return_value=self.now):
                    views, _ = fleet.current_quota_views(self.root, {'antigravity': self.profile})
                self.assertEqual(views['antigravity']['availability']['status'], 'unknown')
                self.assertFalse(views['antigravity']['availability_blocked'])
                self.assertTrue(all(g['passed'] for g in routing_v2.quota_hard_gates(self.profile, views['antigravity'])))


if __name__ == '__main__':
    unittest.main()
