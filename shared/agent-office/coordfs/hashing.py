"""Capture file_hash at claim time and recompute it for stale-resource checks."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .constants import TREE_HASH_MAX_BYTES, TREE_HASH_MAX_FILES
from .identity import is_filesystem_id


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_tree(path: Path) -> str:
    files: list[Path] = []
    for current, dir_names, file_names in os_walk_sorted(path):
        dir_names.sort()
        for name in sorted(file_names):
            files.append(Path(current) / name)
    digest = hashlib.sha256()
    truncated = len(files) > TREE_HASH_MAX_FILES
    selected = files[:TREE_HASH_MAX_FILES] if truncated else files
    total = 0
    listing_only = truncated
    for file_path in selected:
        try:
            size = file_path.stat().st_size
        except OSError:
            continue
        total += size
        if total > TREE_HASH_MAX_BYTES:
            listing_only = True
            break
    digest.update(b"tree-v1\n")
    if listing_only:
        digest.update(b"listing\n")
        for file_path in files:
            try:
                stat = file_path.stat()
            except OSError:
                continue
            relative = file_path.relative_to(path).as_posix()
            digest.update(f"{relative}\0{stat.st_size}\n".encode("utf-8"))
        return digest.hexdigest()
    for file_path in selected:
        relative = file_path.relative_to(path).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hash_file(file_path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def os_walk_sorted(path: Path):
    import os

    return os.walk(path)


def hash_path(path: Path) -> str:
    try:
        if not path.exists():
            return ""
        if path.is_file():
            return hash_file(path)
        if path.is_dir():
            return hash_tree(path)
    except OSError:
        return ""
    return ""


def resource_hash(resource_id: str) -> str:
    if not is_filesystem_id(resource_id):
        return ""
    return hash_path(Path(resource_id))
