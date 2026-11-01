"""Private candidate exporter: pinned Git-blob export with receipts.

Reads committed blobs from parent-supplied frozen snapshot repositories and
materializes them under an explicit output parent. Standard library only.

Public API:
  validate_manifest(manifest) -> dict
  export_candidate(manifest, destination, *, allowed_output_parent) -> dict
  verify_export(destination, receipt) -> dict

All failures raise ValueError with a non-sensitive message. No absolute
source repository paths are ever placed in candidate files or receipts.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from pathlib import Path

from .privacy import _RECOGNIZED_CARETAKER_SOURCE

API_VERSION = 1
DEFAULT_MAX_TOTAL_BYTES = 209715200

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

_TOP_KEYS = frozenset({"version", "max_total_bytes", "sources"})
_SOURCE_KEYS = frozenset({"id", "repo", "commit", "files"})
_FILE_KEYS = frozenset({"path", "target", "sha256"})
_RECEIPT_KEYS = frozenset({"version", "status", "files", "total_bytes", "tree_hash"})
_RECEIPT_FILE_KEYS = frozenset(
    {"source", "commit", "source_path", "target", "sha256", "bytes"}
)

_RESERVED_STEMS = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + ["COM%d" % i for i in range(1, 10)]
    + ["LPT%d" % i for i in range(1, 10)]
)

_DANGEROUS_SEGMENTS = frozenset(
    {
        "secrets",
        "vault",
        "raw",
        "proposals",
        "logs",
        "log",
        "cache",
        "state",
        "databases",
        "database",
        ".git",
        "node_modules",
        ".aws",
        ".ssh",
        ".gnupg",
        ".azure",
        ".kube",
        ".docker",
        ".config",
    }
)
_DANGEROUS_BASENAMES = frozenset(
    {
        ".bashrc",
        ".bash_profile",
        ".profile",
        ".netrc",
        "_netrc",
        ".npmrc",
        ".pypirc",
    }
)


def _fail(message):
    raise ValueError("exporter: %s" % message)


def _check_literal_path(value, field):
    """Reject absolute/Windows/UNC/traversal/NUL/ADS/reserved path text."""
    if not isinstance(value, str) or not value:
        _fail("%s must be a non-empty string" % field)
    if "\x00" in value:
        _fail("%s contains NUL" % field)
    if "\\" in value:
        _fail("%s contains backslash" % field)
    if value.startswith("/"):
        _fail("%s is absolute" % field)
    if re.match(r"^[A-Za-z]:", value):
        _fail("%s looks like a Windows drive path" % field)
    if value.startswith("//"):
        _fail("%s looks like a UNC path" % field)
    parts = value.split("/")
    for seg in parts:
        if seg in ("", ".", ".."):
            _fail("%s has empty/dot/traversal segment" % field)
        if ":" in seg:
            _fail("%s has ADS/drive-colon segment" % field)
        if seg != seg.rstrip(" ."):
            _fail("%s has trailing space/dot segment" % field)
        stem = seg.split(".", 1)[0].upper()
        if stem in _RESERVED_STEMS:
            _fail("%s uses reserved Windows name" % field)
    return parts


def _check_dangerous(parts, field):
    lowered = [p.lower() for p in parts]
    base = lowered[-1]
    for index, seg in enumerate(lowered):
        if seg.startswith(".env"):
            _fail("%s matches dangerous .env* pattern" % field)
        if seg.startswith("auth") and seg.endswith(".json"):
            _fail("%s matches dangerous auth*.json pattern" % field)
        if seg.startswith("credentials"):
            _fail("%s matches dangerous credentials* pattern" % field)
        if seg.startswith("secrets") and not (
            index == len(parts) - 1 and "/".join(parts) == _RECOGNIZED_CARETAKER_SOURCE
        ):
            _fail("%s matches dangerous secrets* pattern" % field)
    for seg in lowered:
        if seg in _DANGEROUS_SEGMENTS:
            _fail("%s passes through dangerous '%s'" % (field, seg))
    if base in _DANGEROUS_BASENAMES:
        _fail("%s is a home-profile file" % field)


def _run_git(repo, *args):
    try:
        proc = subprocess.run(
            ["git", "-C", repo, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            timeout=120,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        _fail("git invocation failed")
    if proc.returncode != 0:
        _fail("git %s failed for repository" % (args[0] if args else "?"))
    return proc.stdout


def _read_blob(repo, commit, path):
    """Return (mode, blob_sha, data) for commit:path; rejects non-blobs."""
    out = _run_git(repo, "ls-tree", commit, "--", path)
    try:
        text = out.decode("utf-8")
    except UnicodeDecodeError:
        _fail("unreadable tree entry")
    entries = [ln for ln in text.splitlines() if ln]
    if len(entries) != 1:
        _fail("source path not found exactly once in commit")
    meta, _, name = entries[0].partition("\t")
    if name != path:
        _fail("source path matched a different entry")
    fields = meta.split()
    if len(fields) != 3:
        _fail("unreadable tree entry")
    mode, objtype, blob_sha = fields
    if objtype == "commit":
        _fail("source path is a gitlink/submodule")
    if objtype != "blob":
        _fail("source path is not a file blob")
    if mode == "120000":
        _fail("source path is a symlink and symlinks are refused")
    if mode == "100755":
        _fail("source path is executable and executables are refused")
    if mode not in ("100644", "100755", "120000"):
        _fail("source path has unsupported mode")
    data = _run_git(repo, "cat-file", "blob", blob_sha)
    return mode, blob_sha, data


def _validate_structure(manifest):
    if not isinstance(manifest, dict):
        _fail("manifest must be an object")
    if set(manifest) != _TOP_KEYS and set(manifest) - _TOP_KEYS:
        _fail("manifest has unknown fields")
    if set(manifest) != _TOP_KEYS:
        _fail("manifest is missing required fields")
    if manifest.get("version") != 1:
        _fail("manifest version must be 1")
    budget = manifest.get("max_total_bytes")
    if (
        not isinstance(budget, int)
        or isinstance(budget, bool)
        or budget <= 0
        or budget > 2**40
    ):
        _fail("max_total_bytes must be a positive int")
    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        _fail("sources must be a non-empty list")
    seen_ids = set()
    for src in sources:
        if not isinstance(src, dict) or set(src) != _SOURCE_KEYS:
            _fail("source entry has unknown or missing fields")
        sid = src.get("id")
        if not isinstance(sid, str) or not _ID_RE.match(sid):
            _fail("source id is invalid")
        if sid in seen_ids:
            _fail("duplicate source id")
        seen_ids.add(sid)
        repo = src.get("repo")
        if not isinstance(repo, str) or not repo or "\x00" in repo:
            _fail("source repo must be a non-empty local path")
        if not os.path.isabs(repo):
            _fail("source repo must be an explicit absolute local path")
        if not os.path.isdir(repo):
            _fail("source repo directory is missing")
        commit = src.get("commit")
        if not isinstance(commit, str) or not _COMMIT_RE.match(commit):
            _fail("source commit must be literal 40/64 lowercase hex")
        files = src.get("files")
        if not isinstance(files, list) or not files:
            _fail("source files must be a non-empty list")
        for entry in files:
            if not isinstance(entry, dict) or set(entry) != _FILE_KEYS:
                _fail("file entry has unknown or missing fields")
            if not isinstance(entry.get("sha256"), str) or not _SHA256_RE.match(
                entry["sha256"]
            ):
                _fail("file sha256 must be 64 lowercase hex")
    return budget


def _load_validated(manifest):
    """Validate every path/blob/hash; return staged (target, record, data)."""
    budget = _validate_structure(manifest)
    staged = []
    seen_targets = set()
    seen_source_paths = set()
    total = 0
    for src in manifest["sources"]:
        sid = src["id"]
        repo = src["repo"]
        commit = src["commit"]
        out = _run_git(repo, "cat-file", "-t", commit)
        if out.decode("utf-8", "replace").strip() != "commit":
            _fail("source commit is not a commit object")
        for entry in src["files"]:
            src_parts = _check_literal_path(entry["path"], "source path")
            tgt_parts = _check_literal_path(entry["target"], "target")
            _check_dangerous(src_parts, "source path")
            _check_dangerous(tgt_parts, "target")
            target = "/".join(tgt_parts)
            folded = target.casefold()
            if folded in seen_targets:
                _fail("case-insensitive target collision")
            seen_targets.add(folded)
            spath = "/".join(src_parts)
            key = (os.path.realpath(repo), commit, spath)
            if key in seen_source_paths:
                _fail("duplicate source path in commit")
            seen_source_paths.add(key)
            _, _, data = _read_blob(repo, commit, spath)
            digest = hashlib.sha256(data).hexdigest()
            if digest != entry["sha256"]:
                _fail("sha256 mismatch for target")
            total += len(data)
            if total > budget:
                _fail("total blob bytes exceed max_total_bytes")
            staged.append(
                (
                    target,
                    {
                        "source": sid,
                        "commit": commit,
                        "source_path": spath,
                        "target": target,
                        "sha256": digest,
                        "bytes": len(data),
                    },
                    data,
                )
            )
    folded_targets = sorted(seen_targets)
    for i, shorter in enumerate(folded_targets):
        for longer in folded_targets[i + 1:]:
            if longer.startswith(shorter + "/"):
                _fail("target file/dir prefix conflict")
    staged.sort(key=lambda item: item[0])
    return staged, total


def _tree_hash(records):
    digest = hashlib.sha256()
    for rec in sorted(records, key=lambda r: r["target"]):
        digest.update(rec["target"].encode("utf-8"))
        digest.update(b"\x00")
        digest.update(rec["sha256"].encode("ascii"))
        digest.update(b"\x00")
        digest.update(str(rec["bytes"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _make_receipt(records, total):
    ordered = sorted(records, key=lambda r: r["target"])
    return {
        "version": 1,
        "status": "exported",
        "files": [dict(r) for r in ordered],
        "total_bytes": total,
        "tree_hash": _tree_hash(ordered),
    }


def validate_manifest(manifest):
    """Validate a manifest without writing any output files."""
    staged, total = _load_validated(manifest)
    return {
        "version": 1,
        "status": "valid",
        "sources": len(manifest["sources"]),
        "files": len(staged),
        "total_bytes": total,
    }


def _contained(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def _is_link_or_reparse(path):
    if os.path.islink(path):
        return True
    try:
        attrs = os.lstat(path).st_file_attributes  # Windows only attr
        return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError):
        return False


def export_candidate(manifest, destination, *, allowed_output_parent):
    """Export all validated blobs under destination; return the receipt."""
    if allowed_output_parent is None:
        _fail("allowed_output_parent is required")
    parent = os.fspath(allowed_output_parent)
    dest = os.fspath(destination)
    if not parent or not dest:
        _fail("destination and allowed_output_parent must be set")
    if not _contained(dest, parent) and (
        Path(dest).resolve() != Path(parent).resolve()
    ):
        _fail("destination is outside allowed_output_parent")
    staged, total = _load_validated(manifest)
    if os.path.lexists(dest):
        _fail("destination already exists and will not be overwritten")
    try:
        os.makedirs(dest)
    except OSError:
        _fail("cannot create destination")
    try:
        for target, record, data in staged:
            out_path = Path(dest).joinpath(*target.split("/"))
            if not _contained(str(out_path), dest):
                _fail("target escapes destination")
            try:
                out_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                _fail("cannot create output directory")
            try:
                with open(out_path, "xb") as handle:
                    handle.write(data)
            except FileExistsError:
                _fail("output file already exists")
            except OSError:
                _fail("cannot write output file")
            try:
                with open(out_path, "rb") as handle:
                    if hashlib.sha256(handle.read()).hexdigest() != record[
                        "sha256"
                    ]:
                        _fail("written bytes failed re-read check")
            except OSError:
                _fail("cannot re-read output file")
    except ValueError:
        raise
    return _make_receipt([rec for _, rec, _ in staged], total)


def _validate_receipt_shape(receipt):
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_KEYS:
        _fail("receipt has unknown or missing fields")
    if receipt.get("version") != 1:
        _fail("receipt version must be 1")
    files = receipt.get("files")
    if not isinstance(files, list) or not files:
        _fail("receipt files must be a non-empty list")
    if not isinstance(receipt.get("total_bytes"), int) or isinstance(
        receipt.get("total_bytes"), bool
    ):
        _fail("receipt total_bytes must be an int")
    if not isinstance(receipt.get("tree_hash"), str) or not _SHA256_RE.match(
        receipt["tree_hash"]
    ):
        _fail("receipt tree_hash must be 64 lowercase hex")
    seen = set()
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != _RECEIPT_FILE_KEYS:
            _fail("receipt file entry has unknown or missing fields")
        _check_literal_path(entry["target"], "receipt target")
        _check_literal_path(entry["source_path"], "receipt source_path")
        if entry["target"].casefold() in seen:
            _fail("receipt has colliding targets")
        seen.add(entry["target"].casefold())
        if not isinstance(entry.get("source"), str) or not _ID_RE.match(
            entry["source"]
        ):
            _fail("receipt source id is invalid")
        if not isinstance(entry.get("commit"), str) or not _COMMIT_RE.match(
            entry["commit"]
        ):
            _fail("receipt commit is invalid")
        if not isinstance(entry.get("sha256"), str) or not _SHA256_RE.match(
            entry["sha256"]
        ):
            _fail("receipt sha256 is invalid")
        if (
            not isinstance(entry.get("bytes"), int)
            or isinstance(entry.get("bytes"), bool)
            or entry["bytes"] < 0
        ):
            _fail("receipt bytes must be a non-negative int")


def verify_export(destination, receipt):
    """Verify destination matches the receipt exactly; refuse extras."""
    _validate_receipt_shape(receipt)
    dest = os.fspath(destination)
    if not os.path.isdir(dest) or _is_link_or_reparse(dest):
        _fail("destination is not a readable directory")
    found = {}
    for root, dirs, names in os.walk(dest, followlinks=False):
        dirs.sort()
        for name in sorted(names):
            full = os.path.join(root, name)
            if _is_link_or_reparse(full):
                _fail("candidate contains a link/reparse point")
            rel = os.path.relpath(full, dest).replace(os.sep, "/")
            try:
                with open(full, "rb") as handle:
                    data = handle.read()
            except OSError:
                _fail("cannot read candidate file")
            found[rel] = (len(data), hashlib.sha256(data).hexdigest())
        for name in dirs:
            if _is_link_or_reparse(os.path.join(root, name)):
                _fail("candidate contains a link/reparse point")
    expected = {e["target"]: e for e in receipt["files"]}
    missing = sorted(set(expected) - set(found))
    extra = sorted(set(found) - set(expected))
    if missing:
        _fail("candidate is missing files")
    if extra:
        _fail("candidate has extra files")
    for target, entry in expected.items():
        size, digest = found[target]
        if size != entry["bytes"] or digest != entry["sha256"]:
            _fail("candidate file changed")
    if _tree_hash(receipt["files"]) != receipt["tree_hash"]:
        _fail("receipt tree hash does not match file list")
    total = sum(e["bytes"] for e in receipt["files"])
    if total != receipt["total_bytes"]:
        _fail("receipt total_bytes does not match file list")
    return {
        "version": 1,
        "status": "verified",
        "files": len(expected),
        "total_bytes": total,
        "tree_hash": receipt["tree_hash"],
    }
