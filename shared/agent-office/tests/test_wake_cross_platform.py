"""Desktop wake bindings migrated between OSes (Windows workstation store -> Linux server).

The Office server runs on Linux, but the four active Claude Desktop bindings were enrolled on
the Windows workstation with Windows workspaces. A desktop dispatch targets a native session
id, never the path, so the server must accept the foreign spelling (validated, no traversal)
and must still refuse it for local CLI drivers, which really run in that directory.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from coordfs.errors import CoordError
from coordfs.leases import format_ts
from coordfs.office import Office
from coordfs.wake import Wake

TARGET = "claude-desktop-target"
SENDER = "codex-relay-sender"
LOCAL_ID = "local_123e4567-e89b-12d3-a456-426614174000"
WINDOWS_WS = 'C:\\fixtures\\office-wake\\synthetic-workspace'
POSIX_WS = "/srv/ai-lab/uat-desktop-ws"


def writable_mkdtemp(prefix: str) -> str:
    if os.name != "nt":
        return tempfile.mkdtemp(prefix=prefix)
    original_mkdir = os.mkdir

    def inherited_acl(path, _mode=0o777, *args, **kwargs):
        return original_mkdir(path, 0o777, *args, **kwargs)

    with mock.patch.object(tempfile._os, "mkdir", inherited_acl):
        return tempfile.mkdtemp(prefix=prefix)


class CrossPlatformDesktopBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(writable_mkdtemp(prefix="office-wake-xplat-"))
        self.home = self.root / "coord"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(parents=True)
        self.clock = [datetime(2030, 1, 1, 17, 1).timestamp()]
        for participant in (TARGET, SENDER):
            Office(agent_id=participant, home=self.home)._remember_canonical_agent()

    # Linux server emulation: whatever the test host is, a Windows path is foreign.
    def as_linux_server(self):
        return mock.patch("coordfs.wake._native_workspace_flavor", return_value="posix")

    def wake(self) -> Wake:
        return Wake(home=self.home, now=lambda: self.clock[0], driver_timeout_s=2.0)

    def enroll(self, wake: Wake, *, binding_id: str, driver: str, workspace: str | None = None,
               triggers: list[str] | None = None, correlation_required: bool = False) -> None:
        wake.enroll(
            binding_id=binding_id,
            office_agent=TARGET,
            driver=driver,
            native_thread_id=LOCAL_ID if driver.startswith("claude-desktop") else "thread-cli-1",
            workspace=workspace or str(self.workspace),
            enrolled_by="owner",
            allowed_wakers=[SENDER],
            allowed_triggers=triggers or ["message"],
            wake_kinds=["question", "handoff"],
            budget_per_day=6,
            expires_at=format_ts(self.clock[0] + 30 * 86400),
            owner_confirm=True,
            correlation_required=correlation_required,
        )
        wake.verify(binding_id, owner_confirm=True)

    def migrate(self, wake: Wake, binding_id: str, workspace: str) -> None:
        """Rewrite a stored binding as it arrives on the server after the store migration."""
        path = wake._binding_path(binding_id)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["workspace"] = workspace
        path.write_text(json.dumps(record), encoding="utf-8")

    def dispatch_files(self) -> list[Path]:
        directory = self.home / "office" / "wake" / "desktop-dispatch"
        return sorted(directory.glob("*.json")) if directory.is_dir() else []

    def test_migrated_windows_relay_binding_dispatches_on_linux_server(self) -> None:
        wake = self.wake()
        self.enroll(wake, binding_id="helpdesk-relay", driver="claude-desktop-relay")
        self.migrate(wake, "helpdesk-relay", WINDOWS_WS)
        Office(agent_id=SENDER, home=self.home, now=lambda: self.clock[0]).send(
            TARGET, "synthetic", kind="question")
        with self.as_linux_server(), mock.patch("coordfs.wake.subprocess.run") as process:
            loaded = wake._load_binding("helpdesk-relay")
            result = wake.tick()
        process.assert_not_called()
        self.assertEqual(loaded["workspace"], WINDOWS_WS)
        self.assertEqual(loaded["execution_mode"], "local")
        self.assertEqual(result["counts"]["dispatched"], 1, result)
        dispatch = json.loads(self.dispatch_files()[0].read_text(encoding="utf-8"))
        self.assertEqual(dispatch["target_local_id"], LOCAL_ID)
        self.assertNotIn("workspace_unavailable", [row["reason"] for row in result["decisions"]])

    def test_same_migrated_binding_on_the_windows_host_keeps_the_native_directory_gate(self) -> None:
        wake = self.wake()
        self.enroll(wake, binding_id="helpdesk-relay", driver="claude-desktop-relay")
        missing = self.root / "gone"
        self.migrate(wake, "helpdesk-relay", str(missing))
        Office(agent_id=SENDER, home=self.home, now=lambda: self.clock[0]).send(
            TARGET, "synthetic", kind="question")
        result = wake.tick()
        self.assertIn("workspace_unavailable", [row["reason"] for row in result["decisions"]])
        self.assertEqual(self.dispatch_files(), [])

    @unittest.skipUnless(os.name == "nt", "requires native Windows pathlib")
    def test_windows_spelling_missing_on_windows_host_keeps_directory_gate(self) -> None:
        wake = self.wake()
        self.enroll(wake, binding_id="windows-relay", driver="claude-desktop-relay")
        self.migrate(wake, "windows-relay", WINDOWS_WS)
        Office(agent_id=SENDER, home=self.home, now=lambda: self.clock[0]).send(
            TARGET, "synthetic", kind="question")
        with mock.patch("coordfs.wake._native_workspace_flavor", return_value="windows"):
            result = wake.tick()
        self.assertIn("workspace_unavailable", [row["reason"] for row in result["decisions"]])
        self.assertEqual(self.dispatch_files(), [])

    def test_migrated_windows_self_pull_binding_loads_and_is_not_blocked(self) -> None:
        wake = self.wake()
        self.enroll(wake, binding_id="lead-pull", driver="claude-desktop-self-pull",
                    correlation_required=True)
        self.migrate(wake, "lead-pull", WINDOWS_WS)
        with self.as_linux_server(), mock.patch("coordfs.wake.subprocess.run") as process:
            loaded = wake._load_binding("lead-pull")
            result = wake.tick()
        process.assert_not_called()
        self.assertEqual(loaded["workspace"], WINDOWS_WS)
        reasons = [row.get("reason") for row in result.get("decisions", [])]
        self.assertNotIn("invalid_workspace", reasons)
        self.assertNotIn("workspace_unavailable", reasons)

    @unittest.skipUnless(os.name == 'nt', 'requires native Windows directory gate')
    def test_codex_native_workspace_keeps_directory_gate(self) -> None:
        wake = self.wake()
        wake.enroll(binding_id='codex-native', office_agent=TARGET,
                    driver='codex-desktop-relay',
                    native_thread_id='123e4567-e89b-12d3-a456-426614174000',
                    workspace=str(self.workspace), enrolled_by='owner',
                    allowed_wakers=[SENDER], allowed_triggers=['message'],
                    wake_kinds=['question'], expires_at=format_ts(self.clock[0] + 3600),
                    owner_confirm=True)
        wake.verify('codex-native', owner_confirm=True)
        self.migrate(wake, 'codex-native', WINDOWS_WS)
        Office(agent_id=SENDER, home=self.home, now=lambda: self.clock[0]).send(
            TARGET, 'synthetic', kind='question')
        result = wake.tick()
        self.assertIn('workspace_unavailable', [row['reason'] for row in result['decisions']])
        self.assertEqual(self.dispatch_files(), [])

    def test_local_cli_driver_stays_strict_for_a_foreign_path(self) -> None:
        wake = self.wake()
        self.enroll(wake, binding_id="cli-local", driver="codex-cli")
        self.migrate(wake, "cli-local", WINDOWS_WS)
        with self.as_linux_server():
            with self.assertRaises(CoordError) as caught:
                wake._load_binding("cli-local")
        self.assertEqual(caught.exception.code, "invalid_workspace")

    def test_foreign_desktop_workspace_spelling_is_still_validated(self) -> None:
        wake = self.wake()
        self.enroll(wake, binding_id="bad-relay", driver="claude-desktop-relay")
        for bad in ('C:\\fixtures\\office-wake\\..\\synthetic-workspace', 'C:\\fixtures\\office-wake\\synthetic\x07-workspace', "relative\\ws"):
            with self.subTest(workspace=bad):
                self.migrate(wake, "bad-relay", bad)
                with self.as_linux_server():
                    with self.assertRaises(CoordError) as caught:
                        wake._load_binding("bad-relay")
                self.assertIn(caught.exception.code, {"invalid_workspace", "invalid_wake_binding"})

    def test_ambiguous_double_slash_workspace_does_not_bypass_local_gate(self) -> None:
        wake = self.wake()
        self.enroll(wake, binding_id="ambiguous-relay", driver="claude-desktop-relay")
        self.migrate(wake, "ambiguous-relay", "//missing/share/path")
        Office(agent_id=SENDER, home=self.home, now=lambda: self.clock[0]).send(
            TARGET, "synthetic", kind="question")
        with self.as_linux_server():
            result = wake.tick()
        self.assertIn("workspace_unavailable", [row["reason"] for row in result["decisions"]])
        self.assertEqual(self.dispatch_files(), [])

    def test_server_can_enroll_a_desktop_binding_for_a_windows_session(self) -> None:
        wake = self.wake()
        with self.as_linux_server():
            self.enroll(wake, binding_id="new-relay", driver="claude-desktop-relay", workspace=WINDOWS_WS)
            loaded = wake._load_binding("new-relay")
        self.assertEqual(loaded["workspace"], WINDOWS_WS)

    def test_server_refuses_a_foreign_path_for_a_local_cli_enrollment(self) -> None:
        wake = self.wake()
        with self.as_linux_server():
            with self.assertRaises(CoordError) as caught:
                self.enroll(wake, binding_id="new-cli", driver="codex-cli", workspace=WINDOWS_WS)
        self.assertEqual(caught.exception.code, "invalid_workspace")

    def test_posix_path_is_foreign_on_a_windows_host_for_desktop_only(self) -> None:
        wake = self.wake()
        self.enroll(wake, binding_id="posix-relay", driver="claude-desktop-relay")
        self.migrate(wake, "posix-relay", POSIX_WS)
        with mock.patch("coordfs.wake._native_workspace_flavor", return_value="windows"):
            loaded = wake._load_binding("posix-relay")
        self.assertEqual(loaded["workspace"], POSIX_WS)


if __name__ == "__main__":
    unittest.main()
