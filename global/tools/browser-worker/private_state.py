"""Owner-only state and a process lock, using only the standard library."""

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import stat


class StateError(Exception):
    pass


def _api():
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    advapi.GetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD)]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    return advapi, kernel


def _owner_sid():
    advapi, kernel = _api()
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise StateError("private_state_unavailable")
    try:
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise StateError("private_state_unavailable")
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid_pointer, ctypes.byref(text)):
            raise StateError("private_state_unavailable")
        try:
            return text.value
        finally:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel.CloseHandle(token)


def reject_link(path):
    info = Path(path).lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise StateError("state_link_not_allowed")
    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
        raise StateError("state_link_not_allowed")


def make_private(path, directory=False):
    path = Path(path)
    reject_link(path)
    if os.name != "nt":
        if path.stat().st_uid != os.getuid():
            raise StateError("state_owner_mismatch")
        path.chmod(0o700 if directory else 0o600)
        return
    advapi, kernel = _api()
    descriptor = ctypes.c_void_p()
    # Inheritance is protected; files created inside private directories inherit
    # this single owner ACE, so a token is never written with a public ACL.
    flags = "OICI" if directory else ""
    sid = _owner_sid()
    sddl = f"O:{sid}D:P(A;{flags};FA;;;{sid})"
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise StateError("private_state_unavailable")
    try:
        if not advapi.SetFileSecurityW(str(path), 0x80000005, descriptor):
            raise StateError("private_state_unavailable")
    finally:
        kernel.LocalFree(descriptor)


def verify_private(path):
    path = Path(path)
    reject_link(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise StateError("invalid_state_file")
    if os.name != "nt":
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise StateError("state_not_private")
        return
    advapi, kernel = _api()
    size = wintypes.DWORD()
    advapi.GetFileSecurityW(str(path), 5, None, 0, ctypes.byref(size))
    buffer = ctypes.create_string_buffer(size.value)
    if not advapi.GetFileSecurityW(str(path), 5, buffer, size, ctypes.byref(size)):
        raise StateError("private_state_unavailable")
    text = wintypes.LPWSTR()
    if not advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(buffer, 1, 5, ctypes.byref(text), None):
        raise StateError("private_state_unavailable")
    try:
        sid = _owner_sid()
        # Require the actual owner and exactly one protected owner-only ACE.
        if text.value != f"O:{sid}D:P(A;;FA;;;{sid})":
            raise StateError("state_not_private")
    finally:
        kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def private_directory(path):
    path = Path(path).absolute()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    make_private(path, directory=True)
    return path


def write_private(path, value):
    path = Path(path)
    if path.exists() or path.is_symlink():
        reject_link(path)
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        make_private(path)
        os.ftruncate(descriptor, 0)
        data = value.encode("utf-8")
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


class StateLock:
    def __init__(self, home):
        path = Path(home) / "server.lock"
        if path.exists():
            reject_link(path)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        self.stream = os.fdopen(descriptor, "r+b")
        try:
            make_private(path)
            if path.stat().st_size == 0:
                self.stream.write(b"0")
                self.stream.flush()
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise StateError("state_in_use") from None
        except Exception:
            self.stream.close()
            raise

    def close(self):
        if not self.stream.closed:
            self.stream.close()
