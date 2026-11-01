"""Bounded subprocess execution with ownership of the complete child tree."""

from __future__ import annotations

import ctypes
import datetime as dt
import errno
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


_WINDOWS_CLEANUP_RESERVE = 5.0


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    started_at: dt.datetime


TimeoutCleanup = Callable[[ProcessIdentity, object, float], object]
TimeoutPreparation = Callable[[ProcessIdentity], object]


def _windows_started_at(pid: int) -> dt.datetime | None:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    )
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel), ctypes.byref(user),
        ):
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(
            microseconds=ticks // 10
        )
    except (OSError, OverflowError, ValueError):
        return None
    finally:
        kernel32.CloseHandle(handle)


class _WindowsJob:
    """A kill-on-close job assigned before the child starts running."""

    def __init__(self) -> None:
        from ctypes import wintypes

        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        )
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32.QueryInformationJobObject.argtypes = (
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        )
        self._kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self.handle = self._kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

        class _BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class _ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimit),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            self.handle, 9, ctypes.byref(info), ctypes.sizeof(info),
        ):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def assign(self, process_handle: int) -> None:
        from ctypes import wintypes

        if not self._kernel32.AssignProcessToJobObject(
            self.handle, wintypes.HANDLE(process_handle),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def terminate(self) -> bool:
        return bool(self._kernel32.TerminateJobObject(self.handle, 1))

    def active_processes(self) -> int | None:
        from ctypes import wintypes

        class _Accounting(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        value = _Accounting()
        returned = wintypes.DWORD()
        if not self._kernel32.QueryInformationJobObject(
            self.handle, 1, ctypes.byref(value), ctypes.sizeof(value),
            ctypes.byref(returned),
        ):
            return None
        return int(value.ActiveProcesses)

    def wait_empty(self, deadline: float) -> bool:
        while True:
            active = self.active_processes()
            if active == 0:
                return True
            if active is None or time.monotonic() >= deadline:
                return False
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))

    def close(self) -> None:
        if getattr(self, "handle", None):
            self._kernel32.CloseHandle(self.handle)
            self.handle = None


def _resume_suspended_child(pid: int) -> None:
    """Resume the single primary thread of this still-suspended owned process.

    Popen closes CreateProcess' primary-thread handle. Toolhelp enumeration is
    safe here because a suspended process cannot have created another thread;
    GetProcessIdOfThread verifies the opened thread still belongs to this PID.
    """
    from ctypes import wintypes

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32))
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32))
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.GetProcessIdOfThread.argtypes = (wintypes.HANDLE,)
    kernel32.GetProcessIdOfThread.restype = wintypes.DWORD
    kernel32.ResumeThread.argtypes = (wintypes.HANDLE,)
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
    if snapshot == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        matches: list[int] = []
        more = bool(kernel32.Thread32First(snapshot, ctypes.byref(entry)))
        while more:
            if entry.th32OwnerProcessID == pid:
                matches.append(int(entry.th32ThreadID))
            more = bool(kernel32.Thread32Next(snapshot, ctypes.byref(entry)))
        if len(matches) != 1:
            raise OSError("Owned suspended child did not have exactly one primary thread")
    finally:
        kernel32.CloseHandle(snapshot)

    thread = kernel32.OpenThread(0x0002 | 0x0800, False, matches[0])  # RESUME | QUERY_LIMITED
    if not thread:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if kernel32.GetProcessIdOfThread(thread) != pid:
            raise OSError("Suspended child primary thread ownership changed")
        previous = kernel32.ResumeThread(thread)
        if previous == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(thread)


def _protected_path_exists(paths: Sequence[Path]) -> bool:
    for path in paths:
        try:
            if path.exists():
                return True
        except OSError:
            return True
    return False


def _raise_timeout(
    argv: Sequence[str], timeout: float, stdout: bytes | None, stderr: bytes | None,
) -> None:
    raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr)


def _run_windows(
    argv: Sequence[str], cwd: str | None, timeout: float,
    prepare_timeout: TimeoutPreparation | None, on_timeout: TimeoutCleanup | None,
    protected_paths: Sequence[Path],
) -> subprocess.CompletedProcess[bytes]:
    job = _WindowsJob()
    process: subprocess.Popen[bytes] | None = None
    identity: ProcessIdentity | None = None
    assigned = False
    try:
        # CREATE_SUSPENDED is a documented Win32 flag but is not exported by
        # every supported CPython build. Assign to the job before the first
        # instruction in the child can create descendants.
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | 0x00000004
        process = subprocess.Popen(
            list(argv), cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=flags,
        )
        started_at = _windows_started_at(process.pid)
        job.assign(int(process._handle))
        assigned = True
        _resume_suspended_child(process.pid)
        if started_at is not None:
            identity = ProcessIdentity(process.pid, started_at)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as timeout_error:
            deadline = time.monotonic() + _WINDOWS_CLEANUP_RESERVE
            prepared = None
            if identity is not None and prepare_timeout is not None:
                try:
                    prepared = prepare_timeout(identity)
                except Exception:
                    prepared = None
            job.terminate()
            tree_stopped = job.wait_empty(deadline)
            try:
                remaining = max(0.001, deadline - time.monotonic())
                stdout, stderr = process.communicate(timeout=remaining)
            except subprocess.TimeoutExpired:
                stdout = timeout_error.output
                stderr = timeout_error.stderr
                tree_stopped = False
            # Never archive an owned checkpoint if Git's index transaction left
            # an index.lock. That lock may belong to Git or another writer.
            if (tree_stopped and identity is not None and on_timeout is not None
                    and time.monotonic() < deadline
                    and not _protected_path_exists(protected_paths)):
                try:
                    on_timeout(identity, prepared, deadline)
                except Exception:
                    # Timeout is primary; the canonical helper leaves unproven
                    # lock objects untouched.
                    pass
            _raise_timeout(argv, timeout, stdout, stderr)
        # A successfully returned root is not allowed to leave its owned worker
        # descendants running. Normally the job is empty already; bound the
        # abnormal drain to the same cleanup reserve before the final close.
        if job.active_processes() != 0:
            deadline = time.monotonic() + _WINDOWS_CLEANUP_RESERVE
            job.terminate()
            if not job.wait_empty(deadline):
                raise OSError("Owned child job did not stop after its root returned")
        return subprocess.CompletedProcess(list(argv), process.returncode, stdout, stderr)
    except BaseException:
        if process is not None and process.poll() is None:
            if assigned:
                job.terminate()
            else:
                # An unassigned suspended root cannot have descendants. Kill
                # its exact Popen process handle if job assignment failed.
                process.kill()
            try:
                process.wait(timeout=_WINDOWS_CLEANUP_RESERVE)
            except subprocess.TimeoutExpired:
                pass
        raise
    finally:
        # KILL_ON_JOB_CLOSE is the final backstop even on setup or return errors.
        job.close()
        if process is not None:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass


def _run_posix(
    argv: Sequence[str], cwd: str | None, timeout: float,
    prepare_timeout: TimeoutPreparation | None, on_timeout: TimeoutCleanup | None,
    protected_paths: Sequence[Path],
) -> subprocess.CompletedProcess[bytes]:
    process = subprocess.Popen(
        list(argv), cwd=cwd, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as timeout_error:
        deadline = time.monotonic() + _WINDOWS_CLEANUP_RESERVE
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError as exc:
            if exc.errno != errno.ESRCH:
                raise
        try:
            stdout, stderr = process.communicate(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            stdout, stderr = timeout_error.output, timeout_error.stderr
        _raise_timeout(argv, timeout, stdout, stderr)
    return subprocess.CompletedProcess(list(argv), process.returncode, stdout, stderr)


def run_process_tree(
    argv: Sequence[str], *, cwd: str | None, timeout: float,
    prepare_timeout: TimeoutPreparation | None = None,
    on_timeout: TimeoutCleanup | None = None,
    protected_paths: Sequence[Path] = (),
) -> subprocess.CompletedProcess[bytes]:
    """Run with a bounded lifetime; timeout cleanup runs only after tree exit."""
    if os.name == "nt":
        return _run_windows(argv, cwd, timeout, prepare_timeout, on_timeout, protected_paths)
    return _run_posix(argv, cwd, timeout, prepare_timeout, on_timeout, protected_paths)
