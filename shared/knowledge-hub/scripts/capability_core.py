#!/usr/bin/env python3
"""Deterministic, fail-closed primitives for Capability Hub V1."""

from __future__ import annotations

import ctypes
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence


LEGACY_MARKER_NAME = ".llm-wiki-managed.json"
CAPABILITY_MARKER_NAME = ".shared-capability-managed.json"
MARKER_NAMES = (CAPABILITY_MARKER_NAME, LEGACY_MARKER_NAME)
# Compatibility alias for the existing llm-wiki regression fixtures.
MARKER_NAME = LEGACY_MARKER_NAME
MANAGED_BY = "shared-agent-knowledge-hub"
SECURITY_STATEMENT = (
    "Capability Hub verifies the integrity of the approved workflow. "
    "It does not protect against host compromise or direct writes to a tool profile."
)
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
GIT_OID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


class CapabilityError(RuntimeError):
    """A deterministic validation or safety failure."""


@dataclass(frozen=True)
class GitEntry:
    mode: str
    object_type: str
    oid: str
    repo_path: str
    relative_path: str
    size: int
    sha256: str
    content: bytes

    def report(self) -> dict[str, Any]:
        return {
            "path": self.relative_path,
            "mode": self.mode,
            "object_type": self.object_type,
            "oid": self.oid,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class GitSource:
    repo: str
    source_commit: str
    source_path: str
    source_tree: str
    entries: tuple[GitEntry, ...]
    payload_fingerprint: str
    legacy_windows_payload_fingerprint: str
    manifest_fingerprint: str
    worktree_contamination: tuple[dict[str, str], ...]
    capability_manifest: dict[str, Any] | None

    def report(self, include_repo: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "source_commit": self.source_commit,
            "source_path": self.source_path,
            "source_tree": self.source_tree,
            "payload_fingerprint": self.payload_fingerprint,
            "legacy_windows_payload_fingerprint": self.legacy_windows_payload_fingerprint,
            "manifest_fingerprint": self.manifest_fingerprint,
            "file_count": len(self.entries),
            "files": [entry.report() for entry in self.entries],
            "worktree_contamination_count": len(self.worktree_contamination),
            "worktree_contamination": list(self.worktree_contamination),
            "capability_manifest": self.capability_manifest,
            "security_statement": SECURITY_STATEMENT,
        }
        if include_repo:
            result["repo"] = self.repo
        return result


@dataclass(frozen=True)
class MarkerInfo:
    schema_version: int
    variant: str
    managed_by: str
    tool: str
    claimed_fingerprint: str
    data: dict[str, Any]

    @property
    def legacy(self) -> bool:
        return self.schema_version == 1 or self.variant == "v2-managed-copy-windows"

    def report(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "variant": self.variant,
            "managed_by": self.managed_by,
            "tool": self.tool,
            "claimed_fingerprint": self.claimed_fingerprint,
        }


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256_bytes(encoded)


def strict_json_loads(value: str) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise CapabilityError(f"Duplicate JSON key: {key}")
            result[key] = item
        return result

    try:
        return json.loads(value, object_pairs_hook=reject_duplicates)
    except json.JSONDecodeError as exc:
        raise CapabilityError("Invalid JSON") from exc


def tree_fingerprint(rows: Iterable[tuple[str, str]]) -> str:
    normalized = sorted(rows, key=lambda row: row[0].encode("utf-8"))
    body = "\n".join(f"{path}:{digest}" for path, digest in normalized)
    return sha256_bytes(body.encode("utf-8"))


def legacy_windows_tree_fingerprint(rows: Iterable[tuple[str, str]]) -> str:
    normalized = sorted(rows, key=lambda row: (row[0].casefold(), row[0]))
    body = "\n".join(f"{path}:{digest}" for path, digest in normalized)
    return sha256_bytes(body.encode("utf-8"))


_PINNED_GIT_EXECUTABLE: str | None = None


def _git_executable() -> str:
    global _PINNED_GIT_EXECUTABLE
    if _PINNED_GIT_EXECUTABLE is None:
        found = shutil.which("git")
        if not found:
            raise CapabilityError("Git executable was not found")
        resolved = Path(found).resolve(strict=True)
        details = resolved.stat()
        if not stat.S_ISREG(details.st_mode):
            raise CapabilityError("Resolved Git executable is not a regular file")
        _PINNED_GIT_EXECUTABLE = str(resolved)
    return _PINNED_GIT_EXECUTABLE


def git_executable_identity() -> dict[str, str]:
    path = Path(_git_executable())
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": str(path), "sha256": digest.hexdigest()}


def _run_git(repo: Path | str, arguments: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    executable = _git_executable()
    command = [
        executable,
        "--no-replace-objects",
        "--literal-pathspecs",
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-C",
        str(repo),
        *arguments,
    ]
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_") and key != "CAPABILITY_GIT"
    }
    environment.update(
        {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PROTOCOL_FROM_USER": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return subprocess.run(
        command,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def run_git(repo: Path | str, arguments: Sequence[str]) -> bytes:
    process = _run_git(repo, arguments)
    if process.returncode != 0:
        error = process.stderr.decode("utf-8", errors="replace").strip()
        raise CapabilityError(f"Git command failed ({process.returncode}): {error}")
    return process.stdout


def git_is_ancestor(repo: Path | str, ancestor: str, descendant: str) -> bool:
    if not GIT_OID.fullmatch(ancestor) or not GIT_OID.fullmatch(descendant):
        raise CapabilityError("Git ancestry requires full object IDs")
    process = _run_git(repo, ["merge-base", "--is-ancestor", ancestor, descendant])
    if process.returncode in {0, 1}:
        return process.returncode == 0
    error = process.stderr.decode("utf-8", errors="replace").strip()
    raise CapabilityError(
        f"Git ancestry check failed ({process.returncode}): {error}"
    )


def validate_source_path(value: str) -> str:
    if not value or value.startswith("-") or "\\" in value or ":" in value:
        raise CapabilityError(f"Invalid Git source path: {value!r}")
    normalized = value.strip("/")
    if normalized != value or not normalized:
        raise CapabilityError(f"Git source path must be normalized and relative: {value!r}")
    validate_relative_path(normalized, ascii_only=True)
    return normalized


def validate_relative_path(value: str, *, ascii_only: bool) -> str:
    if not value or value.startswith("/") or "\\" in value or ":" in value:
        raise CapabilityError(f"Unsafe relative path: {value!r}")
    if unicodedata.normalize("NFC", value) != value:
        raise CapabilityError(f"Path is not NFC normalized: {value!r}")
    if ascii_only and not value.isascii():
        raise CapabilityError(f"V1 pilot paths must be ASCII: {value!r}")
    pure = PurePosixPath(value)
    if pure.as_posix() != value:
        raise CapabilityError(f"Path is not lexically normalized: {value!r}")
    parts = pure.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise CapabilityError(f"Unsafe path segment: {value!r}")
    for part in parts:
        if part.endswith((" ", ".")):
            raise CapabilityError(f"Windows-hostile trailing character: {value!r}")
        if any(ord(char) < 32 or ord(char) == 127 for char in part):
            raise CapabilityError(f"Control character in path: {value!r}")
        stem = part.split(".", 1)[0].casefold()
        if stem in WINDOWS_RESERVED:
            raise CapabilityError(f"Windows reserved device name: {value!r}")
    return value


def _resolve_commit(repo: Path, commit: str) -> str:
    if not commit or commit.startswith("-"):
        raise CapabilityError("A concrete, non-option Git commit is required")
    output = run_git(
        repo,
        ["rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}"],
    ).decode("ascii", errors="strict").strip()
    if not GIT_OID.fullmatch(output):
        raise CapabilityError(f"Git returned an invalid commit OID: {output!r}")
    return output


def _resolve_tree(repo: Path, commit: str, source_path: str) -> str:
    output = run_git(
        repo,
        ["rev-parse", "--verify", "--end-of-options", f"{commit}:{source_path}"],
    ).decode("ascii", errors="strict").strip()
    if not GIT_OID.fullmatch(output):
        raise CapabilityError(f"Git returned an invalid tree OID: {output!r}")
    object_type = run_git(repo, ["cat-file", "-t", output]).decode("ascii").strip()
    if object_type != "tree":
        raise CapabilityError(f"Source path is not a Git tree: {source_path}")
    return output


def _parse_ls_tree(raw: bytes, source_path: str) -> list[tuple[str, str, str, str]]:
    prefix = source_path + "/"
    parsed: list[tuple[str, str, str, str]] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_type, oid = metadata.decode("ascii").split(" ")
            repo_path = raw_path.decode("utf-8", errors="strict")
        except (ValueError, UnicodeDecodeError) as exc:
            raise CapabilityError("Git tree contains an undecodable entry") from exc
        if not repo_path.startswith(prefix):
            raise CapabilityError(f"Git returned an entry outside source tree: {repo_path}")
        relative = repo_path[len(prefix) :]
        if not relative:
            raise CapabilityError("Git returned an empty relative path")
        parsed.append((mode, object_type, oid, relative))
    if not parsed:
        raise CapabilityError(f"Git source tree is empty: {source_path}")
    return parsed


def _validate_path_set(paths: Iterable[str], *, ascii_only: bool) -> None:
    seen: dict[str, str] = {}
    for path in paths:
        validate_relative_path(path, ascii_only=ascii_only)
        folded = path.casefold()
        if folded in seen and seen[folded] != path:
            raise CapabilityError(
                f"Case-insensitive path collision: {seen[folded]!r} and {path!r}"
            )
        seen[folded] = path


def _validate_pilot_layout(entries: Sequence[GitEntry]) -> dict[str, Any]:
    paths = {entry.relative_path for entry in entries}
    if "SKILL.md" not in paths:
        raise CapabilityError("Instruction-only payload requires SKILL.md")
    if "capability.json" not in paths:
        raise CapabilityError("Instruction-only payload requires capability.json")
    for entry in entries:
        path = entry.relative_path
        allowed = path in {"SKILL.md", "capability.json"} or (
            path.startswith("references/") and path.endswith(".md")
        )
        if not allowed:
            raise CapabilityError(f"File is outside the V1 instruction-only layout: {path}")
        if entry.mode != "100644" or entry.object_type != "blob":
            raise CapabilityError(
                f"V1 instruction-only payload rejects mode/type {entry.mode}/{entry.object_type}: {path}"
            )
        if entry.content.startswith(b"\xef\xbb\xbf"):
            raise CapabilityError(f"V1 text payload rejects UTF-8 BOM: {path}")
        if b"\r" in entry.content:
            raise CapabilityError(f"V1 text payload requires LF line endings: {path}")
        if b"\x00" in entry.content:
            raise CapabilityError(f"V1 text payload rejects NUL bytes: {path}")
    manifest_entry = next(entry for entry in entries if entry.relative_path == "capability.json")
    try:
        manifest = strict_json_loads(
            manifest_entry.content.decode("utf-8", errors="strict")
        )
    except (UnicodeDecodeError, CapabilityError) as exc:
        raise CapabilityError("capability.json is not strict UTF-8 JSON") from exc
    if not isinstance(manifest, dict):
        raise CapabilityError("capability.json must contain a JSON object")
    expected = {
        "schema_version": 1,
        "kind": "instruction-skill",
        "entrypoint": "SKILL.md",
        "declared_permissions": [],
        "enforcement": "none",
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise CapabilityError(f"Invalid capability manifest field {key!r}")
    capability_id = manifest.get("capability_id")
    capability_version = manifest.get("capability_version")
    allowed_keys = set(expected) | {
        "capability_id",
        "capability_version",
        "revoked_at",
    }
    unknown_keys = sorted(set(manifest) - allowed_keys)
    if unknown_keys:
        raise CapabilityError(
            f"Unknown capability manifest fields: {', '.join(unknown_keys)}"
        )
    if "revoked_at" not in manifest:
        raise CapabilityError("capability manifest requires revoked_at")
    revoked_at = manifest.get("revoked_at")
    if revoked_at is not None and not isinstance(revoked_at, str):
        raise CapabilityError("revoked_at must be null or an ISO timestamp")
    if isinstance(revoked_at, str):
        parse_iso_timestamp(revoked_at, "revoked_at")
    if not isinstance(capability_id, str) or not SLUG.fullmatch(capability_id):
        raise CapabilityError("capability_id must be a lowercase slug")
    if not isinstance(capability_version, str) or not VERSION.fullmatch(capability_version):
        raise CapabilityError("capability_version must be an explicit semantic version")
    skill_entry = next(entry for entry in entries if entry.relative_path == "SKILL.md")
    try:
        skill_text = skill_entry.content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CapabilityError("SKILL.md is not strict UTF-8") from exc
    name_match = re.search(r"(?m)^name:\s*([a-z0-9][a-z0-9-]*)\s*$", skill_text)
    if not name_match or name_match.group(1) != capability_id:
        raise CapabilityError("SKILL.md frontmatter name must match capability_id")
    return manifest


def git_worktree_contamination(repo: Path | str, source_path: str) -> tuple[dict[str, str], ...]:
    raw = run_git(
        repo,
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--ignored=matching",
            "--untracked-files=all",
            "--",
            source_path,
        ],
    )
    records: list[dict[str, str]] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        if len(record) < 4:
            raise CapabilityError("Malformed Git status record")
        status_code = record[:2].decode("ascii", errors="replace")
        path = record[3:].decode("utf-8", errors="replace")
        records.append({"status": status_code, "path": path})
    return tuple(records)


def inspect_git_source(
    repo: Path | str,
    commit: str,
    source_path: str,
    *,
    pilot_policy: bool,
) -> GitSource:
    repo_path = Path(repo).resolve(strict=True)
    source = validate_source_path(source_path)
    full_commit = _resolve_commit(repo_path, commit)
    tree_oid = _resolve_tree(repo_path, full_commit, source)
    raw_entries = _parse_ls_tree(
        run_git(repo_path, ["ls-tree", "-r", "-z", "--full-tree", full_commit, "--", source]),
        source,
    )
    _validate_path_set((entry[3] for entry in raw_entries), ascii_only=pilot_policy)
    entries: list[GitEntry] = []
    for mode, object_type, oid, relative in raw_entries:
        if object_type != "blob" or mode not in ({"100644"} if pilot_policy else {"100644", "100755"}):
            raise CapabilityError(
                f"Unsupported Git mode/type {mode}/{object_type}: {relative}"
            )
        if not GIT_OID.fullmatch(oid):
            raise CapabilityError(f"Invalid blob OID for {relative}")
        content = run_git(repo_path, ["cat-file", "blob", oid])
        entries.append(
            GitEntry(
                mode=mode,
                object_type=object_type,
                oid=oid,
                repo_path=f"{source}/{relative}",
                relative_path=relative,
                size=len(content),
                sha256=sha256_bytes(content),
                content=content,
            )
        )
    entries.sort(key=lambda item: item.relative_path.encode("utf-8"))
    manifest = _validate_pilot_layout(entries) if pilot_policy else None
    file_reports = [entry.report() for entry in entries]
    return GitSource(
        repo=str(repo_path),
        source_commit=full_commit,
        source_path=source,
        source_tree=tree_oid,
        entries=tuple(entries),
        payload_fingerprint=tree_fingerprint(
            (entry.relative_path, entry.sha256) for entry in entries
        ),
        legacy_windows_payload_fingerprint=legacy_windows_tree_fingerprint(
            (entry.relative_path, entry.sha256) for entry in entries
        ),
        manifest_fingerprint=canonical_json_hash(file_reports),
        worktree_contamination=git_worktree_contamination(repo_path, source),
        capability_manifest=manifest,
    )


def _is_reparse(stat_result: os.stat_result) -> bool:
    attributes = getattr(stat_result, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse)


def absolute_plain_path(value: Path | str, *, must_exist: bool) -> Path:
    candidate = Path(os.path.abspath(os.fspath(value)))
    if must_exist and not candidate.exists():
        raise CapabilityError(f"Path does not exist: {candidate}")
    cursor = candidate if candidate.exists() else candidate.parent
    while True:
        if cursor.exists():
            details = cursor.lstat()
            if cursor.is_symlink() or _is_reparse(details):
                raise CapabilityError(f"Path crosses a reparse point or symlink: {cursor}")
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    return candidate


def _has_named_stream(path: Path) -> bool:
    if os.name != "nt":
        return False

    class StreamData(ctypes.Structure):
        _fields_ = [
            ("StreamSize", ctypes.c_longlong),
            ("cStreamName", ctypes.c_wchar * (260 + 36)),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    find_first = kernel32.FindFirstStreamW
    find_first.argtypes = [ctypes.c_wchar_p, ctypes.c_int, ctypes.POINTER(StreamData), ctypes.c_uint]
    find_first.restype = ctypes.c_void_p
    find_next = kernel32.FindNextStreamW
    find_next.argtypes = [ctypes.c_void_p, ctypes.POINTER(StreamData)]
    find_next.restype = ctypes.c_int
    close = kernel32.FindClose
    close.argtypes = [ctypes.c_void_p]
    invalid = ctypes.c_void_p(-1).value
    data = StreamData()
    handle = find_first(str(path), 0, ctypes.byref(data), 0)
    if handle == invalid:
        error = ctypes.get_last_error()
        if error in {2, 38}:
            return False
        raise CapabilityError(f"Could not enumerate NTFS streams for {path}: {error}")
    try:
        while True:
            if data.cStreamName and data.cStreamName != "::$DATA":
                return True
            if not find_next(handle, ctypes.byref(data)):
                error = ctypes.get_last_error()
                if error == 38:
                    break
                raise CapabilityError(f"Could not enumerate NTFS streams for {path}: {error}")
        return False
    finally:
        close(handle)


def _hardlink_count(path: Path, stat_result: os.stat_result) -> int:
    if os.name != "nt":
        return stat_result.st_nlink
    import msvcrt
    from ctypes import wintypes

    class FileInformation(ctypes.Structure):
        _fields_ = [
            ("FileAttributes", wintypes.DWORD),
            ("CreationTime", wintypes.FILETIME),
            ("LastAccessTime", wintypes.FILETIME),
            ("LastWriteTime", wintypes.FILETIME),
            ("VolumeSerialNumber", wintypes.DWORD),
            ("FileSizeHigh", wintypes.DWORD),
            ("FileSizeLow", wintypes.DWORD),
            ("NumberOfLinks", wintypes.DWORD),
            ("FileIndexHigh", wintypes.DWORD),
            ("FileIndexLow", wintypes.DWORD),
        ]

    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        handle = msvcrt.get_osfhandle(descriptor)
        info = FileInformation()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        query = kernel32.GetFileInformationByHandle
        query.argtypes = [ctypes.c_void_p, ctypes.POINTER(FileInformation)]
        query.restype = wintypes.BOOL
        if not query(handle, ctypes.byref(info)):
            raise CapabilityError(
                f"Could not inspect hardlink count for {path}: {ctypes.get_last_error()}"
            )
        return int(info.NumberOfLinks)
    finally:
        os.close(descriptor)


def scan_directory(
    root: Path | str,
    *,
    exclude_marker: bool = True,
    reject_hardlinks: bool = False,
) -> dict[str, Any]:
    root_path = absolute_plain_path(root, must_exist=True)
    root_stat = root_path.lstat()
    if not stat.S_ISDIR(root_stat.st_mode) or _is_reparse(root_stat):
        raise CapabilityError(f"Target root is not a plain directory: {root_path}")
    rows: list[dict[str, Any]] = []

    def walk(directory: Path, prefix: str) -> None:
        with os.scandir(directory) as iterator:
            children = sorted(iterator, key=lambda item: item.name.encode("utf-8"))
        for child in children:
            relative = f"{prefix}/{child.name}" if prefix else child.name
            validate_relative_path(relative, ascii_only=False)
            child_stat = child.stat(follow_symlinks=False)
            if child.is_symlink() or _is_reparse(child_stat):
                raise CapabilityError(f"Reparse point or symlink in target: {relative}")
            if stat.S_ISDIR(child_stat.st_mode):
                walk(Path(child.path), relative)
                continue
            if not stat.S_ISREG(child_stat.st_mode):
                raise CapabilityError(f"Non-regular file in target: {relative}")
            if exclude_marker and relative in MARKER_NAMES:
                continue
            if reject_hardlinks and _hardlink_count(Path(child.path), child_stat) > 1:
                raise CapabilityError(f"Hard-linked staging file is not allowed: {relative}")
            path = Path(child.path)
            if _has_named_stream(path):
                raise CapabilityError(f"Named NTFS stream detected: {relative}")
            content_hash = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    content_hash.update(block)
            rows.append(
                {
                    "path": relative,
                    "size": child_stat.st_size,
                    "sha256": content_hash.hexdigest(),
                }
            )

    walk(root_path, "")
    _validate_path_set((row["path"] for row in rows), ascii_only=False)
    rows.sort(key=lambda row: row["path"].encode("utf-8"))
    return {
        "root": str(root_path),
        "file_count": len(rows),
        "files": rows,
        "payload_fingerprint": tree_fingerprint(
            (row["path"], row["sha256"]) for row in rows
        ),
        "legacy_windows_payload_fingerprint": legacy_windows_tree_fingerprint(
            (row["path"], row["sha256"]) for row in rows
        ),
    }


def _is_safe_temp_child(path: Path, prefix: str) -> bool:
    temp_root = absolute_plain_path(tempfile.gettempdir(), must_exist=True)
    try:
        candidate = absolute_plain_path(path, must_exist=False)
    except CapabilityError:
        return False
    try:
        candidate.relative_to(temp_root)
    except ValueError:
        return False
    return candidate != temp_root and candidate.name.startswith(prefix)


def materialize_git_source(
    source: GitSource,
    output: Path | str | None = None,
) -> dict[str, Any]:
    prefix = "capability-materialize-"
    if source.capability_manifest and source.capability_manifest.get("revoked_at"):
        raise CapabilityError("Refusing to materialize a revoked capability")
    if output is None:
        output_path = Path(tempfile.mkdtemp(prefix=prefix)).resolve(strict=True)
        created = True
    else:
        output_path = Path(output).resolve(strict=False)
        if not _is_safe_temp_child(output_path, prefix):
            raise CapabilityError(
                f"Materialization output must be a unique {prefix}* child of system TEMP"
            )
        if output_path.exists():
            raise CapabilityError(f"Materialization output already exists: {output_path}")
        output_path.mkdir(parents=False)
        created = True
    try:
        for entry in source.entries:
            destination = output_path.joinpath(*PurePosixPath(entry.relative_path).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as handle:
                handle.write(entry.content)
        scanned = scan_directory(output_path, reject_hardlinks=True)
        if scanned["payload_fingerprint"] != source.payload_fingerprint:
            raise CapabilityError("Post-materialization payload fingerprint mismatch")
        return {
            **source.report(),
            "materialized_to": str(output_path),
            "post_materialize_fingerprint": scanned["payload_fingerprint"],
            "post_materialize_verified": True,
        }
    except Exception:
        if created and output_path.exists() and _is_safe_temp_child(output_path, prefix):
            shutil.rmtree(output_path)
        raise


def _require_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise CapabilityError(f"Marker field {key!r} must be a non-empty string")
    return value


def parse_iso_timestamp(value: str, field: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CapabilityError(f"Invalid ISO timestamp in {field}") from exc
    if parsed.tzinfo is None:
        raise CapabilityError(f"Timestamp in {field} must include a timezone")
    return parsed


def parse_marker_data(data: Any) -> MarkerInfo:
    if not isinstance(data, dict):
        raise CapabilityError("Marker must contain a JSON object")
    schema = data.get("schema_version", 1)
    if not isinstance(schema, int):
        raise CapabilityError("Marker schema_version must be an integer")
    managed_by = _require_string(data, "managed_by")
    tool = _require_string(data, "tool")
    if managed_by != MANAGED_BY:
        raise CapabilityError(f"Marker is not managed by {MANAGED_BY}")
    if schema == 1:
        windows_value = data.get("source_fingerprint")
        linux_value = data.get("fingerprint")
        if windows_value is not None and not isinstance(windows_value, str):
            raise CapabilityError("source_fingerprint must be a string")
        if linux_value is not None and not isinstance(linux_value, str):
            raise CapabilityError("fingerprint must be a string")
        if windows_value and linux_value and windows_value != linux_value:
            raise CapabilityError("Ambiguous v1 marker has conflicting fingerprint fields")
        claimed = windows_value or linux_value
        if not claimed or not HEX_64.fullmatch(claimed):
            raise CapabilityError("Legacy marker has no valid fingerprint")
        if windows_value and linux_value:
            variant = "v1-dual"
        elif windows_value:
            variant = "v1-windows-source_fingerprint"
        else:
            variant = "v1-linux-fingerprint"
        return MarkerInfo(schema, variant, managed_by, tool, claimed, dict(data))
    if schema == 2:
        managed_copy_fields = {
            "schema_version",
            "managed_by",
            "tool",
            "canonical_source",
            "source_fingerprint",
            "installed_at",
            "activation_boundary",
            "transaction_id",
        }
        if set(data) == managed_copy_fields:
            for key in managed_copy_fields - {"schema_version"}:
                _require_string(data, key)
            if not SLUG.fullmatch(tool):
                raise CapabilityError("Invalid marker tool")
            if not HEX_64.fullmatch(data["source_fingerprint"]):
                raise CapabilityError("Invalid marker source_fingerprint")
            if not re.fullmatch(r"[0-9a-f]{32}", data["transaction_id"]):
                raise CapabilityError("Invalid marker transaction_id")
            parse_iso_timestamp(data["installed_at"], "installed_at")
            return MarkerInfo(
                schema,
                "v2-managed-copy-windows",
                managed_by,
                tool,
                data["source_fingerprint"],
                dict(data),
            )
        required = {
            "schema_version",
            "managed_by",
            "tool",
            "capability_id",
            "capability_version",
            "scope",
            "source_commit",
            "source_tree",
            "payload_fingerprint",
            "manifest_fingerprint",
            "installed_at",
            "activation_boundary",
        }
        unknown = sorted(set(data) - required)
        if unknown:
            raise CapabilityError(f"Unknown v2 marker fields: {', '.join(unknown)}")
        for key in required:
            if key not in {"schema_version"}:
                _require_string(data, key)
        if not SLUG.fullmatch(tool):
            raise CapabilityError("Invalid marker tool")
        if not SLUG.fullmatch(data["capability_id"]):
            raise CapabilityError("Invalid marker capability_id")
        if not VERSION.fullmatch(data["capability_version"]):
            raise CapabilityError("Invalid marker capability_version")
        if not GIT_OID.fullmatch(data["source_commit"]):
            raise CapabilityError("Invalid marker source_commit")
        if not GIT_OID.fullmatch(data["source_tree"]):
            raise CapabilityError("Invalid marker source_tree")
        for key in ("payload_fingerprint", "manifest_fingerprint"):
            if not HEX_64.fullmatch(data[key]):
                raise CapabilityError(f"Invalid marker {key}")
        parse_iso_timestamp(data["installed_at"], "installed_at")
        return MarkerInfo(
            schema,
            "v2",
            managed_by,
            tool,
            data["payload_fingerprint"],
            dict(data),
        )
    raise CapabilityError(f"Unsupported marker schema_version: {schema}")


def read_marker(path: Path | str) -> MarkerInfo:
    marker_path = Path(path)
    try:
        raw = marker_path.read_bytes()
        data = strict_json_loads(raw.decode("utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, CapabilityError) as exc:
        raise CapabilityError(f"Malformed marker: {marker_path}") from exc
    return parse_marker_data(data)


def find_managed_marker(target: Path | str) -> Path | None:
    target_path = Path(target)
    found: list[Path] = []
    for name in MARKER_NAMES:
        candidate = target_path / name
        try:
            candidate_stat = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise CapabilityError(f"Could not inspect managed marker: {candidate}") from exc
        if not stat.S_ISREG(candidate_stat.st_mode) or _is_reparse(candidate_stat):
            raise CapabilityError(f"Managed marker is not a plain file: {candidate}")
        found.append(candidate)
    if len(found) > 1:
        names = ", ".join(path.name for path in found)
        raise CapabilityError(f"Multiple managed marker files are present: {names}")
    return found[0] if found else None


def verify_target(
    target: Path | str,
    tool: str,
    *,
    repo: Path | str | None = None,
    commit: str | None = None,
    source_path: str | None = None,
    activation_boundary: str = "unknown",
    last_fetch: str | None = None,
) -> dict[str, Any]:
    target_path = Path(target).resolve(strict=False)
    checked_at = utc_now()
    base: dict[str, Any] = {
        "tool": tool,
        "target": str(target_path),
        "activation_evidence_class": activation_boundary,
        "last_verified": checked_at,
        "writes_performed": 0,
        "security_statement": SECURITY_STATEMENT,
    }
    if not target_path.is_dir():
        return {**base, "status": "UNSUPPORTED", "reason": "target directory missing"}
    try:
        marker_path = find_managed_marker(target_path)
    except CapabilityError as exc:
        return {**base, "status": "MALFORMED_MARKER", "reason": str(exc)}
    if marker_path is None:
        return {**base, "status": "UNSUPPORTED", "reason": "managed marker missing"}
    base["marker_file"] = marker_path.name
    try:
        marker = read_marker(marker_path)
    except CapabilityError as exc:
        return {**base, "status": "MALFORMED_MARKER", "reason": str(exc)}
    base["marker"] = marker.report()
    if marker.tool != tool:
        return {
            **base,
            "status": "MALFORMED_MARKER",
            "reason": f"marker tool {marker.tool!r} does not match target tool {tool!r}",
        }
    try:
        installed = scan_directory(target_path, exclude_marker=True)
    except CapabilityError as exc:
        return {**base, "status": "UNSUPPORTED", "reason": str(exc)}
    if marker.variant == "v1-dual":
        compatible = {
            installed["payload_fingerprint"],
            installed["legacy_windows_payload_fingerprint"],
        }
        marker_comparable_fingerprint = marker.claimed_fingerprint
        marker_fingerprint_key = "v1_dual_compatible"
        marker_matches = marker.claimed_fingerprint in compatible
    else:
        marker_fingerprint_key = (
            "legacy_windows_payload_fingerprint"
            if marker.variant
            in {"v1-windows-source_fingerprint", "v2-managed-copy-windows"}
            else "payload_fingerprint"
        )
        marker_comparable_fingerprint = installed[marker_fingerprint_key]
        marker_matches = marker_comparable_fingerprint == marker.claimed_fingerprint
    base["installed_payload_fingerprint"] = installed["payload_fingerprint"]
    base["marker_comparable_fingerprint"] = marker_comparable_fingerprint
    base["marker_fingerprint_algorithm"] = marker_fingerprint_key
    base["installed_file_count"] = installed["file_count"]
    if not marker_matches:
        return {
            **base,
            "status": "DRIFT",
            "reason": "installed payload differs from marker claim",
        }

    expected: GitSource | None = None
    source_error: str | None = None
    if repo is not None or commit is not None or source_path is not None:
        if repo is None or commit is None or source_path is None:
            source_error = "repo, commit and source_path must be supplied together"
        else:
            try:
                expected = inspect_git_source(repo, commit, source_path, pilot_policy=False)
            except CapabilityError as exc:
                source_error = str(exc)
    if expected is not None:
        base["expected_source"] = {
            "source_commit": expected.source_commit,
            "source_tree": expected.source_tree,
            "payload_fingerprint": expected.payload_fingerprint,
            "legacy_windows_payload_fingerprint": expected.legacy_windows_payload_fingerprint,
            "manifest_fingerprint": expected.manifest_fingerprint,
            "file_count": len(expected.entries),
        }
        base["worktree_contamination_count"] = len(expected.worktree_contamination)
        base["worktree_contamination"] = list(expected.worktree_contamination)
        base["source_matches_installed"] = (
            expected.payload_fingerprint == installed["payload_fingerprint"]
        )
    elif source_error:
        base["source_error"] = source_error

    if marker.legacy:
        return {
            **base,
            "status": "LEGACY_MARKER",
            "reason": "legacy payload matches its marker; migrate only in an approved release",
        }
    if source_error:
        return {**base, "status": "SOURCE_UNAVAILABLE", "reason": source_error}
    if expected is None:
        return {
            **base,
            "status": "SOURCE_UNAVAILABLE",
            "reason": "no pinned Git source was supplied",
        }
    marker_data = marker.data
    if (
        expected.payload_fingerprint != installed["payload_fingerprint"]
        or marker_data["source_commit"] != expected.source_commit
        or marker_data["source_tree"] != expected.source_tree
        or marker_data["manifest_fingerprint"] != expected.manifest_fingerprint
    ):
        return {**base, "status": "DRIFT", "reason": "v2 marker/source mismatch"}
    if not last_fetch:
        return {
            **base,
            "status": "STALE_VIEW",
            "reason": "revocation view has no explicit last-fetch timestamp",
        }
    return {**base, "status": "CURRENT", "revocation_last_fetch": last_fetch}


def marker_v2(
    *,
    capability_id: str,
    capability_version: str,
    tool: str,
    scope: str,
    source: GitSource,
    activation_boundary: str,
    installed_at: str | None = None,
) -> dict[str, Any]:
    candidate = {
        "schema_version": 2,
        "managed_by": MANAGED_BY,
        "capability_id": capability_id,
        "capability_version": capability_version,
        "tool": tool,
        "scope": scope,
        "source_commit": source.source_commit,
        "source_tree": source.source_tree,
        "payload_fingerprint": source.payload_fingerprint,
        "manifest_fingerprint": source.manifest_fingerprint,
        "installed_at": installed_at or utc_now(),
        "activation_boundary": activation_boundary,
    }
    parse_marker_data(candidate)
    return candidate
