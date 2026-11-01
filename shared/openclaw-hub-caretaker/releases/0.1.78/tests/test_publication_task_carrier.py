"""Real Windows CreateProcess regression; local Python fake, never an LLM."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from caretaker.config import TransportSpec
from caretaker.transports import AgentFleetArgvTransport, TransportError


TEMP = Path(os.environ.get("TEAM_TEST_ROOT", tempfile.gettempdir())) / 'publication-carrier-tests'


class PublicationTaskCarrierTests(unittest.TestCase):
    def setUp(self):
        TEMP.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(prefix='carrier73-', dir=TEMP)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.prompt = 'Original publication instructions and references:\n' + 'ą中🙂 evidence\n' * 4000
        self.spec = TransportSpec(kind='openclaw_argv', executable=sys.executable,
            argv=['fleet.py', 'run', '--agent', 'kimi', '--model', 'k3'],
            fleet_role='maker', data_classification='internal')

    def carrier(self, prompt=None, spec=None, state=None):
        from caretaker.transports import _publication_task_carrier
        return _publication_task_carrier(spec or self.spec, str(self.root), str(state or self.root),
                                         self.prompt if prompt is None else prompt)

    def test_real_windows_old_inline_raises206(self):
        if os.name != 'nt':
            self.skipTest('Windows CreateProcess regression')
        with self.assertRaises(OSError) as failed:
            subprocess.run([sys.executable, '-B', '-c', 'pass', '-p', self.prompt],
                capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(failed.exception.winerror, 206)

    def test_real_windows_file_pointer_preserves_full_bytes_and_sha(self):
        if os.name != 'nt':
            self.skipTest('Windows CreateProcess regression')
        fake = self.root / 'fake_kimi.py'
        fake.write_text('''import argparse,hashlib,json,pathlib
p=argparse.ArgumentParser();p.add_argument('-p');p.add_argument('--manifest');p.add_argument('--workspace');a=p.parse_args()
metadata=json.loads(a.p.splitlines()[1]);root=pathlib.Path(a.workspace);data=(root/metadata['path']).read_bytes()
assert len(data)==metadata['bytes'] and hashlib.sha256(data).hexdigest()==metadata['sha256']
manifest=json.loads(pathlib.Path(a.manifest).read_text(encoding='utf-8'))
assert any(x['path']==metadata['path'] and x['sha256']==metadata['sha256'] for x in manifest['inputs'])
print(json.dumps({'full_read':True,'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)}))
''', encoding='utf-8')
        observed = []
        def runner(*, executable, argv, cwd, prompt, timeout):
            # Exercise the real reconstructed --task-file boundary, then the
            # same native -p shape through real Windows CreateProcess.
            task = Path(argv[argv.index('--task-file') + 1]).read_text(encoding='utf-8')
            manifest = argv[argv.index('--required-inputs-file') + 1] if '--required-inputs-file' in argv else 'NONE'
            self.assertNotIn('--knowledge-mode', argv)  # snapshot default retained
            native = subprocess.run([sys.executable, '-B', str(fake), '-p', task,
                '--manifest', manifest, '--workspace', cwd], capture_output=True,
                timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(native.returncode, 0)
            result = json.loads(native.stdout)
            self.assertTrue(result['full_read'])
            self.assertEqual(result['bytes'], len(self.prompt.encode('utf-8')))
            self.assertEqual(result['sha256'], hashlib.sha256(self.prompt.encode('utf-8')).hexdigest())
            observed.append(result)
            return 0, json.dumps({'ok': True, 'status': 'ok', 'provider': 'kimi', 'model': 'k3', 'final': '{}'}), ''
        with patch('caretaker.transports.resolve_fleet_py', return_value=self.root / 'fleet.py'), \
                patch('caretaker.transports._fleet_selected_profiles', return_value={}), \
                patch('caretaker.transports._fleet_task_assessment', return_value={}):
            envelope = AgentFleetArgvTransport(run_callable=runner, expected_identity=('kimi', 'k3')).invoke(
                spec=self.spec, cwd=str(self.root), prompt=self.prompt,
                run_config_path='unused', state_dir=str(self.root), timeout=90)
        self.assertEqual(envelope.model, 'k3')
        self.assertEqual(len(observed), 1)

    def test_prior_required_refs_and_exact_original_task_are_preserved(self):
        prior = {'schema_version': 1, 'scope_note': 'preserved', 'inputs': [
            {'path': 'prior\\input.txt', 'sha256': 'a' * 64, 'label': 'preserved'}]}
        source = self.root / 'prior-inputs.json'
        source.write_text(json.dumps(prior), encoding='utf-8')
        before = source.read_bytes()
        self.spec.argv.extend(['--required-inputs-file', str(source)])
        bootstrap, manifest = self.carrier()
        metadata = json.loads(bootstrap.splitlines()[1])
        self.assertEqual((self.root / metadata['path']).read_bytes(), self.prompt.encode('utf-8'))
        combined = json.loads(Path(manifest).read_text(encoding='utf-8'))
        self.assertEqual(combined['inputs'][:-1], prior['inputs'])
        self.assertEqual(combined['scope_note'], 'preserved')
        self.assertEqual(source.read_bytes(), before)
        receipt = next(self.root.glob('publication-carrier-*.json'))
        self.assertEqual(json.loads(receipt.read_text())['bootstrap_sha256'], hashlib.sha256(bootstrap.encode()).hexdigest())

    def test_short_and_non_target_profiles_are_unchanged(self):
        self.assertEqual(self.carrier(prompt='small task'), ('small task', None))
        self.spec.argv[self.spec.argv.index('kimi')] = 'muse'
        self.assertEqual(self.carrier(), (self.prompt, None))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_modified_immutable_task_is_rejected_without_overwrite(self):
        bootstrap, _ = self.carrier()
        task = self.root / json.loads(bootstrap.splitlines()[1])['path']
        task.write_bytes(b'changed')
        with self.assertRaises(TransportError):
            self.carrier()
        self.assertEqual(task.read_bytes(), b'changed')

    def test_wrong_owner_rejected_before_carrier_write(self):
        elsewhere = self.root / 'different-owned-state'
        elsewhere.mkdir()
        with self.assertRaises(TransportError):
            self.carrier(state=elsewhere)
        self.assertEqual(list(self.root.glob('publication-*')), [])

    def test_unsafe_prior_manifest_rejected_before_carrier_write(self):
        source = self.root / 'prior.json'
        source.write_text(json.dumps({'inputs': [{'path': '../escape', 'sha256': 'a' * 64}]}))
        self.spec.argv.extend(['--required-inputs-file', str(source)])
        with self.assertRaises(TransportError):
            self.carrier()
        self.assertEqual(list(self.root.glob('publication-*')), [])


if __name__ == '__main__':
    unittest.main()
