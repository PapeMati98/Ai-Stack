"""Persist the complete redacted public handoff when projection truncated.

This helper exists so a downstream Office consumer can recover a full,
untouched-by-budget public view when bounded_handoff had to truncate or
drop one or more fields of a maker handoff. It only persists when the
projection declared a real loss (truncated_fields or missing_fields);
it never duplicates a clean projection and never rewrites bounded_handoff's
own loss markers. Every persisted field passes through Fleet's
redact_sensitive_value; failures fail closed, there is no unredacted
fallback. Exclusive creation in the trusted role outdir refuses a
pre-existing target; a failed write never exposes a successful reference.
"""
import hashlib
import json
import os
from pathlib import Path


PUBLIC_HANDOFF_FIELDS = (
    "status", "summary", "files_changed", "verification", "risks", "next_steps",
)
PUBLIC_HANDOFF_LIST_FIELDS = frozenset(
    {"files_changed", "verification", "risks", "next_steps"}
)
# A single documented small maximum full artifact size. The bounded_handoff
# projection caps each item at MAX_HANDOFF_ITEM_BYTES and a handoff has at
# most MAX_HANDOFF_ITEMS items, so an oversized outcome here means the raw
# handoff was gigantic, not that the projection was wrong. The refusal
# marker is honest and stays inside the response metadata.
MAX_PUBLIC_HANDOFF_BYTES = 1024 * 1024  # 1 MiB
PUBLIC_HANDOFF_FILENAME = "public-handoff.json"


def _validate_outdir(outdir):
    """Refuse anything but a plain directory owned by the role.

    The role outdir is part of the per-role worktree state; refusing a
    symlink at any parent segment blocks a hostile relocation. Partial
    resolution failures (broken link, missing path) refuse closed too.
    """
    if outdir is None:
        return None, "refused-outdir"
    if not isinstance(outdir, Path):
        try:
            outdir = Path(outdir)
        except TypeError:
            return None, "refused-outdir"
    if outdir.is_symlink() or getattr(outdir, "is_junction", lambda: False)():
        return None, "refused-outdir-symlink"
    try:
        resolved = outdir.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None, "refused-outdir"
    if not resolved.is_dir():
        return None, "refused-outdir"
    current = outdir
    while current != current.parent:
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            return None, "refused-outdir-symlink"
        current = current.parent
    return resolved, None


def _redact_value(value, fleet):
    """Return redacted value or (None, status) on failure. Preserves shape."""
    redactor = getattr(fleet, "redact_sensitive_value", None)
    if not callable(redactor):
        return None, "redactor-unavailable"
    try:
        redacted = redactor(value)
    except Exception:
        return None, "redaction-failed"
    if isinstance(value, str):
        if not isinstance(redacted, str):
            return None, "redaction-failed"
        return redacted, None
    if isinstance(value, list):
        if (not isinstance(redacted, list)
                or len(redacted) != len(value)
                or not all(isinstance(item, str) for item in redacted)):
            return None, "redaction-failed"
        return redacted, None
    return None, "unsupported-type"


def persist_public_handoff(raw_handoff, handoff_evidence, fleet, outdir):
    """Persist the complete redacted six-field handoff and return a compact ref.

    Returns None when no genuine projection truncation occurred, so the caller
    must not add a public_artifact key in that case. Returns a small reference
    dict (status, path, bytes, sha256, fields, max_bytes) on success, or one
    of the explicit refusal markers below. Never returns the full text inline.
    Never deletes files on partial failure.

    Refusal markers:
      refused-invalid-handoff - a required public field is missing or malformed
      refused-outdir           - outdir is not a usable directory
      refused-outdir-symlink   - outdir or a parent is a symlink or junction
      refused-pre-existing     - target file already exists (foreign)
      refused-oversized        - redacted payload would exceed max bytes
      refused-write            - filesystem rejected creation or writing
      redactor-unavailable     - Fleet has no redact_sensitive_value
      redaction-failed         - redactor raised or returned wrong shape
    """
    if not isinstance(handoff_evidence, dict):
        return None
    if not (handoff_evidence.get("truncated_fields") or handoff_evidence.get("missing_fields")):
        return None
    if not isinstance(raw_handoff, dict):
        return {"status": "refused-invalid-handoff"}
    for field in PUBLIC_HANDOFF_FIELDS:
        value = raw_handoff.get(field)
        if field in PUBLIC_HANDOFF_LIST_FIELDS:
            valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
        else:
            valid = isinstance(value, str)
        if not valid:
            return {"status": "refused-invalid-handoff"}

    resolved_outdir, outdir_problem = _validate_outdir(outdir)
    if outdir_problem is not None:
        return {"status": outdir_problem}

    redacted = {}
    for field in PUBLIC_HANDOFF_FIELDS:
        clean, problem = _redact_value(raw_handoff[field], fleet)
        if problem is not None:
            return {"status": problem}
        redacted[field] = clean

    try:
        payload = json.dumps(redacted, ensure_ascii=False, indent=2).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        return {"status": "redaction-failed"}

    if len(payload) > MAX_PUBLIC_HANDOFF_BYTES:
        return {
            "status": "refused-oversized",
            "bytes": len(payload),
            "max_bytes": MAX_PUBLIC_HANDOFF_BYTES,
        }

    target = resolved_outdir / PUBLIC_HANDOFF_FILENAME
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = None
    try:
        fd = os.open(str(target), flags, 0o600)
        try:
            written = 0
            while written < len(payload):
                chunk = os.write(fd, payload[written:])
                if chunk <= 0:
                    raise OSError("short write")
                written += chunk
        finally:
            os.close(fd)
            fd = None
    except FileExistsError:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        return {"status": "refused-pre-existing"}
    except OSError:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        return {"status": "refused-write"}

    digest = hashlib.sha256(payload).hexdigest()
    return {
        "status": "persisted",
        "path": str(target.resolve(strict=True)),
        "bytes": len(payload),
        "sha256": digest,
        "fields": list(PUBLIC_HANDOFF_FIELDS),
        "max_bytes": MAX_PUBLIC_HANDOFF_BYTES,
    }
