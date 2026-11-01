"""Exercise native relay metadata using the actual installed private payload."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import install_agent_office as installer


class AntigravityInstalledPayloadTests(unittest.TestCase):
    def setUp(self):
        # The test runner binds TEMP to its own task directory. Every path here
        # is synthetic; the owner profile and live Office store are never used.
        self.temp = tempfile.TemporaryDirectory(prefix="office-native-payload-")
        self.root = Path(self.temp.name)
        self.home = self.root / "Synthetic Profile With Spaces"
        self.appdata = self.home / "AppData" / "Roaming"
        self.state = self.home / ".agents" / "agent-office-deploy"
        self.python = Path(sys.executable).resolve()
        self.settings = self.home / ".claude" / "settings.json"
        self.settings.parent.mkdir(parents=True)
        self.foreign = b'{"env":{"MCP_TOOL_TIMEOUT":"100000000"},"hooks":{"UserPromptSubmit":[{"hooks":[{"type":"command","command":"foreign.exe"}]}]}}\n'
        self.settings.write_bytes(self.foreign)
        self.coord = self.home / ".agents" / "coord"
        self.durable = self.coord / "office" / "inbox" / "fixture.jsonl"
        self.durable.parent.mkdir(parents=True)
        self.durable.write_bytes(b'{"fixture":"preserve"}\n')

    def tearDown(self):
        self.temp.cleanup()

    def install(self):
        return installer.install(self.home, self.appdata, self.state, self.python)

    def probe_installed(self):
        runtime = self.home / installer.RUNTIME_RELATIVE
        code = "\n".join([
            "import json, sys, uuid",
            "from pathlib import Path",
            "sys.path.insert(0, " + repr(str(runtime)) + ")",
            "from coordfs.antigravity_relay import NativeObservation, record_hook_state",
            "from coordfs.antigravity_transport import Binding",
            "from coordfs.wake import Wake",
            "coord = Path(" + repr(str(self.coord)) + ")",
            "state = coord / ('synthetic-native-state-' + uuid.uuid4().hex + '.json')",
            "native_id = str(uuid.UUID('00000000-0000-4000-8000-000000000011'))",
            "wake = Wake(home=coord)",
            "observer = NativeObservation(state, native_id, now=lambda: 1700000000.0)",
            "wake.now = lambda: 1700000000.0",
            "states = [observer.state()]",
            "record_hook_state(wake, state, event='PreInvocation', payload={'conversationId':native_id, 'invocationNum':1})",
            "states.append(observer.state())",
            "record_hook_state(wake, state, event='Stop', payload={'conversationId':native_id, 'fullyIdle':True})",
            "states.append(observer.state())",
            "print(json.dumps({'states': states, 'module': str(Path(sys.modules['coordfs.antigravity_relay'].__file__).resolve())}))",
        ])
        result = subprocess.run(
            [str(self.python), "-I", "-B", "-X", "utf8", "-c", code],
            cwd=self.root, capture_output=True, encoding="utf-8", timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["states"], ["unknown", "busy", "idle"])
        self.assertEqual(Path(value["module"]), (runtime / "coordfs" / "antigravity_relay.py").resolve())

    def test_installed_runtime_can_process_native_metadata_without_checkout_imports(self):
        receipt = self.install()
        self.assertEqual(receipt["status"], "DEPLOYED")
        self.probe_installed()
        self.assertEqual(self.settings.read_bytes(), self.foreign)
        self.assertEqual(self.durable.read_bytes(), b'{"fixture":"preserve"}\n')
        result = installer.verify_install(self.home, self.appdata, self.state, self.python)
        self.assertEqual(result["status"], "CURRENT")

    def test_missing_native_module_is_detected_in_installed_runtime(self):
        self.install()
        runtime = self.home / installer.RUNTIME_RELATIVE
        # Keep the created module as evidence; moving rather than deleting it
        # also proves omission is treated as payload drift, never as CURRENT.
        module = runtime / "coordfs" / "antigravity_transport.py"
        retained = self.root / "retained-native-module.py"
        module.rename(retained)
        result = installer.verify_install(self.home, self.appdata, self.state, self.python)
        self.assertNotEqual(result["status"], "CURRENT")
        self.assertTrue(retained.is_file())

    def test_installed_checkpoint_reads_and_consumes_both_streams(self):
        self.install()
        runtime = self.home / installer.RUNTIME_RELATIVE
        seed_code = "\n".join([
            "import json, sys",
            "from pathlib import Path",
            "sys.path.insert(0, " + repr(str(runtime)) + ")",
            "from coordfs.office import Office",
            "from coordfs import office_cli",
            "coord = Path(" + repr(str(self.coord)) + ")",
            "target = Office(harness='checkpoint-fixture', session_id='synthetic-session', home=coord)",
            "assert target.presence(ttl_s=900, status='idle', workspace=" + repr(str(self.root)) + ")[\"status\"] == 'ok'",
            "sender = Office(agent_id='checkpoint-fixture-sender', home=coord)",
            "private = sender.send(target.agent_id, 'private fixture message')",
            "channel = sender.send(None, 'task fixture message', channel='checkpoint-fixture-task')",
            "assert private['status'] == channel['status'] == 'ok'",
            "print(json.dumps({'private_id': private['id'], 'channel_id': channel['id'], 'agent': target.agent_id, 'module': str(Path(office_cli.__file__).resolve())}))",
        ])
        seeded = subprocess.run(
            [str(self.python), "-I", "-B", "-X", "utf8", "-c", seed_code],
            cwd=self.root, capture_output=True, encoding="utf-8", timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(seeded.returncode, 0, seeded.stderr)
        seed = json.loads(seeded.stdout)
        self.assertEqual(Path(seed["module"]), (runtime / "coordfs" / "office_cli.py").resolve())
        command = [str(self.python), "-I", "-B", "-X", "utf8", str(runtime / "office.py"),
                   "--runtime-home", str(self.coord), "--harness", "checkpoint-fixture",
                   "--session-id", "synthetic-session", "checkpoint", "--channel", "checkpoint-fixture-task",
                   "--workspace", str(self.root)]
        results = []
        for _ in range(2):
            done = subprocess.run(command, cwd=self.root, capture_output=True, encoding="utf-8", timeout=30,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(done.returncode, 0, done.stderr)
            results.append(json.loads(done.stdout))
        first, second = results
        self.assertEqual(first["code"], "checkpoint_complete")
        self.assertEqual(first["presence"]["meta"]["status"], "busy")
        self.assertEqual(first["inbox"]["agent"], seed["agent"])
        self.assertEqual(first["inbox"]["messages"][0]["untrusted_message"]["id"], seed["private_id"])
        self.assertEqual(first["channel"]["messages"][0]["untrusted_message"]["id"], seed["channel_id"])
        self.assertTrue(first["inbox"]["cursor_advanced"])
        self.assertTrue(first["channel"]["cursor_advanced"])
        self.assertEqual(second["inbox"]["messages"], [])
        self.assertEqual(second["channel"]["messages"], [])
        self.assertEqual(self.settings.read_bytes(), self.foreign)
        self.assertEqual(self.durable.read_bytes(), b'{"fixture":"preserve"}\n')
        self.assertEqual(installer.verify_install(self.home, self.appdata, self.state, self.python)["status"], "CURRENT")

    def test_no_delete_upgrade_rollback_recover_preserves_native_payload_and_store(self):
        first = self.install()
        before_runtime = installer.snapshot(self.home / installer.RUNTIME_RELATIVE)
        preview = installer.upgrade(self.home, self.appdata, self.state, first["transaction_id"],
                                    self.python, no_delete=True, preview=True)
        self.assertEqual(preview["status"], "READY")
        self.assertEqual(preview["writes_performed"], 0)
        self.assertEqual(installer.snapshot(self.home / installer.RUNTIME_RELATIVE), before_runtime)
        upgraded = installer.upgrade(self.home, self.appdata, self.state, first["transaction_id"],
                                     self.python, no_delete=True)
        self.assertEqual(upgraded["status"], "DEPLOYED")
        self.probe_installed()
        self.assertEqual(installer.rollback(self.home, self.appdata, self.state,
                                            upgraded["transaction_id"])["status"], "ROLLED_BACK")
        self.probe_installed()
        self.assertEqual(installer.recover(self.home, self.appdata, self.state,
                                          upgraded["transaction_id"], self.python)["status"], "DEPLOYED")
        self.probe_installed()
        self.assertEqual(self.durable.read_bytes(), b'{"fixture":"preserve"}\n')
        self.assertEqual(self.settings.read_bytes(), self.foreign)


if __name__ == "__main__":
    unittest.main()
