"""Short cooperative native writer fence for Hub event/log commit windows.

The canonical publisher (curation.py) holds this fence only while it snapshots
the event set and atomically commits the regenerated log plus its journal, and
again while it merges valid late intake into the applied log. Contributors
(knowledge.write_event) hold it only around the event identity choice and the
exclusive create, so a foreign append either lands entirely before the fenced
snapshot (and is merged into the generated log exactly once) or entirely after
it (and is validated as append-only intake). The fence is advisory coordination
between cooperating local processes on one machine, never a security boundary;
it is never held across model calls, whole-vault lint, or output replacement,
and it is non-reentrant by construction (one kernel lock, taken once).
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
import time


class EventFenceError(RuntimeError):
    """The fence could not be acquired within the bounded wait."""


def _try_lock(handle) -> None:
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle) -> None:
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class event_write_fence:
    """Bounded-wait native lock on ``vault/events/.write-fence``.

    Kernel byte-range locks are released by the OS on process exit or crash,
    so a crashed publisher can never wedge contributors. The persistent file
    is created once, never deleted, carries no payload, and is ignored by the
    vault lint (not a ``.json`` event) and by event snapshots (dotfile).
    """

    def __init__(self, vault: Path, *, timeout: float = 10.0, poll: float = 0.05):
        self._vault = Path(vault)
        self._timeout = float(timeout)
        self._poll = float(poll)
        self._handle = None

    def __enter__(self):
        events = self._vault / ".local"
        events.mkdir(exist_ok=True)
        target = events / "event-write-fence.lock"
        for path in (self._vault, events, target):
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 1024:
                raise EventFenceError('event fence reparse path refused')
            if path == target and not stat.S_ISREG(info.st_mode):
                raise EventFenceError('event fence nonregular path refused')
        try:
            handle = open(target, "a+b")
        except OSError as exc:
            raise EventFenceError(f"event write fence unavailable: {type(exc).__name__}") from exc
        try:
            deadline = time.monotonic() + self._timeout
            # A byte-range lock blocks even a probe read of the locked byte,
            # so initialize via file size metadata only.
            while True:
                if os.fstat(handle.fileno()).st_size:
                    break
                try:
                    handle.seek(0)
                    handle.write(b"0")
                    handle.flush()
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise EventFenceError("event write fence wait exceeded")
                    time.sleep(self._poll)
            while True:
                try:
                    _try_lock(handle)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise EventFenceError("event write fence wait exceeded")
                    time.sleep(self._poll)
        except BaseException:
            handle.close()
            raise
        self._handle = handle
        return self

    def __exit__(self, *exc_info):
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                _unlock(handle)
            finally:
                handle.close()
        return False
