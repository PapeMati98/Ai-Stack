"""Read-only, fail-closed proof that a frozen input set exists in current HEAD."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess


# freeze() permits at most 64000 bytes per captured input. Additional aggregate
# bounds intentionally keep this gate narrower, rather than widening freeze.
MAX_INPUTS = 256
MAX_INPUT_BYTES = 64000
MAX_TOTAL_BYTES = 4 * 1024 * 1024
GIT_TIMEOUT = 10
_OID = re.compile(rb"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def _safe_relative(value: object) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 1024
            and not any(ord(c) < 32 or ord(c) == 127 for c in value)
            and not any(c in value for c in "\\:")
            and all(p not in {"", ".", ".."} for p in value.split("/")))


def _plain_path(path: Path, *, directory: bool) -> bool:
    """Refuse links/reparse points before resolving, including ancestor links."""
    for part in (path, *path.parents):
        info = part.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            return False
        if part == path:
            if not (stat.S_ISDIR(info.st_mode) if directory
                    else stat.S_ISREG(info.st_mode)):
                return False
        elif not stat.S_ISDIR(info.st_mode):
            return False
    return True


def _git(cwd: Path, *args: str, data: bytes | None = None) -> bytes:
    # Do not let caller environment redirect object reads to a different repo,
    # inject config, enable replacements, or start an interactive credential UI.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0",
               GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(
        ["git", "--no-pager", "--literal-pathspecs", "-C", str(cwd), *args],
        input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=GIT_TIMEOUT, check=True, shell=False, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return result.stdout


def _head(root: Path) -> bytes:
    value = _git(root, "rev-parse", "--verify", "HEAD^{commit}").strip()
    if not _OID.fullmatch(value):
        raise ValueError("invalid HEAD")
    return value


def inputs_match_head(frozen: dict) -> bool:
    """Return True only for matching regular blobs in one unchanged HEAD.

    This is an integrity gate, not publication authorization. No input body is
    logged, exported, or read from the working tree. Any ambiguity returns False.
    """
    try:
        if not isinstance(frozen, dict):
            return False
        raw_vault = frozen.get("vault_path")
        entries = frozen.get("inputs")
        if (not isinstance(raw_vault, str) or not raw_vault
                or any(ord(c) < 32 for c in raw_vault)
                or not isinstance(entries, list) or not 1 <= len(entries) <= MAX_INPUTS):
            return False
        vault = Path(raw_vault)
        if (not vault.is_absolute() or ".." in vault.parts
                or not _plain_path(vault, directory=True)):
            return False
        vault = vault.resolve(strict=True)
        checked = {}
        total = 0
        for entry in entries:
            if not isinstance(entry, dict):
                return False
            name, digest, size = (entry.get(k) for k in ("path", "sha256", "bytes"))
            if (not _safe_relative(name) or name in checked
                    or not isinstance(digest, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    or type(size) is not int or not 0 <= size <= MAX_INPUT_BYTES):
                return False
            if not _plain_path(vault.joinpath(*name.split("/")), directory=False):
                return False
            total += size
            if total > MAX_TOTAL_BYTES:
                return False
            checked[name] = (digest, size)

        raw_root = _git(vault, "rev-parse", "--show-toplevel")
        root = Path(os.fsdecode(raw_root.rstrip(b"\r\n")))
        if not root.is_absolute() or not _plain_path(root, directory=True):
            return False
        root = root.resolve(strict=True)
        prefix = vault.relative_to(root).as_posix()
        if prefix == "." or not _safe_relative(prefix):
            return False  # a vault must be a proper repo subdirectory
        if _git(vault, "rev-parse", "--show-prefix").rstrip(b"\r\n") != os.fsencode(prefix + "/"):
            return False
        commit = _head(root)
        paths = {prefix + "/" + name: spec for name, spec in checked.items()}
        tree = _git(root, "ls-tree", "-z", commit.decode("ascii"), "--", *paths)
        objects = []
        seen = set()
        for record in tree.split(b"\0"):
            if not record:
                continue
            meta, raw_name = record.split(b"\t", 1)
            mode, kind, oid = meta.split(b" ")
            name = os.fsdecode(raw_name)
            if (name not in paths or name in seen or mode not in {b"100644", b"100755"}
                    or kind != b"blob" or not _OID.fullmatch(oid)):
                return False
            seen.add(name)
            objects.append((oid, paths[name]))
        if seen != set(paths):
            return False
        request = b"".join(oid + b"\n" for oid, _ in objects)
        headers = _git(root, "cat-file", "--batch-check", data=request).splitlines()
        if len(headers) != len(objects):
            return False
        for header, (oid, (_, size)) in zip(headers, objects):
            if header != oid + b" blob " + str(size).encode("ascii"):
                return False
        blob_output = _git(root, "cat-file", "--batch", data=request)
        offset = 0
        for oid, (digest, size) in objects:
            header = oid + b" blob " + str(size).encode("ascii") + b"\n"
            if blob_output[offset:offset + len(header)] != header:
                return False
            offset += len(header)
            body = blob_output[offset:offset + size]
            if len(body) != size or hashlib.sha256(body).hexdigest() != digest:
                return False
            offset += size
            if blob_output[offset:offset + 1] != b"\n":
                return False
            offset += 1
        return offset == len(blob_output) and _head(root) == commit
    except (OSError, ValueError, TypeError, UnicodeError, subprocess.SubprocessError):
        return False
