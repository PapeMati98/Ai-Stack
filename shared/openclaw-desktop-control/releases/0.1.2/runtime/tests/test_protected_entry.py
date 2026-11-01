import ctypes
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys

SPEC = importlib.util.spec_from_file_location("protected_entry",
    Path(__file__).resolve().parents[1] / "windows" / "protected_entry.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class ProtectedEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = self.root / "config.json"
        self.cfg.write_text("{}")
        self.addCleanup(self.temp.cleanup)

    def test_runtime_stays_in_same_process_and_clears_token(self):
        observed = []
        def run(args):
            observed.append((os.getpid(), os.environ["TELEGRAM_BOT_TOKEN"], args))
            return 0
        self.assertEqual(module.entry(str(self.cfg), "unused", decrypt=lambda _: "dummy", run=run), 0)
        self.assertEqual(observed, [(os.getpid(), "dummy", ["--config", str(self.cfg), "--serve"])])
        self.assertNotIn("TELEGRAM_BOT_TOKEN", os.environ)
        self.assertNotIn("dummy", (self.root / "process-status.json").read_text())

    def test_error_does_not_leak_secret_or_exception_and_returns_failure(self):
        def fail(args):
            raise RuntimeError("secret-dummy")
        self.assertEqual(module.entry(str(self.cfg), "unused", decrypt=lambda _: "secret-dummy", run=fail), 1)
        self.assertNotIn("secret-dummy", (self.root / "process-status.json").read_text())
        self.assertNotIn("TELEGRAM_BOT_TOKEN", os.environ)

    def test_decrypt_failure_does_not_start_runtime(self):
        def fail(path):
            raise OSError("private-detail")
        self.assertEqual(module.entry(str(self.cfg), "unused", decrypt=fail, run=lambda _: self.fail()), 1)
        self.assertNotIn("private-detail", (self.root / "process-status.json").read_text())

    def test_missing_config_fails(self):
        self.assertEqual(module.entry(str(self.root / "missing"), "unused", decrypt=lambda _: self.fail()), 2)

    def test_runtime_exit_code_preserved(self):
        self.assertEqual(module.entry(str(self.cfg), "unused", decrypt=lambda _: "dummy", run=lambda _: 2), 2)

    def test_pythonw_real_bad_config_keeps_exit_and_reason(self):
        self.cfg.write_text("not-json")
        with patch.object(sys, "stdout", None):
            result = module.entry(str(self.cfg), "unused", decrypt=lambda _: "dummy")
            self.assertIsNone(sys.stdout)
        self.assertEqual(result, 2)
        status = json.loads((self.root / "process-status.json").read_text())
        self.assertEqual(status["reason"], "bad-config")

    def test_pythonw_real_lock_conflict_keeps_exit_and_reason(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from runtime.instance import InstanceLock
        db = str(self.root / "authority.db")
        self.cfg.write_text(json.dumps({"enabled":False, "authority_db":db}))
        lock = InstanceLock(db + ".runtime.lock")
        self.assertTrue(lock.acquire())
        try:
            with patch.object(sys, "stdout", None):
                result = module.entry(str(self.cfg), "unused", decrypt=lambda _: "dummy")
                self.assertIsNone(sys.stdout)
            self.assertEqual(result, 2)
            status = json.loads((self.root / "process-status.json").read_text())
            self.assertEqual(status["reason"], "already-running")
        finally:
            lock.release()

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_real_dpapi_utf16_roundtrip(self):
        class Blob(ctypes.Structure):
            _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]
        plain = "synthetic-token-for-tests".encode("utf-16-le")
        buf = (ctypes.c_ubyte * len(plain)).from_buffer_copy(plain)
        incoming, outgoing = Blob(len(plain), buf), Blob()
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        crypt.CryptProtectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Blob)]
        crypt.CryptProtectData.restype = ctypes.c_int
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        self.assertTrue(crypt.CryptProtectData(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)))
        try:
            protected = ctypes.string_at(outgoing.data, outgoing.size).hex()
        finally:
            kernel.LocalFree(outgoing.data)
        blob = self.root / "synthetic.securestring"
        blob.write_text(protected)
        self.assertEqual(module.decrypt_blob(str(blob)), "synthetic-token-for-tests")


if __name__ == "__main__":
    unittest.main()
