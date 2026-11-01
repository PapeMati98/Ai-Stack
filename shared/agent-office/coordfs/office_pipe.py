"""Local Windows named-pipe wakeups for Agent Office.

The durable JSONL log is authoritative.  This module transports one byte only.
Pipe names are reconstructed from canonical local inputs and are never read from
disk or accepted from a caller.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path

SUPPORTED = os.name == "nt"
MAX_WAITERS = 64
# Remote-client rejection is a fixed protocol constant, independent of the
# platform the importing process runs on (the test module asserts its value
# even where no kernel pipe exists).
PIPE_REJECT_REMOTE_CLIENTS = 0x8
# Cadence of the portable durable-log probe used where named pipes are
# unavailable. The pipe byte is only a wake hint; the JSONL cursor recheck is
# authoritative, so probing preserves the wait contract without new imports,
# threads, sockets, or ports.
POLL_INTERVAL_S = 0.05
_RETAINED_PENDING: set["PipeWaiter"] = set()
_RETAINED_LOCK = threading.Lock()
_REAP_LOCK = threading.Lock()
_REAPER_WAKE = threading.Event()
_reaper_thread: threading.Thread | None = None
_ADMITTED_WAITERS = 0


class PipeUnavailable(OSError):
    pass


def namespace(home: Path, stream_kind: str, stream_name: str) -> str:
    # realpath/normcase collapses Windows case aliases; only a digest enters the
    # kernel object name, so paths, UNC components and traversal cannot escape.
    canonical = os.path.normcase(os.path.realpath(os.fspath(home.expanduser())))
    material = (canonical + "\0" + stream_kind + "\0" + stream_name).encode("utf-8")
    return r"\\.\pipe\agent-office-" + hashlib.sha256(material).hexdigest()


if SUPPORTED:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
    PIPE_ACCESS_INBOUND = 0x00000001
    FILE_FLAG_OVERLAPPED = 0x40000000
    PIPE_TYPE_BYTE = 0
    PIPE_READMODE_BYTE = 0
    PIPE_WAIT = 0
    OPEN_EXISTING = 3
    GENERIC_WRITE = 0x40000000
    ERROR_IO_PENDING = 997
    ERROR_PIPE_CONNECTED = 535
    ERROR_FILE_NOT_FOUND = 2
    ERROR_PIPE_BUSY = 231
    ERROR_OPERATION_ABORTED = 995
    ERROR_IO_INCOMPLETE = 996
    ERROR_NOT_FOUND = 1168
    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 258
    SDDL_REVISION_1 = 1

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", wintypes.LPVOID),
                    ("bInheritHandle", wintypes.BOOL)]

    kernel32.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES)]
    kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
    kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]
    kernel32.ConnectNamedPipe.restype = wintypes.BOOL
    kernel32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]
    kernel32.CancelIoEx.restype = wintypes.BOOL
    kernel32.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
    kernel32.GetOverlappedResult.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                                   ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.ULONG)]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL


class PipeWaiter:
    transport = "named_pipe"

    def __init__(self, name: str) -> None:
        if not SUPPORTED:
            raise PipeUnavailable("Windows named-pipe wake notification is unavailable on this platform")
        # A permanently stuck cancellation must not permit an unbounded number
        # of kernel-owned OVERLAPPED objects to accumulate in this process.
        global _ADMITTED_WAITERS
        with _RETAINED_LOCK:
            if max(_ADMITTED_WAITERS, len(_RETAINED_PENDING)) >= MAX_WAITERS:
                raise PipeUnavailable("named-pipe pending-operation limit reached")
            _ADMITTED_WAITERS += 1
        self._admitted = True
        self.name = name
        self._closed = False
        self._pending = False
        self._reaping = False
        descriptor = wintypes.LPVOID()
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            "D:P(A;;GA;;;OW)", SDDL_REVISION_1, ctypes.byref(descriptor), None
        ):
            self._release_admission()
            raise PipeUnavailable("cannot create owner-only pipe ACL", ctypes.get_last_error())
        try:
            sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), descriptor, False)
            self.handle = kernel32.CreateNamedPipeW(
                name, PIPE_ACCESS_INBOUND | FILE_FLAG_OVERLAPPED,
                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                MAX_WAITERS, 1, 1, 0, ctypes.byref(sa))
        finally:
            kernel32.LocalFree(descriptor)
        if self.handle == INVALID_HANDLE_VALUE:
            self._release_admission()
            raise PipeUnavailable("cannot create local named pipe", ctypes.get_last_error())
        self.event = kernel32.CreateEventW(None, True, False, None)
        if not self.event:
            kernel32.CloseHandle(self.handle)
            self._release_admission()
            raise PipeUnavailable("cannot create pipe event", ctypes.get_last_error())
        self.overlapped = OVERLAPPED()
        self.overlapped.hEvent = self.event
        connected = kernel32.ConnectNamedPipe(self.handle, ctypes.byref(self.overlapped))
        if not connected:
            error = ctypes.get_last_error()
            if error == ERROR_PIPE_CONNECTED:
                kernel32.SetEvent(self.event)
            elif error == ERROR_IO_PENDING:
                self._pending = True
            else:
                self.close()
                raise PipeUnavailable("cannot listen on local named pipe", error)

    def wait(self, timeout_s: float, cancel: threading.Event | None = None) -> str:
        deadline = time.monotonic() + timeout_s
        while True:
            if cancel is not None and cancel.is_set():
                return "cancelled"
            remaining = max(0.0, deadline - time.monotonic())
            milliseconds = min(int(remaining * 1000 + 0.999), 100 if cancel is not None else 0xFFFFFFFE)
            outcome = kernel32.WaitForSingleObject(self.event, milliseconds)
            if outcome == WAIT_OBJECT_0:
                if self._pending:
                    transferred = wintypes.DWORD()
                    if not kernel32.GetOverlappedResult(self.handle, ctypes.byref(self.overlapped), ctypes.byref(transferred), False):
                        raise PipeUnavailable("named-pipe connect failed", ctypes.get_last_error())
                    self._pending = False
                return "wake"
            if outcome != WAIT_TIMEOUT:
                raise PipeUnavailable("named-pipe wait failed", ctypes.get_last_error())
            if time.monotonic() >= deadline:
                return "timeout"

    def close(self) -> None:
        # The daemon and callers share ownership of the same two handles.  Hold
        # one barrier for the complete state transition so neither path can
        # observe (or close) a snapshot being consumed by the other.
        with _REAP_LOCK:
            self._close_owned()

    def _close_owned(self) -> None:
        if self._closed:
            return
        # OVERLAPPED and its event must outlive the operation.  CancelIoEx only
        # requests cancellation; completion still has to be drained before any
        # of these objects may be released.
        if self._pending:
            # Establish strong ownership before the first fallible kernel call.
            # In particular CancelIoEx may fail without proving completion.
            self._retain_until_complete()
            if not kernel32.CancelIoEx(self.handle, ctypes.byref(self.overlapped)):
                error = ctypes.get_last_error()
                if error != ERROR_NOT_FOUND:  # already completed is benign
                    self._arm_reaper()
                    raise PipeUnavailable("cannot cancel named-pipe connect", error)
            transferred = wintypes.DWORD()
            # bWait=True is the drain barrier: ERROR_IO_INCOMPLETE is not a
            # terminal state and must never be followed by freeing OVERLAPPED.
            if not kernel32.GetOverlappedResult(
                self.handle, ctypes.byref(self.overlapped), ctypes.byref(transferred), True
            ):
                error = ctypes.get_last_error()
                if error == ERROR_IO_INCOMPLETE:
                    # A broken/fake API can violate bWait's drain contract.  The
                    # kernel still owns OVERLAPPED, its event, and the pipe, so
                    # retain the whole waiter.  A later close retry can finish
                    # only after a definitive completion is observable.
                    self._retain_until_complete()
                    self._arm_reaper()
                    raise PipeUnavailable("named-pipe drain still pending", error)
                if error != ERROR_OPERATION_ABORTED:
                    self._arm_reaper()
                    raise PipeUnavailable("named-pipe drain failed", error)
            self._pending = False
            with _RETAINED_LOCK:
                _RETAINED_PENDING.discard(self)
        handle, event = self.handle, self.event
        self.handle = self.event = None
        if not kernel32.CloseHandle(handle):
            raise PipeUnavailable("cannot close named-pipe handle", ctypes.get_last_error())
        if not kernel32.CloseHandle(event):
            raise PipeUnavailable("cannot close named-pipe event", ctypes.get_last_error())
        self._closed = True
        with _RETAINED_LOCK:
            _RETAINED_PENDING.discard(self)
        self._release_admission()

    def _retain_until_complete(self) -> None:
        if getattr(self, "_reaping", False):
            return
        self._reaping = True
        self._reap_ready = False
        with _RETAINED_LOCK:
            _RETAINED_PENDING.add(self)

    def _arm_reaper(self) -> None:
        self._reap_ready = True
        _ensure_reaper()
        _REAPER_WAKE.set()

    def _release_admission(self) -> None:
        global _ADMITTED_WAITERS
        if not getattr(self, "_admitted", False):
            return
        with _RETAINED_LOCK:
            _ADMITTED_WAITERS -= 1
        self._admitted = False

    def __enter__(self) -> "PipeWaiter": return self
    def __exit__(self, *_: object) -> None: self.close()


class PollWaiter:
    """Portable wake fallback where named pipes are unavailable.

    The durable JSONL log stays authoritative: each probe slice reports a
    wake so the caller rechecks its cursor, and a message result is only
    ever produced from that durable recheck. No socket, port, subprocess,
    or new dependency is involved.
    """

    transport = "durable_polling"

    def __init__(self, name: str) -> None:
        self.name = name
        self._closed = False

    def wait(self, timeout_s: float, cancel: threading.Event | None = None) -> str:
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            if cancel is not None and cancel.is_set():
                return "cancelled"
            if time.monotonic() >= deadline:
                return "timeout"
            time.sleep(min(POLL_INTERVAL_S, max(0.0, deadline - time.monotonic())))
            if cancel is not None and cancel.is_set():
                return "cancelled"
            # A slice that crosses the deadline is a timeout, not a
            # notification: quiet waits must report notifications=0 exactly
            # like an unwoken pipe.
            if time.monotonic() >= deadline:
                return "timeout"
            return "wake"

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> "PollWaiter": return self
    def __exit__(self, *_: object) -> None: self.close()


def make_waiter(name: str) -> "PipeWaiter | PollWaiter":
    """Listener-first waiter: a kernel pipe where available, the durable-log
    probe otherwise (or when pipe admission is exhausted)."""
    if SUPPORTED:
        try:
            return PipeWaiter(name)
        except PipeUnavailable:
            pass
    return PollWaiter(name)


def default_wait_transport() -> str:
    """Return the waiter backend selected when platform support is known."""
    return "named_pipe" if SUPPORTED else "durable_polling"


def _reap_retained_once() -> int:
    """Release retained waiters whose kernel operation is definitively done."""
    if not SUPPORTED:
        return 0
    with _REAP_LOCK:
        with _RETAINED_LOCK:
            candidates = [waiter for waiter in _RETAINED_PENDING
                          if getattr(waiter, "_reap_ready", False)]
        reaped = 0
        for waiter in candidates:
            if kernel32.WaitForSingleObject(waiter.event, 0) != WAIT_OBJECT_0:
                continue
            transferred = wintypes.DWORD()
            complete = kernel32.GetOverlappedResult(
                waiter.handle, ctypes.byref(waiter.overlapped), ctypes.byref(transferred), False
            )
            if not complete and ctypes.get_last_error() == ERROR_IO_INCOMPLETE:
                continue
            # Success and every other error are terminal for the OVERLAPPED.
            kernel32.CloseHandle(waiter.handle)
            kernel32.CloseHandle(waiter.event)
            waiter.handle = waiter.event = None
            waiter._pending = False
            waiter._closed = True
            with _RETAINED_LOCK:
                _RETAINED_PENDING.discard(waiter)
            waiter._release_admission()
            reaped += 1
        return reaped


def _reaper_main() -> None:
    while True:
        _REAPER_WAKE.wait(.1)
        _REAPER_WAKE.clear()
        _reap_retained_once()


def _ensure_reaper() -> None:
    global _reaper_thread
    with _RETAINED_LOCK:
        if _reaper_thread is None or not _reaper_thread.is_alive():
            _reaper_thread = threading.Thread(
                target=_reaper_main, name="agent-office-pipe-reaper", daemon=True
            )
            _reaper_thread.start()


def notify(name: str) -> tuple[int, str | None]:
    """Wake all currently listening pipe instances, bounded by MAX_WAITERS."""
    if not SUPPORTED:
        return 0, "Windows named-pipe wake notification is unavailable on this platform"
    count = 0
    for _ in range(MAX_WAITERS):
        handle = kernel32.CreateFileW(name, GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
        if handle == INVALID_HANDLE_VALUE:
            error = ctypes.get_last_error()
            if error in (ERROR_FILE_NOT_FOUND, ERROR_PIPE_BUSY):
                return count, None
            return count, f"named-pipe notify failed ({error})"
        try:
            written = wintypes.DWORD()
            byte = ctypes.c_char(b"W")
            if not kernel32.WriteFile(handle, ctypes.byref(byte), 1, ctypes.byref(written), None):
                return count, f"named-pipe wake write failed ({ctypes.get_last_error()})"
            count += 1
        finally:
            kernel32.CloseHandle(handle)
    return count, "named-pipe waiter limit reached"
