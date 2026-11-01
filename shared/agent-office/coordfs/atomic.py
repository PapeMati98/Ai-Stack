"""Atomic create helpers. Claim uses O_CREAT|O_EXCL only; never check-then-write."""

from __future__ import annotations

import errno
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .constants import MUTEX_STALE_S, MUTEX_WAIT_S
from .errors import CoordError
from .store import assert_path_writable


WINDOWS_COLLISION_CODES = frozenset({5, 32, 80, 183, 303})
COLLISION_RETRY_S = 0.5


def is_collision_error(exc: OSError) -> bool:
    return (
        isinstance(exc, FileExistsError)
        or getattr(exc, "winerror", None) in WINDOWS_COLLISION_CODES
        or (
            os.name == "nt"
            and isinstance(exc, PermissionError)
            and exc.errno in {errno.EACCES, errno.EPERM}
        )
    )


def _normalized_collision(path: Path, exc: OSError) -> FileExistsError:
    normalized = FileExistsError(errno.EEXIST, "coordination path is busy", str(path))
    if getattr(exc, "winerror", None) is not None:
        normalized.winerror = exc.winerror  # type: ignore[attr-defined]
    return normalized


def unlink_with_backoff(path: Path, timeout_s: float = COLLISION_RETRY_S) -> bool:
    assert_path_writable(path)
    deadline = time.monotonic() + timeout_s
    delay = 0.002
    while True:
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            if not is_collision_error(exc):
                raise
            if time.monotonic() >= deadline:
                return False
            time.sleep(delay)
            delay = min(delay * 2, 0.05)


def replace_with_backoff(source: Path, destination: Path, timeout_s: float = COLLISION_RETRY_S) -> bool:
    assert_path_writable(source)
    assert_path_writable(destination)
    deadline = time.monotonic() + timeout_s
    delay = 0.002
    while True:
        try:
            os.replace(source, destination)
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            if not is_collision_error(exc):
                raise
            if time.monotonic() >= deadline:
                return False
            time.sleep(delay)
            delay = min(delay * 2, 0.05)


def exclusive_create_flags() -> int:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOINHERIT", 0)
    return flags


def exclusive_create(path: Path, data: bytes) -> None:
    """Create `path` only if it does not exist. Raises FileExistsError on collision."""
    assert_path_writable(path)
    try:
        fd = os.open(str(path), exclusive_create_flags(), 0o644)
    except OSError as exc:
        if is_collision_error(exc):
            raise _normalized_collision(path, exc) from exc
        raise
    try:
        view = memoryview(data)
        written = 0
        while written < len(view):
            written += os.write(fd, view[written:])
        os.fsync(fd)
    except Exception:
        os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    os.close(fd)


def _release_if_token(path: Path, token: bytes) -> tuple[bool, bool]:
    """Return (cleanup_complete, ownership_detached)."""
    try:
        current = path.read_bytes()
    except FileNotFoundError:
        return True, True
    except OSError as exc:
        if is_collision_error(exc):
            return False, False
        raise
    if current != token:
        return True, True
    tombstone = path.with_name(f"{path.name}.dying-{uuid.uuid4().hex}")
    if not replace_with_backoff(path, tombstone):
        return False, False
    try:
        moved = tombstone.read_bytes()
    except OSError:
        moved = b""
    if moved != token:
        try:
            os.rename(tombstone, path)
        except OSError:
            pass
        return False, False
    return unlink_with_backoff(tombstone), True


def _move_then_delete_if_token(path: Path, token: bytes) -> bool:
    return _release_if_token(path, token)[0]


@contextmanager
def exclusive_mutex(
    path: Path,
    timeout_s: float = MUTEX_WAIT_S,
    stale_s: float = MUTEX_STALE_S,
) -> Iterator[None]:
    """O_EXCL mutex with TTL reclaim of a dead holder. Create remains exclusive."""
    deadline = time.monotonic() + timeout_s
    token = uuid.uuid4().hex.encode("ascii")
    while True:
        try:
            exclusive_create(path, token)
            break
        except FileExistsError:
            try:
                observed_token = path.read_bytes()
                age = time.time() - path.stat().st_mtime
            except FileNotFoundError:
                continue
            except OSError as exc:
                if not is_collision_error(exc):
                    raise
                age = 0.0
                observed_token = b""
            if age >= stale_s:
                _move_then_delete_if_token(path, observed_token)
                continue
            if time.monotonic() >= deadline:
                raise CoordError("coordination mutex is busy", code="busy")
            time.sleep(0.01)
    try:
        yield
    finally:
        cleanup_deadline = time.monotonic() + COLLISION_RETRY_S
        while True:
            cleaned, detached = _release_if_token(path, token)
            if cleaned:
                break
            if detached:
                raise CoordError(
                    "coordination mutex cleanup failed", code="mutex_cleanup_failed",
                    payload={"mutex_phase": "release", "ownership_detached": True},
                )
            if time.monotonic() >= cleanup_deadline:
                # Preserve busy semantics for callers whose ownership remains
                # attached, but distinguish release from failure to acquire.
                # A durable writer may acknowledge its data without claiming
                # the lock has been released or allowing another writer in.
                raise CoordError(
                    "coordination mutex cleanup failed", code="busy",
                    payload={"mutex_phase": "release", "ownership_detached": False},
                )
            time.sleep(0.005)
