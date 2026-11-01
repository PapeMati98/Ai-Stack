"""Single-process Windows entrypoint. No console supervisor or plaintext files."""
from __future__ import annotations

import argparse
import ctypes
import io
import json
import os
from pathlib import Path
import sys
import time


def decrypt_blob(path: str) -> str:
    """Decode a current-user ConvertFrom-SecureString DPAPI blob in memory."""
    if os.name != "nt":
        raise OSError("windows-required")
    source = Path(path)
    if not source.is_absolute() or source.stat().st_size > 65536:
        raise ValueError("invalid-protected-blob")
    encoded = source.read_text(encoding="utf-8-sig").strip()
    if not encoded.startswith("01000000"):
        raise ValueError("dpapi-blob-required")
    data = bytes.fromhex(encoded)

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    incoming = Blob(len(data), buffer)
    outgoing = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
        ctypes.POINTER(Blob)]
    crypt.CryptUnprotectData.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not crypt.CryptUnprotectData(ctypes.byref(incoming), None, None, None,
                                   None, 1, ctypes.byref(outgoing)):
        raise OSError("dpapi-decryption-failed")
    try:
        return ctypes.string_at(outgoing.data, outgoing.size).decode("utf-16-le")
    finally:
        ctypes.memset(outgoing.data, 0, outgoing.size)
        kernel.LocalFree(outgoing.data)


def entry(config: str, secret_blob: str, *, decrypt=decrypt_blob, run=None) -> int:
    config_path = Path(config)
    if not config_path.is_absolute() or not config_path.is_file():
        return 2
    status_path = config_path.parent / "process-status.json"

    def status(state: str, reason: str = ""):
        # Only fixed metadata. Never exception messages, prompts or tokens.
        try:
            status_path.write_text(json.dumps({"pid": os.getpid(), "at": time.time(),
                "state": state, "reason": reason}), encoding="utf-8")
        except OSError:
            pass

    original_stdout = sys.stdout
    captured = io.StringIO() if original_stdout is None else None
    try:
        if captured is not None:
            sys.stdout = captured
        token = decrypt(secret_blob)
        if not token or any(ch.isspace() for ch in token):
            raise ValueError("invalid-token-format")
        os.environ["TELEGRAM_BOT_TOKEN"] = token
        token = None
        if run is None:
            root = str(Path(__file__).resolve().parents[2])
            if root not in sys.path:
                sys.path.insert(0, root)
            from runtime.run import main
            run = main
        status("starting")
        result = int(run(["--config", str(config_path), "--serve"]))
        reason = "runtime-exit-" + str(result)
        if captured is not None and result:
            try:
                doc = json.loads(captured.getvalue())
                candidate = doc.get("payload", {}).get("reason")
                if doc.get("kind") == "error" and candidate in {
                    "bad-config", "authority-db", "already-running"
                }:
                    reason = candidate
            except (ValueError, AttributeError, TypeError):
                pass
        status("exited", reason)
        return result
    except KeyboardInterrupt:
        status("interrupted", "keyboard-interrupt")
        return 130
    except Exception:
        status("failed", "startup-or-runtime-error")
        return 1
    finally:
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        if captured is not None:
            sys.stdout = original_stdout
            captured.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--secret-blob", required=True)
    args = parser.parse_args()
    raise SystemExit(entry(args.config, args.secret_blob))
