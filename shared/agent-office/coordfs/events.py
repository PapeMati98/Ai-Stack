"""Append-only typed events with bounded active rotation and lossless archives."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .atomic import (
    COLLISION_RETRY_S,
    exclusive_mutex,
    is_collision_error,
    replace_with_backoff,
)
from .store import assert_path_writable, assert_writable, events_max_bytes, events_mutex_path, events_path, events_retain


def _utc_now(ts: float | None = None) -> str:
    if ts is None:
        current = datetime.now(timezone.utc)
    else:
        current = datetime.fromtimestamp(ts, tz=timezone.utc)
    return current.strftime("%Y-%m-%dT%H:%M:%SZ")


def rotation_archive_path(path: Path, retain: int) -> Path:
    """Return an unused archive name for the oldest active generation.

    The timestamp makes the name useful to an operator; the counter handles multiple
    rotations in one clock tick and pre-existing names. The caller still moves with
    no-replace semantics, so discovering a free name never authorizes an overwrite.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    stem = f"archived-{path.name}.{retain}-{stamp}"
    counter = 0
    while True:
        candidate = path.with_name(f"{stem}-{counter:04d}")
        if not candidate.exists():
            return candidate
        counter += 1


def _move_to_archive(source: Path, destination: Path) -> bool:
    """Preserve one generation under an exclusive name before its active slot is reused.

    A hard link is atomic and never replaces `destination` on any supported platform.
    The later active-slot shift replaces the old name, leaving these bytes reachable only
    through the archive name after a successful rotation. If a later move fails, both names
    remain and no data is lost.
    """
    assert_path_writable(source)
    assert_path_writable(destination)
    deadline = time.monotonic() + COLLISION_RETRY_S
    delay = 0.002
    while True:
        if destination.exists():
            raise FileExistsError(f"rotation archive already exists: {destination}")
        try:
            os.link(source, destination)
            return True
        except FileExistsError:
            raise
        except NotImplementedError as exc:
            raise OSError(
                f"event rotation cannot archive {source} as {destination}: hard links are "
                "unavailable; rotation aborted before active files moved"
            ) from exc
        except OSError as exc:
            if not is_collision_error(exc):
                raise OSError(
                    f"event rotation cannot archive {source} as {destination}: hard-link "
                    f"creation failed ({exc}); rotation aborted before active files moved"
                ) from exc
            if time.monotonic() >= deadline:
                return False
            time.sleep(delay)
            delay = min(delay * 2, 0.05)


def rotate_if_needed(
    path: Path,
    incoming_bytes: int,
    max_bytes: int,
    retain: int,
    *,
    archive_path: Path | None = None,
) -> Path | None:
    """Rotate active generations and return the archive created, if any.

    `archive_path` lets Office record a pending archive name in its ledger before any
    files move. It must be an unused sibling of the stream; existing archive files are
    never replaced.
    """
    assert_path_writable(path)
    if not path.is_file():
        return None
    if path.stat().st_size + incoming_bytes <= max_bytes:
        return None
    oldest = path.with_name(f"{path.name}.{retain}")
    archived: Path | None = None
    if oldest.exists():
        archived = archive_path or rotation_archive_path(path, retain)
        if archived.parent != path.parent:
            raise ValueError("rotation archive must be a sibling of the stream")
        if not _move_to_archive(oldest, archived):
            raise OSError(
                f"event rotation cannot archive {oldest} as {archived}: source remained busy "
                "or hard links are unavailable; rotation aborted before active files moved"
            )
    for index in range(retain, 1, -1):
        source = path.with_name(f"{path.name}.{index - 1}")
        destination = path.with_name(f"{path.name}.{index}")
        if source.exists():
            if not replace_with_backoff(source, destination):
                raise OSError(f"event rotation source remained busy: {source}")
    if not replace_with_backoff(path, path.with_name(f"{path.name}.1")):
        raise OSError(f"event log remained busy during rotation: {path}")
    return archived


def append_event(
    home: Path,
    event_type: str,
    *,
    now_ts: float | None = None,
    max_bytes: int | None = None,
    retain: int | None = None,
    **fields: Any,
) -> dict[str, Any]:
    assert_writable(home)
    payload: dict[str, Any] = {
        "ts": _utc_now(now_ts),
        "type": event_type,
    }
    payload.update(fields)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    encoded = line.encode("utf-8")
    limit = events_max_bytes() if max_bytes is None else max_bytes
    keep = events_retain() if retain is None else retain
    target = events_path(home)
    with exclusive_mutex(events_mutex_path(home)):
        rotate_if_needed(target, len(encoded), limit, keep)
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    return payload


def read_events(home: Path) -> list[dict[str, Any]]:
    assert_writable(home)
    target = events_path(home)
    if not target.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows
