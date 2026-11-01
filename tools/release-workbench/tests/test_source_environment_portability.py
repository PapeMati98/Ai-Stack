"""Source environment references differ from materialized configuration paths."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from release_workbench.configuration import validate_config, verify_portability


class SourceEnvironmentPortability(unittest.TestCase):
    def fixture(self):
        root = Path(os.environ.get('TEAM_TEST_ROOT', str(Path(__file__).resolve().parents[1])))
        context = tempfile.TemporaryDirectory(dir=root, prefix='source-env-')
        self.addCleanup(context.cleanup)
        return Path(context.name)

    def test_source_references_are_not_owner_identifiers(self):
        root = self.fixture()
        (root / 'source.py').write_text(
            'candidate = "%USERPROFILE%/native/tool.exe"\n'
            'settings = "%APPDATA%/native/settings.json"\n', encoding='utf-8')
        result = verify_portability(root)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['files_scanned'], 1)

    def test_both_actual_machine_path_rules_stay_sensitive(self):
        root = self.fixture()
        owner = 'C:/Users/' + 'ma' + 'teu' + '/private/file.py'
        stack = 'D:/' + 'AL Lab/' + 'Agenci AI/private/file.py'
        (root / 'source.py').write_text(owner + '\n' + stack + '\n', encoding='utf-8')
        result = verify_portability(root)
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual([(x['rule'], x['line']) for x in result['findings']],
                         [('owner_path', 1), ('owner_path', 2)])
        self.assertNotIn(owner, json.dumps(result))
        self.assertNotIn(stack, json.dumps(result))

    def test_materialized_config_still_refuses_environment_bindings(self):
        root = self.fixture()
        baseline = json.loads((Path(__file__).resolve().parents[1] / 'config/stack.example.json').read_text(encoding='utf-8'))
        for value in ('%USERPROFILE%/runtime', '%APPDATA%/runtime', '$HOME/runtime'):
            with self.subTest(value=value):
                config = dict(baseline, runtime_root=value)
                with self.assertRaises(ValueError):
                    validate_config(config, owned_root=root)

    def test_explicit_forbidden_environment_literal_still_matches(self):
        root = self.fixture()
        (root / 'source.py').write_text('native = "%USERPROFILE%/tool.exe"\n', encoding='utf-8')
        result = verify_portability(root, forbidden_literals=['%USERPROFILE%'])
        self.assertEqual(result['status'], 'FAIL')
        self.assertIn({'rule': 'topology_literal_1', 'path': 'source.py', 'line': 1}, result['findings'])


if __name__ == '__main__':
    unittest.main()
