"""Fail-open Agent Office bridge for Agent Graph status events (server store when configured).

The bridge deliberately talks to the installed runtime through an explicit
interpreter and ``office.py`` argv. It never imports the runtime package, uses
shell parsing, or sends graph inputs other than run/node/status.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any


EVENT_STATUSES = frozenset({"dispatch", "completed", "rejected", "blocked", "heartbeat"})
MAX_COMPONENT_CHARS = 256
MAX_TOTAL_SECONDS = 1.8
MIN_CHILD_TIMEOUT = 0.05
# One server call over the forced-command SSH gate measured ~0.6 s (2026-10-04).
REMOTE_CONFIG_NAME = "office-remote.json"
REMOTE_CALL_SECONDS = 8.0
REMOTE_TOTAL_SECONDS = 20.0
HEX64 = re.compile(r"^[0-9a-f]{64}$")
SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
FORBIDDEN_COMPONENT = re.compile(r"[\x00-\x1f\x7f\\/]|\.\.(?:$|[\\/])")
SECRET_SHAPES = (
    re.compile(r"(?i)\bBearer\s+"),
    re.compile(r"(?i)\b(?:sk-[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9_]{12,})\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def _explicit_runtime_root() -> Path | None:
    """Caller-owned runtime root from AGENT_OFFICE_RUNTIME_ROOT, or None.

    A supplied unsafe binding raises ValueError; it never selects the host
    runtime or remote configuration. Parent links and Windows junctions count
    as links too.
    """
    raw = os.environ.get("AGENT_OFFICE_RUNTIME_ROOT")
    if raw is None:
        return None
    if not raw.strip():
        raise ValueError("office-runtime-binding-empty")
    path = Path(raw)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("office-runtime-binding-invalid")
    if any(anchor.is_symlink() or getattr(anchor, "is_junction", lambda: False)()
           for anchor in (path, *path.parents)):
        raise ValueError("office-runtime-binding-linked")
    return path


def _runtime_root() -> Path:
    explicit = _explicit_runtime_root()
    if explicit is not None:
        return explicit
    return Path.home() / ".agents" / "runtime" / "agent-office"


def _office_home(office_home: str | os.PathLike[str] | None) -> Path:
    if office_home is not None:
        return Path(office_home).expanduser()
    override = os.environ.get("COORDFS_HOME")
    if override:
        return Path(override).expanduser()
    return Path(os.environ.get("USERPROFILE") or Path.home()) / ".agents" / "coord"


def _office_location(office_home: str | os.PathLike[str] | None) -> tuple[list[str], bool] | None:
    """Store-selection argv and whether it is the server, or None when no safe store exists.

    An explicit home (argument or COORDFS_HOME) keeps the local store for tests and
    synthetic homes. Otherwise the installer's pinned ``office-remote.json`` next to
    the runtime selects the server: since the 2026-10-04 cutover the workstation
    store is frozen recovery evidence, and a silent fallback to it would split the
    store. A present but unverifiable remote config therefore blocks instead of
    falling back. A store carrying the FROZEN marker is never written.
    """
    if office_home is None and not os.environ.get("COORDFS_HOME"):
        try:
            config = _runtime_root() / REMOTE_CONFIG_NAME
        except (OSError, ValueError):
            return None
        if os.path.lexists(config):
            try:
                if config.is_symlink() or not config.is_file():
                    return None
                marker = json.loads((_runtime_root() / ".agent-office-runtime.json").read_text(encoding="utf-8"))
                expected = marker.get("remote_config_sha256")
                if not isinstance(expected, str) or hashlib.sha256(config.read_bytes()).hexdigest() != expected:
                    return None
            except (OSError, ValueError, TypeError, AttributeError, RecursionError):
                return None
            return ["--remote-config", str(config)], True
    home = _office_home(office_home)
    try:
        if (home / "office" / "FROZEN").exists():
            return None
    except OSError:
        return None
    return ["--home", str(home)], False


def _safe_component(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    value = value.strip()
    if len(value) > MAX_COMPONENT_CHARS or FORBIDDEN_COMPONENT.search(value):
        raise ValueError(f"{field} contains forbidden path/control data")
    if any(pattern.search(value) for pattern in SECRET_SHAPES):
        raise ValueError(f"{field} contains a secret-shaped value")
    return value


def _runtime_source_fingerprint(root: Path) -> str | None:
    """Hash installed runtime sources, excluding only generated launcher metadata."""
    rows: list[tuple[str, int, str]] = []
    try:
        for current, dir_names, file_names in os.walk(root, followlinks=False):
            current_path = Path(current)
            for name in dir_names:
                path = current_path / name
                if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                    return None
            for name in file_names:
                path = current_path / name
                if path.is_symlink() or not path.is_file():
                    return None
                relative = path.relative_to(root).as_posix()
                # Instance-specific installer outputs: the wrapper and the pinned remote
                # config (verified separately against remote_config_sha256).
                if relative in {"office.cmd", ".agent-office-runtime.json", REMOTE_CONFIG_NAME}:
                    continue
                data = path.read_bytes()
                rows.append((relative, len(data), hashlib.sha256(data).hexdigest()))
    except (OSError, ValueError):
        return None
    digest = hashlib.sha256()
    for relative, size, file_hash in sorted(rows):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _runtime_command() -> tuple[list[str], Path] | None:
    """Return a verified runtime command only when marker and source tree agree."""
    try:
        root = _runtime_root()
        marker_path = root / ".agent-office-runtime.json"
        office_py = root / "office.py"
        if marker_path.is_symlink() or not marker_path.is_file():
            return None
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        python_executable = Path(marker["python_executable"])
        if (
            marker.get("managed_by") != "agent-office-installer"
            or marker.get("package_id") != "agent-office"
            or marker.get("schema_version") != 1
            or not SEMVER.fullmatch(str(marker.get("package_version", "")))
            or marker.get("tool") != "runtime"
            or not HEX64.fullmatch(str(marker.get("package_fingerprint", "")))
            or not HEX64.fullmatch(str(marker.get("source_fingerprint", "")))
            or not python_executable.is_absolute()
            or not python_executable.is_file()
            or not office_py.is_file()
        ):
            return None
        if _runtime_source_fingerprint(root) != marker["source_fingerprint"]:
            return None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return None
    return [str(python_executable), "-B", "-I", str(office_py)], root


def _run(command: list[str], args: list[str], location: Path | list[str], timeout: float) -> dict[str, Any] | None:
    """Run one Office call; ``location`` is a store argv from _office_location (a Path means --home)."""
    if timeout < MIN_CHILD_TIMEOUT:
        return None
    selector = ["--home", str(location)] if isinstance(location, Path) else list(location)
    try:
        completed = subprocess.run(
            command + selector + args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            shell=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    try:
        payload = json.loads(completed.stdout)
    except (ValueError, TypeError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


def publish(run_id: str, node_id: str, status: str, *, office_home: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Publish one bounded Agent Graph status event and keep graph execution fail-open."""
    try:
        run = _safe_component(run_id, "run_id")
        node = _safe_component(node_id, "node_id")
        event_status = _safe_component(status, "status")
        if event_status not in EVENT_STATUSES:
            raise ValueError("Unsupported graph event status")
    except ValueError as exc:
        return {"status": "rejected", "code": "invalid_input", "message": str(exc)}

    channel = "project-run-" + hashlib.sha256(run.encode("utf-8")).hexdigest()[:16]
    agent = "graph-" + hashlib.sha256(run.encode("utf-8")).hexdigest()[:16]
    base = {
        "event_status": event_status,
        "channel": channel,
        "agent": agent,
        "runtime_verification": "source_fingerprint",
    }
    command_info = _runtime_command()
    if command_info is None:
        return {"status": "blocked", "code": "office_runtime_unavailable", **base}

    command, _runtime = command_info
    location = _office_location(office_home)
    if location is None:
        return {"status": "blocked", "code": "office_store_unavailable", **base}
    home, remote = location
    per_call = REMOTE_CALL_SECONDS if remote else 0.9
    deadline = time.monotonic() + (REMOTE_TOTAL_SECONDS if remote else MAX_TOTAL_SECONDS)
    presence = _run(
        command,
        ["--agent", agent, "presence", "--ttl", "60", "--status", event_status],
        home,
        min(per_call, deadline - time.monotonic()),
    )
    if presence is None or presence.get("status") != "ok":
        return {"status": "degraded", "code": "office_unavailable", **base}
    if event_status == "heartbeat":
        return {"status": "ok", "code": "presence-refreshed", **base}

    body = json.dumps({"run": run, "node": node, "status": event_status}, ensure_ascii=True, separators=(",", ":"))
    sent = _run(
        command,
        ["--agent", agent, "send", "--channel", channel, "--body", body, "--kind", "note"],
        home,
        min(per_call, deadline - time.monotonic()),
    )
    if sent is None or sent.get("status") != "ok":
        return {"status": "degraded", "code": "office_unavailable", **base}
    return {"status": "ok", "code": "published", **base}


__all__ = ["publish"]
