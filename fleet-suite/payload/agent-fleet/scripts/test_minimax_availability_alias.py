"""HD-20261021-43469b: a configured MiniMax runtime alias is availability evidence.

The native MiniMax run receipt records the trusted catalog model
(MiniMax-M3.1-Flash) while the verified native identity attests the managed
runtime alias (minimax/MiniMax-M3.1-Flash-Preview). Direct equality rejected
that run, so a real COMPLETED exit-0 run produced no native availability
evidence for the pool. Only the profile's own trusted execution profile may
approve that pair; everything else stays rejected and the numeric remaining
quota stays unknown.

Offline only: synthetic rows, a task-owned temporary root, no network and no
native calls.
"""
import json
from pathlib import Path
import tempfile
import unittest

import fleet
import quota_availability as qa

ALIAS = 'minimax/MiniMax-M3.1-Flash-Preview'
CATALOG_MODEL = 'MiniMax-M3.1-Flash'
POOL = 'minimax-coding-plan'
PROFILES = Path(__file__).resolve().parent.parent / 'references' / 'profiles.json'


def profile(agent):
    """The trusted Fleet profile configuration, read only."""
    raw = json.loads(PROFILES.read_text(encoding='utf-8'))
    return raw['profiles'][agent]


class MiniMaxAvailabilityAliasTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = qa.seconds('2026-10-21T18:00:00Z')
        self.profile = profile('minimax')

    def tearDown(self):
        self.temp.cleanup()

    def identity(self, **overrides):
        value = {'status': 'verified', 'expected_runtime_model': ALIAS,
                 'observed_runtime_models': [ALIAS], 'error': None}
        value.update(overrides)
        return value

    def success(self, **overrides):
        row = {'agent': 'minimax', 'adapter': 'minimax', 'model': CATALOG_MODEL,
               'model_provider_id': 'minimax', 'quota_pool_id': POOL,
               'run_status': 'COMPLETED', 'exit_code': 0, 'timed_out': False,
               'runtime_attestation': {'status': 'VERIFIED'},
               'model_identity': self.identity(),
               'token_usage': {'status': 'known', 'total_tokens': 4096},
               'provider_attempts': [],
               'started_at': qa.stamp(self.now - 60), 'completed_at': qa.stamp(self.now)}
        row.update(overrides)
        return row

    def failure(self, completed=None):
        completed = self.now if completed is None else completed
        return {'agent': 'minimax', 'adapter': 'minimax', 'model': CATALOG_MODEL,
                'model_provider_id': 'minimax', 'quota_pool_id': POOL,
                'run_status': 'FAILED', 'exit_code': 3, 'timed_out': False,
                'runtime_attestation': {'status': 'VERIFIED'},
                'model_identity': self.identity(),
                'token_usage': {'status': 'known', 'total_tokens': 0},
                'provider_attempts': [{'exit_code': 3, 'terminal_reason': 'error',
                                       'transport_outcome': {'status': 'failed'},
                                       'token_usage': {'status': 'known', 'total_tokens': 0}}],
                'started_at': qa.stamp(completed - 5), 'completed_at': qa.stamp(completed)}

    def observe(self, row, agent='minimax', agent_profile=None):
        return qa.native_run_observation(row, agent, agent_profile or self.profile,
                                         now=self.now)

    def import_rows(self, rows, now=None):
        qa.import_native_observations(self.root, {'minimax': self.profile}, rows,
                                      lock_factory=fleet.ReleasableKernelLock,
                                      now=self.now if now is None else now)
        return qa.read_cache(qa.cache_path(self.root, POOL))

    def check(self, prior=None, now=None):
        return qa.check(self.root, POOL, prior or {}, lock_factory=fleet.ReleasableKernelLock,
                        now=self.now if now is None else now)

    def test_trusted_configuration_maps_catalog_model_to_runtime_alias(self):
        # The approval is exactly the configured mapping, nothing else.
        entry = self.profile['execution_profiles']['minimax-m31-flash-medium']
        self.assertEqual(entry['model'], CATALOG_MODEL)
        self.assertEqual(entry['runtime_model_id'], ALIAS)
        self.assertEqual(entry['model_provider_id'], 'minimax')
        self.assertEqual(self.profile['cli_provider_id'], 'minimax')

    def test_configured_alias_is_native_available_evidence(self):
        observation = self.observe(self.success())
        self.assertIsNotNone(observation)
        self.assertEqual(observation['status'], 'available')
        self.assertEqual(observation['source'], 'native-run')
        self.assertEqual(observation['observed_at'], qa.stamp(self.now))
        self.assertEqual(observation['started_at'], qa.stamp(self.now - 60))
        # A run is availability evidence only; it never states a numeric quota.
        for key in ('headroom', 'reported_headroom', 'used', 'limit', 'window_hours'):
            self.assertNotIn(key, observation)

    def test_alias_evidence_is_persisted_and_keeps_numeric_quota_unknown(self):
        state = self.import_rows([self.success()])
        self.assertEqual(state['status'], 'available')
        self.assertEqual(state['source'], 'native-run')
        prior = {'status': 'unknown', 'headroom': None, 'observed_at': qa.stamp(self.now - 10)}
        checked = self.check(prior)
        self.assertEqual(checked['status'], 'available')
        row = qa.overlay(prior, checked)
        self.assertFalse(row['availability_blocked'])
        self.assertTrue(row['availability_verified'])
        self.assertIsNone(row['headroom'])
        self.assertNotIn('reported_headroom', row)
        for key in ('used', 'limit'):
            self.assertNotIn(key, checked)

    def test_prefix_and_unconfigured_alias_are_rejected(self):
        for expected in ('minimax/MiniMax-M3.1-Flash', 'minimax/MiniMax-M3.1-Flash-Preview-2',
                         'MiniMax-M3.1-Flash-Preview', 'other/MiniMax-M3.1-Flash-Preview',
                         'minimax/Some-Other-Model'):
            with self.subTest(expected=expected):
                row = self.success(model_identity=self.identity(
                    expected_runtime_model=expected,
                    observed_runtime_models=[expected]))
                self.assertIsNone(self.observe(row))

    def test_missing_mapping_is_rejected(self):
        row = self.success(model_identity=self.identity(
            expected_runtime_model='minimax/Not-Configured',
            observed_runtime_models=['minimax/Not-Configured']))
        self.assertIsNone(self.observe(row))
        # A profile without trusted execution profiles approves nothing.
        bare = {'adapter': 'minimax', 'quota_pool_id': POOL, 'cli_provider_id': 'minimax'}
        self.assertIsNone(self.observe(self.success(), agent_profile=bare))

    def test_wrong_or_missing_provider_is_rejected(self):
        self.assertIsNone(self.observe(self.success(model_provider_id='anthropic')))
        row = self.success()
        row.pop('model_provider_id')
        self.assertIsNone(self.observe(row))

    def test_mismatched_observed_alias_is_rejected(self):
        row = self.success(model_identity=self.identity(
            observed_runtime_models=['minimax/MiniMax-M3.1-Flash']))
        self.assertIsNone(self.observe(row))
        row = self.success(model_identity=self.identity(observed_runtime_models=[]))
        self.assertIsNone(self.observe(row))

    def test_unattested_identity_or_runtime_is_rejected(self):
        for status in ('unavailable', 'mismatch', 'not-applicable'):
            with self.subTest(status=status):
                row = self.success(model_identity=self.identity(status=status))
                self.assertIsNone(self.observe(row))
        row = self.success(runtime_attestation={'status': 'SOURCE_TREE'})
        self.assertIsNone(self.observe(row))
        row = self.success(timed_out=True)
        self.assertIsNone(self.observe(row))

    def test_wrong_pool_or_adapter_is_rejected(self):
        self.assertIsNone(self.observe(self.success(quota_pool_id='other-pool')))
        self.assertIsNone(self.observe(self.success(adapter='claude')))
        self.assertIsNone(self.observe(self.success(agent='other-agent'), agent='minimax'))

    def test_stale_and_future_evidence_is_rejected(self):
        stale = self.success(started_at=qa.stamp(self.now - 90000),
                             completed_at=qa.stamp(self.now - 86400))
        self.assertIsNone(self.observe(stale))
        future = self.success(started_at=qa.stamp(self.now + 10),
                              completed_at=qa.stamp(self.now + 60))
        self.assertIsNone(self.observe(future))

    def test_newer_refusal_survives_a_concurrent_alias_success(self):
        refusal = self.failure(completed=self.now)
        self.assertEqual(self.observe(refusal)['status'], 'recheck-required')
        later_refusal = self.failure(completed=self.now + 2)
        state = self.import_rows([self.success(), later_refusal], now=self.now + 2)
        self.assertEqual(state['status'], 'recheck-required')
        self.assertEqual(state['reason'], 'native-zero-token-failure')
        checked = self.check(now=self.now + 2)
        self.assertEqual(checked['status'], 'recheck-required')
        prior = {'status': 'unknown', 'headroom': None, 'observed_at': qa.stamp(self.now)}
        self.assertTrue(qa.overlay(prior, checked)['availability_blocked'])

    def test_older_refusal_is_cleared_by_a_later_native_alias_run(self):
        qa.record_rejection(self.root, POOL,
                            {'status': 'recheck-required', 'source': 'native-provider-attempt',
                             'reason': 'native-zero-token-failure', 'scope': 'native',
                             'clear_requires': 'native-cli-check'},
                            lock_factory=fleet.ReleasableKernelLock, now=self.now - 100)
        self.assertEqual(self.import_rows([self.success()])['status'], 'available')

    def test_legacy_exact_model_and_codex_kimi_exceptions_are_preserved(self):
        # The catalog model attested directly keeps working without a mapping.
        row = self.success(model_identity={'status': 'verified',
                                           'expected_model': CATALOG_MODEL})
        self.assertEqual(self.observe(row)['status'], 'available')
        # Codex/Kimi exact-launch exceptions keep their native identity.
        codex = profile('codex')
        codex_row = {'agent': 'codex', 'adapter': 'codex', 'model': 'gpt-6-astra',
                     'model_provider_id': 'openai', 'run_status': 'COMPLETED', 'exit_code': 0,
                     'runtime_attestation': {'status': 'VERIFIED'},
                     'model_identity': {'status': 'not-applicable', 'expected_model': 'gpt-6-astra',
                                        'error': None, 'observed_models': [],
                                        'observed_runtime_models': []},
                     'token_usage': {'status': 'known', 'total_tokens': 10},
                     'started_at': qa.stamp(self.now - 5), 'completed_at': qa.stamp(self.now)}
        self.assertEqual(qa.native_run_observation(codex_row, 'codex', codex,
                                                   now=self.now)['status'], 'available')
        kimi = profile('kimi')
        kimi_row = dict(codex_row, agent='kimi', adapter='kimi', model='k3',
                        model_provider_id='moonshot',
                        model_identity={'status': 'not-applicable', 'expected_model': 'k3',
                                        'error': None, 'observed_models': [],
                                        'observed_runtime_models': []})
        self.assertEqual(qa.native_run_observation(kimi_row, 'kimi', kimi,
                                                   now=self.now)['status'], 'available')

    def test_another_providers_alias_is_not_approved_through_this_agent(self):
        # The alias must belong to the agent's own configured provider.
        other = {'adapter': 'opencode', 'quota_pool_id': 'opencode',
                 'cli_provider_id': 'opencode',
                 'execution_profiles': {'opencode-zai': {
                     'model': 'glm-5.3-flash', 'model_provider_id': 'zai',
                     'runtime_model_id': 'zai-coding-plan/glm-5.3-flash'}}}
        row = {'agent': 'opencode', 'adapter': 'opencode', 'model': 'glm-5.3-flash',
               'model_provider_id': 'zai', 'run_status': 'COMPLETED', 'exit_code': 0,
               'runtime_attestation': {'status': 'VERIFIED'},
               'model_identity': {'status': 'verified',
                                  'expected_runtime_model': 'zai-coding-plan/glm-5.3-flash',
                                  'observed_runtime_models': ['zai-coding-plan/glm-5.3-flash']},
               'token_usage': {'status': 'known', 'total_tokens': 10},
               'started_at': qa.stamp(self.now - 5), 'completed_at': qa.stamp(self.now)}
        self.assertIsNone(qa.native_run_observation(row, 'opencode', other, now=self.now))


if __name__ == '__main__':
    unittest.main()
