"""File source admission for the Knowledge Hub caretaker (SPEC-0.1.8-FILES).

This module implements the **file admission model** and **source configuration**
described in SPEC-0.1.8-FILES.md. It does not read the Knowledge Hub, does not
invoke a model, and does not perform network I/O. Its sole responsibility is to
walk an explicit allowlist of roots, qualify candidate files, and expose a
content-sha256 cursor that downstream consumers can use to deduplicate events.

Core guarantees (all enforced here):

* **Allowlist-only**: empty ``roots`` is a valid no-scan state and never raises.
* **Containment**: paths escaping the declared root via a symlink or a relative
  ``..`` are rejected with an explicit ``path_outside_root`` reason.
* **Depth**: each root carries ``max_depth`` and files below that are skipped.
* **Quiet period**: a file with ``mtime`` newer than ``now - quiet_minutes``
  is *not* a candidate (it is still being written).
* **Stability**: a file becomes a candidate only after ``size`` and
  ``sha256(content)`` are identical in two consecutive scans.
* **Content-sha256 cursor**: once admitted, an unchanged file never produces
  a second event.
* **Secret gate**: imports the hard gate from :mod:`caretaker.secrets`. A hit
  rejects the whole file and records only metadata (relative path, reason,
  rule id) — never the body or any fragment of it.
* **Limits**: ``max_file_bytes`` rejects oversize files without reading them;
  ``max_files_per_tick`` caps the number of candidates admitted per scan.
* **Explicit unsupported**: an extension outside ``extensions`` is reported
  as ``unsupported_extension`` rather than silently skipped.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

_CURSOR_SEP = "\x1f"

from . import secrets as _secrets
from .secrets import Verdict, inspect_path, inspect_text


# A single explicit walk implementation is exported so tests can replace it.
WalkFn = Callable[[Path, int], Iterable[Tuple[Path, int]]]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RootConfig:
    """One allowlisted root directory."""

    path: str
    project_slug: str = ""
    max_depth: int = 4

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path.strip():
            raise ValueError("root.path must be a non-empty string")
        if "\0" in self.path:
            raise ValueError("root.path must not contain NUL")
        if not isinstance(self.project_slug, str) or "\0" in self.project_slug:
            raise ValueError("root.project_slug must be a string without NUL")
        if len(self.project_slug) > 128:
            raise ValueError("root.project_slug must be <= 128 chars")
        if not isinstance(self.max_depth, int) or isinstance(self.max_depth, bool):
            raise ValueError("root.max_depth must be an integer")
        if not 0 <= self.max_depth <= 32:
            raise ValueError("root.max_depth must be in [0, 32]")


@dataclass(frozen=True)
class FileSourceConfig:
    """Configuration for one ``kind: files`` source."""

    source_id: str
    roots: Tuple[RootConfig, ...]
    extensions: Tuple[str, ...]
    quiet_minutes: int = 15
    max_file_bytes: int = 262_144
    max_files_per_tick: int = 50
    exclude_directory_names: Tuple[str, ...] = ()
    max_files_per_root: Optional[int] = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise ValueError("source_id must be a non-empty string")
        if "\0" in self.source_id or len(self.source_id) > 128:
            raise ValueError("source_id must be a short NUL-free string")
        if not isinstance(self.roots, tuple):
            raise ValueError("roots must be a tuple of RootConfig")
        for r in self.roots:
            if not isinstance(r, RootConfig):
                raise ValueError("each root must be a RootConfig")
        if not isinstance(self.extensions, tuple):
            raise ValueError("extensions must be a tuple of strings")
        for ext in self.extensions:
            if not isinstance(ext, str) or not ext.strip():
                raise ValueError("each extension must be a non-empty string")
            if not ext.startswith("."):
                raise ValueError(f"extension must start with '.': {ext!r}")
            if len(ext) > 32:
                raise ValueError(f"extension too long: {ext!r}")
        if not isinstance(self.quiet_minutes, int) or self.quiet_minutes < 0:
            raise ValueError("quiet_minutes must be a non-negative integer")
        if not isinstance(self.max_file_bytes, int) or self.max_file_bytes < 1:
            raise ValueError("max_file_bytes must be a positive integer")
        if not isinstance(self.max_files_per_tick, int) \
                or self.max_files_per_tick < 1:
            raise ValueError("max_files_per_tick must be a positive integer")
        if (self.max_files_per_root is not None
                and (not isinstance(self.max_files_per_root, int)
                     or self.max_files_per_root < 1)):
            raise ValueError("max_files_per_root must be a positive integer")
        if any(not isinstance(name, str) or not name or name in {'.', '..'}
               or '/' in name or '\\' in name for name in self.exclude_directory_names):
            raise ValueError("invalid exclude_directory_names")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *,
                     source_id: str) -> "FileSourceConfig":
        """Build from a JSON-like mapping. Raises ``ValueError`` on bad shape."""
        if not isinstance(value, Mapping):
            raise ValueError("source config must be an object")

        raw_roots = value.get("roots", [])
        if not isinstance(raw_roots, list):
            raise ValueError("roots must be a list")
        roots: List[RootConfig] = []
        for r in raw_roots:
            if not isinstance(r, Mapping):
                raise ValueError("root must be an object")
            roots.append(RootConfig(
                path=str(r.get("path", "")).strip(),
                project_slug=str(r.get("project_slug", "")).strip(),
                max_depth=int(r.get("max_depth", 4)),
            ))

        raw_ext = value.get("extensions", [])
        if not isinstance(raw_ext, list) or not raw_ext:
            raise ValueError("extensions must be a non-empty list")
        extensions: List[str] = []
        for e in raw_ext:
            if not isinstance(e, str) or not e.strip():
                raise ValueError("extension must be a non-empty string")
            s = e.strip().lower()
            if not s.startswith("."):
                raise ValueError(f"extension must start with '.': {e!r}")
            extensions.append(s)
        # de-duplicate while preserving order
        seen = set()
        unique_ext: List[str] = []
        for e in extensions:
            if e not in seen:
                seen.add(e)
                unique_ext.append(e)

        return cls(
            source_id=source_id,
            roots=tuple(roots),
            extensions=tuple(unique_ext),
            quiet_minutes=int(value.get("quiet_minutes", 15)),
            max_file_bytes=int(value.get("max_file_bytes", 262_144)),
            max_files_per_tick=int(value.get("max_files_per_tick", 50)),
            exclude_directory_names=tuple(str(name).lower() for name in
                                          value.get("exclude_directory_names", [])),
            max_files_per_root=(int(value["max_files_per_root"])
                                if value.get("max_files_per_root") is not None else None),
        )


# ---------------------------------------------------------------------------
# Admission results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileCandidate:
    """A file that has been admitted and is ready for downstream processing.

    The ``sha256`` field is the content hash and doubles as the deterministic
    cursor — downstream consumers can dedup on it.
    """

    source_id: str
    root_path: str
    project_slug: str
    relative_path: str
    absolute_path: str
    size: int
    sha256: str
    mtime: float


@dataclass(frozen=True)
class FileRejection:
    """A file that was explicitly rejected with a stable reason and rule id.

    Secret-classified rejections store only relative path, reason and rule id.
    ``absolute_path`` is left empty for those records so a path-borne secret
    cannot leak into logs, reports or the intake database.
    """

    absolute_path: str
    relative_path: str
    reason: str
    rule: str = ""
    project_slug: str = ""


@dataclass
class AdmissionStats:
    """Counts collected during one scan.

    ``rejections`` is a per-reason counter (e.g. ``{"max_file_bytes_exceeded":
    3, "unsupported_extension": 1}``) so the operator can see *why* files were
    dropped without inspecting the bodies of the rejection records.
    """

    roots_total: int = 0
    files_visited: int = 0
    files_admitted: int = 0
    files_unsupported: int = 0
    files_unchanged: int = 0
    files_quiet: int = 0
    files_awaiting_stability: int = 0
    files_deferred_overflow: int = 0
    rejections: Dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------


def _cursor_key(root_path: str, relative_path: str) -> str:
    """Stable cursor key scoped to one allowlisted root."""
    root = str(root_path).replace("\\", "/")
    rel = str(relative_path).replace("\\", "/")
    return f"{root}{_CURSOR_SEP}{rel}"


@dataclass
class FileAdmissionCursor:
    """Per-source state of admission across ticks.

    Attributes:
        pending: ``(root, relative_path) -> (size, sha256)`` for files seen
            once and awaiting stability confirmation on the next scan.
        stable: ``(root, relative_path) -> (size, sha256)`` for files already
            evaluated; the cursor prevents re-evaluation as long as size and
            sha256 do not change.

    Keys are produced by :func:`_cursor_key` so two roots that share a
    relative path do not collide.

    The cursor is JSON-serialisable via :meth:`to_dict` and :meth:`from_dict`
    so a parent pipeline can persist it between ticks.
    """

    pending: Dict[str, Tuple[int, str]] = field(default_factory=dict)
    stable: Dict[str, Tuple[int, str]] = field(default_factory=dict)

    @staticmethod
    def entry_key(root_path: str, relative_path: str) -> str:
        return _cursor_key(root_path, relative_path)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pending": {k: [int(v[0]), str(v[1])] for k, v in self.pending.items()},
            "stable": {k: [int(v[0]), str(v[1])] for k, v in self.stable.items()},
        }

    @classmethod
    def from_dict(cls, raw: Optional[Mapping[str, Any]]) -> "FileAdmissionCursor":
        out = cls()
        if not raw or not isinstance(raw, Mapping):
            return out
        for section in ("pending", "stable"):
            data = raw.get(section)
            if not isinstance(data, Mapping):
                continue
            target: Dict[str, Tuple[int, str]] = {}
            for k, v in data.items():
                if not isinstance(k, str):
                    continue
                if isinstance(v, (list, tuple)) and len(v) == 2:
                    try:
                        size = int(v[0])
                        sha = str(v[1])
                    except (TypeError, ValueError):
                        continue
                    target[k] = (size, sha)
            if section == "pending":
                out.pending = target
            else:
                out.stable = target
        return out


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------


def default_walk(root: Path, max_depth: int,
                 excluded_names: Iterable[str] = ()) -> Iterable[Tuple[Path, int]]:
    """Yield ``(absolute_path, depth)`` for every regular file under ``root``.

    Symlinks to directories are not followed (``followlinks=False``); symlinks
    to files are still listed and resolved later by the containment check.
    Blocked directory names from layer 1 of the secret gate are pruned from
    the walk. Files below ``max_depth`` are excluded.
    """

    blocked = {name.lower() for name in _secrets.BLOCKED_DIR_NAMES}
    blocked.update(name.lower() for name in excluded_names)

    def _on_error(_exc: BaseException) -> None:
        # Permission errors etc. are silently skipped — the file simply does
        # not become a candidate. The caller cannot open it either, so this is
        # the only safe behaviour.
        return None

    try:
        root_resolved = root.resolve(strict=False)
    except (OSError, RuntimeError):
        return

    for dirpath, dirnames, filenames in os.walk(
        str(root_resolved), followlinks=False, onerror=_on_error
    ):
        current = Path(dirpath)
        try:
            rel = current.resolve(strict=False).relative_to(root_resolved)
        except ValueError:
            depth = 10 ** 6  # outside root; should not happen with walk
        else:
            depth = 0 if rel == Path(".") else len(rel.parts)

        # Prune blocked directories before deciding what to descend into.
        kept = [n for n in dirnames if n.lower() not in blocked
                and not n.startswith((".", "_"))]
        dirnames[:] = kept

        if depth > max_depth:
            # Files at this depth and below are out of scope.
            dirnames[:] = []
            continue

        for name in filenames:
            yield current / name, depth


# ---------------------------------------------------------------------------
# Path containment
# ---------------------------------------------------------------------------


def is_within_root(child: Path, root: Path) -> bool:
    """Return True if ``child`` resolves to a path inside ``root``.

    Both paths are canonicalised (symlinks followed) before comparison, so a
    symlink inside ``root`` that points outside is correctly rejected.
    """
    try:
        child_resolved = child.resolve(strict=False)
        root_resolved = root.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    try:
        return child_resolved.is_relative_to(root_resolved)
    except AttributeError:  # pragma: no cover - Python < 3.9
        try:
            child_resolved.relative_to(root_resolved)
            return True
        except ValueError:
            return False


def safe_relpath(child: Path, root: Path) -> str:
    """Return the POSIX relative path of ``child`` from ``root``.

    Falls back to ``os.path.relpath`` if canonical comparison fails. The
    output uses forward slashes so it is portable across platforms and stable
    for cursor keys.
    """
    try:
        return child.resolve(strict=False).relative_to(
            root.resolve(strict=False)
        ).as_posix()
    except (ValueError, OSError, RuntimeError):
        try:
            return os.path.relpath(str(child), str(root)).replace("\\", "/")
        except ValueError:
            return child.name


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_SECRET_REJECTION_REASONS = frozenset({
    "secret_path_blacklisted",
    "secret_detected",
})


def _content_rejection_reason(rule_id: str) -> str:
    """Map a content-gate rule to an admission reason.

    Only ``secret_in_content`` is reported as ``secret_detected``. Binary,
    encoding and scan errors keep their own rule id so telemetry is not
    inflated with false secret hits.
    """
    if rule_id == "secret_in_content":
        return "secret_detected"
    return rule_id or "content_rejected"


def _make_rejection(
    *,
    absolute_path: str,
    relative_path: str,
    reason: str,
    rule: str,
    project_slug: str,
) -> FileRejection:
    if reason in _SECRET_REJECTION_REASONS:
        absolute_path = ""
    return FileRejection(
        absolute_path=absolute_path,
        relative_path=relative_path,
        reason=reason,
        rule=rule,
        project_slug=project_slug,
    )


def _scan_one_root(
    config: FileSourceConfig,
    root_cfg: RootConfig,
    cursor: FileAdmissionCursor,
    *,
    now: float,
    walker: WalkFn,
    remaining_slots: int,
) -> Tuple[List[FileCandidate], List[FileRejection], AdmissionStats]:
    """Scan a single root. Helper for :func:`scan_files`."""
    candidates: List[FileCandidate] = []
    rejections: List[FileRejection] = []
    stats = AdmissionStats()

    root_path = Path(root_cfg.path)
    try:
        root_resolved = root_path.resolve(strict=False)
    except (OSError, RuntimeError):
        rejections.append(FileRejection(
            absolute_path=root_cfg.path,
            relative_path="",
            reason="root_unreadable",
            rule="root_resolve_failed",
            project_slug=root_cfg.project_slug,
        ))
        stats.rejections["root_unreadable"] = \
            stats.rejections.get("root_unreadable", 0) + 1
        return candidates, rejections, stats

    if not root_resolved.exists() or not root_resolved.is_dir():
        rejections.append(FileRejection(
            absolute_path=str(root_resolved),
            relative_path="",
            reason="root_unreadable",
            rule="root_not_directory",
            project_slug=root_cfg.project_slug,
        ))
        stats.rejections["root_unreadable"] = \
            stats.rejections.get("root_unreadable", 0) + 1
        return candidates, rejections, stats

    extensions_lower = tuple(e.lower() for e in config.extensions)
    quiet_seconds = int(config.quiet_minutes) * 60
    cap = int(remaining_slots)
    root_key = str(root_resolved)

    if walker is default_walk:
        paths = default_walk(root_resolved, root_cfg.max_depth,
                             config.exclude_directory_names)
    else:
        paths = walker(root_resolved, root_cfg.max_depth)
    for abs_path, _depth in paths:
        stats.files_visited += 1

        # 1. Containment: symlink / .. escapes.
        if not is_within_root(abs_path, root_resolved):
            rejections.append(FileRejection(
                absolute_path=str(abs_path),
                relative_path="",
                reason="path_outside_root",
                rule="symlink_escape",
                project_slug=root_cfg.project_slug,
            ))
            stats.rejections["path_outside_root"] = \
                stats.rejections.get("path_outside_root", 0) + 1
            continue

        rel_path = safe_relpath(abs_path, root_resolved)
        if any(part.lower() in config.exclude_directory_names
               for part in Path(rel_path).parts[:-1]):
            continue

        # 2. Layers 1 + 2 of the secret gate (no file open yet).
        path_verdict = inspect_path(abs_path)
        if not path_verdict.allowed:
            rejections.append(_make_rejection(
                absolute_path=str(abs_path),
                relative_path=rel_path,
                reason="secret_path_blacklisted",
                rule=path_verdict.rule_id or "blocked_path",
                project_slug=root_cfg.project_slug,
            ))
            stats.rejections["secret_path_blacklisted"] = \
                stats.rejections.get("secret_path_blacklisted", 0) + 1
            # Do not remember this in cursor: a future policy or name change
            # could legitimately admit it.
            continue

        # 3. Extension allowlist — explicit unsupported, never silent.
        ext = abs_path.suffix.lower()
        if ext not in extensions_lower:
            rejections.append(FileRejection(
                absolute_path=str(abs_path),
                relative_path=rel_path,
                reason="unsupported_extension",
                rule="extension_not_allowlisted",
                project_slug=root_cfg.project_slug,
            ))
            stats.files_unsupported += 1
            stats.rejections["unsupported_extension"] = \
                stats.rejections.get("unsupported_extension", 0) + 1
            continue

        # 4. Stat the file (size + mtime + existence).
        try:
            st = abs_path.stat()
        except OSError:
            rejections.append(FileRejection(
                absolute_path=str(abs_path),
                relative_path=rel_path,
                reason="stat_failed",
                rule="stat_error",
                project_slug=root_cfg.project_slug,
            ))
            stats.rejections["stat_failed"] = \
                stats.rejections.get("stat_failed", 0) + 1
            continue

        size = int(st.st_size)
        mtime = float(st.st_mtime)
        ck = _cursor_key(root_key, rel_path)
        prev_stable = cursor.stable.get(ck)

        # 5. max_file_bytes: never open oversize files.
        if size > config.max_file_bytes:
            if prev_stable is not None and prev_stable[0] == size:
                # Already recorded as oversize at this size; do not re-reject.
                continue
            rejections.append(FileRejection(
                absolute_path=str(abs_path),
                relative_path=rel_path,
                reason="max_file_bytes_exceeded",
                rule="size_limit",
                project_slug=root_cfg.project_slug,
            ))
            stats.rejections["max_file_bytes_exceeded"] = \
                stats.rejections.get("max_file_bytes_exceeded", 0) + 1
            cursor.stable[ck] = (size, "")
            continue

        # 6. Quiet period: file still being written.
        if mtime + quiet_seconds > now:
            stats.files_quiet += 1
            stats.rejections["quiet_period"] = \
                stats.rejections.get("quiet_period", 0) + 1
            # Do not remember in cursor — re-evaluate next tick.
            continue

        # 7. Read once, hash, then apply stability + content gate.
        try:
            content = abs_path.read_bytes()
        except OSError:
            rejections.append(FileRejection(
                absolute_path=str(abs_path),
                relative_path=rel_path,
                reason="read_failed",
                rule="read_error",
                project_slug=root_cfg.project_slug,
            ))
            stats.rejections["read_failed"] = \
                stats.rejections.get("read_failed", 0) + 1
            continue

        sha = _hash_bytes(content)

        if prev_stable is not None and prev_stable == (size, sha):
            stats.files_unchanged += 1
            # Cursor already records this size+sha — no second event.
            continue

        # 9. Stability: same size+sha must appear in two consecutive scans.
        prev_pending = cursor.pending.get(ck)
        if prev_pending is None or prev_pending != (size, sha):
            cursor.pending[ck] = (size, sha)
            stats.files_awaiting_stability += 1
            stats.rejections["awaiting_stability"] = \
                stats.rejections.get("awaiting_stability", 0) + 1
            continue

        # 10. Stable. Run the layer-3 content gate before admitting.
        text_verdict = inspect_text(content, path=str(abs_path))
        if not text_verdict.allowed:
            rule = text_verdict.rule_id or "content_rejected"
            reason = _content_rejection_reason(rule)
            rejections.append(_make_rejection(
                absolute_path=str(abs_path),
                relative_path=rel_path,
                reason=reason,
                rule=rule,
                project_slug=root_cfg.project_slug,
            ))
            stats.rejections[reason] = stats.rejections.get(reason, 0) + 1
            # Drop pending. We deliberately do NOT remember the sha in
            # ``stable`` so a later policy / content fix is observable.
            cursor.pending.pop(ck, None)
            continue

        # 11. Cap. If we already admitted the maximum, defer to next tick.
        # ``cap`` is the remaining shared budget for this tick across roots.
        if len(candidates) >= cap:
            cursor.pending.pop(ck, None)
            # Re-arm for next tick: the file is stable but over the cap.
            cursor.pending[ck] = (size, sha)
            stats.files_deferred_overflow += 1
            stats.rejections["deferred_overflow"] = \
                stats.rejections.get("deferred_overflow", 0) + 1
            continue

        # 12. Admit.
        cursor.pending.pop(ck, None)
        cursor.stable[ck] = (size, sha)
        candidates.append(FileCandidate(
            source_id=config.source_id,
            root_path=str(root_resolved),
            project_slug=root_cfg.project_slug,
            relative_path=rel_path,
            absolute_path=str(abs_path),
            size=size,
            sha256=sha,
            mtime=mtime,
        ))
        stats.files_admitted += 1

    return candidates, rejections, stats


def scan_files(
    config: FileSourceConfig,
    cursor: FileAdmissionCursor,
    *,
    now: Optional[float] = None,
    walker: WalkFn = default_walk,
) -> Tuple[List[FileCandidate], List[FileRejection], AdmissionStats]:
    """Walk roots, qualify files, update ``cursor`` in place.

    Args:
        config: Allowlist configuration. Empty ``roots`` is valid: the call
            returns empty results and does not raise.
        cursor: Per-source state. Mutated in place; the caller's copy
            reflects the post-scan state.
        now: Override "now" in epoch seconds. Defaults to ``time.time()``.
        walker: Replace the file walker for tests. The default follows
            ``os.walk(..., followlinks=False)`` and prunes blocked dirs.

    Returns:
        ``(candidates, rejections, stats)``. ``candidates`` is sorted by
        ``relative_path`` for determinism.
    """
    if now is None:
        now = time.time()

    candidates: List[FileCandidate] = []
    rejections: List[FileRejection] = []
    stats = AdmissionStats(roots_total=len(config.roots))

    # Empty allowlist is the documented no-scan state. Never an error.
    if not config.roots:
        return candidates, rejections, stats

    remaining = int(config.max_files_per_tick)
    for root_cfg in config.roots:
        root_cap = (min(remaining, config.max_files_per_root)
                    if config.max_files_per_root is not None else remaining)
        cand, rej, st = _scan_one_root(
            config, root_cfg, cursor, now=now, walker=walker,
            remaining_slots=root_cap,
        )
        remaining = max(0, remaining - len(cand))
        candidates.extend(cand)
        rejections.extend(rej)
        stats.files_visited += st.files_visited
        stats.files_admitted += st.files_admitted
        stats.files_unsupported += st.files_unsupported
        stats.files_unchanged += st.files_unchanged
        stats.files_quiet += st.files_quiet
        stats.files_awaiting_stability += st.files_awaiting_stability
        stats.files_deferred_overflow += st.files_deferred_overflow
        for reason, count in st.rejections.items():
            stats.rejections[reason] = stats.rejections.get(reason, 0) + count

    # Deterministic ordering of candidates so callers can rely on it.
    candidates.sort(key=lambda c: (c.root_path, c.relative_path, c.sha256))
    return candidates, rejections, stats


__all__ = [
    "AdmissionStats",
    "FileAdmissionCursor",
    "FileCandidate",
    "FileRejection",
    "FileSourceConfig",
    "RootConfig",
    "WalkFn",
    "default_walk",
    "is_within_root",
    "safe_relpath",
    "scan_files",
]
