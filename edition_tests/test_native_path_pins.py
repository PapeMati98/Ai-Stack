"""Real adapter validators against a different, synthetic user's native paths."""
import ast
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'fleet-suite/payload/agent-fleet/scripts'


def load(name):
    module_name = 'edition_pin_' + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class NativePathPins(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TEAM_TEST_ROOT', str(ROOT)))
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'synthetic-operator'
        self.environment = patch.dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home))
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.profiles = json.loads((SCRIPTS.parent / 'references/profiles.json').read_text(encoding='utf-8'))['profiles']
        tree = ast.parse((SCRIPTS / 'fleet.py').read_text(encoding='utf-8-sig'))
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                     and n.name in {'bind_native_profile_paths', 'expand_candidate'}]
        self.assertEqual(len(functions), 2)
        ns = {'Any': object, 'Path': Path, 'os': os, 'FleetError': ValueError}
        exec(compile(ast.Module(body=functions, type_ignores=[]), 'real_fleet_binding', 'exec'), ns)
        self.bind = ns['bind_native_profile_paths']

    def test_mistral_real_validator_accepts_relocated_pins(self):
        runtime = load('mistral_runtime.py')
        profile = copy.deepcopy(self.profiles['mistral'])
        self.bind('mistral', profile)
        runtime.validate_mistral_profile(profile)
        self.assertEqual(profile['candidates'], [runtime.MISTRAL_NATIVE_EXECUTABLE])
        self.assertEqual(Path(runtime.MISTRAL_NATIVE_PYTHON), self.home / '.agents/runtime/mistral-vibe/2.26.1/Scripts/python.exe')
        profile['candidates'] = [str(self.home / 'unreviewed-vibe.exe')]
        with self.assertRaises(runtime.MistralRuntimeError):
            runtime.validate_mistral_profile(profile)
        with self.assertRaises(runtime.MistralRuntimeError):
            runtime.require_native_executable(str(self.home / 'unreviewed-vibe.exe'))
        self.assertFalse(self.home.exists(), 'No installation, login or account store may be touched')

    def test_mercury_real_validator_accepts_relocated_pins(self):
        runtime = load('mercury_runtime.py')
        profile = copy.deepcopy(self.profiles['opencode-mercury'])
        self.bind('opencode-mercury', profile)
        runtime.validate_profile(profile)
        self.assertEqual(profile['data_boundary']['workspace_root'], runtime.WORKSPACE_ROOT)
        self.assertEqual(profile['candidates'], [runtime.NATIVE_EXE])
        self.assertEqual(runtime.STATE_ROOT, self.home / '.agents/mercury3-pilot')
        for key, value in [('paid_fallback', True), ('office_access', 'relay')]:
            changed = copy.deepcopy(profile)
            changed[key] = value
            with self.assertRaises(runtime.MercuryGateError):
                runtime.validate_profile(changed)
        changed = copy.deepcopy(profile)
        changed['candidates'] = [str(self.home / 'unreviewed-opencode.exe')]
        with self.assertRaises(runtime.MercuryGateError):
            runtime.validate_profile(changed)
        with self.assertRaises(runtime.MercuryGateError):
            runtime.require_native(str(self.home / 'unreviewed-opencode.exe'))
        self.assertFalse(self.home.exists(), 'No native executable, ledger or credential read')


if __name__ == '__main__':
    unittest.main()
