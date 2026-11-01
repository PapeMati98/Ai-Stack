"""Health checks for the coord runtime. Advisory only; no hooks or deny policy."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .constants import RUNTIME_HOME_TEMPLATE, VERSION
from .leases import CoordBoard, parse_lease
from .store import default_home, events_path, leases_dir


def _archive_stats(directory: Path, pattern: str) -> dict[str, int]:
    """Count matching archive files and their bytes without changing the runtime."""
    files = 0
    total_bytes = 0
    try:
        if not directory.is_dir():
            return {"files": files, "bytes": total_bytes}
        entries = directory.glob(pattern)
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
                files += 1
                total_bytes += entry.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return {"files": files, "bytes": total_bytes}


def doctor(board: CoordBoard) -> dict[str, Any]:
    issues: list[str] = []
    home = board.home
    writable = os.access(home, os.W_OK)
    if not writable:
        issues.append("runtime home is not writable")
    lease_root = leases_dir(home)
    if not lease_root.is_dir():
        issues.append("leases directory is missing")
        writable_leases = False
    else:
        writable_leases = os.access(lease_root, os.W_OK)
        if not writable_leases:
            issues.append("leases directory is not writable")
    expired = 0
    unreadable = 0
    active = 0
    now_ts = board.now()
    if lease_root.is_dir():
        from .leases import is_expired

        for path in lease_root.glob("*.lock"):
            try:
                lease = parse_lease(path.read_bytes())
            except OSError:
                unreadable += 1
                continue
            if lease is None:
                unreadable += 1
                continue
            if is_expired(lease, now_ts):
                expired += 1
            else:
                active += 1
    if unreadable:
        issues.append(f"unreadable lease files: {unreadable}")
    events = events_path(home)
    events_bytes = events.stat().st_size if events.is_file() else 0
    if events.is_file():
        for index, line in enumerate(events.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError:
                issues.append(f"events.jsonl line {index} is not valid JSON")
                break
    override = os.environ.get("COORDFS_HOME")
    expected = Path(os.environ.get("USERPROFILE") or str(Path.home())) / ".agents" / "coord"
    if override:
        runtime_note = f"COORDFS_HOME override active: {home}"
    elif home.resolve() != expected.resolve():
        issues.append(f"runtime home is not {RUNTIME_HOME_TEMPLATE}")
        runtime_note = str(home)
    else:
        runtime_note = RUNTIME_HOME_TEMPLATE
    inbox_archives = _archive_stats(home / "office" / "inbox", "archived-*.jsonl.*")
    channel_archives = _archive_stats(home / "office" / "channels", "archived-*.jsonl.*")
    event_archives = _archive_stats(home, "archived-events.jsonl.*")
    archive_files = sum(
        section["files"] for section in (inbox_archives, channel_archives, event_archives)
    )
    archive_bytes = sum(
        section["bytes"] for section in (inbox_archives, channel_archives, event_archives)
    )
    status = "ok" if not issues else "warning"
    return {
        "status": status,
        "code": "doctor_ok" if not issues else "doctor_issues",
        "version": VERSION,
        "home": str(home),
        "runtime_template": RUNTIME_HOME_TEMPLATE,
        "runtime_note": runtime_note,
        "default_home": str(default_home()),
        "writable": writable,
        "leases_active": active,
        "leases_expired": expired,
        "leases_unreadable": unreadable,
        "events_bytes": events_bytes,
        "issues": issues,
        "office_archives": {
            "inboxes": inbox_archives,
            "channels": channel_archives,
            "events": event_archives,
            "total": {"files": archive_files, "bytes": archive_bytes},
        },
    }
