"""Read-only mirror of Claude Desktop sessions stored on disk.

The bridge from OpenClaw to Claude Desktop requires recognizing the existing
session a UI identifier (``local_<uuid>``) refers to. Claude Desktop stores
each session as a JSONL file under::

    %USERPROFILE%\\.claude\\projects\\<project-slug>\\<uuid>.jsonl

The ``<uuid>`` filename is **not** the same value the UI surfaces (the UI
prefixes a different UUID with ``local_`` and prints that). The UI identifier
does appear inside the JSONL body, so mapping is discoverable: scan the file,
collect every ``local_<uuid>`` token, and resolve back to the file that owns
the token. When the same token lives in more than one file (or none), the
result is explicit ``ambiguous`` / ``unknown`` rather than a guess.

Hard rule of this module: **read-only**. No public function opens a file in
write mode, no temp file is created inside ``projects_root`` and no metadata
is touched. The contract is checked by a test that snapshots the tree before
and after exercising the module.

Failures that must NOT propagate to callers:

* ``projects_root`` missing or unreadable → empty list / unknown resolution.
* ``<slug>/<uuid>.jsonl`` empty → still listed with ``record_count=0``.
* JSONL record corrupted → that line is skipped, others are kept.
* File name without a UUID base → silently ignored.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

# --- Constants ---------------------------------------------------------------

# Files that look like Claude sessions live under USERPROFILE\\.claude\\projects.
# ``projects_root`` may also be passed in explicitly for testing.
DEFAULT_PROJECTS_SUBDIR = Path(".claude") / "projects"

# A session file is named after a UUID v4-like identifier with a ``.jsonl``
# extension. We only consider such files when listing sessions.
UUID_BASENAME = re.compile(
    r"^(?P<uuid>[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})\.jsonl$",
    re.IGNORECASE,
)

# The UI surfaces identifiers prefixed with ``local_``. We accept the literal
# lowercase form; anything else is treated as an unparseable UI token and
# returns ``unknown`` instead of being guessed.
UI_PREFIX = "local_"
LOCAL_ID_RE = re.compile(
    r"local_(?P<uuid>[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})",
    re.IGNORECASE,
)

# The UI identifier is recorded near the top of the JSONL. Scanning only the
# first N bytes keeps ``list_sessions`` cheap on multi-megabyte session files.
SCAN_BYTES = 64 * 1024


# --- Public dataclasses ------------------------------------------------------


@dataclass(frozen=True)
class SessionRecord:
    """A snapshot of one ``<slug>/<uuid>.jsonl`` file. Immutable."""

    file_path: str
    file_uuid: str
    project_slug: str
    projects_root: str
    last_activity: float
    size_bytes: int
    record_count: int
    local_ids: Tuple[str, ...]
    working_directory: str


@dataclass(frozen=True)
class Resolution:
    """Outcome of resolving a UI ``local_<uuid>`` to a session file.

    The contract:

    * ``status == "resolved"``  → ``sessions`` has exactly one entry.
    * ``status == "ambiguous"`` → ``sessions`` has every match; caller must ask.
    * ``status == "unknown"``   → ``sessions`` is empty and ``reason`` explains
      why (no such token anywhere, malformed token, etc.).
    """

    local_id: str
    status: str
    sessions: Tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""


# --- Path helpers ------------------------------------------------------------


def default_projects_root() -> Optional[Path]:
    """Return ``%USERPROFILE%\\.claude\\projects`` or ``None`` if unset.

    Reads ``USERPROFILE`` first and falls back to ``HOME`` so non-Windows
    testing environments without ``USERPROFILE`` still produce a usable
    candidate. Returns ``None`` only when neither variable is set.
    """
    base = os.environ.get("USERPROFILE") or os.environ.get("HOME")
    if not base:
        return None
    return Path(base) / DEFAULT_PROJECTS_SUBDIR


def _resolve_root(projects_root: Optional[object]) -> Optional[Path]:
    """Normalize a caller-supplied root or fall back to ``default_projects_root``."""
    if projects_root is None:
        return default_projects_root()
    return Path(str(projects_root))  # type: ignore[arg-type]


def _slug_to_cwd(slug: str) -> str:
    """Best-effort reversal of Claude's project slug back to a path.

    The encoding is heuristic: Claude Code turns the original ``cwd`` into a
    hyphen-separated slug (a drive and directory names encoded as hyphen-separated words).
    Without an authoritative reverse map the result is a guess, so callers
    are expected to also look at ``cwd`` recorded inside the JSONL itself.
    """
    if not slug:
        return ""
    # Drive prefix: ``C--`` -> ``C:\\`` (the colon is dropped, separators
    # become ``-`` so two consecutive hyphens encode one separator and a
    # lost colon; we treat a leading single letter as a drive).
    if len(slug) >= 2 and slug[1] == "-" and slug[0].isalpha():
        candidate = slug[0] + ":" + slug[1:].replace("-", "\\")
        return candidate
    # POSIX-style path (no drive letter).
    return slug.replace("-", "/")


# --- File scanning -----------------------------------------------------------


def _iter_local_ids(path: Path) -> Tuple[str, ...]:
    """Return every ``local_<uuid>`` token that appears in the first SCAN_BYTES.

    The implementation is deliberately tolerant: the prefix may appear in
    arbitrary fields (``sessionId``, ``conversation_id``, raw user prompts,
    etc.), so we run a regex over the textual representation rather than
    parsing the JSON tree. Duplicates are collapsed; order is preserved by
    first occurrence.
    """
    try:
        with path.open("rb") as fh:
            blob = fh.read(SCAN_BYTES)
    except OSError:
        return ()
    try:
        text = blob.decode("utf-8", errors="replace")
    except Exception:
        return ()
    seen: List[str] = []
    seen_set: set = set()
    for match in LOCAL_ID_RE.finditer(text):
        token = (UI_PREFIX + match.group("uuid")).lower()
        if token not in seen_set:
            seen_set.add(token)
            seen.append(token)
    return tuple(seen)


def _iter_records(path: Path) -> Iterable[dict]:
    """Yield every JSON-decoded record from ``path``; skip corrupted lines.

    Lines that are empty or fail to decode are silently dropped. The caller
    sees an empty iterator for an empty file and a partial iterator for a
    partially valid file.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except (ValueError, TypeError):
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def _file_uuid(path: Path) -> Optional[str]:
    """Return the UUID from a session file name, or ``None`` if it doesn't match."""
    match = UUID_BASENAME.match(path.name)
    if not match:
        return None
    return match.group("uuid")


def _resolve_cwd(record: Optional[dict], slug: str) -> str:
    """Pick the most authoritative cwd we can produce.

    Priority:

    1. ``cwd`` recorded inside any JSONL record (most authoritative).
    2. Reversed slug (heuristic, may be wrong on UNC paths and on paths with
       literal hyphens in their segments).
    3. Empty string if nothing can be inferred.
    """
    if record is not None:
        for key in ("cwd", "workingDirectory", "working_directory"):
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return _slug_to_cwd(slug)


# --- Public API --------------------------------------------------------------


def list_sessions(
    projects_root: Optional[object] = None,
) -> List[SessionRecord]:
    """Return every Claude Desktop session visible under ``projects_root``.

    The function is intentionally tolerant:

    * The directory is allowed to be missing, unreadable, or a non-directory;
      the result is an empty list.
    * Files whose name is not a UUID-shaped ``.jsonl`` are skipped silently.
    * Per-file errors (permission denied, file vanished mid-scan) drop just
      that file, not the whole listing.
    * Corrupted JSONL lines are skipped; an empty file yields a record with
      ``record_count == 0``.

    The returned list is sorted by ``last_activity`` descending so the
    freshest session appears first.
    """
    root = _resolve_root(projects_root)
    if root is None:
        return []
    try:
        if not root.is_dir():
            return []
    except OSError:
        return []

    now = time.time()
    records: List[SessionRecord] = []
    try:
        slug_dirs = list(root.iterdir())
    except OSError:
        return []
    for slug_dir in slug_dirs:
        try:
            if not slug_dir.is_dir():
                continue
        except OSError:
            continue
        try:
            children = list(slug_dir.iterdir())
        except OSError:
            continue
        for entry in children:
            try:
                if not entry.is_file():
                    continue
            except OSError:
                continue
            file_uuid = _file_uuid(entry)
            if file_uuid is None:
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            size_bytes = int(stat.st_size)
            mtime = float(stat.st_mtime) or now
            local_ids = _iter_local_ids(entry)
            record_count = 0
            cwd = ""
            for obj in _iter_records(entry):
                record_count += 1
                if not cwd:
                    cwd = _resolve_cwd(obj, slug_dir.name)
            if not cwd:
                cwd = _slug_to_cwd(slug_dir.name)
            records.append(
                SessionRecord(
                    file_path=str(entry.resolve()),
                    file_uuid=file_uuid.lower(),
                    project_slug=slug_dir.name,
                    projects_root=str(root.resolve()),
                    last_activity=mtime,
                    size_bytes=size_bytes,
                    record_count=record_count,
                    local_ids=local_ids,
                    working_directory=cwd,
                )
            )

    records.sort(key=lambda r: r.last_activity, reverse=True)
    return records


def resolve_local_id(
    local_id: object,
    projects_root: Optional[object] = None,
) -> Resolution:
    """Map a UI ``local_<uuid>`` token to the session file that owns it.

    The function never guesses. ``Resolution.status`` is one of:

    * ``"resolved"``  — exactly one file carries the token.
    * ``"ambiguous"`` — multiple files reference the token; ``sessions``
      enumerates them.
    * ``"unknown"``   — no file references the token, the token is malformed,
      or the projects root is missing.

    An invalid token (missing ``local_`` prefix or non-UUID body) yields
    ``unknown`` immediately, without scanning the directory.
    """
    if not isinstance(local_id, str):
        return Resolution(
            local_id=str(local_id),
            status="unknown",
            reason="malformed-local-id",
        )
    candidate = local_id.strip()
    if not candidate.lower().startswith(UI_PREFIX):
        return Resolution(
            local_id=candidate,
            status="unknown",
            reason="malformed-local-id",
        )
    body = candidate[len(UI_PREFIX):]
    if not re.fullmatch(
        r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}",
        body,
        re.IGNORECASE,
    ):
        return Resolution(
            local_id=candidate,
            status="unknown",
            reason="malformed-local-id",
        )
    normalized = candidate.lower()

    sessions = list_sessions(projects_root=projects_root)
    matches: List[str] = []
    for record in sessions:
        if normalized in record.local_ids:
            matches.append(record.file_path)

    if len(matches) == 1:
        return Resolution(
            local_id=normalized,
            status="resolved",
            sessions=(matches[0],),
        )
    if len(matches) > 1:
        return Resolution(
            local_id=normalized,
            status="ambiguous",
            sessions=tuple(matches),
            reason="multiple-sessions-reference-token",
        )
    return Resolution(
        local_id=normalized,
        status="unknown",
        reason="token-not-found",
    )


def find_session_by_file_uuid(
    file_uuid: object,
    projects_root: Optional[object] = None,
) -> Optional[SessionRecord]:
    """Return the unique session whose file name carries ``file_uuid``.

    ``None`` is returned when the UUID is missing or when no / more than one
    session matches. Used by tests and by future adapters that may want to
    confirm a file name without going through the UI identifier.
    """
    if not isinstance(file_uuid, str) or not file_uuid:
        return None
    target = file_uuid.lower()
    matches = [
        r for r in list_sessions(projects_root=projects_root)
        if r.file_uuid == target
    ]
    if len(matches) == 1:
        return matches[0]
    return None


__all__ = (
    "SessionRecord",
    "Resolution",
    "default_projects_root",
    "list_sessions",
    "resolve_local_id",
    "find_session_by_file_uuid",
)
