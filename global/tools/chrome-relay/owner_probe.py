"""Read-only Windows process metadata probes for owner Chrome Relay startup."""

import ctypes
import os
from ctypes import wintypes as w


class ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("size", w.DWORD), ("usage", w.DWORD), ("pid", w.DWORD),
        ("heap", ctypes.c_size_t), ("module", w.DWORD), ("threads", w.DWORD),
        ("parent", w.DWORD), ("priority", w.LONG), ("flags", w.DWORD),
        ("exe", w.WCHAR * 260),
    ]


class UnicodeString(ctypes.Structure):
    _fields_ = [("length", w.USHORT), ("maximum", w.USHORT), ("buffer", ctypes.c_void_p)]


if os.name == "nt":
    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    _kernel.OpenProcess.restype = w.HANDLE
    _kernel.CloseHandle.argtypes = [w.HANDLE]
    _kernel.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    _kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    _kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
    _kernel.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(ProcessEntry)]
    _kernel.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(ProcessEntry)]
else:
    _kernel = None


def _require_windows():
    if _kernel is None:
        raise RuntimeError("windows_process_probe_unavailable")


def processes():
    """Return PID, parent PID and executable name only; never return argv."""
    _require_windows()
    handle = _kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise RuntimeError("process_inventory_unavailable")
    try:
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        rows = []
        if not _kernel.Process32FirstW(handle, ctypes.byref(entry)):
            raise RuntimeError("process_inventory_unavailable")
        while True:
            rows.append({"pid": entry.pid, "parent": entry.parent, "name": entry.exe})
            if not _kernel.Process32NextW(handle, ctypes.byref(entry)):
                break
        return rows
    finally:
        _kernel.CloseHandle(handle)


def _live(pid):
    _require_windows()
    handle = _kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        # A snapshot PID can disappear between enumeration and this check, but
        # access denied is also possible. Treat either case as unknown so an
        # inaccessible Chrome process cannot be mistaken for a free profile.
        return None
    try:
        code = w.DWORD()
        if not _kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return code.value == 259
    finally:
        _kernel.CloseHandle(handle)


def _normalized_path(value):
    return os.path.normcase(os.path.normpath(str(value))).replace("/", "\\").casefold()


def command_line(pid):
    """Read process command metadata only; inaccessible metadata returns None."""
    _require_windows()
    handle = _kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        ntdll = ctypes.WinDLL("ntdll")
        query = ntdll.NtQueryInformationProcess
        query.argtypes = [w.HANDLE, w.ULONG, ctypes.c_void_p, w.ULONG, ctypes.POINTER(w.ULONG)]
        query.restype = w.LONG
        buffer = ctypes.create_string_buffer(131072)
        needed = w.ULONG()
        if query(handle, 60, buffer, len(buffer), ctypes.byref(needed)) != 0:
            return None
        value = UnicodeString.from_buffer(buffer)
        low = ctypes.addressof(buffer)
        high = low + len(buffer)
        if (value.length % 2 or not value.buffer or not low <= value.buffer
                or not value.buffer + value.length <= high):
            raise RuntimeError("command_metadata_invalid")
        return ctypes.string_at(value.buffer, value.length).decode("utf-16-le")
    finally:
        _kernel.CloseHandle(handle)


def profile_users(profile):
    """Find live Chrome processes using a profile; unknown command metadata fails closed."""
    matches = []
    unknown = []
    needle = _normalized_path(profile)
    for row in processes():
        if str(row.get("name", "")).casefold() != "chrome.exe":
            continue
        live = _live(row["pid"])
        if live is None:
            unknown.append(row["pid"])
            continue
        if not live:
            continue
        try:
            command = command_line(row["pid"])
        except Exception:
            command = None
        if not isinstance(command, str) or not command.strip():
            unknown.append(row["pid"])
        elif needle in _normalized_path(command):
            matches.append({"pid": row["pid"]})
    return {"matches": matches, "unknown_chrome_pids": unknown}
