"""Windows owner-child timeout recovery for an existing canonical intake pair."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

WORKTREE = Path(__file__).resolve().parents[2]
CARETAKER_ROOT = Path(os.environ.get(
    "CARETAKER_TIMEOUT_CARETAKER_ROOT", str(WORKTREE / "caretaker-release"),
))
GATEWAY_SOURCE = Path(os.environ.get(
    "CARETAKER_TIMEOUT_GATEWAY_SOURCE", str(WORKTREE / "gateway" / "knowledge.py"),
))
TASK_TMP = Path(os.environ.get(
    "CARETAKER_TIMEOUT_TEST_TMP", str(WORKTREE / "caretaker-release" / "tests" / ".test-tmp"),
))
UAT_INPUT = Path(os.environ.get(
    "CARETAKER_TIMEOUT_UAT_INPUT",
    str(Path(r"C:\Users\example-user\.agents\private-artifacts\kh-program\caretaker-throughput-20261011")
        / "lock-timeout50" / "UAT-INPUT.json"),
))

if str(CARETAKER_ROOT) not in sys.path:
    sys.path.insert(0, str(CARETAKER_ROOT))

from caretaker.checkpoint import INTAKE_LOCK_NAME, _machine_name, timeout_cleanup_context
from caretaker.hub import KnowledgeTransportError, _default_runner
from caretaker.process_tree import run_process_tree


def _run_hidden(args, *, cwd=None, check=True, **kwargs):
    return subprocess.run(
        list(map(str, args)), cwd=cwd, check=check, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **kwargs,
    )


def _git(root: Path, *args: str):
    return _run_hidden(
        ["git", "-c", "core.autocrlf=false", "-c", "core.quotePath=false", "-C", root, *args],
        cwd=root,
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pid_alive(pid: int) -> bool:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return ctypes.get_last_error() != 87
    try:
        exit_code = wintypes.DWORD()
        return not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value == 259
    finally:
        kernel32.CloseHandle(handle)


def _load_uat_input() -> dict:
    if not UAT_INPUT.is_file():
        raise unittest.SkipTest("prepared synthetic UAT input is not available on this host")
    payload = json.loads(UAT_INPUT.read_text(encoding="utf-8"))
    if payload.get("synthetic_only") is not True:
        raise AssertionError("UAT fixture must be explicitly synthetic")
    return payload


def _copy_uat_repo(root: Path, payload: dict) -> tuple[Path, dict]:
    fixture = Path(payload["fixture"]).resolve(strict=True)
    for relative, expected in payload["pins"].items():
        if _sha(fixture / Path(relative)) != expected:
            raise AssertionError(f"synthetic fixture pin mismatch: {relative}")
    shutil.copytree(fixture, root)
    vault = root / "vault"

    # Use the exact current generic gateway source under test in this isolated
    # synthetic checkout, then make its implementation part of the clone HEAD.
    shutil.copy2(GATEWAY_SOURCE, root / "skills" / "llm-wiki" / "scripts" / "knowledge.py")
    config_path = vault / "knowledge-vault.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.update({
        "auto_checkpoint_intake": True,
        "checkpoint_mode": "local-commit-only",
        "curator_machine": _machine_name(),
    })
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _git(root, "add", "vault/knowledge-vault.json", "skills/llm-wiki/scripts/knowledge.py")
    _git(root, "commit", "-m", "Prepare synthetic timeout recovery policy")
    return vault, payload["receipt"]


def _handoff_argv(vault: Path, note: Path) -> list[str]:
    script = vault.parent / "skills" / "llm-wiki" / "scripts" / "knowledge.py"
    return [
        sys.executable, "-X", "utf8", str(script), "record-handoff",
        "--vault", str(vault), "--path", str(note),
        "--title", "Owner checkpoint state", "--origin", "local-test",
        "--agent", "claude", "--classification", "internal",
        "--project-slug", "example-solutions-app",
        "--state-summary", "Example Solutions App owner checkpoint test state.",
        "--target-page", "wiki/projects/example-solutions-app.md",
    ]


def _install_timeout_hook(path: Path, lock_path: Path, marker: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    source = (
        "from pathlib import Path\n"
        "import subprocess, time\n"
        f"lock = Path({str(lock_path)!r})\n"
        f"marker = Path({str(marker)!r})\n"
        "_run = subprocess.run\n"
        "_fired = False\n"
        "def _wait_after_owner_lock(args, *a, **kw):\n"
        "    global _fired\n"
        "    command = args if isinstance(args, (list, tuple)) else [args]\n"
        "    first = Path(str(command[0])).name.casefold() if command else ''\n"
        "    if not _fired and first in {'git', 'git.exe'} and lock.exists():\n"
        "        _fired = True\n"
        "        marker.write_text('owner lock observed', encoding='utf-8')\n"
        "        time.sleep(30)\n"
        "    return _run(args, *a, **kw)\n"
        "subprocess.run = _wait_after_owner_lock\n"
    )
    (path / "sitecustomize.py").write_text(source, encoding="utf-8")


def _lock_writer_script(path: Path, lock_path: Path, proposal_id: str, machine: str,
                        *, make_index_lock: bool = False) -> None:
    source = r'''import datetime as dt, json, os, pathlib, sys, time, uuid
import ctypes
from ctypes import wintypes
lock = pathlib.Path(sys.argv[1])
proposal = sys.argv[2]
machine = sys.argv[3]
index = pathlib.Path(sys.argv[4]) if sys.argv[4] else None
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
class FT(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]
kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel.OpenProcess.restype = wintypes.HANDLE
kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, ctypes.POINTER(FT), ctypes.POINTER(FT), ctypes.POINTER(FT), ctypes.POINTER(FT))
kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
h = kernel.OpenProcess(0x1000, False, os.getpid())
created, exited, system, user = (FT() for _ in range(4))
assert h and kernel.GetProcessTimes(h, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user))
kernel.CloseHandle(h)
ticks = (created.high << 32) | created.low
started = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(microseconds=ticks // 10)
iso = lambda value: value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
payload = {"lock_id": uuid.uuid4().hex, "pid": os.getpid(), "machine": machine,
           "created_at": iso(dt.datetime.now(dt.timezone.utc)),
           "owner_process_started_at": iso(started), "intake_proposal_id": proposal,
           "intake_receipt_sha256": "a" * 64}
lock.write_text(json.dumps(payload) + "\n", encoding="utf-8")
if index is not None:
    index.write_bytes(b"foreign Git index lock\n")
time.sleep(60)
'''
    path.write_text(source, encoding="utf-8")


@unittest.skipUnless(os.name == "nt", "requires native Windows job objects")
class CheckpointTimeoutResumeTests(unittest.TestCase):
    def setUp(self):
        TASK_TMP.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="fix50-", dir=TASK_TMP))

    def test_real_timeout_archives_exact_owner_and_normal_intake_resumes_same_pair(self):
        input_payload = _load_uat_input()
        root = self.root / "uat-clone"
        vault, receipt = _copy_uat_repo(root, input_payload)
        note = root / "note.md"
        original_unrelated = (root / "unrelated.txt").read_bytes()
        expected_ids = {
            "source_id": receipt["source_id"],
            "proposal_id": receipt["proposal_id"],
            "event_ids": sorted(receipt["event_ids"]),
        }
        argv = _handoff_argv(vault, note)
        lock_path = root / ".git" / INTAKE_LOCK_NAME
        marker = self.root / "lock-seen.txt"
        hook = self.root / "timeout-hook"
        _install_timeout_hook(hook, lock_path, marker)
        previous_pythonpath = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = str(hook) + (os.pathsep + previous_pythonpath if previous_pythonpath else "")
        started = time.monotonic()
        try:
            with self.assertRaises(KnowledgeTransportError):
                _default_runner(argv=argv, cwd=str(root), timeout=8)
        finally:
            if previous_pythonpath is None:
                os.environ.pop("PYTHONPATH", None)
            else:
                os.environ["PYTHONPATH"] = previous_pythonpath
        self.assertLess(time.monotonic() - started, 13.5, "timeout and one 5-second cleanup reserve bound handling")
        self.assertTrue(marker.is_file(), "the real gateway child reached its owned lock before timeout")
        self.assertFalse(lock_path.exists(), "the exact captured owned lock was archived after tree exit")
        self.assertFalse((root / ".git" / "index.lock").exists())
        timeout_archives = list((root / ".git").glob(INTAKE_LOCK_NAME + ".timeout-pair-*"))
        self.assertEqual(len(timeout_archives), 1)
        archived = json.loads(timeout_archives[0].read_text(encoding="utf-8"))
        filename_pid = timeout_archives[0].name.rsplit("-", 4)[1]
        self.assertEqual(archived["pid"], int(filename_pid))
        self.assertEqual(archived["intake_proposal_id"], receipt["proposal_id"])
        self.assertEqual((root / "unrelated.txt").read_bytes(), original_unrelated)
        self.assertEqual(_sha(vault / "raw/sources/2026/10" / receipt["source_id"] / "original.md"),
                         input_payload["pins"]["vault/raw/sources/2026/10/" + receipt["source_id"] + "/original.md"])

        # This is the normal caretaker intake pass: it processes the exact
        # already-present pair, without a model call or new handoff generation.
        import maintenance

        drained = maintenance.drain_intake_checkpoints({"hub": {"vault": str(vault)}})
        self.assertEqual(drained["status"], "COMPLETED")
        self.assertEqual(len(drained["results"]), 1)
        self.assertEqual(drained["results"][0]["status"], "CHECKPOINTED")
        self.assertEqual(drained["results"][0]["proposal_id"], receipt["proposal_id"])
        self.assertFalse(lock_path.exists())
        self.assertFalse((root / ".git" / "index.lock").exists())

        completed = json.loads(_default_runner(argv=argv, cwd=str(root), timeout=30)[1])
        self.assertTrue(completed["duplicate"])
        self.assertEqual(completed["checkpoint"]["status"], "CURRENT")
        self.assertEqual(completed["source_id"], expected_ids["source_id"])
        self.assertEqual(completed["proposal_id"], expected_ids["proposal_id"])
        self.assertEqual(sorted(completed["event_ids"]), expected_ids["event_ids"])
        self.assertEqual(len(list((vault / "raw" / "sources").rglob("source.json"))), 1)
        self.assertEqual(len(list((vault / "proposals").rglob("*.md"))), 1)

        # A copied lock with identical JSON but a different file identity must
        # survive the post-timeout recheck as an unowned replacement.
        protected, prepare, cleanup = timeout_cleanup_context(argv)
        writer = self.root / "lock-writer.py"
        _lock_writer_script(writer, lock_path, receipt["proposal_id"], _machine_name())
        replaced_path = root / ".git" / "replaced-original-lock-for-test"

        def replace_after_snapshot(identity):
            snapshot = prepare(identity)
            self.assertIsInstance(snapshot, dict)
            os.replace(lock_path, replaced_path)
            lock_path.write_text(json.dumps(snapshot["payload"]) + "\n", encoding="utf-8")
            return snapshot

        with self.assertRaises(subprocess.TimeoutExpired):
            run_process_tree(
                [sys.executable, str(writer), str(lock_path), receipt["proposal_id"],
                 _machine_name(), ""], cwd=str(root), timeout=1.5,
                prepare_timeout=replace_after_snapshot, on_timeout=cleanup,
                protected_paths=protected,
            )
        self.assertTrue(lock_path.is_file(), "replacement active lock is preserved")
        self.assertTrue(replaced_path.is_file(), "the displaced owned artifact is preserved")
        self.assertFalse(list((root / ".git").glob(INTAKE_LOCK_NAME + ".timeout-pair-*"))[1:])

        # An extant Git index.lock is never removed or inferred stale by the
        # timeout path. It keeps the canonical checkpoint lock fail-closed.
        foreign_index = root / ".git" / "index.lock"
        foreign_bytes = b"foreign Git index lock\n"
        writer2 = self.root / "lock-writer-index.py"
        _lock_writer_script(writer2, lock_path, receipt["proposal_id"], _machine_name(), make_index_lock=True)
        with self.assertRaises(subprocess.TimeoutExpired):
            run_process_tree(
                [sys.executable, str(writer2), str(lock_path), receipt["proposal_id"],
                 _machine_name(), str(foreign_index)], cwd=str(root), timeout=1.5,
                prepare_timeout=prepare, on_timeout=cleanup, protected_paths=protected,
            )
        self.assertEqual(foreign_index.read_bytes(), foreign_bytes)
        self.assertTrue(lock_path.is_file(), "unknown Git transaction keeps the active lock intact")

    def test_real_windows_timeout_kills_child_and_grandchild_without_console(self):
        pids_path = self.root / "descendant-pids.txt"
        code = (
            "import os,subprocess,sys,time,pathlib; "
            "grand=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'], "
            "creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0x08000000)); "
            f"pathlib.Path({str(pids_path)!r}).write_text(f'{{os.getpid()}} {{grand.pid}}'); "
            "time.sleep(60)"
        )
        actual_popen = subprocess.Popen
        seen_flags = []

        def track_flags(*args, **kwargs):
            seen_flags.append(kwargs.get("creationflags", 0))
            return actual_popen(*args, **kwargs)

        with mock.patch("caretaker.process_tree.subprocess.Popen", side_effect=track_flags):
            with self.assertRaises(subprocess.TimeoutExpired):
                run_process_tree([sys.executable, "-c", code], cwd=str(self.root), timeout=1.5)
        self.assertTrue(seen_flags)
        self.assertTrue(seen_flags[0] & 0x08000000, "root has CREATE_NO_WINDOW")
        self.assertTrue(seen_flags[0] & 0x00000004, "root is assigned while CREATE_SUSPENDED")
        self.assertTrue(pids_path.is_file())
        root_pid, grandchild_pid = map(int, pids_path.read_text().split())
        self.assertFalse(_pid_alive(root_pid), "owned root child is dead")
        self.assertFalse(_pid_alive(grandchild_pid), "owned grandchild is dead")

    def test_job_assignment_failure_reaps_unassigned_suspended_root(self):
        import caretaker.process_tree as tree

        original_popen = subprocess.Popen
        started = []

        def capture_popen(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            started.append(process)
            return process

        with mock.patch("caretaker.process_tree.subprocess.Popen", side_effect=capture_popen):
            with mock.patch.object(tree._WindowsJob, "assign", side_effect=OSError("synthetic assign failure")):
                with self.assertRaisesRegex(OSError, "synthetic assign failure"):
                    run_process_tree([sys.executable, "-c", "raise SystemExit(9)"],
                                     cwd=str(self.root), timeout=2)
        self.assertEqual(len(started), 1)
        self.assertIsNotNone(started[0].poll())
        self.assertFalse(_pid_alive(started[0].pid), "failed assignment did not orphan a suspended root")


if __name__ == "__main__":
    unittest.main()
