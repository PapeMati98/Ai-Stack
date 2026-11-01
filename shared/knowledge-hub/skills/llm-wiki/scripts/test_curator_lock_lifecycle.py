"""Curator owner evidence and bounded begin-task waits on synthetic vaults."""

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import knowledge


HERE = Path(__file__).resolve().parent


class CuratorLockLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name) / "vault"
        (self.vault / "governance").mkdir(parents=True)
        (self.vault / "knowledge-vault.json").write_text(
            json.dumps({"curator_machine": knowledge.local_machine_name()}), encoding="utf-8"
        )
        self.lock = self.vault / "governance" / "curator.lock"
        self.receipts = Path(self.temp.name) / "receipts"

    def tearDown(self):
        self.temp.cleanup()

    def write_lock(self, **overrides):
        payload = {
            "lock_id": "synthetic-lock", "agent": "test-curator",
            "acquired_at": knowledge.iso_time(knowledge.now_utc()),
            "machine": knowledge.local_machine_name(), "process_id": os.getpid(),
            "owner_process_started_at": knowledge.iso_time(knowledge.windows_process_started_at(os.getpid()))
            if os.name == "nt" else "2026-09-29T00:00:00Z",
            "owner_source": "explicit", "helper_process_id": 1,
        }
        payload.update(overrides)
        self.lock.write_text(json.dumps(payload), encoding="utf-8")
        return self.lock.read_bytes()

    @unittest.skipUnless(os.name == "nt", "PowerShell and GetProcessTimes require Windows")
    def test_powershell_acquire_records_parent_and_explicit_owner(self):
        powershell = shutil.which("powershell.exe")
        if not powershell:
            self.skipTest("powershell.exe unavailable")
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                vault = Path(self.temp.name) / ("explicit" if explicit else "parent")
                (vault / "governance").mkdir(parents=True)
                (vault / "knowledge-vault.json").write_text(
                    json.dumps({"curator_machine": knowledge.local_machine_name()}), encoding="utf-8"
                )
                cmd = [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass",
                       "-File", str(HERE / "knowledge.ps1"),
                       "-Action", "acquire-lock", "-Vault", str(vault), "-Agent", "test-curator"]
                if explicit:
                    cmd.extend(["-OwnerPid", str(os.getpid())])
                run = subprocess.run(cmd, capture_output=True, text=True, check=False)
                self.assertEqual(run.returncode, 0, run.stderr)
                lock = json.loads((vault / "governance" / "curator.lock").read_text(encoding="utf-8"))
                self.assertNotEqual(lock["helper_process_id"], os.getpid())
                if not explicit and lock["owner_source"] == "helper":
                    # Restricted CI hosts may deny the Win32_Process parent query.
                    self.assertEqual(lock["process_id"], lock["helper_process_id"])
                    self.assertEqual(knowledge.curator_lock_owner_state(vault / "governance" / "curator.lock")["state"], "unknown")
                    continue
                self.assertEqual(lock["process_id"], os.getpid())
                self.assertEqual(lock["owner_source"], "explicit" if explicit else "parent")
                self.assertEqual(lock["owner_process_name"].casefold(), "python.exe")
                self.assertIsNotNone(knowledge.parse_recorded_time(lock["owner_process_started_at"]))

    @unittest.skipUnless(os.name == "nt", "PowerShell process ownership requires Windows")
    def test_shell_parent_is_unknown_after_shell_exits(self):
        powershell = shutil.which("powershell.exe")
        cmd_shell = shutil.which("cmd.exe")
        if not powershell or not cmd_shell:
            self.skipTest("cmd.exe or powershell.exe unavailable")
        cmd = [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", str(HERE / "knowledge.ps1"),
               "-Action", "acquire-lock", "-Vault", str(self.vault), "-Agent", "test-curator"]
        run = subprocess.run(subprocess.list2cmdline(cmd), shell=True,
                             capture_output=True, text=True, check=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        original = self.lock.read_bytes()
        lock = json.loads(original)
        if lock["owner_source"] == "helper":
            self.assertEqual(knowledge.curator_lock_owner_state(self.lock)["state"], "unknown")
            self.skipTest("Win32_Process parent query denied in this environment")
        self.assertEqual(lock["owner_process_name"].casefold(), "cmd.exe")
        self.assertEqual(lock["owner_source"], "parent-shell")
        self.assertEqual(knowledge.curator_lock_owner_state(self.lock)["state"], "unknown")
        self.assertEqual(self.lock.read_bytes(), original)

    @unittest.skipUnless(os.name == "nt", "PowerShell process ownership requires Windows")
    def test_shell_parent_name_marks_lock_unknown(self):
        powershell = shutil.which("powershell.exe")
        if not powershell:
            self.skipTest("powershell.exe unavailable")
        # Stub only the parent query so this branch runs even where CIM is denied.
        wrapper = Path(self.temp.name) / "acquire-from-shell.ps1"
        wrapper.write_text("""
param([string]$VaultPath, [string]$KnowledgeScript, [int]$OwnerProcessId)
function Get-CimInstance {
    [CmdletBinding()]
    param([string]$ClassName, [string]$Filter)
    if ($Filter -eq "ProcessId=$PID") {
        return [pscustomobject]@{ ParentProcessId = $OwnerProcessId }
    }
    return [pscustomobject]@{
        Name = 'cmd.exe'
        CreationDate = (Get-Process -Id $OwnerProcessId -ErrorAction Stop).StartTime
    }
}
& $KnowledgeScript -Action acquire-lock -Vault $VaultPath -Agent test-curator
""", encoding="utf-8")
        run = subprocess.run([powershell, "-NoProfile", "-ExecutionPolicy", "Bypass",
                              "-File", str(wrapper), "-VaultPath", str(self.vault),
                              "-KnowledgeScript", str(HERE / "knowledge.ps1"),
                              "-OwnerProcessId", str(os.getpid())],
                             capture_output=True, text=True, check=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        original = self.lock.read_bytes()
        lock = json.loads(original)
        self.assertEqual(lock["process_id"], os.getpid())
        self.assertEqual(lock["owner_process_name"], "cmd.exe")
        self.assertEqual(lock["owner_source"], "parent-shell")
        self.assertEqual(knowledge.curator_lock_owner_state(self.lock)["state"], "unknown")
        self.assertEqual(self.lock.read_bytes(), original)

    @unittest.skipUnless(os.name == "nt", "GetProcessTimes requires Windows")
    def test_owner_state_alive_dead_and_pid_reuse(self):
        self.write_lock()
        self.assertEqual(knowledge.curator_lock_owner_state(self.lock)["state"], "alive")
        self.write_lock(process_id=99999999)
        self.assertEqual(knowledge.curator_lock_owner_state(self.lock)["state"], "dead")
        self.write_lock(owner_process_started_at="2000-01-01T00:00:00Z")
        self.assertEqual(knowledge.curator_lock_owner_state(self.lock)["state"], "dead")

    def test_owner_state_fail_closed_and_status(self):
        self.write_lock(owner_process_started_at=None)
        self.assertEqual(knowledge.curator_lock_owner_state(self.lock)["state"], "unknown")
        self.write_lock(machine="other-machine", process_id=99999999)
        self.assertEqual(knowledge.curator_lock_owner_state(self.lock)["state"], "unknown")
        self.write_lock(owner_source="helper", process_id=99999999)
        self.assertEqual(knowledge.curator_lock_owner_state(self.lock)["state"], "unknown")
        self.write_lock(owner_source="parent-shell", owner_process_name="cmd.exe", process_id=99999999)
        self.assertEqual(knowledge.curator_lock_owner_state(self.lock)["state"], "unknown")
        self.lock.write_bytes(b"{invalid")
        self.assertEqual(knowledge.curator_lock_owner_state(self.lock)["state"], "unknown")
        with mock.patch.object(knowledge, "read_regular_file_snapshot", side_effect=PermissionError):
            self.assertEqual(knowledge.curator_lock_owner_state(self.lock)["state"], "unknown")
        with mock.patch.object(knowledge, "git_dirty_assessment", return_value={
            "git_root": None, "change_count": 0, "changed_file_count": 0,
            "state": "clean", "oldest_age_hours": None, "age_unknown_count": 0,
            "write_blocked": False, "policy": "test",
        }), mock.patch.object(knowledge, "curation_health", return_value={}):
            self.assertEqual(knowledge.status(self.vault)["curator_lock_owner"]["state"], "unknown")

    def begin(self, wait_seconds, *, sleep=None, clock=None, status_side_effect=None):
        args = argparse.Namespace(
            agent="test-agent", session_id="synthetic-session", project_slug="test-project",
            require_context=False, receipt_root=str(self.receipts), limit=10,
            lock_wait_seconds=wait_seconds,
        )
        context = {
            "project_slug": "test-project", "project_resolution": "exact",
            "context_found": True, "related_candidates": [], "project_resolution_warning": None,
            "stable_state": [], "pending_state": [], "related_context": [],
        }
        status_value = {"curator_lock": False, "dirty_tree_state": "clean",
                        "dirty_tree_write_blocked": False, "curation_health": {}}
        with mock.patch.object(knowledge, "project_snapshot", return_value=("revision", {})), \
             mock.patch.object(knowledge, "status", side_effect=status_side_effect or (lambda _: status_value)), \
             mock.patch.object(knowledge, "lint", return_value={"error_count": 0, "warning_count": 0}), \
             mock.patch.object(knowledge, "project_brief", return_value=context), \
             mock.patch.object(knowledge, "project_context_fingerprint", return_value="fingerprint"):
            return knowledge.begin_task(self.vault, args, sleep=sleep, clock=clock)

    def test_wait_until_external_release(self):
        original = self.write_lock(owner_process_started_at=None)
        released = []

        def release():
            time.sleep(0.05)
            released.append(self.lock.read_bytes())
            self.lock.unlink()  # Synthetic test vault; the gateway never releases it.

        thread = threading.Thread(target=release)
        thread.start()
        try:
            result = self.begin(1, sleep=lambda _: time.sleep(0.01))
        finally:
            thread.join(timeout=2)
        self.assertEqual(result["status"], "COMPLETE")
        self.assertGreater(result["lock_wait_seconds"], 0)
        self.assertEqual(released, [original])

    def test_stale_deadline_and_zero_wait_preserve_lock_bytes(self):
        if os.name == "nt":
            original = self.write_lock(process_id=99999999)
            stale = self.begin(1200, sleep=lambda _: self.fail("stale lock must not sleep"))
            self.assertEqual(stale["failure_code"], "CURATOR_LOCK_STALE")
            self.assertEqual(stale["execution"]["attempts"][0]["curator_lock_owner"]["state"], "dead")
            self.assertEqual(self.lock.read_bytes(), original)
        original = self.write_lock(owner_process_started_at=None)
        ticks = [0.0]

        def sleep(seconds):
            ticks[0] += seconds

        deadline = self.begin(7, sleep=sleep, clock=lambda: ticks[0])
        self.assertEqual(deadline["failure_code"], "CURATOR_LOCK")
        self.assertEqual(deadline["lock_wait_seconds"], 7)
        self.assertEqual(self.lock.read_bytes(), original)
        ticks[0] = 0
        immediate = self.begin(0, sleep=lambda _: self.fail("zero wait must not sleep"), clock=lambda: ticks[0])
        self.assertEqual(immediate["failure_code"], "CURATOR_LOCK")
        self.assertEqual(immediate["lock_wait_seconds"], 0)
        self.assertEqual(self.lock.read_bytes(), original)

    def test_lock_after_read_waits_before_retry(self):
        original = self.write_lock(owner_process_started_at=None)
        self.lock.unlink()  # Arrange an initially unlocked synthetic vault.
        observed = []
        calls = [0]

        def status(_):
            calls[0] += 1
            if calls[0] == 1:
                self.lock.write_bytes(original)
            return {"curator_lock": False, "dirty_tree_state": "clean",
                    "dirty_tree_write_blocked": False, "curation_health": {}}

        def sleep(_):
            observed.append(self.lock.read_bytes())
            self.lock.unlink()  # Simulate release by another actor in the temp vault.

        result = self.begin(10, sleep=sleep, status_side_effect=status)
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(len(result["execution"]["attempts"]), 2)
        self.assertEqual(result["execution"]["attempts"][0]["failure_code"], "CURATOR_LOCK")
        self.assertEqual(observed, [original])


if __name__ == "__main__":
    unittest.main()
