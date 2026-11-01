"""Find a local Codex app pipe after restart, bound to a pinned executable.

No prompt, account credential, app database or process environment is read.
Different server processes fail closed. Pipes owned by the same verified
server process are equivalent transports; every request still binds exact thread IDs.
"""
import os
from pathlib import Path
import re

PIPE_NAME = re.compile(r'^codex-browser-use-[0-9a-f-]{36}$')


def pipe_server_image(pipe):
    if os.name != 'nt':
        raise OSError('Windows required')
    import ctypes as c
    from ctypes import wintypes as w
    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.GetNamedPipeServerProcessId.argtypes = [w.HANDLE, c.POINTER(w.ULONG)]
    kernel.GetNamedPipeServerProcessId.restype = w.BOOL
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = w.BOOL
    kernel.GetCurrentProcess.restype = w.HANDLE
    security = c.WinDLL('advapi32', use_last_error=True)
    security.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]
    security.GetTokenInformation.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]
    security.EqualSid.argtypes = [c.c_void_p, c.c_void_p]
    security.EqualSid.restype = w.BOOL

    def user_sid(process):
        token = w.HANDLE()
        if not security.OpenProcessToken(process, 8, c.byref(token)):
            raise OSError('Process user unavailable')
        try:
            length = w.DWORD()
            security.GetTokenInformation(token, 1, None, 0, c.byref(length))
            if not 0 < length.value < 65536:
                raise OSError('Invalid user metadata')
            buffer = c.create_string_buffer(length.value)
            if not security.GetTokenInformation(token, 1, buffer, length, c.byref(length)):
                raise OSError('Process user unavailable')
            return buffer, c.cast(buffer, c.POINTER(c.c_void_p)).contents.value
        finally:
            kernel.CloseHandle(token)
    handle = kernel.CreateFileW(pipe, 0xC0000000, 0, None, 3, 0, None)
    if handle == w.HANDLE(-1).value:
        raise OSError('Pipe unavailable')
    try:
        pid = w.ULONG()
        if not kernel.GetNamedPipeServerProcessId(handle, c.byref(pid)):
            raise OSError('Pipe owner unavailable')
        process = kernel.OpenProcess(0x1000, False, pid.value)
        if not process:
            raise OSError('Process unavailable')
        try:
            own_buffer, own_sid = user_sid(kernel.GetCurrentProcess())
            peer_buffer, peer_sid = user_sid(process)
            if not security.EqualSid(own_sid, peer_sid):
                raise OSError('Different Windows user')
            size = w.DWORD(32768)
            path = c.create_unicode_buffer(size.value)
            if not kernel.QueryFullProcessImageNameW(process, 0, path, c.byref(size)):
                raise OSError('Process image unavailable')
            return path.value, pid.value
        finally:
            kernel.CloseHandle(process)
    finally:
        kernel.CloseHandle(handle)


def discover_pipe(expected_image, *, preferred='', names=None, inspect=pipe_server_image, all_matches=False):
    image = Path(expected_image)
    if not image.is_absolute() or not image.is_file() or image.is_symlink():
        raise OSError('Pinned Codex executable unavailable')
    names = os.listdir('\\\\.\\pipe\\') if names is None else names
    matches = []
    for name in names:
        if not PIPE_NAME.fullmatch(name):
            continue
        pipe = '\\\\.\\pipe\\' + name
        try:
            observed, pid = inspect(pipe)
            if Path(observed).resolve() == image.resolve():
                matches.append((pipe, pid))
        except OSError:
            continue
    if not matches or len({pid for _, pid in matches}) != 1:
        raise OSError('Codex pipe absent or ambiguous')
    ordered = sorted(matches, key=lambda m: (m[0] != preferred, m[0]))
    if len(ordered) > 16:
        raise OSError('Too many Codex endpoints')
    return ordered if all_matches else ordered[0]
