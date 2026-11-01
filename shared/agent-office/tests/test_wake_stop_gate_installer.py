"""Opt-in hook installation and transaction tests on synthetic profiles only."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import install_agent_office as installer


class WakeStopHookPatchTests(unittest.TestCase):
    def setUp(self):
        self.hook = installer.wake_stop_hook(Path('D:/Python With Spaces/python.exe'), Path('D:/Office With Spaces'))
        self.foreign = b'{"matcher":"", "hooks": [{"type":"command","command":"foreign.exe"}]}'
        self.data = b'\xef\xbb\xbf{\r\n  "env": {"OTHER":"keep"}, "hooks": {"UserPromptSubmit": [' + self.foreign + b']}, "theme":"dark"\r\n}\r\n'

    def test_preserves_original_foreign_bytes_bom_and_crlf(self):
        result = installer.patch_wake_stop_gate(self.data, self.hook)
        self.assertTrue(result.startswith(b'\xef\xbb\xbf'))
        self.assertIn(self.foreign, result)
        self.assertIn(b'"env": {"OTHER":"keep"}', result)
        self.assertIn(b'\r\n', result)
        parsed = json.loads(result.decode('utf-8-sig'))
        self.assertEqual(parsed['theme'], 'dark')
        self.assertEqual(parsed['hooks']['UserPromptSubmit'][-1], {'hooks': [self.hook]})
        self.assertEqual(self.hook['command'], 'D:\\Python With Spaces\\python.exe')
        self.assertEqual(self.hook['args'][0], '-I')

    def test_owned_idempotence_and_unknown_ownership_refusal(self):
        result = installer.patch_wake_stop_gate(self.data, self.hook)
        self.assertEqual(installer.patch_wake_stop_gate(result, self.hook, owned_previous=True), result)
        with self.assertRaisesRegex(installer.DeployError, 'no installer ownership'):
            installer.patch_wake_stop_gate(result, self.hook)

    def test_rejects_changed_duplicate_or_removed_owned_gate(self):
        result = installer.patch_wake_stop_gate(self.data, self.hook)
        parsed = json.loads(result.decode('utf-8-sig'))
        for change in ('command', 'matcher', 'duplicate', 'removed'):
            candidate = json.loads(json.dumps(parsed))
            group = candidate['hooks']['UserPromptSubmit'][-1]
            if change == 'command':
                group['hooks'][0]['command'] = 'untrusted.exe'
            elif change == 'matcher':
                group['matcher'] = 'narrow'
            elif change == 'duplicate':
                candidate['hooks']['UserPromptSubmit'].append(group.copy())
            else:
                candidate['hooks']['UserPromptSubmit'].pop()
            with self.subTest(change=change), self.assertRaises(installer.DeployError):
                installer.patch_wake_stop_gate(json.dumps(candidate).encode(), self.hook, owned_previous=True)

    def test_append_missing_hooks_event_or_empty_array(self):
        for data in (b'{}', b'{"hooks":{}}', b'{"hooks":{"UserPromptSubmit":[]}}'):
            with self.subTest(data=data):
                result = installer.patch_wake_stop_gate(data, self.hook)
                installer.validate_wake_stop_gate_bytes(result, self.hook)

    def test_malformed_hook_structure_and_receipt_fail_closed(self):
        for data in (b'{"hooks":[]}', b'{"hooks":{"UserPromptSubmit":{}}}', b'{"hooks":{"UserPromptSubmit":[{}]}}'):
            with self.subTest(data=data), self.assertRaises(installer.DeployError):
                installer.patch_wake_stop_gate(data, self.hook)
        for value in ('false', 1, None, {}):
            with self.subTest(value=value), self.assertRaises(installer.DeployError):
                installer.receipt_wake_stop_gate({'wake_stop_gate_enabled': value})


class WakeStopGateTransactionTests(unittest.TestCase):
    def setUp(self):
        evidence = Path(__file__).resolve().parent / '.test-artifacts' / 'wake-stop-gate'
        evidence.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-', dir=evidence)
        self.home = Path(self.temp.name) / 'Profile With Spaces'
        self.home.mkdir()
        self.appdata = self.home / 'AppData/Roaming'
        self.state = self.home / '.agents/agent-office-deploy'
        self.settings = self.home / '.claude/settings.json'
        self.settings.parent.mkdir()
        self.before = b'{"env":{"FOREIGN":"keep"},"hooks":{"UserPromptSubmit":[{"hooks":[{"type":"command","command":"foreign.exe"}]}]}}\n'
        self.settings.write_bytes(self.before)
        self.python = Path(sys.executable).resolve()
        self.pin = mock.patch.object(installer, 'pin_python', return_value=self.python)
        self.pin.start()
        installed = installer.install(self.home, self.appdata, self.state, self.python)
        self.assertEqual(installed['status'], 'DEPLOYED')
        self.old_id = installed['transaction_id']
        self.inactive = self.settings.read_bytes()

    def tearDown(self):
        self.pin.stop()
        # Only files created in this test's explicit task-evidence directory.
        self.temp.cleanup()

    def upgrade(self, **kwargs):
        return installer.upgrade(self.home, self.appdata, self.state, self.old_id,
                                 self.python, no_delete=True, **kwargs)

    def receipt(self, transaction):
        return json.loads((self.state / 'transactions' / transaction / 'receipt.json').read_text())

    def test_preview_is_read_only_and_default_upgrade_is_inactive(self):
        before = sorted(str(p.relative_to(self.home)) for p in self.home.rglob('*'))
        preview = self.upgrade(preview=True, enable_wake_stop_gate=True)
        self.assertEqual(preview['status'], 'READY')
        self.assertEqual(preview['writes_performed'], 0)
        self.assertTrue(preview['wake_stop_gate_enabled'])
        self.assertEqual(self.settings.read_bytes(), self.inactive)
        self.assertEqual(sorted(str(p.relative_to(self.home)) for p in self.home.rglob('*')), before)
        result = self.upgrade()
        self.assertEqual(result['status'], 'DEPLOYED')
        self.assertFalse(installer.receipt_wake_stop_gate(self.receipt(result['transaction_id'])))
        self.assertEqual(self.settings.read_bytes(), self.inactive)

    def test_activation_rollback_recover_and_future_upgrade_preserve_hook(self):
        result = self.upgrade(enable_wake_stop_gate=True)
        new_id = result['transaction_id']
        self.assertTrue(self.receipt(new_id)['wake_stop_gate_enabled'])
        active = self.settings.read_bytes()
        self.assertIn(b'foreign.exe', active)
        desired = installer.wake_stop_hook(self.python, self.home / installer.RUNTIME_RELATIVE)
        installer.validate_wake_stop_gate_bytes(active, desired)
        self.assertEqual(installer.verify_install(self.home, self.appdata, self.state, self.python)['status'], 'CURRENT')
        self.assertEqual(installer.rollback(self.home, self.appdata, self.state, new_id)['status'], 'ROLLED_BACK')
        self.assertEqual(self.settings.read_bytes(), self.inactive)
        self.assertEqual(installer.recover(self.home, self.appdata, self.state, new_id, self.python)['status'], 'DEPLOYED')
        self.assertEqual(self.settings.read_bytes(), active)
        next_result = installer.upgrade(self.home, self.appdata, self.state, new_id, self.python, no_delete=True)
        self.assertTrue(self.receipt(next_result['transaction_id'])['wake_stop_gate_enabled'])
        self.assertEqual(self.settings.read_bytes(), active)

    def test_tolerant_verify_rejects_owned_drift_but_accepts_foreign_fields(self):
        self.upgrade(enable_wake_stop_gate=True)
        active = json.loads(self.settings.read_text())
        active['skillUsage'] = {'foreign': 42}
        self.settings.write_text(json.dumps(active))
        self.assertEqual(installer.verify_install(self.home, self.appdata, self.state, self.python, strict=False)['status'], 'CURRENT')
        active['hooks']['UserPromptSubmit'][-1]['hooks'][0]['args'][-1] = 'D:/wrong/office.cmd'
        self.settings.write_text(json.dumps(active))
        self.assertNotEqual(installer.verify_install(self.home, self.appdata, self.state, self.python, strict=False)['status'], 'CURRENT')

    def test_upgrade_refuses_existing_unowned_gate_without_writes(self):
        desired = installer.wake_stop_hook(self.python, self.home / installer.RUNTIME_RELATIVE)
        self.settings.write_bytes(installer.patch_wake_stop_gate(self.inactive, desired))
        before = self.settings.read_bytes()
        with self.assertRaises(installer.DeployError):
            self.upgrade(preview=True, enable_wake_stop_gate=True)
        self.assertEqual(self.settings.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
