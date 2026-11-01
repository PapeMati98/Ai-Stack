"""Native Kimi 5-hour provider.auth_error refusal is a short-window exhaustion."""
import tempfile
import unittest
from pathlib import Path

import fleet
import quota_availability as qa
import routing_v2


# Attested native stderr line. The following sentence names the window; it is
# not a reset timestamp and not remaining time.
NATIVE_5H_LINE = (
    "error: failed to run prompt: provider.auth_error: 403 You've reached your 5-hour usage limit. "
    "Your quota will reset when the current 5-hour window ends. To continue now, purchase extra usage "
    "or upgrade your plan: https://www.kimi.com/membership/subscription?tab=quota"
)
LIBUV = 'Assertion failed: UV_HANDLE_CLOSING'
NATIVE_STDERR = NATIVE_5H_LINE + '\r\n' + LIBUV + '\r\n'
LEGACY_LINES = {
    'monthly': "error: failed to run prompt: provider.api_error: 403 You've reached your monthly usage limit",
    'weekly': "error: failed to run prompt: provider.api_error: 403 You've reached your weekly usage limit",
    'daily': "error: failed to run prompt: provider.api_error: 403 You've reached your daily usage limit",
}


def _denial(scope):
    return {'status': 'exhausted', 'scope': scope, 'source': 'native-provider-error',
            'reason': 'usage-limit', 'reset_at': None, 'clear_requires': 'native-cli-check'}


class KimiShortWindowRejectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = 2000000000.0

    def tearDown(self):
        self.temp.cleanup()

    def test_native_5h_auth_error_is_short_window_exhaustion(self):
        denial = qa.rejection('kimi', NATIVE_STDERR, 1)
        self.assertEqual(denial, _denial('short-window'))

    def test_legacy_monthly_weekly_daily_refusals_remain(self):
        for scope, line in LEGACY_LINES.items():
            with self.subTest(scope=scope):
                self.assertEqual(qa.rejection('kimi', line, 1), _denial(scope))

    def test_negative_controls_are_not_quota_proof(self):
        # A separate silent exit 1 stays unexplained. A later quota sentence,
        # on stdout or as unanchored prose, does not classify that failure.
        controls = {
            'silent-exit-1': ('', 1, ''),
            'libuv-only': (LIBUV + '\r\n', 1, ''),
            'generic-exit-1': ('error: failed to run prompt\r\n', 1, ''),
            'exit-0-with-native-line': (NATIVE_STDERR, 0, ''),
            'stdout-native-line': ('', 1, NATIVE_5H_LINE),
            'libuv-then-stdout-quota': (LIBUV + '\r\n', 1, NATIVE_5H_LINE),
            'unanchored-prose-after-crash': (
                'error: process exited\r\n' + LIBUV + '\r\n'
                "You've reached your 5-hour usage limit. Your quota will reset when the current 5-hour window ends.\r\n",
                1, ''),
            'model-prose': ("Model says You've reached your 5-hour usage limit", 1, ''),
            'auth-403-forbidden': ('error: failed to run prompt: provider.auth_error: 403 forbidden', 1, ''),
            'auth-403-unauthorized': ('error: failed to run prompt: provider.auth_error: 403 unauthorized', 1, ''),
            'api-403-unauthorized': ('error: failed to run prompt: provider.api_error: 403 unauthorized', 1, ''),
            'bare-403-forbidden': ('403 forbidden', 1, ''),
            'auth-without-403': (
                "error: failed to run prompt: provider.auth_error: You've reached your 5-hour usage limit",
                1, ''),
            'auth-429-usage-limit': (
                "error: failed to run prompt: provider.auth_error: 429 You've reached your 5-hour usage limit",
                1, ''),
            'generic-429': ('error: failed to run prompt: provider.api_error: 429 requests per minute', 1, ''),
            'api-error-5h-not-attested-envelope': (
                "error: failed to run prompt: provider.api_error: 403 You've reached your 5-hour usage limit",
                1, ''),
            'indented-envelope': ('  ' + NATIVE_5H_LINE, 1, ''),
        }
        for name, (stderr, exit_code, stdout) in controls.items():
            with self.subTest(name=name):
                self.assertIsNone(qa.rejection('kimi', stderr, exit_code, stdout=stdout))
        for adapter in ('claude', 'grok', 'codebuddy', 'qwen', 'muse', 'codex'):
            with self.subTest(adapter=adapter):
                self.assertIsNone(qa.rejection(adapter, NATIVE_STDERR, 1))

    def test_denial_persists_blocks_and_clears_only_natively(self):
        denial = qa.rejection('kimi', NATIVE_STDERR, 1)
        self.assertEqual(denial, _denial('short-window'))
        stored = qa.record_rejection(self.root, 'kimi', denial, lock_factory=fleet.ReleasableKernelLock, now=self.now)
        self.assertEqual(stored['status'], 'exhausted')
        self.assertEqual(stored['scope'], 'short-window')
        self.assertEqual(stored['source'], 'native-provider-error')
        self.assertEqual(stored['reason'], 'usage-limit')
        self.assertEqual(stored['clear_requires'], 'native-cli-check')
        self.assertIsNone(stored.get('reset_at'))
        self.assertEqual(qa.seconds(stored['recheck_at']) - qa.seconds(stored['observed_at']), 300)
        cached = qa.read_cache(qa.cache_path(self.root, 'kimi'))
        self.assertEqual(cached['scope'], 'short-window')
        self.assertEqual(cached['clear_requires'], 'native-cli-check')
        self.assertIsNone(cached.get('reset_at'))

        def forbidden():
            self.fail('do not probe while the native refusal is inside its backoff')

        held = qa.check(self.root, 'kimi', {}, probe=forbidden, lock_factory=fleet.ReleasableKernelLock,
                        now=self.now + 10)
        self.assertEqual(held['status'], 'exhausted')
        self.assertEqual(held['scope'], 'short-window')
        blocked = qa.overlay({}, held)
        self.assertTrue(blocked['availability_blocked'])
        gate = next(item for item in routing_v2.quota_hard_gates({}, blocked) if item['code'] == 'quota-not-exhausted')
        self.assertFalse(gate['passed'])
        self.assertIn('usage-limit', gate['evidence'])

        # A /usages reading that covers the short window is not native proof.
        contradicted = qa.check(
            self.root, 'kimi', {}, lock_factory=fleet.ReleasableKernelLock, now=self.now + 301,
            probe=lambda: {'status': 'available', 'source': 'kimi-native-usage',
                           'covered_scopes': ['short-window', 'weekly', 'daily', 'monthly']})
        self.assertEqual(contradicted['status'], 'recheck-required')
        self.assertEqual(contradicted['clear_requires'], 'native-cli-check')
        self.assertTrue(qa.overlay({}, contradicted)['availability_blocked'])

        cleared = qa.check(
            self.root, 'kimi', {}, lock_factory=fleet.ReleasableKernelLock, now=self.now + 400,
            probe=lambda: {'status': 'available', 'source': 'native-cli-check'})
        self.assertEqual(cleared['status'], 'available')
        self.assertEqual(cleared['source'], 'native-cli-check')
        self.assertIsNone(cleared.get('reset_at'))
        row = qa.overlay({}, cleared)
        self.assertFalse(row['availability_blocked'])
        self.assertTrue(row['availability_verified'])
        gate = next(item for item in routing_v2.quota_hard_gates({}, row) if item['code'] == 'quota-not-exhausted')
        self.assertTrue(gate['passed'])


if __name__ == '__main__':
    unittest.main()
