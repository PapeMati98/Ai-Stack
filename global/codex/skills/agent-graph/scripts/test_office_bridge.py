from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location("office_bridge", Path(__file__).with_name("office_bridge.py"))
assert SPEC and SPEC.loader
office_bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(office_bridge)


class OfficeBridgeTests(unittest.TestCase):
    def _write_runtime_marker(self, root: Path, *, version: str = "9.4.0") -> None:
        (root / "coordfs").mkdir(parents=True)
        (root / "office.py").write_bytes(b"# synthetic runtime\n")
        (root / "coordfs" / "office.py").write_bytes(b"# synthetic office module\n")
        source_fingerprint = office_bridge._runtime_source_fingerprint(root)
        self.assertIsNotNone(source_fingerprint)
        (root / ".agent-office-runtime.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "managed_by": "agent-office-installer",
                    "package_id": "agent-office",
                    "package_version": version,
                    "package_fingerprint": "a" * 64,
                    "source_fingerprint": source_fingerprint,
                    "tool": "runtime",
                    "python_executable": sys.executable,
                }
            ),
            encoding="utf-8",
        )

    def test_accepts_newer_semver_when_runtime_sources_match(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-graph-office-marker-") as temp:
            root = Path(temp)
            self._write_runtime_marker(root, version="0.2.0")
            with mock.patch.object(office_bridge, "_runtime_root", return_value=root):
                command_info = office_bridge._runtime_command()
            self.assertIsNotNone(command_info)
            command, resolved_root = command_info
            self.assertEqual(resolved_root, root)
            self.assertEqual(command[:3], [sys.executable, "-B", "-I"])

    def test_rejects_marker_when_runtime_source_fingerprint_does_not_match(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-graph-office-fingerprint-") as temp:
            root = Path(temp)
            self._write_runtime_marker(root)
            (root / "office.py").write_bytes(b"# tampered runtime\n")
            with mock.patch.object(office_bridge, "_runtime_root", return_value=root):
                self.assertIsNone(office_bridge._runtime_command())

    def test_missing_runtime_fails_open_without_waiting(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-graph-no-office-") as temp:
            started = time.monotonic()
            with mock.patch.object(office_bridge, "_runtime_root", return_value=Path(temp) / "missing"):
                result = office_bridge.publish("run-synthetic-02", "node-a", "blocked", office_home=Path(temp))
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["code"], "office_runtime_unavailable")
            self.assertLess(time.monotonic() - started, 2.0)

    def test_rejects_path_like_input_without_starting_office(self) -> None:
        result = office_bridge.publish(r"C:\secret\project", "node-1", "dispatch")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["code"], "invalid_input")

    def test_real_installed_runtime_publishes_to_temp_and_reader_sees_exact_payload(self) -> None:
        if office_bridge._runtime_command() is None:
            self.skipTest("managed Agent Office runtime is not installed")
        with tempfile.TemporaryDirectory(prefix="agent-graph-office-") as temp:
            home = Path(temp) / "coord-home"
            result = office_bridge.publish("run-synthetic-01", "node-a", "dispatch", office_home=home)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["code"], "published")
            self.assertEqual(result["event_status"], "dispatch")

            command, _ = office_bridge._runtime_command()  # checked above; test uses the same pinned command
            reader = subprocess.run(
                command + ["--home", str(home), "--agent", "reader", "read", "--channel", result["channel"]],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=2,
                check=False,
                shell=False,
                text=True,
                encoding="utf-8",
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            self.assertEqual(reader.returncode, 0, reader.stderr)
            payload = json.loads(reader.stdout)
            self.assertEqual(payload["count"], 1)
            self.assertEqual(
                payload["messages"][0]["untrusted_message"]["body"],
                '{"run":"run-synthetic-01","node":"node-a","status":"dispatch"}',
            )
            self.assertEqual(payload["messages"][0]["untrusted_message"]["to"], "#" + result["channel"])


class OfficeLocationTests(unittest.TestCase):
    """Store selection after the 2026-10-04 server cutover (no silent local fallback)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="agent-graph-office-location-")
        self.root = Path(self.tmp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.config = self.runtime / office_bridge.REMOTE_CONFIG_NAME
        self.config.write_bytes(b'{"synthetic": true}\n')
        self._marker(office_bridge.hashlib.sha256(self.config.read_bytes()).hexdigest())
        self.patches = [
            mock.patch.object(office_bridge, "_runtime_root", return_value=self.runtime),
            mock.patch.dict(office_bridge.os.environ, {"USERPROFILE": str(self.root / "profile")}),
        ]
        for patch in self.patches:
            patch.start()
        office_bridge.os.environ.pop("COORDFS_HOME", None)

    def tearDown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def _marker(self, remote_sha: str | None) -> None:
        marker = {} if remote_sha is None else {"remote_config_sha256": remote_sha}
        (self.runtime / ".agent-office-runtime.json").write_text(json.dumps(marker), encoding="utf-8")

    def test_pinned_remote_config_selects_the_server(self) -> None:
        self.assertEqual(office_bridge._office_location(None), (["--remote-config", str(self.config)], True))

    def test_remote_config_hash_mismatch_blocks_instead_of_falling_back_local(self) -> None:
        self._marker("0" * 64)
        self.assertIsNone(office_bridge._office_location(None))
        self._marker(None)
        self.assertIsNone(office_bridge._office_location(None))

    def test_remote_config_that_is_not_a_file_blocks(self) -> None:
        self.config.unlink()
        self.config.mkdir()
        self.assertIsNone(office_bridge._office_location(None))

    def test_without_remote_config_the_local_default_home_is_used(self) -> None:
        self.config.unlink()
        home = self.root / "profile" / ".agents" / "coord"
        self.assertEqual(office_bridge._office_location(None), (["--home", str(home)], False))

    def test_explicit_home_and_coordfs_home_keep_the_local_store(self) -> None:
        explicit = self.root / "synthetic-home"
        self.assertEqual(office_bridge._office_location(explicit), (["--home", str(explicit)], False))
        with mock.patch.dict(office_bridge.os.environ, {"COORDFS_HOME": str(explicit)}):
            self.assertEqual(office_bridge._office_location(None), (["--home", str(explicit)], False))

    def test_frozen_store_is_never_selected(self) -> None:
        frozen = self.root / "frozen-home"
        (frozen / "office").mkdir(parents=True)
        (frozen / "office" / "FROZEN").write_text("", encoding="utf-8")
        self.assertIsNone(office_bridge._office_location(frozen))
        self.config.unlink()
        with mock.patch.dict(office_bridge.os.environ, {"COORDFS_HOME": str(frozen)}):
            self.assertIsNone(office_bridge._office_location(None))

    def test_frozen_store_blocks_publish_before_any_office_call(self) -> None:
        frozen = self.root / "frozen-home"
        (frozen / "office").mkdir(parents=True)
        (frozen / "office" / "FROZEN").write_text("", encoding="utf-8")
        with mock.patch.object(office_bridge, "_runtime_command", return_value=(["python"], self.runtime)),                 mock.patch.object(office_bridge, "_run") as run:
            result = office_bridge.publish("run-1", "node-a", "dispatch", office_home=frozen)
        self.assertEqual(result["code"], "office_store_unavailable")
        run.assert_not_called()

    def test_remote_publish_passes_remote_config_and_remote_budget(self) -> None:
        calls = []

        def fake_run(command, args, location, timeout):
            calls.append((list(location), timeout))
            return {"status": "ok"}

        with mock.patch.object(office_bridge, "_runtime_command", return_value=(["python"], self.runtime)),                 mock.patch.object(office_bridge, "_run", side_effect=fake_run):
            result = office_bridge.publish("run-1", "node-a", "dispatch")
        self.assertEqual(result["code"], "published")
        self.assertEqual([c[0] for c in calls], [["--remote-config", str(self.config)]] * 2)
        self.assertTrue(all(0.9 < c[1] <= office_bridge.REMOTE_CALL_SECONDS for c in calls))

    def test_runtime_fingerprint_ignores_the_instance_remote_config(self) -> None:
        (self.runtime / "office.py").write_bytes(b"# synthetic\n")
        before = office_bridge._runtime_source_fingerprint(self.runtime)
        self.config.write_bytes(b'{"other": true}\n')
        self.assertEqual(office_bridge._runtime_source_fingerprint(self.runtime), before)


if __name__ == "__main__":
    unittest.main()
