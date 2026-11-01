"""Native Muse subscription refusals must block later jobs before task input."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import fleet
import quota_availability as qa
import routing_v2


class MuseQuotaTests(unittest.TestCase):
    now = 2000000000.0

    def error(self, reset=None):
        reset = reset or qa.stamp(self.now + 3600)
        return ('run ended with Failed: API error 429 [request_id=synthetic-123]: '
                'Subscription quota exhausted. Your usage window resets at '
                + reset + '. (rate_limit_error)')

    def test_native_refusal_retains_only_safe_metadata(self):
        denial = qa.rejection('muse', self.error(), 1)
        self.assertIsNotNone(denial)
        self.assertEqual(denial['status'], 'exhausted')
        self.assertEqual(denial['scope'], 'subscription')
        self.assertEqual(denial['reset_at'], qa.stamp(self.now + 3600))
        self.assertEqual(denial['source'], 'native-provider-error')
        self.assertNotIn('synthetic-123', str(denial))

    def test_prose_success_other_provider_and_transient_rate_limit_do_not_block(self):
        for adapter, stderr, code, stdout in (
                ('muse', self.error(), 0, ''),
                ('muse', '', 1, self.error()),
                ('muse', 'The model says: ' + self.error(), 1, ''),
                ('muse', self.error().replace('Subscription quota exhausted.', 'Too many requests.'), 1, ''),
                ('muse', self.error().replace('API error 429', 'API error 401'), 1, ''),
                ('grok', self.error(), 1, '')):
            with self.subTest(adapter=adapter, code=code, stderr=stderr):
                self.assertIsNone(qa.rejection(adapter, stderr, code, stdout=stdout))

    def test_invalid_reset_never_becomes_fabricated_reset(self):
        denial = qa.rejection('muse', self.error('not-a-date'), 1)
        self.assertIsNotNone(denial)
        self.assertIsNone(denial['reset_at'])

    def test_refusal_without_reset_still_blocks(self):
        stderr = ('run ended with Failed: API error 429: '
                  'Subscription quota exhausted. (rate_limit_error)')
        denial = qa.rejection('muse', stderr, 1)
        self.assertIsNotNone(denial)
        self.assertIsNone(denial['reset_at'])

    def test_second_job_is_blocked_and_time_alone_does_not_restore_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            denial = qa.rejection('muse', self.error(), 1)
            self.assertIsNotNone(denial)
            qa.record_rejection(root, 'muse', denial,
                                lock_factory=fleet.ReleasableKernelLock, now=self.now)
            state = qa.check(root, 'muse', {'status': 'unknown'},
                             lock_factory=fleet.ReleasableKernelLock, now=self.now + 600)
            self.assertEqual(state['status'], 'exhausted')
            self.assertEqual(state['recheck_at'], qa.stamp(self.now + 3600))
            later = qa.check(root, 'muse', {'status': 'unknown'},
                             lock_factory=fleet.ReleasableKernelLock, now=self.now + 3601)
            self.assertEqual(later['status'], 'recheck-required')
            self.assertTrue(qa.overlay({}, later)['availability_blocked'])

    def test_live_router_uses_cached_denial_without_provider_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            denial = qa.rejection('muse', self.error(), 1)
            self.assertIsNotNone(denial)
            qa.record_rejection(root, 'muse', denial,
                                lock_factory=fleet.ReleasableKernelLock, now=self.now)
            profiles = {'muse': {'adapter': 'muse', 'quota_pool_id': 'muse'},
                        'grok': {'adapter': 'grok'}}
            with patch.object(qa.time, 'time', return_value=self.now + 10), \
                    patch.object(fleet.adaptive, 'latest_quotas', return_value={}), \
                    patch.object(fleet.quota_collectors, 'collect_profile_quotas', return_value=({}, {})):
                views, _ = fleet.current_quota_views(root, profiles)
            allowed = [name for name in profiles if all(g['passed'] for g in
                       routing_v2.quota_hard_gates(profiles[name], views[name]))]
            self.assertEqual(allowed, ['grok'])
            self.assertIsNone(views['muse'].get('headroom'))


if __name__ == '__main__':
    unittest.main()
