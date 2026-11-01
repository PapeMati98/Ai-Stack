"""Source metadata is not a credential; literal secret canaries stay detectable."""
import os
from pathlib import Path
import tempfile
import unittest

from release_workbench.privacy import scan_tree


class SourcePrivacyContexts(unittest.TestCase):
    def scan(self, text, suffix='.py'):
        root = Path(os.environ.get('TEAM_TEST_ROOT', str(Path(__file__).resolve().parents[1])))
        with tempfile.TemporaryDirectory(dir=root, prefix='privacy-context-') as name:
            p = Path(name)
            (p / ('source' + suffix)).write_text(text, encoding='utf-8')
            return scan_tree(p)

    def test_supported_native_env_reference_is_not_a_literal_key(self):
        for text, suffix in [
            ('{"apiKey": "{env:NATIVE_EXAMPLE_KEY}"}\n', '.json'),
            ('config = {"apiKey": "{env:NATIVE_EXAMPLE_KEY}"}\n', '.py'),
        ]:
            with self.subTest(suffix=suffix):
                self.assertEqual(self.scan(text, suffix)['status'], 'PASS')

    def test_opaque_os_reference_is_not_a_literal_credential(self):
        for text in ['reference = "os-credential://native/default"\n',
                     '"""References use os-credential://<provider>/<handle>."""\n']:
            with self.subTest(text=text):
                self.assertEqual(self.scan(text)['status'], 'PASS')

    def test_sql_parameter_markers_are_not_literal_tokens(self):
        text = 'query = "UPDATE leases SET owner=? WHERE token=? AND expires_at>?"\n'
        self.assertEqual(self.scan(text)['status'], 'PASS')

    def test_incomplete_detection_pattern_literals_are_not_values(self):
        text = 'patterns = ("api_key=", "api-key:", "password=", "secret=", "authorization: bearer")\n'
        self.assertEqual(self.scan(text)['status'], 'PASS')

    def test_native_auth_method_metadata_is_not_a_credential(self):
        for text, suffix in [('{"auth":{"selectedType":"openai"}}\n', '.json'),
                             ('settings = {"auth": {"selectedType": "openai"}}\n', '.py')]:
            with self.subTest(suffix=suffix):
                self.assertEqual(self.scan(text, suffix)['status'], 'PASS')

    def test_literal_secret_assignments_in_every_context_remain_sensitive(self):
        marker = 'opaque-sensitive-' + 'Z' * 24
        cases = [
            ('api_key = "' + marker + '"\n', '.py'),
            ('{"auth":"' + marker + '"}\n', '.json'),
            ('token=?\n', '.ini'),
            ('query = "UPDATE leases SET token=\'' + marker + '\' WHERE id=?"\n', '.py'),
            ('{"auth":{"selectedType":"openai","token":"' + marker + '"}}\n', '.json'),
            ('{"apiKey":"{env:NATIVE_EXAMPLE_KEY}' + marker + '"}\n', '.json'),
            ('credential = "os-credential://native/default?password=' + marker + '"\n', '.py'),
        ]
        for text, suffix in cases:
            with self.subTest(suffix=suffix, case=text[:35]):
                result = self.scan(text, suffix)
                self.assertEqual(result['status'], 'FAIL')
                self.assertTrue(any(x['rule'] == 'privacy.credential_assignment' for x in result['findings']))


if __name__ == '__main__':
    unittest.main()
