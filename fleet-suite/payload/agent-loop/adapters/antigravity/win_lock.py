"""Thin Windows-facing wrapper around the canonical lock and atomic writer."""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import security_contract as security  # noqa: E402


class LockAcquisitionError(Exception):
    """Raised when the canonical OS-held lock cannot be acquired in time."""


@contextmanager
def acquire_loop_lock(
    lock_path: Path,
    timeout_seconds: float = 0.0,
) -> Iterator[None]:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    lock = security.LoopLock(lock_path)
    while True:
        try:
            lock.acquire(blocking=False)
            break
        except security.CircuitOpenError as exc:
            if time.monotonic() >= deadline:
                raise LockAcquisitionError(
                    f"Could not acquire lock for {lock_path} within {timeout_seconds}s"
                ) from exc
            time.sleep(0.05)
    try:
        yield
    finally:
        lock.release()


def atomic_write_json(file_path: Path, data: dict[str, Any] | list[Any]) -> None:
    security.atomic_write_json(file_path, data)
