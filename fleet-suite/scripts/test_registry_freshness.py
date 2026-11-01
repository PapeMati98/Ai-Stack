import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('radar_subject', ROOT / 'payload/model-radar/scripts/model_radar.py')
radar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(radar)


class RegistryFreshness(unittest.TestCase):
    def check(self, binding=True, changed=False, timestamp='2026-09-06T18:00:00Z'):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = (ROOT / 'payload/agent-fleet/references/profiles.json').read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if changed:
                raw += b'\n'
            profiles = root / 'profiles.json'
            profiles.write_bytes(raw)
            meta = f'registry_sha256: {digest}\nregistry_verified_at: {timestamp}\n' if binding else ''
            audit = root / 'audit.md'
            audit.write_text('---\nschema_version: 1\nlast_verified_at: 2026-09-03T08:22:26Z\nvalid_for_days: 7\n' + meta + '---\n' + '\n'.join(radar.REQUIRED_SECTIONS) + '\n' + '\n'.join(f'https://example.com/{i}' for i in range(6)), encoding='utf-8')
            return radar.combined_check(audit, root / 'missing.json', max_age_days=7, now=radar.parse_timestamp('2026-09-06T20:00:00Z'), profiles_path=profiles)

    def test_missing_registry_binding_requires_refresh(self):
        self.assertFalse(self.check(binding=False)['routing_allowed'])

    def test_matching_registry_is_current_without_refreshing_market(self):
        result = self.check()
        self.assertTrue(result['routing_allowed'])
        self.assertEqual(result['last_verified_at'], '2026-09-03T08:22:26Z')

    def test_changed_registry_requires_refresh(self):
        result = self.check(changed=True)
        self.assertFalse(result['routing_allowed'])
        self.assertFalse(result['fresh'])
        self.assertEqual(result['status'], 'STALE')

    def test_malformed_registry_timestamp_rejected(self):
        self.assertFalse(self.check(timestamp='not-a-date')['routing_allowed'])

    def test_future_registry_timestamp_rejected(self):
        self.assertFalse(self.check(timestamp='2026-09-07T18:00:00Z')['routing_allowed'])

    def test_old_registry_timestamp_requires_refresh(self):
        self.assertFalse(self.check(timestamp='2026-08-28T18:00:00Z')['routing_allowed'])


if __name__ == '__main__':
    unittest.main()
