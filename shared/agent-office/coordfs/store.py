"""Runtime layout under %USERPROFILE%/.agents/coord, overridable for tests."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from .errors import CoordError

from .constants import (
    DEFAULT_EVENTS_MAX_BYTES,
    DEFAULT_EVENTS_RETAIN,
    RUNTIME_HOME_TEMPLATE,
)


def default_home() -> Path:
    override = os.environ.get("COORDFS_HOME")
    if override:
        return Path(override).expanduser()
    userprofile = os.environ.get("USERPROFILE") or str(Path.home())
    return Path(userprofile) / ".agents" / "coord"


def events_max_bytes() -> int:
    raw = os.environ.get("COORDFS_EVENTS_MAX_BYTES")
    if raw:
        return max(64, int(raw))
    return DEFAULT_EVENTS_MAX_BYTES


def events_retain() -> int:
    raw = os.environ.get("COORDFS_EVENTS_RETAIN")
    if raw:
        return max(1, int(raw))
    return DEFAULT_EVENTS_RETAIN


def assert_writable(home: Path) -> None:
    """A retired store is never a fallback, including stale reads/diagnostics."""
    root = Path(home).expanduser().resolve()
    if os.path.lexists(root / "FROZEN") or os.path.lexists(root / "office" / "FROZEN"):
        raise CoordError(
            "This Office store is FROZEN. Use office.cmd with its pinned remote-config; "
            "local access is refused, including reads.",
            code="store_frozen",
        )


def assert_path_writable(path: Path) -> None:
    """Guard low-level atomic operations anywhere below a frozen store."""
    # Resolve the parent, not the final component: replacing/unlinking a symlink
    # mutates the directory containing the link, not its target.
    current = Path(os.path.abspath(Path(path).expanduser())).parent
    for parent in (current, *current.parents):
        assert_writable(parent)


def ensure_home(home: Path | None = None) -> Path:
    root = (home or default_home()).expanduser()
    assert_writable(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "leases").mkdir(parents=True, exist_ok=True)
    return root


def leases_dir(home: Path) -> Path:
    return home / "leases"


def lock_name(resource_id: str) -> str:
    digest = hashlib.sha256(resource_id.encode("utf-8")).hexdigest()
    return f"{digest}.lock"


def lock_path(home: Path, resource_id: str) -> Path:
    return leases_dir(home) / lock_name(resource_id)


def lease_mutex_path(home: Path, resource_id: str) -> Path:
    return leases_dir(home) / f"{lock_name(resource_id)}.mutex"


def events_path(home: Path) -> Path:
    return home / "events.jsonl"


def events_mutex_path(home: Path) -> Path:
    return home / "events.lock"


def runtime_home_template() -> str:
    return RUNTIME_HOME_TEMPLATE
