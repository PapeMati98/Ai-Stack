"""Tests for fleet.probe_command bounded execution and deadline enforcement."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

import fleet

TEST_WORK_DIR = Path(__file__).resolve().parent / "test-work"


def _handle64():
    import ctypes

    return ctypes.c_ulonglong


def _windows_kernel32():
    """Local kernel32 wrapper with 64-bit HANDLE prototypes.

    Default ctypes restype is c_int, which truncates handles above 32 bits.
    Prototypes stay on this instance and do not mutate other kernel32 wrappers.
    """
    import ctypes
    from ctypes import wintypes

    handle = _handle64()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = handle
    kernel32.TerminateProcess.argtypes = [handle, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [handle, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [handle]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        handle,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    return kernel32


def _windows_command_line(handle: int) -> str | None:
    """Return the process command line, or None when it cannot be proven."""
    import ctypes

    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    query = ntdll.NtQueryInformationProcess
    query.argtypes = [
        _handle64(),
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    query.restype = ctypes.c_long

    class UNICODE_STRING(ctypes.Structure):
        _fields_ = [
            ("Length", ctypes.c_ushort),
            ("MaximumLength", ctypes.c_ushort),
            ("Buffer", ctypes.c_void_p),
        ]

    raw = ctypes.create_string_buffer(128 * 1024)
    needed = ctypes.c_ulong(0)
    # ProcessCommandLineInformation = 60. Any non-zero NTSTATUS is not proof.
    status = query(handle, 60, raw, len(raw), ctypes.byref(needed))
    if status != 0:
        return None
    text = UNICODE_STRING.from_buffer_copy(raw[: ctypes.sizeof(UNICODE_STRING)])
    if text.Length < 2:
        return None
    nchars = text.Length // 2
    if text.Buffer:
        try:
            return ctypes.wstring_at(text.Buffer, nchars)
        except (OSError, ValueError):
            return None
    start = ctypes.sizeof(UNICODE_STRING)
    end = start + text.Length
    if end > len(raw):
        return None
    return raw[start:end].decode("utf-16-le", errors="replace")


def _windows_image_is_this_python(kernel32, handle: int) -> bool:
    import ctypes
    from ctypes import wintypes

    buffer = ctypes.create_unicode_buffer(32768)
    size = wintypes.DWORD(32768)
    if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
        return False
    image = buffer.value
    if not image:
        return False
    try:
        return os.path.samefile(image, sys.executable)
    except OSError:
        return os.path.normcase(os.path.abspath(image)) == os.path.normcase(
            os.path.abspath(sys.executable)
        )


def _cleanup_synthetic_pid(pid: int, identity: str) -> str:
    """Terminate one synthetic fixture only when PID and command line both match.

    Returns a short status for assertions. Refuses the kill when identity cannot
    be proven. Does not terminate unrelated PIDs.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or pid == os.getpid():
        return "skipped"
    if not isinstance(identity, str) or len(identity) < 16:
        return "skipped"
    if os.name == "nt":
        kernel32 = _windows_kernel32()
        access = 0x0001 | 0x0400 | 0x1000 | 0x00100000
        handle = kernel32.OpenProcess(access, False, pid)
        if not handle:
            return "not-running"
        try:
            if not _windows_image_is_this_python(kernel32, handle):
                return "identity-mismatch:image"
            command = _windows_command_line(handle)
            if not command or identity not in command:
                shown = command if command is not None else "unreadable"
                return f"identity-mismatch:{shown[:240]}"
            if not kernel32.TerminateProcess(handle, 1):
                return "terminate-failed"
            kernel32.WaitForSingleObject(handle, 2000)
            return "terminated"
        finally:
            kernel32.CloseHandle(handle)
    cmdline = _posix_cmdline(pid)
    if cmdline is None or identity not in cmdline:
        return "identity-mismatch"
    exe = Path(f"/proc/{pid}/exe")
    try:
        if not os.path.samefile(exe, sys.executable):
            return "identity-mismatch:image"
    except OSError:
        return "identity-mismatch:image"
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return "not-running"
    return "terminated"


def _posix_cmdline(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")


def _fixture_pid_alive(pid: int) -> bool:
    """True only when the PID still has a running process."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    kernel32 = _windows_kernel32()
    handle = kernel32.OpenProcess(0x00100000, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x00000102
    finally:
        kernel32.CloseHandle(handle)


def _fixture_identity() -> str:
    return f"fleet-probe-fixture-{uuid.uuid4().hex}"


def _read_fixture_pid(pid_file: Path) -> int:
    if not pid_file.exists():
        return 0
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except ValueError:
        return 0


class ProbeDeadlineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        TEST_WORK_DIR.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls) -> None:
        if TEST_WORK_DIR.exists():
            for item in TEST_WORK_DIR.iterdir():
                try:
                    if item.is_file():
                        item.unlink()
                except OSError:
                    pass

    def test_inherited_handles_grandchild_parent_exits_zero_early(self) -> None:
        """Parent exit 0 is not completion while a grandchild holds the pipes.

        The 4s descendant must surface TimeoutExpired near the deadline.
        Waiting for that descendant, or reporting launchable success, is a miss.
        The upper bound leaves room for Windows process startup and still
        rejects the historical ~4s hang.
        """
        identity = _fixture_identity()
        timeout = 1.0
        holder = {"pid": 0}
        self.addCleanup(lambda: _cleanup_synthetic_pid(holder["pid"], identity))
        with tempfile.TemporaryDirectory(dir=str(TEST_WORK_DIR), ignore_cleanup_errors=True) as tmp:
            pid_file = Path(tmp) / "grandchild.pid"
            grandchild = f"import time; time.sleep(4)  # {identity}"
            code = (
                "import os, subprocess, sys\n"
                "pid_path = sys.argv[1]\n"
                "work_dir = os.path.dirname(os.path.dirname(os.path.abspath(pid_path)))\n"
                "flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0\n"
                "p = subprocess.Popen(\n"
                f"    [sys.executable, '-S', '-c', {grandchild!r}],\n"
                "    stdout=sys.stdout,\n"
                "    stderr=sys.stderr,\n"
                "    cwd=work_dir,\n"
                "    creationflags=flags,\n"
                ")\n"
                "with open(pid_path, 'w', encoding='utf-8') as handle:\n"
                "    handle.write(str(p.pid))\n"
                "    handle.flush()\n"
                "sys.stdout.write('fixture-probe-success-v1.0\\n')\n"
                "sys.stdout.flush()\n"
                "sys.exit(0)\n"
            )
            started = time.monotonic()
            result = fleet.probe_command(
                sys.executable,
                ["-S", "-c", code, str(pid_file)],
                timeout=timeout,
                cwd=Path(tmp),
            )
            elapsed = time.monotonic() - started
            holder["pid"] = _read_fixture_pid(pid_file)

            self.assertGreaterEqual(elapsed, timeout - 0.05)
            self.assertLess(elapsed, timeout + 2.0)
            self.assertFalse(result["launchable"])
            self.assertIsNone(result["exit_code"])
            self.assertEqual(result["output"], "")
            self.assertIn("TimeoutExpired", result["error"])
            self.assertGreaterEqual(result["duration_seconds"], timeout - 0.05)
            self.assertLess(result["duration_seconds"], timeout + 2.0)
            self._assert_fixture_stopped(holder["pid"], identity)

    def test_inherited_handles_grandchild_parent_remains_alive(self) -> None:
        """A parent that ignores the deadline is stopped without waiting out its child."""
        identity = _fixture_identity()
        timeout = 0.5
        holder = {"pid": 0}
        self.addCleanup(lambda: _cleanup_synthetic_pid(holder["pid"], identity))
        with tempfile.TemporaryDirectory(dir=str(TEST_WORK_DIR), ignore_cleanup_errors=True) as tmp:
            pid_file = Path(tmp) / "grandchild_hang.pid"
            grandchild = f"import time; time.sleep(4)  # {identity}"
            code = (
                "import os, subprocess, sys, time\n"
                "pid_path = sys.argv[1]\n"
                "work_dir = os.path.dirname(os.path.dirname(os.path.abspath(pid_path)))\n"
                "flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0\n"
                "p = subprocess.Popen(\n"
                f"    [sys.executable, '-S', '-c', {grandchild!r}],\n"
                "    stdout=sys.stdout,\n"
                "    stderr=sys.stderr,\n"
                "    cwd=work_dir,\n"
                "    creationflags=flags,\n"
                ")\n"
                "with open(pid_path, 'w', encoding='utf-8') as handle:\n"
                "    handle.write(str(p.pid))\n"
                "    handle.flush()\n"
                "time.sleep(10)\n"
            )
            started = time.monotonic()
            result = fleet.probe_command(
                sys.executable,
                ["-S", "-c", code, str(pid_file)],
                timeout=timeout,
                cwd=Path(tmp),
            )
            elapsed = time.monotonic() - started
            holder["pid"] = _read_fixture_pid(pid_file)

            self.assertGreaterEqual(elapsed, timeout - 0.05)
            self.assertLess(elapsed, timeout + 2.0)
            self.assertFalse(result["launchable"])
            self.assertIsNone(result["exit_code"])
            self.assertIn("TimeoutExpired", result["error"])
            self._assert_fixture_stopped(holder["pid"], identity)

    def test_reader_bounds_bytes_and_releases_stream(self) -> None:
        """Capture stops at the limit, and abandon keeps later writes from sticking."""
        limit = 8192
        cap = fleet._ProbeByteCapture(limit)
        read_fd, write_fd = os.pipe()
        stream = os.fdopen(read_fd, "rb")

        def release() -> None:
            cap.abandon()
            try:
                os.close(write_fd)
            except OSError:
                pass

        self.addCleanup(release)
        worker = threading.Thread(
            target=fleet._read_probe_stream, args=(stream, cap), daemon=True
        )
        worker.start()

        def writer() -> None:
            blob = b"B" * 4096
            try:
                while not cap.abandoned():
                    os.write(write_fd, blob)
            except OSError:
                pass
            finally:
                try:
                    os.close(write_fd)
                except OSError:
                    pass

        writer_thread = threading.Thread(target=writer, daemon=True)
        writer_thread.start()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and cap.retained < limit:
            time.sleep(0.01)
        self.assertEqual(cap.retained, limit)
        self.assertEqual(cap.peak, limit)
        cap.abandon()
        writer_thread.join(timeout=2)
        worker.join(timeout=2)
        self.assertFalse(writer_thread.is_alive())
        self.assertFalse(worker.is_alive())
        self.assertTrue(stream.closed)
        self.assertEqual(cap.retained, 0)
        self.assertLessEqual(cap.peak, limit)

    def test_noisy_inherited_descendant_capture_is_bounded(self) -> None:
        """A descendant that keeps writing after parent exit cannot grow capture."""
        identity = _fixture_identity()
        timeout = 1.5
        holder = {"pid": 0}
        self.addCleanup(lambda: _cleanup_synthetic_pid(holder["pid"], identity))
        holders: list[fleet._ProbeByteCapture] = []

        class Recording(fleet._ProbeByteCapture):
            def __init__(self, limit: int) -> None:
                super().__init__(limit)
                holders.append(self)

        noisy = (
            "import sys, time\n"
            f"marker = {identity!r}\n"
            "chunk = b'N' * 32768\n"
            "end = time.monotonic() + 6\n"
            "while time.monotonic() < end:\n"
            "    try:\n"
            "        sys.stdout.buffer.write(chunk)\n"
            "        sys.stdout.buffer.flush()\n"
            "    except Exception:\n"
            "        break\n"
        )
        with tempfile.TemporaryDirectory(dir=str(TEST_WORK_DIR), ignore_cleanup_errors=True) as tmp:
            pid_file = Path(tmp) / "noisy.pid"
            code = (
                "import os, subprocess, sys\n"
                "pid_path = sys.argv[1]\n"
                "work_dir = os.path.dirname(os.path.dirname(os.path.abspath(pid_path)))\n"
                "flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0\n"
                "sys.stdout.buffer.write(b'P' * 262144)\n"
                "sys.stdout.buffer.flush()\n"
                "p = subprocess.Popen(\n"
                f"    [sys.executable, '-S', '-c', {noisy!r}],\n"
                "    stdout=sys.stdout,\n"
                "    stderr=sys.stderr,\n"
                "    cwd=work_dir,\n"
                "    creationflags=flags,\n"
                ")\n"
                "with open(pid_path, 'w', encoding='utf-8') as handle:\n"
                "    handle.write(str(p.pid))\n"
                "    handle.flush()\n"
                "sys.exit(0)\n"
            )
            with mock.patch.object(fleet, "_ProbeByteCapture", Recording):
                started = time.monotonic()
                result = fleet.probe_command(
                    sys.executable,
                    ["-S", "-c", code, str(pid_file)],
                    timeout=timeout,
                    cwd=Path(tmp),
                )
                elapsed = time.monotonic() - started
            holder["pid"] = _read_fixture_pid(pid_file)

            self.assertGreaterEqual(elapsed, timeout - 0.05)
            self.assertLess(elapsed, timeout + 2.0)
            self.assertFalse(result["launchable"])
            self.assertIsNone(result["exit_code"])
            self.assertEqual(result["output"], "")
            self.assertIn("TimeoutExpired", result["error"])
            self.assertLessEqual(len(result["output"]), 4000)
            self.assertTrue(holders)
            self.assertTrue(any(cap.peak == fleet._PROBE_CAPTURE_BYTES for cap in holders))
            for cap in holders:
                self.assertLessEqual(cap.retained, fleet._PROBE_CAPTURE_BYTES)
                self.assertLessEqual(cap.peak, fleet._PROBE_CAPTURE_BYTES)
            time.sleep(0.3)
            for cap in holders:
                self.assertLessEqual(cap.retained, fleet._PROBE_CAPTURE_BYTES)
                self.assertLessEqual(cap.peak, fleet._PROBE_CAPTURE_BYTES)
            self._assert_fixture_stopped(holder["pid"], identity)

    def _assert_fixture_stopped(self, pid: int, identity: str) -> None:
        if not pid:
            return
        status = _cleanup_synthetic_pid(pid, identity)
        self.assertFalse(
            _fixture_pid_alive(pid),
            f"fixture pid {pid} still alive after {status}",
        )

    def test_normal_stdout_success(self) -> None:
        """Standard executable printing to stdout exits 0 and captures output."""
        result = fleet.probe_command(
            sys.executable,
            ["-c", "print('normal-tool-v2.1')"],
            timeout=2.0,
        )
        self.assertTrue(result["launchable"])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["output"], "normal-tool-v2.1")
        self.assertIsNone(result["error"])

    def test_stderr_nonzero_exit(self) -> None:
        """Executable writing to stderr and exiting nonzero captures error and launchable False."""
        result = fleet.probe_command(
            sys.executable,
            ["-c", "import sys; sys.stderr.write('synthetic probe failure\\n'); sys.exit(3)"],
            timeout=2.0,
        )
        self.assertFalse(result["launchable"])
        self.assertEqual(result["exit_code"], 3)
        self.assertEqual(result["error"], "synthetic probe failure")

    def test_unicode_output_handled_safely(self) -> None:
        """Non-ASCII Unicode stdout is decoded with utf-8 replacement without error."""
        text = "Antigravity 🚀 zażółć gęślą jaźń \u2713"
        result = fleet.probe_command(
            sys.executable,
            ["-c", f"import sys; sys.stdout.buffer.write({text!r}.encode('utf-8') + b'\\n')"],
            timeout=2.0,
        )
        self.assertTrue(result["launchable"])
        self.assertEqual(result["exit_code"], 0)
        self.assertIn(text, result["output"])

    def test_missing_executable_fails_fast(self) -> None:
        """Missing executable produces launchable False, exit_code None, and OSError."""
        result = fleet.probe_command(
            "nonexistent_fleet_probe_binary_xyz_987",
            ["--version"],
            timeout=2.0,
        )
        self.assertFalse(result["launchable"])
        self.assertIsNone(result["exit_code"])
        self.assertEqual(result["output"], "")
        self.assertTrue(
            "FileNotFoundError" in result["error"] or "OSError" in result["error"],
            f"Expected FileNotFoundError or OSError in error: {result['error']}",
        )

    def test_mercury_scrub_preserved(self) -> None:
        """Mercury profile output scrubbing redacts secrets from stdout and stderr."""
        secret = "synthetic-probe-deadline-secret"
        mock_runtime = mock.MagicMock()
        mock_runtime.require_native.return_value = None
        mock_runtime.scrub_output.side_effect = (
            lambda text, secrets: text.replace(secret, "[REDACTED]")
        )

        with mock.patch.object(fleet, "is_mercury_profile", return_value=True), \
             mock.patch.object(fleet, "qualify_mercury_workspace", return_value=Path(".")), \
             mock.patch.object(fleet, "mercury_runtime_module", return_value=mock_runtime):
            result = fleet.probe_command(
                sys.executable,
                ["--version"],
                cwd=Path.cwd(),
                env={"INCEPTION_API_KEY": secret, "PATH": os.environ.get("PATH", "")},
                profile={"mercury_pilot": True},
            )
            # Verify scrub_output was invoked with the secret
            mock_runtime.scrub_output.assert_called()
            call_secrets = mock_runtime.scrub_output.call_args[0][1]
            self.assertEqual(call_secrets, (secret,))

    def test_overall_deadline_enforcement(self) -> None:
        """Subsecond requested timeout is strictly respected for long-running process."""
        started = time.monotonic()
        result = fleet.probe_command(
            sys.executable,
            ["-c", "import time; time.sleep(10)"],
            timeout=0.3,
        )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.5)
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertFalse(result["launchable"])
        self.assertIsNone(result["exit_code"])
        self.assertIn("TimeoutExpired", result["error"])


if __name__ == "__main__":
    unittest.main()
