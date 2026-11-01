"""Offline snapshot, verify and restore of an Agent Office coord home (one-store transfer).

Step "One-store transfer" of docs/agent-office/OFFICE-HOST-24-7.md. The live store is only
read, and only while the operator's writer pause is in place (<home>/office/FROZEN exists as
a regular file; this tool never creates or removes it). Nothing is ever deleted, truncated or
overwritten: every output file is created exclusively inside a new or empty real directory,
and restore re-verifies the extracted tree (files and directories) against the manifest.

    python office_store_snapshot.py snapshot --home <coord home> --out <new dir>
    python office_store_snapshot.py verify   --snapshot <dir>
    python office_store_snapshot.py restore  --snapshot <dir> --dest <new dir>

Each command prints one JSON object. Exit codes: 0 ok, 1 refused or mismatch, 2 usage.
Threat model: an operator-run offline tool on a paused store. Links, junctions, mount points
and any change of the tree between walk, archive and re-walk make the snapshot fail.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import stat
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

SCHEMA = "office.store-snapshot/v1"
ARCHIVE = "store.tar"
MANIFEST = "manifest.json"
FROZEN = ("office", "FROZEN")
CHUNK = 1024 * 1024
# A name that means the same thing on Windows and POSIX: no reserved device names, no
# characters Windows rejects or rewrites, no trailing dot/space (Win32 strips them).
_RESERVED = re.compile(r"(?i)^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³]|conin\$|conout\$)(\..*)?$")
_BAD_CHARS = re.compile(r'[<>:"|?*\\\x00-\x1f]')


class Refused(Exception):
    """A safety rule refused the operation (exit 1)."""


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_name(name: str) -> str:
    """Validated posix-relative path, identical in meaning on Windows and POSIX."""
    if not isinstance(name, str) or not name or name.startswith("/"):
        raise Refused(f"unsafe path: {name!r}")
    parts = name.split("/")
    for part in parts:
        if (not part or set(part) == {"."} or part != part.rstrip(" .") or part != part.lstrip(" ")
                or _BAD_CHARS.search(part) or _RESERVED.match(part)):
            raise Refused(f"unsafe path component {part!r} in {name!r}")
    return "/".join(parts)


def _is_link(info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _real_dir_for_output(path: Path, label: str, *, forbidden: Path | None = None) -> Path:
    """Create or accept an EMPTY real directory; never a link, junction or file.

    The parent chain is resolved first, so a link or junction in a parent is followed
    to its real target BEFORE any check; every later write uses that real path. A
    target inside `forbidden` (the live store) is refused before anything is created.
    """
    parent = path.absolute().parent
    if not parent.is_dir():
        raise Refused(f"{label} parent does not exist: {parent}")
    real = parent.resolve(strict=True) / path.name
    if forbidden is not None:
        live = forbidden.resolve(strict=True)
        if real == live or live in real.parents:
            raise Refused(f"{label} must be outside the coord home: {real}")
    try:
        info = os.lstat(real)
    except FileNotFoundError:
        os.mkdir(real)  # the new directory itself is ours
        info = os.lstat(real)
    if _is_link(info) or not stat.S_ISDIR(info.st_mode):
        raise Refused(f"{label} must be a real directory, not a link/junction/file: {path}")
    if any(os.scandir(real)):
        raise Refused(f"{label} must be empty: {path}")
    return real


def _linux_mount_points() -> list[Path]:
    """Mount points from /proc/self/mountinfo (Linux only; empty elsewhere)."""
    try:
        text = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError:
        return []
    points = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) > 4:
            # Octal escapes (\040 for space) as written by the kernel.
            points.append(Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4])))
    return points


def _refuse_mounts_inside(home: Path, mount_points: list[Path]) -> None:
    """A bind mount on the same device keeps st_dev; the mount table still shows it."""
    real_home = home.resolve()
    for point in mount_points:
        if real_home in point.parents:
            raise Refused(f"mount point inside the store: {point}")


def _walk(home: Path, home_dev: int) -> tuple[dict[str, tuple[int, int]], dict[str, tuple[int, int]]]:
    """(directories, files) -> identity; refuse links, special files and other devices."""
    directories: dict[str, tuple[int, int]] = {}
    files: dict[str, tuple[int, int]] = {}
    stack = [home]
    while stack:
        current = stack.pop()
        for entry in sorted(os.listdir(current)):
            path = current / entry
            info = os.lstat(path)
            relative = safe_name(path.relative_to(home).as_posix())
            if _is_link(info):
                raise Refused(f"link or reparse point in the store: {relative}")
            if info.st_dev != home_dev:
                raise Refused(f"mount point or other device in the store: {relative}")
            if stat.S_ISDIR(info.st_mode):
                directories[relative] = _identity(info)
                stack.append(path)
            elif stat.S_ISREG(info.st_mode):
                files[relative] = _identity(info)
            else:
                raise Refused(f"special file in the store: {relative}")
    return directories, files


def _open_same(path: Path, identity: tuple[int, int]) -> BinaryIO:
    """Open a file only if it is still the object recorded by the walk."""
    handle = path.open("rb")
    info = os.fstat(handle.fileno())
    if _identity(info) != identity or not stat.S_ISREG(info.st_mode):
        handle.close()
        raise Refused(f"store changed during snapshot: {path}")
    return handle


def _hash_stream(handle: BinaryIO) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    for block in iter(lambda: handle.read(CHUNK), b""):
        digest.update(block)
        size += len(block)
    return digest.hexdigest(), size


def _sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return _hash_stream(handle)[0]


def _counts_from_archive(archive_path: Path) -> dict[str, Any]:
    counts: dict[str, Any] = {"jsonl_files": 0, "jsonl_lines": 0, "json_files": 0, "unreadable": []}
    with tarfile.open(archive_path, "r:") as archive:
        for member in archive:
            if not member.isreg():
                continue
            name = member.name
            if not name.endswith((".jsonl", ".json")):
                continue
            data = archive.extractfile(member).read()  # type: ignore[union-attr]
            if name.endswith(".jsonl"):
                counts["jsonl_files"] += 1
                for number, raw in enumerate(data.split(b"\n"), 1):
                    if not raw.strip():
                        continue
                    counts["jsonl_lines"] += 1
                    try:
                        json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError):
                        counts["unreadable"].append(f"{name}:{number}")
            else:
                counts["json_files"] += 1
                try:
                    json.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    counts["unreadable"].append(name)
    return counts


def snapshot(home: Path, out: Path) -> dict[str, Any]:
    home_info = os.lstat(home)
    if _is_link(home_info) or not stat.S_ISDIR(home_info.st_mode):
        raise Refused(f"coord home must be a real directory: {home}")
    home = home.absolute()
    marker = home.joinpath(*FROZEN)
    try:
        marker_info = os.lstat(marker)
    except FileNotFoundError:
        raise Refused("writers are not paused: office/FROZEN is missing") from None
    if _is_link(marker_info) or not stat.S_ISREG(marker_info.st_mode):
        raise Refused("office/FROZEN must be a regular file")
    _refuse_mounts_inside(home, _linux_mount_points())
    directories, files = _walk(home, home_info.st_dev)
    out = _real_dir_for_output(out, "--out", forbidden=home)
    entries = []
    with open(out / ARCHIVE, "xb") as raw, tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for relative in directories:
            info = tarfile.TarInfo(relative)
            info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, 0
            archive.addfile(info)
        for relative in sorted(files):
            path = home.joinpath(*relative.split("/"))
            with _open_same(path, files[relative]) as handle:
                digest, size = _hash_stream(handle)
                handle.seek(0)
                info = tarfile.TarInfo(relative)
                info.size, info.mode, info.mtime = size, 0o644, 0
                archive.addfile(info, handle)
            entries.append({"path": relative, "bytes": size, "sha256": digest})
    # The paused tree must be exactly the same after archiving: same entries, same objects,
    # same bytes (a swap, a new file or a write during the run fails the snapshot).
    again_dirs, again_files = _walk(home, home_info.st_dev)
    if again_dirs != directories or again_files != files:
        raise Refused("store changed during snapshot (tree differs after archiving)")
    for entry in entries:
        path = home.joinpath(*entry["path"].split("/"))
        with _open_same(path, files[entry["path"]]) as handle:
            if _hash_stream(handle)[0] != entry["sha256"]:
                raise Refused(f"store changed during snapshot: {entry['path']}")
    manifest = {
        "schema": SCHEMA,
        "source_home": str(home),
        "created_at": _utc(),
        "directories": sorted(directories),
        "files": entries,
        "totals": {"files": len(entries), "bytes": sum(e["bytes"] for e in entries)},
        "counts": _counts_from_archive(out / ARCHIVE),
        "archive_sha256": _sha256_file(out / ARCHIVE),
    }
    with open(out / MANIFEST, "x", encoding="utf-8") as handle:
        handle.write(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n")
    return {"status": "ok", "code": "snapshot_written", "out": str(out), "totals": manifest["totals"],
            "counts": manifest["counts"], "archive_sha256": manifest["archive_sha256"]}


def _load_manifest(snapshot_dir: Path) -> dict[str, Any]:
    try:
        manifest = json.loads((snapshot_dir / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(f"manifest unreadable: {exc}") from None
    if (not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA
            or not isinstance(manifest.get("files"), list) or not isinstance(manifest.get("directories"), list)
            or not isinstance(manifest.get("archive_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", manifest["archive_sha256"]) is None):
        raise Refused("manifest schema mismatch or missing archive_sha256")
    for entry in manifest["files"]:
        safe_name(entry.get("path") if isinstance(entry, dict) else None)
    for name in manifest["directories"]:
        safe_name(name)
    return manifest


def _check_archive(archive: tarfile.TarFile, manifest: dict[str, Any]) -> list[str]:
    """Problems of an OPEN archive against the manifest (files, bytes, hashes, directories)."""
    expected = {e["path"]: e for e in manifest["files"]}
    expected_dirs = set(manifest["directories"])
    seen: dict[str, str] = {}
    seen_dirs: set[str] = set()
    problems: list[str] = []
    for member in archive:
        name = safe_name(member.name)
        if member.isdir():
            if name not in expected_dirs:
                problems.append(f"extra directory: {name}")
            seen_dirs.add(name)
            continue
        if not member.isreg():
            raise Refused(f"non-regular archive member: {name}")
        if name in seen:
            problems.append(f"duplicate member: {name}")
        digest, size = _hash_stream(archive.extractfile(member))  # type: ignore[arg-type]
        seen[name] = digest
        entry = expected.get(name)
        if entry is None:
            problems.append(f"extra file: {name}")
        elif entry["sha256"] != digest or entry["bytes"] != size:
            problems.append(f"changed file: {name}")
    problems += [f"missing file: {n}" for n in sorted(set(expected) - set(seen))]
    problems += [f"missing directory: {n}" for n in sorted(expected_dirs - seen_dirs)]
    return problems


def verify(snapshot_dir: Path) -> dict[str, Any]:
    manifest = _load_manifest(snapshot_dir)
    problems: list[str] = []
    if _sha256_file(snapshot_dir / ARCHIVE) != manifest["archive_sha256"]:
        problems.append("archive sha256 differs from manifest")
    try:
        with tarfile.open(snapshot_dir / ARCHIVE, "r:") as archive:
            problems += _check_archive(archive, manifest)
    except tarfile.TarError as exc:
        raise Refused(f"archive unreadable: {exc}") from None
    if problems:
        return {"status": "error", "code": "snapshot_mismatch", "problems": problems}
    return {"status": "ok", "code": "snapshot_verified", "files": len(manifest["files"])}


def restore(snapshot_dir: Path, dest: Path) -> dict[str, Any]:
    manifest = _load_manifest(snapshot_dir)
    expected = {e["path"] for e in manifest["files"]}
    expected_dirs = set(manifest["directories"])
    # One read of the archive bytes: hash, check and extract the same content (no reopen).
    data = (snapshot_dir / ARCHIVE).read_bytes()
    if hashlib.sha256(data).hexdigest() != manifest["archive_sha256"]:
        return {"status": "error", "code": "snapshot_mismatch", "problems": ["archive sha256 differs from manifest"]}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        problems = _check_archive(archive, manifest)
    if problems:
        return {"status": "error", "code": "snapshot_mismatch", "problems": problems}
    root = _real_dir_for_output(dest, "--dest")
    written: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        for member in archive:
            name = safe_name(member.name)
            target = root.joinpath(*name.split("/"))
            if member.isdir():
                if name not in expected_dirs:
                    raise Refused(f"archive directory not in manifest: {name}")
                target.mkdir(exist_ok=True)
                continue
            if not member.isreg() or name not in expected:
                raise Refused(f"archive member not in manifest: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "xb") as handle:  # never overwrite
                source = archive.extractfile(member)
                for block in iter(lambda: source.read(CHUNK), b""):  # type: ignore[union-attr]
                    handle.write(block)
            written.append(name)
    # The extracted tree must be exactly the manifest: files, bytes, hashes and directories.
    got_dirs, got_files = _walk(root, os.lstat(root).st_dev)
    problems = []
    if set(got_files) != expected:
        problems.append("restored file set differs from manifest")
    if set(got_dirs) != expected_dirs:
        problems.append("restored directory set differs from manifest")
    problems += [f"restored file differs: {e['path']}" for e in manifest["files"]
                 if _sha256_file(root.joinpath(*e["path"].split("/"))) != e["sha256"]]
    if problems:
        return {"status": "error", "code": "restore_mismatch", "problems": problems, "written": written}
    return {"status": "ok", "code": "restore_verified", "dest": str(root), "files": len(written)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline Agent Office store snapshot/verify/restore.")
    commands = parser.add_subparsers(dest="command", required=True)
    take = commands.add_parser("snapshot")
    take.add_argument("--home", type=Path, required=True)
    take.add_argument("--out", type=Path, required=True)
    check = commands.add_parser("verify")
    check.add_argument("--snapshot", type=Path, required=True)
    put = commands.add_parser("restore")
    put.add_argument("--snapshot", type=Path, required=True)
    put.add_argument("--dest", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    try:
        if args.command == "snapshot":
            result = snapshot(args.home, args.out)
        elif args.command == "verify":
            result = verify(args.snapshot)
        else:
            result = restore(args.snapshot, args.dest)
    except Refused as exc:
        result = {"status": "error", "code": "refused", "reason": str(exc)}
    except OSError as exc:
        result = {"status": "error", "code": "io_error", "reason": f"{type(exc).__name__}: {exc}"}
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
