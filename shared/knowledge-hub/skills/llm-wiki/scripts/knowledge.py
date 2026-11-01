#!/usr/bin/env python3
"""Cross-platform gateway for the shared knowledge vault."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
import contextvars
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid


TEXT_EXTENSIONS = {
    ".md", ".txt", ".json", ".jsonl", ".yaml", ".yml", ".toml",
    ".csv", ".tsv", ".html", ".htm", ".xml", ".ps1", ".py",
    ".js", ".ts", ".sh",
}
SECRET_PATTERNS = {
    "private-key": r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
    "github-token": r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
    "openai-style-key": r"\bsk-[A-Za-z0-9_-]{20,}\b",
    "aws-access-key": r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    "bearer-token": r"authorization\s*:\s*bearer\s+[A-Za-z0-9._~+/-]{16,}",
    "password-assignment": r"\b(?:password|passwd|pwd|api[_-]?key|secret|token)\s*[:=]\s*[\"'][^\"']{8,}[\"']",
}
INJECTION_PATTERNS = {
    "instruction-override": r"\b(ignore|disregard|override)\b.{0,40}\b(previous|prior|system|developer)\b.{0,30}\b(instruction|prompt|rule)",
    "system-prompt-request": r"\b(reveal|print|show|expose)\b.{0,40}\b(system prompt|hidden instruction|developer message)",
    "credential-exfiltration": r"\b(upload|send|post|exfiltrat|transmit)\b.{0,60}\b(secret|token|credential|\.env|private key)",
    "hidden-html": r"<!--.{0,200}(ignore|instruction|prompt).{0,200}-->",
    "concealment": r"\b(do not tell|hide this from|keep this secret from)\b.{0,40}\b(user|owner|operator)",
}
INVISIBLE_PATTERN = re.compile(r"[\u200B-\u200F\u202A-\u202E\u2060\uFEFF]")
SOURCE_ID_PATTERN = re.compile(r"^src-\d{8}T\d{9}Z-[a-z0-9-]+-[a-f0-9]{8}$")
PROPOSAL_ID_PATTERN = re.compile(r"^prop-\d{8}T\d{9}Z-[a-f0-9]{8}$")
AGENT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
PROJECT_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
KNOWLEDGE_KINDS = {"general", "critical-state"}
CONTEXT_RECEIPT_SCHEMA_VERSION = 2
VAULT_BINDING_VERSION = 1
CONTEXT_RECEIPT_TTL = dt.timedelta(hours=12)
CONTEXT_RECEIPT_TTL_HOURS = 12
CONTEXT_RECEIPT_CLOCK_SKEW = dt.timedelta(minutes=5)
SHA256_HEX_PATTERN = re.compile(r"^[a-f0-9]{64}$")
RESOLUTION_ID_PATTERN = re.compile(r"^res-\d{8}T\d{9}Z-[a-f0-9]{8}$")
EVENT_ID_PATTERN = re.compile(r"^evt-[A-Za-z0-9][A-Za-z0-9._-]{0,240}$")
DISPOSITION_VALUES = {"synthesized", "historical", "unresolved", "orphan"}
RESOLUTION_RECORD_KEYS = {
    "schema_version", "resolution_id", "proposal_id", "supersedes",
    "effective_disposition", "reason", "evidence",
    "review", "review_sha256", "resolution_sha256", "recorded_at", "agent", "event_id",
}
RESOLUTION_REVIEW_KEYS = {
    "schema_version", "verdict", "maker", "reviewer", "resolution_sha256", "evidence",
}
RESOLUTION_BINDING_FIELDS = (
    "schema_version", "resolution_id", "proposal_id",
    "supersedes", "effective_disposition", "reason", "evidence",
)


class GatewayError(RuntimeError):
    pass


class AmbiguousProjectReference(GatewayError):
    def __init__(self, result: dict):
        super().__init__("Ambiguous project reference; use an exact canonical slug.")
        self.result = result


class ContextReceiptError(GatewayError):
    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code
        self.result = {
            "action": "verify-context",
            "valid": False,
            "reason_code": reason_code,
            "message": message,
            "ttl_hours": CONTEXT_RECEIPT_TTL_HOURS,
            "trust_boundary": (
                "local execution evidence; not authorization, permission, "
                "or cryptographic producer authentication"
            ),
        }


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8-sig", errors="strict")


def write_text(path: Path, content: str, *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x" if exclusive else "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


_FRONTMATTER_PREFIX_LIMIT = 65536
_FRONTMATTER_CHUNK = 4096
_INVOCATION_READ_COUNTERS = (
    "proposal_prefix_reads",
    "proposal_full_reads",
    "source_metadata_reads",
    "wiki_prefix_reads",
    "wiki_full_reads",
    "reparse_walks",
    "secure_snapshot_reads",
    "known_slug_builds",
)
_INVOCATION_READ_SCOPE: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "knowledge_invocation_read_scope",
    default=None,
)
_LAST_INVOCATION_READ_REPORT: dict[str, int] = {}


def _path_key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _new_invocation_read_state() -> dict:
    return {
        "vault_keys": set(),
        "counts": {name: 0 for name in _INVOCATION_READ_COUNTERS},
        "proposal_rows": {},
        "wiki_rows": {},
        "source_index": {},
        "known_slugs": {},
        "resolved_roots": {},
        "ancestors": {},
        "closing": False,
    }


def _bind_invocation_vault(state: dict, vault: Path) -> None:
    state["vault_keys"].add(_path_key(vault))
    try:
        state["vault_keys"].add(_path_key(vault.resolve()))
    except OSError:
        return


def _active_scope(vault: Path | None = None) -> dict | None:
    state = _INVOCATION_READ_SCOPE.get()
    if state is None or state.get("closing"):
        return None
    if vault is None:
        return state
    key = _path_key(vault)
    if key in state["vault_keys"]:
        return state
    if not state["vault_keys"]:
        _bind_invocation_vault(state, vault)
        return state
    try:
        resolved_key = _path_key(vault.resolve())
    except OSError:
        return None
    if resolved_key in state["vault_keys"]:
        return state
    return None


def _scope_for_reparse(root: Path) -> dict | None:
    state = _INVOCATION_READ_SCOPE.get()
    if state is None or state.get("closing") or not state["vault_keys"]:
        return None
    if _path_key(root) in state["vault_keys"]:
        return state
    try:
        resolved_key = _path_key(root.resolve())
    except OSError:
        return None
    if resolved_key in state["vault_keys"]:
        return state
    return None


def _close_invocation_ancestors(state: dict) -> None:
    for record in state["ancestors"].values():
        descriptor = record.get("fd")
        record["fd"] = None
        if descriptor is None:
            continue
        try:
            os.close(descriptor)
        except OSError:
            continue
    state["ancestors"].clear()


@contextmanager
def invocation_read_scope(vault: Path | None = None):
    """Reuse frontmatter indexes for one public read, then drop them.

    Nested scopes share the outer indexes and directory handles. The owner
    closes those handles. Nothing is kept for the next public call.
    """
    global _LAST_INVOCATION_READ_REPORT
    current = _INVOCATION_READ_SCOPE.get()
    if current is not None:
        if vault is not None:
            _bind_invocation_vault(current, vault)
        yield current
        return
    state = _new_invocation_read_state()
    if vault is not None:
        _bind_invocation_vault(state, vault)
    token = _INVOCATION_READ_SCOPE.set(state)
    try:
        yield state
    finally:
        _close_invocation_ancestors(state)
        _INVOCATION_READ_SCOPE.reset(token)
        _LAST_INVOCATION_READ_REPORT = dict(state["counts"])


def invocation_read_report() -> dict[str, int]:
    current = _INVOCATION_READ_SCOPE.get()
    counts = current["counts"] if current is not None else _LAST_INVOCATION_READ_REPORT
    return {name: int(counts.get(name, 0)) for name in _INVOCATION_READ_COUNTERS}


def _frontmatter_text(buffer: bytes) -> tuple[str | None, bool]:
    """Return the leading text that carries a closed frontmatter block.

    The second value says whether that text covers the whole buffer. Only the
    frontmatter has to decode: a body that is not valid UTF-8 must not force a
    full read, and must not hide a complete frontmatter either.
    """
    try:
        text = buffer.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        head = buffer[: exc.start]
        try:
            head_text = head.decode("utf-8-sig")
        except UnicodeDecodeError:
            return None, False
        return (head_text, False) if frontmatter(head_text) is not None else (None, False)
    return (text, True) if frontmatter(text) is not None else (None, False)


def _read_frontmatter_prefix(path: Path, full_counter: str) -> tuple[str, bool]:
    """Return text and whether that text is the whole file.

    The read follows symlinks, matching read_text. It stops at the first chunk
    that carries a closed frontmatter block, so a large irrelevant body is
    never pulled in. A file without a closed frontmatter inside the cap is
    read in full, exactly as read_text would read it.
    """
    with path.open("rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        buffer = b""
        while len(buffer) <= _FRONTMATTER_PREFIX_LIMIT:
            chunk = handle.read(min(_FRONTMATTER_CHUNK, _FRONTMATTER_PREFIX_LIMIT + 1 - len(buffer)))
            if not chunk:
                break
            buffer += chunk
            text, whole = _frontmatter_text(buffer)
            if text is not None:
                return text, whole and len(buffer) >= size
        if len(buffer) >= size:
            return buffer.decode("utf-8-sig"), True
    text = read_text(path)
    scope = _INVOCATION_READ_SCOPE.get()
    if scope is not None and not scope.get("closing"):
        scope["counts"][full_counter] += 1
    return text, True


def _wiki_prefix_index(vault: Path) -> tuple[list[tuple[Path, str, bool]], list[tuple[Path, BaseException]]]:
    """Frontmatter rows of every wiki page, plus the pages that could not be read."""
    scope = _active_scope(vault)
    key = _path_key(vault)
    if scope is not None and key in scope["wiki_rows"]:
        return scope["wiki_rows"][key]
    wiki_root = vault / "wiki"
    rows: list[tuple[Path, str, bool]] = []
    errors: list[tuple[Path, BaseException]] = []
    for path in sorted(wiki_root.rglob("*.md")) if wiki_root.is_dir() else []:
        try:
            text, complete = _read_frontmatter_prefix(path, "wiki_full_reads")
        except (OSError, UnicodeError) as exc:
            if path.name == "index.md":
                continue
            errors.append((path, exc))
            continue
        if scope is not None:
            scope["counts"]["wiki_prefix_reads"] += 1
        rows.append((path, text, complete))
    result = (rows, errors)
    if scope is not None:
        scope["wiki_rows"][key] = result
    return result


def _wiki_prefix_rows(vault: Path, *, strict: bool) -> list[tuple[Path, str, bool]]:
    rows, errors = _wiki_prefix_index(vault)
    if strict and errors:
        raise errors[0][1]
    return rows


def _wiki_frontmatter_rows(vault: Path) -> list[tuple[Path, str, bool]]:
    """Frontmatter rows for one caller; an unreadable page fails closed.

    The index is reused inside one invocation, so a page that cannot be read
    is reported once and never silently dropped from a context decision.
    """
    rows, errors = _wiki_prefix_index(vault)
    if errors:
        raise errors[0][1]
    return rows


def _proposal_prefix_rows(vault: Path) -> list[tuple[str, Path, bool]]:
    scope = _active_scope(vault)
    key = _path_key(vault)
    if scope is not None and key in scope["proposal_rows"]:
        return scope["proposal_rows"][key]
    root = vault / "proposals"
    rows: list[tuple[str, Path, bool]] = []
    for path in sorted(root.rglob("*.md")) if root.is_dir() else []:
        try:
            text, complete = _read_frontmatter_prefix(path, "proposal_full_reads")
        except (OSError, UnicodeError):
            continue
        if scope is not None:
            scope["counts"]["proposal_prefix_reads"] += 1
        rows.append((text, path, complete))
    if scope is not None:
        scope["proposal_rows"][key] = rows
    return rows


def _source_metadata_index(vault: Path) -> list[tuple[dict | None, Path]]:
    scope = _active_scope(vault)
    key = _path_key(vault)
    if scope is not None and key in scope["source_index"]:
        return scope["source_index"][key]
    root = vault / "raw" / "sources"
    records: list[tuple[dict | None, Path]] = []
    for path in sorted(root.rglob("source.json")) if root.is_dir() else []:
        if scope is not None:
            scope["counts"]["source_metadata_reads"] += 1
        try:
            records.append((json.loads(read_text(path)), path))
        except (OSError, UnicodeError, json.JSONDecodeError):
            records.append((None, path))
    if scope is not None:
        scope["source_index"][key] = records
    return records


def _component_is_reparse(path: Path, stat_result: object) -> bool:
    if bool(getattr(stat_result, "st_file_attributes", 0) or 0) & 0x400:
        return True
    try:
        return path.is_symlink()
    except OSError:
        return True


def _component_identity(stat_result: object) -> tuple:
    inode = int(getattr(stat_result, "st_ino", 0) or 0)
    identity = (
        int(getattr(stat_result, "st_dev", 0) or 0),
        inode,
        int(getattr(stat_result, "st_mode", 0) or 0),
        int(getattr(stat_result, "st_file_attributes", 0) or 0) & 0x400,
        int(getattr(stat_result, "st_reparse_tag", 0) or 0),
    )
    if inode == 0:
        return identity + (int(getattr(stat_result, "st_ctime_ns", 0) or 0),)
    return identity


def _remember_ancestor(scope: dict | None, cursor: Path, stat_result: object) -> None:
    if scope is None:
        return
    key = _path_key(cursor)
    if key in scope["ancestors"]:
        return
    identity = _component_identity(stat_result)
    descriptor = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(os.fspath(cursor), flags)
        held = os.fstat(descriptor)
        if not stat.S_ISDIR(held.st_mode) or _component_identity(held)[:5] != identity[:5]:
            os.close(descriptor)
            descriptor = None
            raise GatewayError(f"Checkpoint candidate changed during secure read: {cursor}")
        identity = _component_identity(held)
    except GatewayError:
        raise
    except OSError:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
            descriptor = None
    scope["ancestors"][key] = {"path": cursor, "identity": identity, "fd": descriptor}


def _cached_reparse_result(scope: dict, root: Path, path: Path) -> bool | None:
    try:
        leaf = path.lstat()
    except OSError:
        return True
    if _component_is_reparse(path, leaf):
        return True
    cached = scope["resolved_roots"].get(_path_key(root))
    if cached is None:
        return None
    resolved_root = cached[1]
    resolved_key = _path_key(resolved_root)
    cursor = path if stat.S_ISDIR(getattr(leaf, "st_mode", 0)) else path.parent
    while True:
        if _path_key(cursor) not in scope["ancestors"]:
            return None
        if cursor == resolved_root or _path_key(cursor) == resolved_key:
            return False
        if cursor.parent == cursor:
            return None
        cursor = cursor.parent


def _walk_reparse_components(root: Path, path: Path, scope: dict | None) -> bool:
    if scope is not None:
        scope["counts"]["reparse_walks"] += 1
    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        return True
    if scope is not None:
        scope["resolved_roots"][_path_key(root)] = (root, resolved_root)
    cursor = path
    while True:
        try:
            current = cursor.lstat()
        except OSError:
            return True
        if _component_is_reparse(cursor, current):
            return True
        if scope is not None and (cursor != path or stat.S_ISDIR(getattr(current, "st_mode", 0))):
            _remember_ancestor(scope, cursor, current)
        if cursor == resolved_root:
            return False
        if cursor.parent == cursor:
            return True
        cursor = cursor.parent


def _path_stat_for_recheck(path: Path):
    return path.lstat()


def _recheck_invocation_ancestors() -> None:
    scope = _INVOCATION_READ_SCOPE.get()
    if scope is None or scope.get("closing"):
        return
    scope["closing"] = True
    try:
        for record in list(scope["ancestors"].values()):
            path = record["path"]
            try:
                current = _path_stat_for_recheck(path)
            except OSError as exc:
                raise GatewayError(f"Checkpoint candidate changed during secure read: {path}") from exc
            if _component_is_reparse(path, current):
                raise GatewayError(f"Checkpoint candidate contains a reparse component: {path}")
            stable_changed = _component_identity(current)[:5] != record["identity"][:5]
            ctime_changed = _component_identity(current) != record["identity"]
            descriptor = record.get("fd")
            if descriptor is not None:
                try:
                    held = os.fstat(descriptor)
                except OSError as exc:
                    raise GatewayError(f"Checkpoint candidate changed during secure read: {path}") from exc
                if not stat.S_ISDIR(held.st_mode) or _component_identity(held)[:5] != record["identity"][:5] or stable_changed:
                    raise GatewayError(f"Checkpoint candidate changed during secure read: {path}")
            elif ctime_changed or stable_changed:
                raise GatewayError(f"Checkpoint candidate changed during secure read: {path}")
        for root, resolved in list(scope["resolved_roots"].values()):
            try:
                current_root = root.resolve(strict=True)
            except OSError as exc:
                raise GatewayError(f"Checkpoint candidate changed during secure read: {root}") from exc
            if current_root != resolved:
                raise GatewayError(f"Checkpoint candidate changed during secure read: {root}")
    finally:
        scope["closing"] = False


def write_json(path: Path, value: object, *, exclusive: bool = False) -> None:
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n", exclusive=exclusive)


def canonical_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def resolution_binding_sha256(payload: dict) -> str:
    """Key-order independent hash binding resolution document fields."""
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def force_utf8_streams() -> None:
    """Make stdout/stderr UTF-8 regardless of the host console code page.

    Windows shells default to a legacy code page (cp1250 here). Vault content is
    UTF-8, so any brief containing a character outside that page made the gateway
    die with a UnicodeEncodeError and exit 2 instead of returning JSON. The
    PowerShell launcher already passes -X utf8; a direct `python knowledge.py`
    call - which SKILL.md documents - did not.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, OSError, ValueError):
            continue


READ_TELEMETRY_ACTIONS = {"project-brief", "inventory", "begin-task", "verify-context", "node-brief"}
CLI_READ_SCOPE_ACTIONS = frozenset({
    "status", "lint", "project-brief", "node-brief", "inventory",
    "begin-task", "verify-context", "navigate",
})


def read_telemetry_path(vault: Path) -> Path:
    override = os.environ.get("KNOWLEDGE_READ_TELEMETRY_ROOT")
    if override is not None and not override.strip():
        raise GatewayError("Invalid read telemetry root: path must be absolute.")
    if override is not None and override.strip():
        try:
            explicit = Path(override.strip()).expanduser()
            resolved = explicit.resolve()
        except (OSError, ValueError, RuntimeError) as exc:
            raise GatewayError("Invalid read telemetry root: unresolvable path.") from exc
        if not explicit.is_absolute():
            raise GatewayError("Invalid read telemetry root: path must be absolute.")
        try:
            inside = resolved.relative_to(vault.resolve())
        except ValueError:
            inside = None
        if inside is not None:
            raise GatewayError("Invalid read telemetry root: must be outside the vault.")
        root = explicit / vault_binding(vault)["vault_path_sha256"]
    else:
        try:
            root = Path.home() / ".agents" / "knowledge-read-telemetry" / vault_binding(vault)["vault_path_sha256"]
        except (OSError, ValueError, RuntimeError) as exc:
            raise GatewayError("Read telemetry home could not be resolved.") from exc
    return root / f"{now_utc():%Y-%m}.jsonl"


def record_read_telemetry(vault: Path, action: str, args: argparse.Namespace, result: dict) -> None:
    """Append one local line describing a READ, so retrieval becomes measurable.

    write_event only ever fired for stage-source and new-proposal, so the
    read:write ratio was zero by construction - not because nobody reads. The
    audit could therefore not answer whether the Hub is used at all.

    Deliberately NOT a vault event: vault/events is git-tracked and the gateway
    blocks writes once the working tree carries 50 uncommitted changes, so one
    event per search would lock every agent out of writing after 50 searches.

    Stays on this machine, is never sent anywhere, and never breaks a read.
    Disable with KNOWLEDGE_READ_TELEMETRY=0.
    """
    if os.environ.get("KNOWLEDGE_READ_TELEMETRY", "").strip() == "0":
        return
    try:
        query = str(getattr(args, "query", "") or "")
        record = {
            "ts": now_utc().isoformat().replace("+00:00", "Z"),
            "action": action,
            "agent": str(getattr(args, "agent", "") or "") or None,
            "project_slug": result.get("project_slug"),
            "query": query[:200] if query and not any(risk_assessment(query).values()) else None,
            "query_present": bool(query),
            "found": bool(result.get("context_found") or result.get("total")),
            "total": result.get("total"),
            "stable_total": result.get("stable_total"),
            "pending_total": result.get("pending_total"),
            "continuity_status": result.get("continuity_status"),
        }
        path = read_telemetry_path(vault)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # telemetry must never turn a read into a failure
        return


def print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def id_time(value: dt.datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%S") + f"{value.microsecond // 1000:03d}Z"


def iso_time(value: dt.datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    plain = "".join(char for char in normalized if not unicodedata.combining(char))
    result = re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")[:64].rstrip("-")
    return result or "item"


def risk_assessment(content: str) -> dict[str, list[str]]:
    secrets = sorted(label for label, pattern in SECRET_PATTERNS.items() if re.search(pattern, content, re.IGNORECASE))
    injections = sorted(label for label, pattern in INJECTION_PATTERNS.items() if re.search(pattern, content, re.IGNORECASE))
    if INVISIBLE_PATTERN.search(content):
        injections = sorted(set(injections + ["invisible-unicode"]))
    return {"secret_hits": secrets, "injection_hits": injections}


def assert_safe(label: str, value: str) -> None:
    if not value or not value.strip():
        return
    risk = risk_assessment(value)
    if risk["secret_hits"]:
        raise GatewayError(f"{label} contains secret indicators: {', '.join(risk['secret_hits'])}.")
    if risk["injection_hits"]:
        raise GatewayError(f"{label} contains prompt-injection indicators: {', '.join(risk['injection_hits'])}.")


def canonical_project_slug(value: str) -> str:
    candidate = (value or "").strip()
    if not PROJECT_SLUG_PATTERN.fullmatch(candidate) or slug(candidate) != candidate:
        raise GatewayError(
            "A canonical project slug is required: lowercase ASCII letters, digits, and hyphens only."
        )
    return candidate


def normalized_project_reference(value: str) -> tuple[str, str]:
    requested = (value or "").strip()
    if (
        not requested
        or len(requested) > 128
        or ".." in requested
        or "/" in requested
        or "\\" in requested
        or any(ord(char) < 32 for char in requested)
        or INVISIBLE_PATTERN.search(requested)
    ):
        raise GatewayError(
            "A canonical project slug or safe project reference is required; path segments and control characters are not allowed."
        )
    normalized = slug(requested)
    if not PROJECT_SLUG_PATTERN.fullmatch(normalized):
        raise GatewayError(
            "A canonical project slug or safe project reference is required: at least three letters or digits."
        )
    return requested, normalized


def compact_project_key(value: str) -> str:
    return slug(value).replace("-", "")


def one_line_state_summary(value: str) -> str:
    candidate = (value or "").strip()
    if not candidate or len(candidate) > 500 or "\n" in candidate or "\r" in candidate:
        raise GatewayError("State summary must be one non-empty line of at most 500 characters.")
    assert_safe("State summary", candidate)
    return candidate


def meaningful_label(field: str, value: str) -> str:
    """Reject placeholder metadata such as a bare 'x'.

    Observed 2026-09-20 22:45: a handoff was recorded twice thirty seconds apart,
    the second copy carrying title/agent/state_summary of 'x' over a real 3.5 kB
    report. Proposal TEXT is deliberately not searchable, so a record whose
    metadata says nothing cannot be found by inventory at all - it only grows the
    curation backlog. This catches placeholders, not merely terse writing.
    """
    candidate = (value or "").strip()
    alphanumeric = [char for char in candidate if char.isalnum()]
    if len(alphanumeric) < 3 or len(set(char.casefold() for char in alphanumeric)) < 2:
        raise GatewayError(
            f"{field} looks like placeholder metadata: {candidate!r}. It needs at least "
            "three alphanumeric characters and more than one distinct character, so the "
            "record stays findable through inventory."
        )
    return candidate


def yaml_single_quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def canonical_target_page(value: str) -> str:
    candidate = (value or "").strip().replace("\\", "/")
    if (
        not candidate.startswith("wiki/")
        or not candidate.endswith(".md")
        or candidate.startswith("/")
        or re.match(r"^[A-Za-z]:", candidate)
        or any(part in {"", ".", ".."} for part in candidate.split("/"))
    ):
        raise GatewayError(f"Target page must be a safe path below wiki/: {value}")
    return candidate


def selected_knowledge_config() -> tuple:
    raw = os.environ.get("AGENT_KNOWLEDGE_CONFIG")
    if raw is None:
        return None, None
    if not raw.strip():
        raise GatewayError("Invalid knowledge config override: path must be absolute.")
    try:
        candidate = Path(raw.strip()).expanduser()
    except (OSError, ValueError, RuntimeError) as exc:
        raise GatewayError("Invalid knowledge config override: unresolvable path.") from exc
    if not candidate.is_absolute():
        raise GatewayError("Invalid knowledge config override: path must be absolute.")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, ValueError, RuntimeError):
        # Unresolvable bindings (missing file, embedded null bytes, invalid
        # syntax) fail closed with a controlled error, never a raw exception.
        raise GatewayError("Invalid knowledge config override: file not found.")
    if not resolved.is_file() or resolved.is_symlink():
        raise GatewayError("Invalid knowledge config override: must be a regular file.")
    try:
        config = json.loads(read_text(resolved))
    except (OSError, ValueError, RecursionError):
        # Deeply nested JSON can exhaust the parser stack (RecursionError);
        # a provided binding that cannot be read must fail closed, never leak
        # a raw exception or fall back to another profile's config.
        raise GatewayError("Invalid knowledge config override: unreadable config.")
    if not isinstance(config, dict):
        raise GatewayError("Invalid knowledge config override: unreadable config.")
    return resolved, config


def _vault_from_selected_config(override_path, override_config) -> Path:
    """Validate an explicitly provided knowledge config and return its vault."""
    if not isinstance(override_config, dict):
        raise GatewayError(f"Invalid knowledge config without vault_path: {override_path}")
    if override_config.get("managed_by") != "shared-agent-knowledge-hub":
        raise GatewayError(f"Unmanaged knowledge config: {override_path}")
    candidate = override_config.get("vault_path")
    if not isinstance(candidate, str) or not candidate.strip():
        raise GatewayError(f"Invalid knowledge config without vault_path: {override_path}")
    try:
        vault = Path(candidate.strip()).expanduser().resolve(strict=True)
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
        raise GatewayError("Invalid vault_path in knowledge config override.")
    if not (vault / "knowledge-vault.json").is_file():
        raise GatewayError("Invalid vault_path in knowledge config override.")
    return vault


def resolve_vault(requested: str | None) -> Path:
    candidate = requested or os.environ.get("AGENT_KNOWLEDGE_VAULT")
    if not candidate:
        override_path, override_config = selected_knowledge_config()
        if override_path is not None:
            return _vault_from_selected_config(override_path, override_config)
        try:
            config_path = Path.home() / ".agents" / "knowledge-config.json"
        except (OSError, ValueError, RuntimeError) as exc:
            raise GatewayError("Knowledge config home could not be resolved.") from exc
        if config_path.is_file():
            try:
                config = json.loads(read_text(config_path))
            except (OSError, ValueError, RecursionError) as exc:
                raise GatewayError(f"Unmanaged knowledge config: {config_path}") from exc
            if not isinstance(config, dict) or config.get("managed_by") != "shared-agent-knowledge-hub":
                raise GatewayError(f"Unmanaged knowledge config: {config_path}")
            candidate = config.get("vault_path")
            if isinstance(candidate, bool) or (candidate is not None and not isinstance(candidate, str)):
                raise GatewayError(f"Unmanaged knowledge config: {config_path}")
    if not candidate:
        for cursor in (Path.cwd(), *Path.cwd().parents):
            if (cursor / "knowledge-vault.json").is_file():
                candidate = str(cursor)
                break
    if not candidate:
        raise GatewayError("Knowledge vault not found. Pass --vault or install ~/.agents/knowledge-config.json.")
    try:
        vault = Path(candidate).expanduser().resolve(strict=True)
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
        raise GatewayError("Knowledge vault not found. Pass --vault or install ~/.agents/knowledge-config.json.")
    if not (vault / "knowledge-vault.json").is_file():
        raise GatewayError(f"Not a knowledge vault: {vault}")
    return vault


def frontmatter(content: str) -> str | None:
    match = re.match(r"\A(?:\ufeff)?---[ \t]*\r?\n(?P<body>.*?)(?:\r?\n)---[ \t]*(?:\r?\n|$)", content, re.DOTALL)
    return match.group("body") if match else None


def decode_frontmatter_scalar(value: str) -> str:
    candidate = value.strip()
    if len(candidate) >= 2 and candidate[0] == candidate[-1] == "'":
        return candidate[1:-1].replace("''", "'")
    if len(candidate) >= 2 and candidate[0] == candidate[-1] == '"':
        try:
            decoded = json.loads(candidate)
            return decoded if isinstance(decoded, str) else candidate[1:-1]
        except json.JSONDecodeError:
            return candidate[1:-1]
    return candidate


def fm_value(content: str, name: str) -> str | None:
    block = frontmatter(content)
    if block is None:
        return None
    match = re.search(rf"^{re.escape(name)}[ \t]*:[ \t]*(?P<value>[^\r\n]*)[ \t]*\r?$", block, re.MULTILINE)
    return decode_frontmatter_scalar(match.group("value")) if match else None


def fm_list(content: str, name: str) -> list[str]:
    inline = fm_value(content, name)
    if inline is None or inline == "[]":
        return []
    if inline.startswith("[") and inline.endswith("]"):
        return [decode_frontmatter_scalar(item) for item in inline[1:-1].split(",") if item.strip()]
    block = frontmatter(content) or ""
    match = re.search(
        rf"^{re.escape(name)}[ \t]*:[ \t]*\r?\n(?P<items>(?:[ \t]{{2,}}-[ \t]+[^\r\n]*(?:\r?\n|$))*)",
        block,
        re.MULTILINE,
    )
    if not match:
        return []
    return [decode_frontmatter_scalar(item.group("value")) for item in re.finditer(r"^[ \t]{2,}-[ \t]+(?P<value>[^\r\n]*)\r?$", match.group("items"), re.MULTILINE)]


def source_records(vault: Path) -> list[tuple[dict | None, Path]]:
    root = vault / "raw" / "sources"
    records: list[tuple[dict | None, Path]] = []
    for path in sorted(root.rglob("source.json")) if root.is_dir() else []:
        try:
            records.append((json.loads(read_text(path)), path))
        except (OSError, UnicodeError, json.JSONDecodeError):
            records.append((None, path))
    return records


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_git_root(start: Path) -> Path | None:
    for cursor in (start, *start.parents):
        if (cursor / ".git").exists():
            return cursor
    return None


DIRTY_TREE_POLICY = {
    "warn_count": 25,
    "block_count": 50,
    "warn_age_hours": 24,
    "block_age_hours": 168,
}


def git_dirty_assessment(root: Path | None) -> dict:
    if root is None:
        return {
            "git_root": None,
            "change_count": 0,
            "changed_file_count": 0,
            "oldest_age_hours": None,
            "age_unknown_count": 0,
            "state": "unmanaged",
            "write_blocked": False,
            "reason": None,
            "policy": dict(DIRTY_TREE_POLICY),
        }
    try:
        if external_filter_attribute_files(root):
            raise GatewayError("Knowledge Hub Git hygiene refuses external filter attributes.")
        changed_paths = sorted(git_changed_paths(root))
        oldest_mtime: float | None = None
        age_unknown_count = 0
        for relative in changed_paths:
            candidate = root / relative
            try:
                mtime = candidate.stat().st_mtime
            except OSError:
                age_unknown_count += 1
                continue
            oldest_mtime = mtime if oldest_mtime is None else min(oldest_mtime, mtime)
        oldest_age_hours = None
        if oldest_mtime is not None:
            oldest_age_hours = round(max(0.0, (dt.datetime.now(dt.timezone.utc).timestamp() - oldest_mtime) / 3600), 3)
        state = "clean"
        if len(changed_paths) >= DIRTY_TREE_POLICY["block_count"] or (
            oldest_age_hours is not None and oldest_age_hours >= DIRTY_TREE_POLICY["block_age_hours"]
        ):
            state = "blocked"
        elif len(changed_paths) >= DIRTY_TREE_POLICY["warn_count"] or (
            oldest_age_hours is not None and oldest_age_hours >= DIRTY_TREE_POLICY["warn_age_hours"]
        ):
            state = "warning"
        elif changed_paths:
            state = "pending"
        return {
            "git_root": str(root),
            "change_count": len(changed_paths),
            "changed_file_count": len(changed_paths),
            "oldest_age_hours": oldest_age_hours,
            "age_unknown_count": age_unknown_count,
            "state": state,
            "write_blocked": state == "blocked",
            "reason": None,
            "policy": dict(DIRTY_TREE_POLICY),
        }
    except (GatewayError, OSError, subprocess.SubprocessError) as exc:
        return {
            "git_root": str(root),
            "change_count": None,
            "changed_file_count": None,
            "oldest_age_hours": None,
            "age_unknown_count": None,
            "state": "unavailable",
            "write_blocked": True,
            "reason": str(exc),
            "policy": dict(DIRTY_TREE_POLICY),
        }


def git_uncommitted_paths(root: Path) -> set[str]:
    try:
        return git_changed_paths(root)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GatewayError("Knowledge Hub completion check cannot read Git status.") from exc


CHECKPOINT_MODE = "local-commit-only"
CHECKPOINT_LOCK_NAME = "llm-wiki-intake-checkpoint.lock"
CHECKPOINT_MANUAL_BLOCK_NAME = "llm-wiki-intake-checkpoint.manual.json"
STAGE_SOURCE_EXPECTED_ADDITIONAL_FILES = 4
CHECKPOINT_LOCK_MIN_AGE_SECONDS = 900.0
PROCESS_TIME_TOLERANCE_SECONDS = 2.0
RECEIPT_DIGEST_TRAILER = "knowledge-receipt-sha256"
RECEIPT_EVENTS_TRAILER = "knowledge-receipt-events"
RECEIPT_PATHS_TRAILER = "knowledge-receipt-paths"


def git_subprocess_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_EDITOR": "true",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
        }
    )
    return environment


def run_git(
    root: Path,
    arguments: list[str],
    *,
    check: bool = True,
    timeout: int = 20,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={root}",
            "-c",
            "core.quotePath=false",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(root),
            *arguments,
        ],
        check=check,
        capture_output=True,
        text=True,
        input=input_text,
        timeout=timeout,
        env=git_subprocess_environment(),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def git_scratch_dir(root: Path, name: str) -> Path:
    # A private directory inside the repository's Git directory. On 2026-09-23 a
    # sandboxed agent had NO usable system temp directory, yet could write the
    # repository, which every checkpoint needs anyway (index.lock, objects).
    path = git_internal_path(root, name)
    path.mkdir(exist_ok=True)
    return path


def validate_retain_temp_root(value: object) -> Path:
    """Validate a caller-supplied scratch root for record-session retain mode.

    Refuses empty, non-absolute, non-existent, file, and reparse roots, and never
    silently falls back to TEMP. Called BEFORE any vault mutation so invalid
    roots cannot stage a partial write.
    """
    candidate = os.fspath(value).strip() if isinstance(value, (str, os.PathLike)) else ""
    if not candidate:
        raise GatewayError("--retain-temp-root requires an absolute directory path.")
    raw = Path(candidate)
    if not raw.is_absolute():
        raise GatewayError(f"--retain-temp-root must be an absolute path: {candidate}")
    try:
        raw.lstat()
    except OSError as exc:
        raise GatewayError(f"--retain-temp-root does not exist or cannot be inspected: {candidate}") from exc
    try:
        # Inspect the lexical path before resolve erases symlink/junction evidence.
        if path_has_reparse_component(Path(raw.anchor), raw):
            raise GatewayError("--retain-temp-root must exist without symlink, junction, or reparse components.")
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise GatewayError(f"--retain-temp-root does not exist: {candidate}") from exc
    if not resolved.is_dir():
        raise GatewayError(f"--retain-temp-root is not a directory: {resolved}")
    return resolved


def validate_session_lock_root(vault: Path, value: object, flag_name: str = "--receipt-root") -> Path:
    """Validate an explicit lock or receipt root for record-session.

    Refuses empty, non-absolute, non-existent, file, reparse, and inside-vault roots.
    Called BEFORE any vault mutation so invalid roots cannot stage a partial write.
    """
    candidate = os.fspath(value).strip() if isinstance(value, (str, os.PathLike)) else ""
    if not candidate:
        raise GatewayError(f"{flag_name} requires an absolute directory path.")
    raw = Path(candidate)
    if not raw.is_absolute():
        raise GatewayError(f"{flag_name} must be an absolute path: {candidate}")
    try:
        raw.lstat()
    except OSError as exc:
        raise GatewayError(f"{flag_name} does not exist or cannot be inspected: {candidate}") from exc
    try:
        if path_has_reparse_component(Path(raw.anchor), raw):
            raise GatewayError(f"{flag_name} must exist without symlink, junction, or reparse components.")
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise GatewayError(f"{flag_name} does not exist: {candidate}") from exc
    if not resolved.is_dir():
        raise GatewayError(f"{flag_name} is not a directory: {resolved}")
    try:
        resolved.relative_to(vault.resolve())
    except ValueError:
        pass
    else:
        raise GatewayError(f"{flag_name} must be outside the Knowledge Hub vault.")
    return resolved


def create_retained_scratch(root: Path, key: str) -> Path:
    """Make one unique empty directory under the explicit retain root.

    No system-temp probe, no TemporaryDirectory finalizer: the parent directory
    and the scratch dir itself stay on disk for the caller to inspect, move, or
    retain. A short uuid-derived name keeps collisions avoidable without TEMP.
    """
    unique = uuid.uuid4().hex
    scratch = root / f"hub-session-note-{key[:8]}-{unique}"
    scratch.mkdir(parents=False, exist_ok=False)
    return scratch


def run_git_without_hooks(
    root: Path,
    arguments: list[str],
    *,
    check: bool = True,
    timeout: int = 20,
) -> subprocess.CompletedProcess[str]:
    # The empty hooks directory lives in the Git directory, not in the system temp:
    # the temp-based one failed AFTER staging, so the checkpoint could neither commit
    # nor unstage, and its sticky manual block stopped every later intake checkpoint.
    hooks_dir = git_scratch_dir(root, "llm-wiki-empty-hooks")
    if any(hooks_dir.iterdir()):
        raise GatewayError("The Git-internal empty hooks directory is not empty.")
    return run_git(
        root,
        ["-c", f"core.hooksPath={hooks_dir}", *arguments],
        check=check,
        timeout=timeout,
    )


def git_changed_paths(root: Path) -> set[str]:
    worktree = run_git(
        root,
        ["ls-files", "--modified", "--deleted", "--others", "--exclude-standard", "-z"],
    )
    index = run_git(root, ["diff", "--cached", "--name-only", "--no-ext-diff", "-z"])
    return {
        path.replace("\\", "/")
        for path in (worktree.stdout + index.stdout).split("\0")
        if path
    }


def external_filter_attribute_files(root: Path) -> list[str]:
    candidates: set[Path] = set()
    for path in root.rglob(".gitattributes"):
        if ".git" not in path.relative_to(root).parts:
            candidates.add(path)
    info_attributes = git_internal_path(root, "info/attributes")
    if info_attributes.is_file():
        candidates.add(info_attributes)
    configured = run_git(root, ["config", "--path", "--get-all", "core.attributesFile"], check=False)
    configured_paths = [
        Path(value.strip()).expanduser()
        for value in configured.stdout.splitlines()
        if value.strip()
    ] if configured.returncode == 0 else []
    if configured_paths:
        candidates.update(path for path in configured_paths if path.is_file())
    else:
        xdg_config = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
        default_global = xdg_config / "git" / "attributes"
        if default_global.is_file():
            candidates.add(default_global)

    active: list[str] = []
    for path in sorted(candidates):
        try:
            if path.stat().st_size > 1024 * 1024:
                active.append(str(path))
                continue
            for line in read_text(path).splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                tokens = stripped.split()
                if any(token == "filter" or token.startswith("filter=") for token in tokens):
                    active.append(str(path))
                    break
        except (OSError, UnicodeError):
            active.append(str(path))
    return active


def git_internal_path(root: Path, name: str, *, timeout: int = 20) -> Path:
    result = run_git(root, ["rev-parse", "--git-path", name], timeout=timeout)
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else (root / path).resolve()


def git_status_map(root: Path, relative_paths: list[str]) -> dict[str, str]:
    def z_paths(arguments: list[str]) -> set[str]:
        return {
            value.replace("\\", "/")
            for value in run_git(root, arguments).stdout.split("\0")
            if value
        }

    pathspec = ["--", *relative_paths]
    tracked = z_paths(["ls-files", "-z", *pathspec])
    untracked = z_paths(["ls-files", "--others", "--exclude-standard", "-z", *pathspec])
    staged = z_paths(["diff", "--cached", "--name-only", "--no-ext-diff", "-z", *pathspec])
    modified = z_paths(["diff", "--name-only", "--no-ext-diff", "-z", *pathspec])
    statuses: dict[str, str] = {}
    for relative in relative_paths:
        if relative in staged:
            statuses[relative] = "STAGED"
        elif relative in modified:
            statuses[relative] = "MODIFIED"
        elif relative in untracked:
            statuses[relative] = "??"
        elif relative in tracked:
            statuses[relative] = "IN"
    return statuses


def path_has_reparse_component(root: Path, path: Path) -> bool:
    scope = _scope_for_reparse(root)
    if scope is not None:
        cached = _cached_reparse_result(scope, root, path)
        if cached is not None:
            return cached
    return _walk_reparse_components(root, path, scope)


def repository_relative_path(root: Path, path: Path) -> str:
    resolved_root = root.resolve(strict=True)
    resolved_path = path.resolve(strict=True)
    try:
        return resolved_path.relative_to(resolved_root).as_posix()
    except ValueError as exc:
        raise GatewayError(f"Checkpoint path escapes the Knowledge Hub worktree: {path}") from exc


def checkpoint_policy(vault: Path, *, timeout: int = 20) -> dict:
    root = find_git_root(vault)
    config: dict = {}
    policy_source = "untrusted-worktree"
    if root is not None:
        try:
            config_relative = repository_relative_path(root, vault / "knowledge-vault.json")
            committed = run_git(root, ["show", f"HEAD:{config_relative}"], timeout=timeout)
            config = json.loads(committed.stdout)
            policy_source = "git-head"
        except (GatewayError, OSError, subprocess.SubprocessError, json.JSONDecodeError):
            config = {}
    enabled = config.get("auto_checkpoint_intake") is True
    mode = str(config.get("checkpoint_mode") or "")
    curator_machine = str(config.get("curator_machine") or "").strip()
    machine = local_machine_name()
    machine_identity_error = os.name == "nt" and not machine
    owner_machine = bool(curator_machine) and curator_machine.casefold() == machine.casefold()
    return {
        "enabled": enabled,
        "mode": mode,
        "curator_machine": curator_machine,
        "machine": machine,
        "machine_identity_error": machine_identity_error,
        "owner_machine": owner_machine,
        "required": enabled and (owner_machine or machine_identity_error),
        "policy_source": policy_source,
    }


def checkpoint_result(status: str, policy: dict, **extra: object) -> dict:
    return {
        "action": "checkpoint-intake",
        "status": status,
        "required": bool(policy.get("required")),
        "mode": policy.get("mode"),
        "machine": policy.get("machine"),
        "curator_machine": policy.get("curator_machine"),
        "policy_source": policy.get("policy_source"),
        **extra,
    }


def git_operation_in_progress(root: Path) -> bool:
    for name in (
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "rebase-merge",
        "rebase-apply",
        "BISECT_LOG",
    ):
        if git_internal_path(root, name).exists():
            return True
    return False


def index_is_clean(root: Path) -> bool:
    result = run_git(root, ["diff", "--cached", "--quiet"], check=False)
    if result.returncode not in {0, 1}:
        raise GatewayError("Knowledge checkpoint cannot inspect the Git index.")
    return result.returncode == 0


def stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def read_regular_file_snapshot(root: Path, path: Path, max_bytes: int | None = None) -> tuple[bytes, tuple[int, int, int, int, int]]:
    scope = _scope_for_reparse(root)
    if scope is not None:
        scope["counts"]["secure_snapshot_reads"] += 1
    if path_has_reparse_component(root, path):
        raise GatewayError(f"Checkpoint candidate contains a reparse component: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        descriptor_before = os.fstat(descriptor)
        path_before = path.lstat()
        if not stat.S_ISREG(descriptor_before.st_mode) or not stat.S_ISREG(path_before.st_mode):
            raise GatewayError(f"Checkpoint candidate is not a regular file: {path}")
        if stat_identity(descriptor_before)[:2] != stat_identity(path_before)[:2]:
            raise GatewayError(f"Checkpoint candidate changed while it was opened: {path}")
        if max_bytes is not None and descriptor_before.st_size > max_bytes:
            raise GatewayError("Snapshot exceeds its byte limit.")
        chunks: list[bytes] = []
        total_bytes = 0
        while True:
            read_size = 1024 * 1024 if max_bytes is None else min(1024 * 1024, max_bytes - total_bytes + 1)
            chunk = os.read(descriptor, read_size)
            if not chunk:
                break
            total_bytes += len(chunk)
            if max_bytes is not None and total_bytes > max_bytes:
                raise GatewayError("Snapshot exceeds its byte limit.")
            chunks.append(chunk)
        descriptor_after = os.fstat(descriptor)
        path_after = path.lstat()
        if (
            stat_identity(descriptor_before) != stat_identity(descriptor_after)
            or stat_identity(descriptor_after) != stat_identity(path_after)
            or path_has_reparse_component(root, path)
        ):
            raise GatewayError(f"Checkpoint candidate changed during secure read: {path}")
        return b"".join(chunks), stat_identity(descriptor_after)
    finally:
        os.close(descriptor)


def capture_candidate_bytes(root: Path, relative_paths: list[str]) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    for relative in relative_paths:
        path = root / relative
        try:
            snapshot[relative] = read_regular_file_snapshot(root, path)[0]
        except (OSError, GatewayError) as exc:
            raise GatewayError(f"Checkpoint candidate is not a safe regular file: {relative}") from exc
    return snapshot


def snapshot_fingerprint(snapshot: dict[str, bytes]) -> dict[str, tuple[int, str]]:
    return {
        relative: (len(content), hashlib.sha256(content).hexdigest())
        for relative, content in snapshot.items()
    }


def receipt_content_digest(snapshot: dict[str, bytes]) -> str:
    """Deterministic digest over a complete receipt snapshot (path, hash, length)."""
    hasher = hashlib.sha256()
    for relative in sorted(snapshot):
        content = snapshot[relative]
        hasher.update(relative.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(hashlib.sha256(content).digest())
        hasher.update(b"\0")
        hasher.update(str(len(content)).encode("ascii"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def parse_receipt_trailers(message: str) -> dict[str, str]:
    trailers: dict[str, str] = {}
    for line in (message or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        if key in {RECEIPT_DIGEST_TRAILER, RECEIPT_EVENTS_TRAILER, RECEIPT_PATHS_TRAILER}:
            trailers[key] = value.strip()
    return trailers


def backlog_admission(assessment: dict, expected_additional_files: int) -> dict:
    """Same admission calculation as assert_git_backlog_writable (no completion bypass)."""
    expected = int(expected_additional_files)
    projected_count = int(assessment.get("changed_file_count") or 0) + expected
    block_count = int(assessment["policy"]["block_count"])
    count_would_block = projected_count >= block_count
    oldest_age = assessment.get("oldest_age_hours")
    age_would_block = (
        oldest_age is not None and oldest_age >= assessment["policy"]["block_age_hours"]
    )
    unavailable = assessment.get("state") == "unavailable"
    write_blocked = bool(assessment.get("write_blocked"))
    blocked = unavailable or write_blocked or count_would_block or age_would_block
    reasons: list[str] = []
    if unavailable:
        reasons.append(
            f"Git hygiene is unavailable: {assessment.get('reason') or 'unknown reason'}."
        )
    if write_blocked and not unavailable:
        reasons.append(
            f"dirty tree already blocked at {assessment.get('changed_file_count')} changed files"
        )
    if count_would_block:
        reasons.append(
            f"prospective changed files {projected_count} >= {block_count} "
            f"({assessment.get('changed_file_count')} current + {expected} expected)"
        )
    if age_would_block:
        reasons.append(
            f"oldest known age {oldest_age} h >= {assessment['policy']['block_age_hours']} h"
        )
    return {
        "blocked": bool(blocked),
        "prospective_changed_file_count": expected,
        "projected_changed_file_count": projected_count,
        "count_would_block": count_would_block,
        "age_would_block": age_would_block,
        "unavailable": unavailable,
        "reason": "; ".join(reasons) if reasons else None,
    }


def local_machine_name() -> str:
    if os.name == "nt":
        try:
            import ctypes

            size = ctypes.c_uint(256)
            buffer = ctypes.create_unicode_buffer(size.value)
            if ctypes.windll.kernel32.GetComputerNameW(buffer, ctypes.byref(size)):
                return buffer.value
        except (AttributeError, OSError, ValueError):
            pass
        return ""
    return socket.gethostname()


def git_blob_oid(root: Path, relative_path: str, content: bytes, *, write: bool) -> str:
    arguments = ["hash-object"]
    if write:
        arguments.append("-w")
    arguments.append(f"--path={relative_path}")
    arguments.append("--stdin")
    result = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={root}",
            "-C",
            str(root),
            "-c",
            "core.fsmonitor=false",
            *arguments,
        ],
        input=content,
        check=True,
        capture_output=True,
        timeout=20,
        env=git_subprocess_environment(),
    )
    return result.stdout.decode("ascii", errors="strict").strip()


def write_git_blob(root: Path, relative_path: str, content: bytes) -> str:
    return git_blob_oid(root, relative_path, content, write=True)


def unstage_checkpoint_paths(root: Path, changed_paths: list[str]) -> bool:
    if not changed_paths:
        return True
    try:
        cached = run_git(
            root,
            ["diff", "--cached", "--name-only", "--no-ext-diff", "-z", "--", *changed_paths],
            check=False,
        )
        if cached.returncode != 0:
            return False
        # Nothing changed before staging, or the original staged prefix owns
        # every path. Do not restore absent paths or an empty pathspec.
        present = [path for path in cached.stdout.split("\0") if path]
        if not present:
            return True
        restored = run_git_without_hooks(
            root,
            ["restore", "--staged", "--", *present],
            check=False,
        )
        if restored.returncode != 0:
            return False
        staged = run_git(
            root,
            ["diff", "--cached", "--name-only", "--no-ext-diff", "--", *changed_paths],
            check=False,
        )
        return staged.returncode == 0 and not staged.stdout.strip()
    except (OSError, subprocess.SubprocessError, GatewayError):
        return False


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        error_invalid_parameter = 87
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        if pid > 0xFFFFFFFF:
            return False
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            # Only ERROR_INVALID_PARAMETER proves that the process object is absent.
            # Access and transient failures must preserve the lock fail-closed.
            return ctypes.get_last_error() != error_invalid_parameter
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ProcessLookupError, OverflowError):
        return False


def windows_process_started_at(pid: int) -> dt.datetime | None:
    """Read a process creation time without treating access failures as absence."""
    if os.name != "nt" or pid <= 0 or pid > 0xFFFFFFFF:
        return None
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
    )
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        created, exited, kernel, user = (FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel), ctypes.byref(user),
        ):
            return None
        ticks = (created.high << 32) | created.low
        return dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(microseconds=ticks // 10)
    except (OSError, OverflowError, ValueError):
        return None
    finally:
        kernel32.CloseHandle(handle)


def curator_lock_owner_state(lock_path: Path) -> dict:
    """Report only proven local owner state; malformed and legacy locks stay unknown."""
    result = {
        "state": "unknown", "lock_id": None, "agent": None,
        "acquired_at": None, "age_minutes": None, "process_id": None,
    }
    try:
        content, _ = read_regular_file_snapshot(lock_path.parent, lock_path)
        payload = json.loads(content.decode("utf-8", errors="strict"))
        if not isinstance(payload, dict):
            return result
        for key in ("lock_id", "agent", "acquired_at"):
            if isinstance(payload.get(key), str):
                result[key] = payload[key]
        pid = payload.get("process_id")
        if isinstance(pid, int) and not isinstance(pid, bool) and 0 < pid <= 0xFFFFFFFF:
            result["process_id"] = pid
        acquired = parse_recorded_time(result["acquired_at"])
        if acquired:
            result["age_minutes"] = round(max(0, (now_utc() - acquired).total_seconds()) / 60, 3)
        started_raw = payload.get("owner_process_started_at")
        started = parse_recorded_time(started_raw) if isinstance(started_raw, str) else None
        if (
            not result["lock_id"] or not result["agent"] or not acquired
            or not started or not result["process_id"]
            or payload.get("owner_source") not in {"explicit", "parent"}
            or not isinstance(payload.get("machine"), str)
            or not local_machine_name()
            or payload["machine"].casefold() != local_machine_name().casefold()
        ):
            return result
        if not process_is_running(pid):
            result["state"] = "dead"
            return result
        actual_start = windows_process_started_at(pid)
        if actual_start is not None:
            result["state"] = "alive" if abs((actual_start - started).total_seconds()) <= 2 else "dead"
    except (OSError, UnicodeError, json.JSONDecodeError, GatewayError, TypeError, ValueError):
        pass
    return result


def lock_snapshot(lock_path: Path) -> tuple[dict, tuple[int, int, int, int, int]] | None:
    try:
        content, identity = read_regular_file_snapshot(lock_path.parent, lock_path)
        payload = json.loads(content.decode("utf-8", errors="strict"))
        if not isinstance(payload, dict) or not str(payload.get("lock_id") or ""):
            return None
        return payload, identity
    except (OSError, UnicodeError, json.JSONDecodeError, GatewayError):
        return None


@contextmanager
def checkpoint_lock_opsync(lock_path: Path, *, timeout: float = 10):
    """Serialize checkpoint lock create and release on this host.

    The 2026-09-23 release race split ownership check from the name move: a
    foreign lock could replace the active name in that window, release then
    quarantined the foreign bytes, and a third O_EXCL create could occupy the
    free name before restore. Acquire and release both hold this opsync so a
    cooperating third cannot land between check and move.
    """
    sync_path = lock_path.with_name(lock_path.name + ".opsync")
    sync_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + max(0.0, timeout)
        locked = False
        while not locked:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise GatewayError("CHECKPOINT_LOCK_OPS_BUSY: retry after the active lock operator.") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_lock_descriptor(descriptor: int) -> tuple[dict, tuple[int, int, int, int, int]] | None:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        total = 0
        while total <= 1024 * 1024:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > 1024 * 1024:
            return None
        payload = json.loads(b"".join(chunks).decode("utf-8", errors="strict"))
        if not isinstance(payload, dict) or not str(payload.get("lock_id") or ""):
            return None
        return payload, stat_identity(os.fstat(descriptor))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _open_owned_checkpoint_lock(
    lock_path: Path,
    lock_id: str,
    expected_identity: tuple[int, int, int, int, int],
) -> int | None:
    """Open the exact owned lock object, or fail closed.

    On Windows the open uses DELETE access so the verified file object can be
    renamed by handle. A path-based check-then-replace can move a foreign lock
    that swapped in after the check; renaming the open object cannot.
    """
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        kernel32.CreateFileW.restype = ctypes.c_void_p
        generic_read = 0x80000000
        delete_access = 0x00010000
        share_all = 0x00000007
        open_existing = 3
        file_attribute_normal = 0x80
        handle = kernel32.CreateFileW(
            str(lock_path),
            generic_read | delete_access,
            share_all,
            None,
            open_existing,
            file_attribute_normal,
            None,
        )
        if not handle or handle == ctypes.c_void_p(-1).value:
            return None
        import msvcrt

        descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY)
    else:
        try:
            descriptor = os.open(lock_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return None
    snapshot = _read_lock_descriptor(descriptor)
    if (
        snapshot is None
        or snapshot[0].get("lock_id") != lock_id
        or snapshot[1] != expected_identity
    ):
        os.close(descriptor)
        return None
    return descriptor


def _checkpoint_lock_name_matches(
    lock_path: Path, expected_identity: tuple[int, int, int, int, int],
) -> bool:
    """Check the active name without reopening a DELETE-access file handle."""
    try:
        info = lock_path.lstat()
        return stat.S_ISREG(info.st_mode) and stat_identity(info) == expected_identity
    except OSError:
        return False


def _rename_owned_checkpoint_lock(descriptor: int, lock_path: Path, quarantine: Path) -> None:
    """Move the verified file object to quarantine without path TOCTOU."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetFileInformationByHandle.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
        import msvcrt

        class _FILE_RENAME_INFO(ctypes.Structure):
            class _U(ctypes.Union):
                _fields_ = [("ReplaceIfExists", wintypes.BOOLEAN), ("Flags", wintypes.DWORD)]

            _fields_ = [
                ("DUMMYUNIONNAME", _U),
                ("RootDirectory", wintypes.HANDLE),
                ("FileNameLength", wintypes.DWORD),
                ("FileName", wintypes.WCHAR * 1),
            ]

        encoded = os.path.abspath(str(quarantine)).encode("utf-16-le")
        name_offset = _FILE_RENAME_INFO.FileName.offset
        # FileNameLength excludes the terminator; keep one UTF-16 NUL in the
        # actual buffer for file systems that also inspect a wide string.
        buffer_size = max(name_offset + len(encoded) + 2, ctypes.sizeof(_FILE_RENAME_INFO))
        buffer = ctypes.create_string_buffer(buffer_size)
        info = _FILE_RENAME_INFO.from_buffer(buffer)
        info.DUMMYUNIONNAME.ReplaceIfExists = False
        info.RootDirectory = None
        info.FileNameLength = len(encoded)
        ctypes.memmove(ctypes.addressof(info) + name_offset, encoded, len(encoded))
        handle = msvcrt.get_osfhandle(descriptor)
        if not kernel32.SetFileInformationByHandle(
            wintypes.HANDLE(handle),
            3,
            buffer,
            buffer_size,
        ):
            raise OSError(ctypes.get_last_error(), "SetFileInformationByHandle(FileRenameInfo) failed")
        return
    # The persistent opsync file serializes cooperating gateway writers. Keep
    # the owned lock as an audit artifact; never unlink an active lock name.
    if stat_identity(os.lstat(lock_path))[:2] != stat_identity(os.fstat(descriptor))[:2]:
        raise OSError("Checkpoint lock name no longer refers to the owned file")
    os.replace(lock_path, quarantine)


def remove_owned_checkpoint_lock(
    lock_path: Path,
    lock_id: str,
    expected_identity: tuple[int, int, int, int, int],
) -> bool:
    try:
        with checkpoint_lock_opsync(lock_path):
            descriptor = _open_owned_checkpoint_lock(lock_path, lock_id, expected_identity)
            if descriptor is None:
                return False
            os.lseek(descriptor, 0, os.SEEK_SET)
            owned_snapshot = _read_lock_descriptor(descriptor)
            if owned_snapshot is None:
                os.close(descriptor)
                return False
            payload = owned_snapshot[0]
            proposal_id = payload.get("intake_proposal_id")
            if isinstance(proposal_id, str) and PROPOSAL_ID_PATTERN.fullmatch(proposal_id):
                prefix = _checkpoint_pair_archive_prefix(lock_path, proposal_id)
            else:
                prefix = f"{lock_path.name}.release-"
            quarantine = lock_path.with_name(prefix + uuid.uuid4().hex)
            try:
                # Keep the exact owned file as an audit artifact. A foreign lock
                # at the active name must remain in place.
                _rename_owned_checkpoint_lock(descriptor, lock_path, quarantine)
                return True
            finally:
                os.close(descriptor)
    except (GatewayError, OSError):
        return False


def _lock_owner_pid_reused(payload: dict, identity: tuple[int, int, int, int, int], created_at: dt.datetime, pid: int) -> bool:
    """True only when process creation is known and proves a reused owner PID."""
    actual_start = windows_process_started_at(pid)
    if actual_start is None:
        # Unknown/unreadable process time preserves the lock fail-closed.
        return False
    # A process born after the actual lock file cannot be its original owner.
    # Corrupt/forged recorded timestamps must never release a live original owner.
    try:
        file_time = dt.datetime.fromtimestamp(identity[4] / 1_000_000_000, tz=dt.timezone.utc)
    except (OSError, OverflowError, ValueError):
        return False
    return (actual_start - max(created_at, file_time)).total_seconds() > PROCESS_TIME_TOLERANCE_SECONDS



def recover_stale_checkpoint_lock(lock_path: Path, policy: dict, *, requested: bool) -> bool:
    if not lock_path.exists() or not requested:
        return False
    try:
        snapshot = lock_snapshot(lock_path)
        if snapshot is None:
            return False
        payload, identity = snapshot
        created_at = parse_recorded_time(str(payload.get("created_at") or ""))
        if created_at is None:
            return False
        age_seconds = (now_utc() - created_at).total_seconds()
        if age_seconds < CHECKPOINT_LOCK_MIN_AGE_SECONDS:
            return False
        machine = str(payload.get("machine") or "")
        policy_machine = str(policy.get("machine") or "")
        if not machine or not policy_machine or machine.casefold() != policy_machine.casefold():
            return False
        try:
            pid = int(payload.get("pid") or 0)
        except (TypeError, ValueError):
            return False
        if pid <= 0:
            return False
        if process_is_running(pid) and not _lock_owner_pid_reused(payload, identity, created_at, pid):
            return False
        # Recheck the exact owned object before the identity-bound rename.
        recheck = lock_snapshot(lock_path)
        if (
            recheck is None
            or recheck[0].get("lock_id") != payload.get("lock_id")
            or recheck[1] != identity
        ):
            return False
        return remove_owned_checkpoint_lock(lock_path, str(payload["lock_id"]), identity)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False



def _checkpoint_pair_archive_prefix(lock_path: Path, proposal_id: str) -> str:
    key = hashlib.sha256(proposal_id.encode("utf-8")).hexdigest()[:32]
    return f"{lock_path.name}.release-pair-{key}-"


def _timeout_pair_archive_name(
    lock_path: Path, payload: dict, identity: tuple[int, int, int, int, int],
) -> str | None:
    proposal_id = payload.get("intake_proposal_id")
    lock_id = payload.get("lock_id")
    pid = payload.get("pid")
    started = payload.get("owner_process_started_at")
    if (not isinstance(proposal_id, str) or not PROPOSAL_ID_PATTERN.fullmatch(proposal_id)
            or not isinstance(lock_id, str) or not re.fullmatch(r"[0-9a-f]{32}", lock_id)
            or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
            or not isinstance(started, str) or parse_recorded_time(started) is None):
        return None
    pair_key = hashlib.sha256(proposal_id.encode("utf-8")).hexdigest()[:32]
    birth_key = hashlib.sha256(started.encode("ascii")).hexdigest()[:16]
    identity_key = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode("ascii")).hexdigest()[:16]
    return (f"{lock_path.name}.timeout-pair-{pair_key}-{lock_id}-{pid}-"
            f"{birth_key}-{identity_key}-")


def snapshot_owned_timeout_checkpoint_lock(
    lock_path: Path, index_lock_path: Path, owner_pid: int, owner_started_at: dt.datetime,
) -> dict | None:
    """Capture the exact young lock object while its known owner is still alive."""
    if (os.name != "nt" or owner_pid <= 0 or index_lock_path.exists()
            or not process_is_running(owner_pid)):
        return None
    actual_start = windows_process_started_at(owner_pid)
    if actual_start is None or abs((actual_start - owner_started_at).total_seconds()) > 0.002:
        return None
    snapshot = lock_snapshot(lock_path)
    if snapshot is None:
        return None
    payload, identity = snapshot
    created = parse_recorded_time(str(payload.get("created_at") or ""))
    started = parse_recorded_time(str(payload.get("owner_process_started_at") or ""))
    if (payload.get("pid") != owner_pid or started is None
            or abs((started - owner_started_at).total_seconds()) > 0.002
            or created is None
            or not 0 <= (now_utc() - created).total_seconds() < CHECKPOINT_LOCK_MIN_AGE_SECONDS
            or not isinstance(payload.get("machine"), str)
            or payload["machine"].casefold() != local_machine_name().casefold()
            or not PROPOSAL_ID_PATTERN.fullmatch(str(payload.get("intake_proposal_id") or ""))
            or not SHA256_HEX_PATTERN.fullmatch(str(payload.get("intake_receipt_sha256") or ""))):
        return None
    # Bind both metadata and the exact pre-timeout filesystem object. On a
    # replacement race, identical copied JSON still has a different identity.
    current = lock_snapshot(lock_path)
    if current != snapshot or index_lock_path.exists():
        return None
    return {
        "payload": payload,
        "identity": identity,
        "content_sha256": hashlib.sha256(read_regular_file_snapshot(lock_path.parent, lock_path)[0]).hexdigest(),
    }


def archive_timed_out_checkpoint_lock(
    vault: Path, owner_pid: int, owner_started_at: dt.datetime, expected: dict,
    expected_lock_path: Path, expected_index_lock_path: Path, deadline: float,
) -> bool:
    """Archive only this canonical child process' young, exact-pair checkpoint lock.

    Called by the caretaker only after its Windows job object reports that the
    spawned gateway and every descendant have exited. This is deliberately
    separate from stale-lock recovery: the ordinary 900 second guard is unchanged.
    """
    if os.name != "nt" or owner_pid <= 0 or time.monotonic() >= deadline:
        return False
    root = find_git_root(vault)
    if root is None:
        return False
    try:
        remaining = int(deadline - time.monotonic())
        if remaining < 1:
            return False
        policy = checkpoint_policy(vault, timeout=remaining)
        if (policy.get("enabled") is not True
                or policy.get("mode") != CHECKPOINT_MODE
                or policy.get("owner_machine") is not True
                or policy.get("policy_source") != "git-head"):
            return False
        # A leftover native Git lock can belong to an interrupted update-index,
        # commit, or another writer. Never infer ownership from its age.
        if (expected_index_lock_path.name != "index.lock"
                or expected_lock_path.name != CHECKPOINT_LOCK_NAME
                or expected_index_lock_path.parent.resolve() != expected_lock_path.parent.resolve()
                or expected_index_lock_path.parent.resolve() == root.resolve()
                or expected_index_lock_path.exists()):
            return False
        index_lock = expected_index_lock_path
        lock_path = expected_lock_path
        if process_is_running(owner_pid):
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        with checkpoint_lock_opsync(lock_path, timeout=min(1.0, remaining)):
            if time.monotonic() >= deadline:
                return False
            snapshot = lock_snapshot(lock_path)
            if snapshot is None:
                return False
            payload, identity = snapshot
            started = parse_recorded_time(str(payload.get("owner_process_started_at") or ""))
            created = parse_recorded_time(str(payload.get("created_at") or ""))
            if (payload.get("pid") != owner_pid
                    or payload.get("machine", "").casefold() != str(policy.get("machine") or "").casefold()
                    or started is None
                    or abs((started - owner_started_at).total_seconds()) > 0.002
                    or payload != expected.get("payload")
                    or identity != tuple(expected.get("identity", ()))
                    or hashlib.sha256(read_regular_file_snapshot(lock_path.parent, lock_path)[0]).hexdigest()
                    != expected.get("content_sha256")
                    or created is None
                    or not 0 <= (now_utc() - created).total_seconds() < CHECKPOINT_LOCK_MIN_AGE_SECONDS
                    or not SHA256_HEX_PATTERN.fullmatch(str(payload.get("intake_receipt_sha256") or ""))
                    or not isinstance(payload.get("intake_proposal_id"), str)
                    or not PROPOSAL_ID_PATTERN.fullmatch(payload["intake_proposal_id"])):
                return False
            prefix = _timeout_pair_archive_name(lock_path, payload, identity)
            if prefix is None:
                return False
            descriptor = _open_owned_checkpoint_lock(lock_path, str(payload["lock_id"]), identity)
            if descriptor is None:
                return False
            try:
                again = _read_lock_descriptor(descriptor)
                if again is None or again != snapshot:
                    return False
                # Recheck both PID liveness and Git's lock after identity-bound
                # open, immediately before moving the exact file object.
                if (time.monotonic() >= deadline or process_is_running(owner_pid) or index_lock.exists()
                        or not _checkpoint_lock_name_matches(lock_path, identity)):
                    return False
                _rename_owned_checkpoint_lock(
                    descriptor, lock_path,
                    lock_path.with_name(prefix + uuid.uuid4().hex),
                )
                return True
            finally:
                os.close(descriptor)
    except (GatewayError, OSError, ValueError, TypeError, AttributeError):
        return False


def _retained_checkpoint_lock_proof(
    root: Path, path: Path, policy: dict, proposal_id: str, receipt_sha256: str,
    *, legacy_explicit: bool = False,
) -> tuple | None:
    """Read preserved lock evidence; it never replaces a current exclusive lock."""
    lock_path = git_internal_path(root, CHECKPOINT_LOCK_NAME)
    try:
        if path.parent.resolve() != lock_path.parent.resolve():
            return None
        legacy_name = re.fullmatch(re.escape(lock_path.name) + r"\.release-[0-9a-f]{32}", path.name)
        pair_name = re.fullmatch(re.escape(_checkpoint_pair_archive_prefix(lock_path, proposal_id)) + r"[0-9a-f]{32}", path.name)
        timeout_name = re.fullmatch(
            re.escape(lock_path.name)
            + r"\.timeout-pair-([0-9a-f]{32})-([0-9a-f]{32})-(\d+)-([0-9a-f]{16})-([0-9a-f]{16})-([0-9a-f]{32})",
            path.name,
        )
        timeout_archive = timeout_name is not None
        if not (pair_name or timeout_archive or (legacy_explicit and legacy_name)):
            return None
        if path_has_reparse_component(lock_path.parent, path):
            return None
        content, identity = read_regular_file_snapshot(lock_path.parent, path, max_bytes=16384)
        payload = json.loads(content.decode("utf-8"))
        if not isinstance(payload, dict) or not re.fullmatch(r"[0-9a-f]{32}", str(payload.get("lock_id", ""))):
            return None
        created = parse_recorded_time(str(payload.get("created_at", "")))
        if created is None:
            return None
        if not timeout_archive and (now_utc() - created).total_seconds() < CHECKPOINT_LOCK_MIN_AGE_SECONDS:
            return None
        if (not payload.get("machine") or not policy.get("machine")
                or str(payload["machine"]).casefold() != str(policy["machine"]).casefold()):
            return None
        pid = int(payload.get("pid", 0))
        if pid <= 0 or (process_is_running(pid) and not _lock_owner_pid_reused(payload, identity, created, pid)):
            return None
        if (pair_name or timeout_archive) and (payload.get("intake_proposal_id") != proposal_id
                                               or payload.get("intake_receipt_sha256") != receipt_sha256):
            return None
        if timeout_archive:
            prefix = _timeout_pair_archive_name(lock_path, payload, identity)
            if (prefix is None or path.name[:-32] != prefix
                    or timeout_name.group(1) != hashlib.sha256(proposal_id.encode("utf-8")).hexdigest()[:32]
                    or timeout_name.group(2) != payload.get("lock_id")
                    or int(timeout_name.group(3)) != pid):
                return None
        return (str(path), hashlib.sha256(content).hexdigest(), identity)
    except (GatewayError, OSError, ValueError, TypeError, UnicodeError):
        return None


def _find_retained_pair_lock(root: Path, policy: dict, proposal_id: str, receipt_sha256: str) -> tuple | None:
    lock_path = git_internal_path(root, CHECKPOINT_LOCK_NAME)
    # Exact per-pair prefix; do not scan the accumulated archives of other pairs.
    paths = []
    prefixes = (
        _checkpoint_pair_archive_prefix(lock_path, proposal_id),
        f"{lock_path.name}.timeout-pair-{hashlib.sha256(proposal_id.encode('utf-8')).hexdigest()[:32]}-",
    )
    for prefix in prefixes:
        for path in lock_path.parent.glob(prefix + "*"):
            paths.append(path)
            if len(paths) > 16:
                return None
    proofs = [_retained_checkpoint_lock_proof(root, p, policy, proposal_id, receipt_sha256) for p in paths]
    proven = [p for p in proofs if p is not None]
    return sorted(proven)[0] if proven else None


def acquire_checkpoint_lock(
    lock_path: Path,
    policy: dict,
    *,
    recover_stale_lock: bool,
    intake_proposal_id: str | None = None,
    intake_receipt_sha256: str | None = None,
) -> tuple[str, tuple[int, int, int, int, int], bool] | None:
    stale_lock_recovered = recover_stale_checkpoint_lock(
        lock_path,
        policy,
        requested=recover_stale_lock,
    )
    lock_id = uuid.uuid4().hex
    descriptor: int | None = None
    try:
        with checkpoint_lock_opsync(lock_path):
            try:
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                for _ in range(20):
                    try:
                        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                        break
                    except FileExistsError:
                        time.sleep(0.1)
                if descriptor is None:
                    return None
                lock_payload = {
                    "lock_id": lock_id,
                    "pid": os.getpid(),
                    "machine": policy["machine"],
                    "created_at": iso_time(now_utc()),
                }
                if intake_proposal_id is not None and intake_receipt_sha256 is not None:
                    lock_payload["intake_proposal_id"] = intake_proposal_id
                    lock_payload["intake_receipt_sha256"] = intake_receipt_sha256
                owner_started = windows_process_started_at(os.getpid())
                if owner_started is not None:
                    lock_payload["owner_process_started_at"] = iso_time(owner_started)
                os.write(
                    descriptor,
                    (json.dumps(lock_payload) + "\n").encode("utf-8"),
                )
                os.fsync(descriptor)
                identity = stat_identity(os.fstat(descriptor))
                return lock_id, identity, stale_lock_recovered
            finally:
                if descriptor is not None:
                    os.close(descriptor)
    except (GatewayError, OSError):
        return None


def checkpoint_manual_block(root: Path) -> dict | None:
    path = git_internal_path(root, CHECKPOINT_MANUAL_BLOCK_NAME)
    if not path.exists():
        return None
    try:
        content, _ = read_regular_file_snapshot(path.parent, path)
        payload = json.loads(content.decode("utf-8", errors="strict"))
        if not isinstance(payload, dict) or not str(payload.get("block_id") or ""):
            raise ValueError("invalid manual block payload")
        return payload
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, GatewayError):
        return {
            "block_id": None,
            "status": "CORRUPT_MANUAL_BLOCK",
            "reason": "The checkpoint manual block exists but cannot be validated.",
        }


def persist_checkpoint_manual_block(
    root: Path,
    *,
    status: str,
    proposal_id: str,
    commit_sha: str | None,
    head_before: str | None,
    changed_paths: list[str],
    reason: str,
) -> dict:
    path = git_internal_path(root, CHECKPOINT_MANUAL_BLOCK_NAME)
    existing = checkpoint_manual_block(root)
    if existing is not None:
        return existing
    payload = {
        "block_id": uuid.uuid4().hex,
        "status": status,
        "proposal_id": proposal_id,
        "commit": commit_sha,
        "head_before": head_before,
        "changed_paths": changed_paths,
        "machine": local_machine_name(),
        "created_at": iso_time(now_utc()),
        "reason": reason,
    }
    descriptor: int | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(descriptor, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(descriptor)
        return payload
    except FileExistsError:
        return checkpoint_manual_block(root) or {
            "block_id": None,
            "status": "UNREADABLE_MANUAL_BLOCK",
        }
    except OSError:
        return {
            "block_id": None,
            "status": "MANUAL_BLOCK_WRITE_FAILED",
        }
    finally:
        if descriptor is not None:
            os.close(descriptor)


def git_tree_blobs(root: Path, revision: str, relative_paths: list[str]) -> dict[str, str]:
    entries: dict[str, str] = {}
    result = run_git(root, ["ls-tree", "-r", revision, "--", *relative_paths])
    for line in result.stdout.splitlines():
        metadata, relative = line.split("\t", 1)
        mode, kind, object_id = metadata.split()
        if mode != "100644" or kind != "blob":
            raise GatewayError(f"Checkpoint tree entry is not a regular non-executable blob: {relative}")
        entries[relative.replace("\\", "/")] = object_id
    return entries


def _introducing_commit_for_path(root: Path, relative: str) -> str | None:
    result = run_git(
        root,
        ["log", "--diff-filter=A", "--format=%H", "-1", "--", relative],
    )
    commit = result.stdout.strip()
    return commit or None


def _commit_additions_only_paths(root: Path, commit_sha: str) -> list[tuple[str, str]]:
    changes: list[tuple[str, str]] = []
    result = run_git(
        root,
        ["diff-tree", "--root", "--no-commit-id", "--name-status", "-r", "--no-renames", commit_sha],
    )
    for line in result.stdout.splitlines():
        status, relative = line.split("\t", 1)
        changes.append((status, relative.replace("\\", "/")))
    return changes


def verify_current_checkpoint_receipt(
    root: Path,
    relative_paths: list[str],
    snapshot: dict[str, bytes],
) -> tuple[bool, str | None, str | None]:
    try:
        expected_blobs = {
            relative: git_blob_oid(root, relative, snapshot[relative], write=False)
            for relative in relative_paths
        }
        if git_tree_blobs(root, "HEAD", relative_paths) != expected_blobs:
            return False, None, "HEAD blob IDs differ from the validated receipt bytes."
        introducing_map: dict[str, str] = {}
        for relative in relative_paths:
            commit = _introducing_commit_for_path(root, relative)
            if not commit:
                return False, None, f"No introducing commit was found for {relative}."
            introducing_map[relative] = commit
        unique_commits = sorted(set(introducing_map.values()))
        for commit_sha in unique_commits:
            ancestry = run_git(root, ["merge-base", "--is-ancestor", commit_sha, "HEAD"], check=False)
            if ancestry.returncode != 0:
                return False, None, "The introducing commit is not an ancestor of HEAD."
        if len(unique_commits) == 1:
            # Ordinary exact additions-only receipt check; unchanged for proven single commits.
            commit_sha = unique_commits[0]
            changes = _commit_additions_only_paths(root, commit_sha)
            expected_changes = sorted(("A", relative) for relative in relative_paths)
            if sorted(changes) != expected_changes:
                return False, commit_sha, "The introducing commit is not the exact additions-only receipt."
            if git_tree_blobs(root, commit_sha, relative_paths) != expected_blobs:
                return False, commit_sha, "Introducing commit blobs differ from the validated receipt bytes."
            return True, commit_sha, None
        # Multi-commit introduction is accepted only with a validated partial-receipt proof.
        proof_commits: list[tuple[str, dict[str, str]]] = []
        for commit_sha in unique_commits:
            message = run_git(root, ["log", "-1", "--format=%B", commit_sha]).stdout
            trailers = parse_receipt_trailers(message)
            if RECEIPT_DIGEST_TRAILER in trailers:
                proof_commits.append((commit_sha, trailers))
        if len(proof_commits) != 1:
            return False, None, "Receipt paths were introduced by different commits without one receipt proof."
        proof_sha, trailers = proof_commits[0]
        digest = trailers.get(RECEIPT_DIGEST_TRAILER) or ""
        if not SHA256_HEX_PATTERN.fullmatch(digest) or digest != receipt_content_digest(snapshot):
            return False, proof_sha, "Receipt proof digest does not match the validated receipt bytes."
        paths_raw = trailers.get(RECEIPT_PATHS_TRAILER) or ""
        if not paths_raw.isdigit() or int(paths_raw) != len(relative_paths):
            return False, proof_sha, "Receipt proof path cardinality does not match the validated receipt."
        event_refs = [item.strip() for item in (trailers.get(RECEIPT_EVENTS_TRAILER) or "").split(",") if item.strip()]
        event_ids: list[str] = []
        for relative in relative_paths:
            try:
                event = json.loads(snapshot[relative].decode("utf-8", errors="strict"))
            except (UnicodeError, json.JSONDecodeError):
                event = None
            if isinstance(event, dict) and str(event.get("action") or "") in {"stage-source", "new-proposal"}:
                event_ids.append(str(event.get("event_id") or ""))
        if sorted(event_refs) != sorted(event_ids) or len(event_ids) != len(set(event_ids)):
            return False, proof_sha, "Receipt proof event refs do not match the validated receipt events."
        for commit_sha in unique_commits:
            introduced = sorted(relative for relative, commit in introducing_map.items() if commit == commit_sha)
            changes = _commit_additions_only_paths(root, commit_sha)
            expected_changes = sorted(("A", relative) for relative in introduced)
            if commit_sha == proof_sha:
                if sorted(changes) != expected_changes:
                    return False, commit_sha, "The completion commit is not the exact additions-only receipt."
            else:
                # Earlier publication may have included other files. Only our exact
                # unchanged event additions are bound by the completion proof.
                if any(not relative.startswith("vault/events/") for relative in introduced):
                    return False, commit_sha, "Earlier receipt portion is not exclusively events."
                if any(("A", relative) not in changes for relative in introduced):
                    return False, commit_sha, "Earlier event is not a genuine addition."
                if run_git(root, ["merge-base", "--is-ancestor", commit_sha, proof_sha], check=False).returncode:
                    return False, commit_sha, "Earlier events do not precede the completion proof."
            if git_tree_blobs(root, commit_sha, introduced) != {key: expected_blobs[key] for key in introduced}:
                return False, commit_sha, "Introducing commit blobs differ from the validated receipt bytes."
        return True, proof_sha, None
    except (GatewayError, OSError, subprocess.SubprocessError, ValueError) as exc:
        return False, None, str(exc)



def _staged_index_proposal_ids(root: Path, vault: Path) -> set[str] | None:
    """Proposal IDs identifiable from staged index blobs, or None on ambiguity.

    Only staged proposal files and staged new-proposal events identify a pair;
    unreadable or malformed staged blobs make identification fail closed.
    """
    try:
        proposals_prefix = repository_relative_path(root, vault / "proposals").rstrip("/") + "/"
        events_prefix = repository_relative_path(root, vault / "events").rstrip("/") + "/"
    except (GatewayError, OSError):
        return None
    try:
        staged = [
            path.replace("\\", "/")
            for path in run_git(root, ["diff", "--cached", "--name-only", "-z"]).stdout.split("\0")
            if path
        ]
    except (GatewayError, OSError, subprocess.SubprocessError):
        return None
    ids: set[str] = set()
    for relative in staged:
        try:
            if relative.startswith(proposals_prefix) and relative.endswith(".md"):
                proposal_id = fm_value(run_git(root, ["show", f":{relative}"]).stdout, "id")
                if not isinstance(proposal_id, str) or not PROPOSAL_ID_PATTERN.fullmatch(proposal_id):
                    return None
                ids.add(proposal_id)
            elif relative.startswith(events_prefix) and relative.endswith(".json"):
                event = json.loads(run_git(root, ["show", f":{relative}"]).stdout)
                if isinstance(event, dict) and event.get("action") == "new-proposal":
                    for proposal_id in event.get("proposal_ids") or []:
                        if not isinstance(proposal_id, str) or not PROPOSAL_ID_PATTERN.fullmatch(proposal_id):
                            return None
                        ids.add(proposal_id)
        except (GatewayError, OSError, UnicodeError, ValueError, subprocess.SubprocessError):
            return None
    return ids


def recover_pending_intake(vault: Path, *, limit: int = 8) -> dict:
    """Owner-only bounded completion of retained append-only intake, no new knowledge."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 8:
        raise GatewayError("Pending intake limit must be an integer from 1 through 8.")
    policy = checkpoint_policy(vault)
    if (not policy.get("enabled") or not policy.get("owner_machine")
            or policy.get("policy_source") != "git-head" or policy.get("mode") != CHECKPOINT_MODE):
        return {"status": "NOT_OWNER", "results": [], "new_sources": 0}
    root = find_git_root(vault)
    if root is None:
        return {"status": "BLOCKED_GIT_STATE", "results": [], "new_sources": 0}
    if not index_is_clean(root):
        # A crashed checkpoint can leave exactly one of its pairs staged.  Only
        # that single proven pair is dispatched to the guarded stale-lock
        # recovery; ambiguous, foreign or multiple pairs keep the generic block.
        staged_ids = _staged_index_proposal_ids(root, vault)
        if staged_ids is None or len(staged_ids) != 1:
            return {
                "status": "BLOCKED_GIT_STATE",
                "results": [],
                "new_sources": 0,
                "reason": "dirty index without exactly one identifiable pending pair",
            }
        try:
            result = checkpoint_intake(vault, sorted(staged_ids)[0], recover_stale_lock=True)
        except (GatewayError, OSError, subprocess.SubprocessError) as exc:
            return {"status": "BLOCKED_GIT_STATE", "results": [], "new_sources": 0,
                    "reason": f"staged pair recovery refused: {exc}"}
        recovered = result.get("status") in {"CHECKPOINTED", "CURRENT"}
        return {
            "status": "COMPLETED" if recovered else "PARTIAL",
            "results": [result],
            "new_sources": 0,
        }
    prefix = repository_relative_path(root, vault / "proposals").rstrip("/") + "/"
    candidates = sorted(p for p in git_uncommitted_paths(root) if p.startswith(prefix) and p.endswith(".md"))
    results = []
    for relative in candidates[:limit]:
        try:
            path = root / relative
            content = read_regular_file_snapshot(root, path, max_bytes=5 * 1024 * 1024)[0].decode("utf-8-sig")
            proposal_id = fm_value(content, "id")
            if not isinstance(proposal_id, str) or not PROPOSAL_ID_PATTERN.fullmatch(proposal_id):
                continue
            result = checkpoint_intake(vault, proposal_id, recover_stale_lock=True, recover_partial_pair=True)
        except (GatewayError, OSError, UnicodeError, ValueError) as exc:
            result = {"status": "BLOCKED_INVALID_PAIR", "reason": str(exc)[:300]}
        results.append(result)
        if result.get("status") in {"BLOCKED_CHECKPOINT_LOCK", "BLOCKED_DIRTY_INDEX", "BLOCKED_CONCURRENT_GIT_STATE"}:
            break
    return {"status": "COMPLETED" if all(r.get("status") in {"CHECKPOINTED", "CURRENT"} for r in results) else "PARTIAL",
            "results": results, "new_sources": 0, "candidate_count": len(candidates)}


def rollback_checkpoint_commit(
    root: Path,
    commit_sha: str,
    head_before: str,
    changed_paths: list[str],
) -> bool:
    try:
        current_head = run_git(root, ["rev-parse", "HEAD"]).stdout.strip()
        parent_sha = run_git(root, ["rev-parse", f"{commit_sha}^"]).stdout.strip()
        if current_head != commit_sha or parent_sha != head_before:
            return False
        moved = run_git_without_hooks(
            root,
            ["update-ref", "HEAD", head_before, commit_sha],
            check=False,
        )
        if moved.returncode != 0 or run_git(root, ["rev-parse", "HEAD"]).stdout.strip() != head_before:
            return False
        return unstage_checkpoint_paths(root, changed_paths) and (
            run_git(root, ["rev-parse", "HEAD"]).stdout.strip() == head_before
        )
    except (OSError, subprocess.SubprocessError, GatewayError):
        return False


def _validate_partial_pair_recovery(
    root: Path,
    statuses: dict[str, str],
    proposal_path: Path,
    proposal_event_path: Path,
    source_groups: list[list[Path]],
) -> tuple[bool, str | None]:
    """Validate that a partial pair is recoverable.  Returns (valid, reason)."""
    event_relatives = {repository_relative_path(root, proposal_event_path)}
    non_event_relatives = {repository_relative_path(root, proposal_path)}
    for group in source_groups:
        metadata_path, original_path, stage_event_path = group
        event_relatives.add(repository_relative_path(root, stage_event_path))
        non_event_relatives.add(repository_relative_path(root, metadata_path))
        non_event_relatives.add(repository_relative_path(root, original_path))

    in_paths = {path for path, status in statuses.items() if status == "IN"}
    untracked_paths = {path for path, status in statuses.items() if status == "??"}

    if not in_paths:
        return False, "no committed portion to recover"
    if not in_paths <= event_relatives:
        return False, "committed portion includes non-event paths"
    if not untracked_paths <= non_event_relatives:
        return False, "uncommitted portion includes event paths"

    for relative in sorted(in_paths):
        path = root / relative
        try:
            current_content = read_regular_file_snapshot(root, path)[0]
        except (OSError, GatewayError):
            return False, f"cannot read committed event: {relative}"
        head_blobs = git_tree_blobs(root, "HEAD", [relative])
        if relative not in head_blobs:
            return False, f"committed event not in HEAD: {relative}"
        expected_blob = git_blob_oid(root, relative, current_content, write=False)
        if head_blobs[relative] != expected_blob:
            return False, f"committed event content differs from HEAD: {relative}"
        introducing = _introducing_commit_for_path(root, relative)
        if not introducing or git_tree_blobs(root, introducing, [relative]) != {relative: expected_blob}:
            return False, f"committed event differs from its original addition: {relative}"
    proposal = read_text(proposal_path)
    proposal_event = json.loads(read_text(proposal_event_path))
    if proposal_event.get("agent") != fm_value(proposal, "agent") or proposal_event.get("proposal_ids") != [fm_value(proposal, "id")]:
        return False, "proposal event producer/identity mismatch"
    event_ids = [proposal_event.get("event_id")]
    for group in source_groups:
        metadata = json.loads(read_text(group[0])); event = json.loads(read_text(group[2]))
        if event.get("agent") != metadata.get("captured_by") or event.get("source_ids") != [metadata.get("source_id")]:
            return False, "source event producer/identity mismatch"
        event_ids.append(event.get("event_id"))
    if any(not isinstance(e, str) or not e or len(e) > 128 for e in event_ids) or len(set(event_ids)) != len(event_ids):
        return False, "receipt event identity/cardinality mismatch"
    return True, None


def _validate_partial_index_recovery(
    root: Path, relative_paths: list[str]
) -> tuple[bool, str | None, dict[str, str]]:
    """Validate a staged crash remainder of THIS already validated candidate pair.

    Returns (valid, reason, staged_blobs).  Every staged path in the whole
    index must be a pure stage-0 100644 addition belonging to the candidate
    set, its index blob must equal the immutable current worktree bytes, and
    the staged set must be a nonempty subset of the candidate pair;
    anything else refuses and leaves HEAD/index/live files unchanged.
    """
    candidates = set(relative_paths)
    staged: dict[str, None] = {}
    name_status = run_git(root, ["diff", "--cached", "--name-status", "--no-renames", "-z"]).stdout
    tokens = [token for token in name_status.split("\0") if token]
    if len(tokens) % 2:
        return False, "staged index listing is malformed", {}
    for index in range(0, len(tokens), 2):
        status = tokens[index]
        relative = tokens[index + 1].replace("\\", "/")
        if status != "A":
            return False, f"staged entry is not a pure addition: {status} {relative}", {}
        if relative not in candidates:
            return False, f"staged path is foreign to the candidate pair: {relative}", {}
        staged[relative] = None
    if not staged:
        return False, "index has no staged additions to recover", {}
    index_entries: dict[str, tuple[str, str, str]] = {}
    listing = run_git(root, ["ls-files", "--stage", "-z", "--", *sorted(staged)]).stdout
    for line in listing.split("\0"):
        if not line:
            continue
        metadata, relative = line.split("\t", 1)
        mode, object_id, stage = metadata.split()
        index_entries[relative.replace("\\", "/")] = (mode, object_id, stage)
    blobs: dict[str, str] = {}
    for relative in sorted(staged):
        entry = index_entries.get(relative)
        if entry is None:
            return False, f"staged candidate is missing from the index: {relative}", {}
        mode, object_id, stage = entry
        if mode != "100644" or stage != "0":
            return False, f"staged candidate is not a stage-0 100644 blob: {relative}", {}
        try:
            content = read_regular_file_snapshot(root, root / relative)[0]
        except (OSError, GatewayError):
            return False, f"cannot snapshot the staged candidate bytes: {relative}", {}
        if object_id != git_blob_oid(root, relative, content, write=False):
            return False, f"staged blob differs from the current candidate bytes: {relative}", {}
        blobs[relative] = object_id
    return True, None, blobs


def proposal_path_for_id(vault: Path, proposal_id: str) -> Path:
    if not PROPOSAL_ID_PATTERN.fullmatch(proposal_id):
        raise GatewayError(f"Invalid proposal ID: {proposal_id}")
    matches = []
    for content, path in proposal_records(vault):
        if fm_value(content, "id") == proposal_id:
            matches.append(path)
    if len(matches) != 1:
        raise GatewayError(f"Checkpoint requires exactly one proposal for {proposal_id}; found {len(matches)}.")
    return matches[0]


def _recover_missing_critical_proposal_event(
    vault: Path,
    root: Path,
    proposal_id: str,
    proposal_path: Path,
    proposal_content: str,
    source_ids: list[str],
    source_groups: list[list[Path]],
    source_map: dict[str, tuple[dict, Path]],
    event_records: list[tuple[dict, Path]],
) -> dict:
    """Append one explicitly marked recovery event for a fully proven orphan.

    This is intentionally limited to a critical handoff created by the normal
    record-handoff path: the original producer must match its proposal path,
    source metadata, and stage-source event. The canonical event fence guards
    both the zero-event recheck and exclusive event creation. No proposal or
    source bytes are synthesized or edited.
    """
    def blocked(status: str, reason: str) -> dict:
        return {"status": status, "reason": reason}

    producer = str(fm_value(proposal_content, "agent") or "")
    created_raw = str(fm_value(proposal_content, "created_at") or "")
    created_match = re.fullmatch(
        r"prop-(\d{8}T\d{9}Z)-([a-f0-9]{8})", proposal_id,
    )
    try:
        created_at = dt.datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        created_at_utc = created_at.astimezone(dt.timezone.utc)
        proposal_bytes = read_regular_file_snapshot(root, proposal_path)[0]
        if proposal_bytes.decode("utf-8-sig", errors="strict") != proposal_content:
            return blocked("BLOCKED_CONCURRENT_CONTENT_CHANGE", "proposal bytes changed before recovery validation")
    except (ValueError, TypeError, OverflowError):
        return blocked("BLOCKED_PROPOSAL_EVENT_RECOVERY", "proposal creation time is invalid")
    except (OSError, UnicodeError, GatewayError):
        return blocked("BLOCKED_PROPOSAL_EVENT_RECOVERY", "proposal bytes are unreadable")
    if (
        created_at.tzinfo is None
        or not created_match
        or id_time(created_at_utc) != created_match.group(1)
        or fm_value(proposal_content, "type") != "proposal"
        or fm_value(proposal_content, "status") != "proposed"
        or fm_value(proposal_content, "knowledge_kind") != "critical-state"
        or fm_value(proposal_content, "classification") not in {"public", "internal"}
        or not AGENT_PATTERN.fullmatch(producer)
        or slug(producer) != producer
        or proposal_path.parent.name != producer
        or not proposal_path.name.startswith(proposal_id + "-")
        or len(source_ids) != 1
        or source_ids != sorted(set(source_ids))
        or not source_groups
        or len(source_groups) != len(source_ids)
    ):
        return blocked("BLOCKED_PROPOSAL_EVENT_RECOVERY", "proposal is not a unique, normal critical handoff")

    # All old records must parse and be regular files. A wrong-shape event
    # mentioning this proposal is a conflict, not an invitation to repair it.
    if any(
        proposal_id in (event.get("proposal_ids") or [])
        or (
            event.get("action") == "new-proposal"
            and sorted(event.get("source_ids") or []) == sorted(source_ids)
        )
        for event, _path in event_records
    ):
        return blocked(
            "BLOCKED_PROPOSAL_EVENT_CARDINALITY",
            "an event already references this proposal or its exact source set",
        )
    proven_bytes: dict[str, bytes] = {
        repository_relative_path(root, proposal_path): proposal_bytes,
    }
    if path_has_reparse_component(root, vault / "events"):
        return blocked("BLOCKED_REPARSE_PATH", "the event directory crosses a reparse component")
    if any(
        path_has_reparse_component(root, path)
        for group in source_groups
        for path in group
    ):
        return blocked("BLOCKED_REPARSE_PATH", "a source evidence path crosses a reparse component")
    for source_id, group in zip(source_ids, source_groups):
        metadata_path, original_path, stage_event_path = group
        matching_metadata = [
            metadata for metadata, _path in source_records(vault)
            if metadata and metadata.get("source_id") == source_id
        ]
        if len(matching_metadata) != 1:
            return blocked("BLOCKED_SOURCE_INTEGRITY", "source metadata identity is duplicated or missing")
        metadata = matching_metadata[0]
        if (
            metadata.get("source_id") != source_id
            or metadata.get("captured_by") != producer
            or metadata.get("project_slug") != fm_value(proposal_content, "project_slug")
            or metadata.get("knowledge_kind") != "critical-state"
            or metadata.get("state_summary") != fm_value(proposal_content, "state_summary")
            or metadata.get("classification") != fm_value(proposal_content, "classification")
            or metadata.get("local_only") is not False
            or metadata.get("original_file") != original_path.name
            or metadata_path.parent != original_path.parent
        ):
            return blocked("BLOCKED_SOURCE_INTEGRITY", "source metadata does not bind to the proposal producer")
        try:
            source_original = read_regular_file_snapshot(root, original_path)[0]
            source_metadata_bytes = read_regular_file_snapshot(root, metadata_path)[0]
            parsed_metadata = json.loads(source_metadata_bytes.decode("utf-8-sig", errors="strict"))
            if parsed_metadata != metadata:
                return blocked("BLOCKED_CONCURRENT_CONTENT_CHANGE", "source metadata changed during recovery validation")
            if (hashlib.sha256(source_original).hexdigest() != metadata.get("sha256")
                    or len(source_original) != metadata.get("byte_length")):
                return blocked("BLOCKED_SOURCE_INTEGRITY", "source bytes do not match their metadata hash")
            stage_event_bytes = read_regular_file_snapshot(root, stage_event_path)[0]
            stage_event = json.loads(stage_event_bytes.decode("utf-8-sig", errors="strict"))
        except (OSError, UnicodeError, ValueError, GatewayError) as exc:
            return blocked("BLOCKED_SOURCE_INTEGRITY", f"source evidence is unreadable: {type(exc).__name__}")
        stage_event_id = stage_event_path.stem
        if (
            not isinstance(stage_event, dict)
            or stage_event.get("event_id") != stage_event_id
            or not EVENT_ID_PATTERN.fullmatch(stage_event_id)
            or stage_event.get("action") != "stage-source"
            or stage_event.get("agent") != producer
            or stage_event.get("source_ids") != [source_id]
        ):
            return blocked("BLOCKED_SOURCE_EVENT_CARDINALITY", "stage event producer or identity is ambiguous")
        proven_bytes.update({
            repository_relative_path(root, metadata_path): source_metadata_bytes,
            repository_relative_path(root, original_path): source_original,
            repository_relative_path(root, stage_event_path): stage_event_bytes,
        })
        associated_stage = [
            event for event, _path in event_records
            if isinstance(event, dict) and event.get("action") == "stage-source"
            and source_id in (event.get("source_ids") or [])
        ]
        if len(associated_stage) != 1 or associated_stage[0] != stage_event:
            return blocked("BLOCKED_SOURCE_EVENT_CARDINALITY", "source has duplicate or conflicting stage events")
        captured_raw = str(metadata.get("captured_at") or "")
        try:
            captured_at = dt.datetime.fromisoformat(captured_raw.replace("Z", "+00:00"))
            stage_time = dt.datetime.fromisoformat(
                str(stage_event.get("occurred_at") or "").replace("Z", "+00:00")
            )
        except (ValueError, TypeError, OverflowError):
            return blocked("BLOCKED_SOURCE_INTEGRITY", "source capture time is invalid")
        if (captured_at.tzinfo is None or stage_time.tzinfo is None
                or captured_at.astimezone(dt.timezone.utc) > created_at_utc
                or stage_time.astimezone(dt.timezone.utc) < captured_at.astimezone(dt.timezone.utc)):
            return blocked("BLOCKED_SOURCE_INTEGRITY", "source and proposal chronology is inconsistent")

    integrity = critical_handoff_integrity(
        vault, proposal_content, sources=source_map, events=event_records,
    )
    if integrity.get("issues") != ["proposal-event-cardinality:0"]:
        return blocked("BLOCKED_PROPOSAL_EVENT_RECOVERY", "the proposal is not the sole remaining critical-handoff lint error")

    relative_paths = sorted({
        repository_relative_path(root, proposal_path),
        *(repository_relative_path(root, path) for group in source_groups for path in group),
    })
    if any(path_has_reparse_component(root, root / path) for path in relative_paths):
        return blocked("BLOCKED_REPARSE_PATH", "a proven handoff path crosses a reparse component")
    if path_has_reparse_component(root, vault / "events"):
        return blocked("BLOCKED_REPARSE_PATH", "the event directory crosses a reparse component")
    if git_operation_in_progress(root):
        return blocked("BLOCKED_GIT_OPERATION", "a Git operation is in progress")
    if not index_is_clean(root):
        return blocked("BLOCKED_DIRTY_INDEX", "the Git index is not clean")
    try:
        statuses = git_status_map(root, relative_paths)
        if set(statuses) != set(relative_paths) or any(
            status not in {"??", "IN"} for status in statuses.values()
        ):
            return blocked("BLOCKED_UNSUPPORTED_PATH_STATUS", "handoff paths contain conflicting Git changes")
        tracked = [path for path, status in statuses.items() if status == "IN"]
        head_before = run_git(root, ["rev-parse", "--verify", "HEAD"]).stdout.strip()
        if tracked:
            head_blobs = git_tree_blobs(root, "HEAD", tracked)
            for relative in tracked:
                current = read_regular_file_snapshot(root, root / relative)[0]
                if head_blobs.get(relative) != git_blob_oid(root, relative, current, write=False):
                    return blocked("BLOCKED_CONCURRENT_PATH_CHANGE", "a committed handoff file differs from HEAD")
        assessment = git_dirty_assessment(root)
        admission = backlog_admission(assessment, 1)
        if admission.get("blocked"):
            return blocked("BLOCKED_BACKLOG_POLICY", admission.get("reason") or "Git backlog policy blocked recovery")
        candidate_snapshot = capture_candidate_bytes(root, relative_paths)
        if any(candidate_snapshot.get(path) != content for path, content in proven_bytes.items()):
            return blocked("BLOCKED_CONCURRENT_CONTENT_CHANGE", "handoff evidence changed during recovery validation")
        event_inventory: dict[str, str] = {}
        for event, path in event_records:
            relative = repository_relative_path(root, path)
            raw = read_regular_file_snapshot(root, path)[0]
            if json.loads(raw.decode("utf-8-sig", errors="strict")) != event:
                return blocked("BLOCKED_CONCURRENT_CONTENT_CHANGE", "an event changed during recovery validation")
            event_inventory[relative] = hashlib.sha256(raw).hexdigest()
    except (GatewayError, OSError, UnicodeError, ValueError, subprocess.SubprocessError) as exc:
        return blocked("BLOCKED_PROPOSAL_EVENT_RECOVERY", f"Git or snapshot proof failed: {type(exc).__name__}")

    lint_result = lint(vault)
    expected_lint = f"Incomplete critical handoff receipt: {proposal_path} (proposal-event-cardinality:0)"
    if lint_result.get("errors") != [expected_lint]:
        return blocked("BLOCKED_LINT", "recovery requires this cardinality error to be the sole lint error")

    try:
        with _event_write_fence(vault):
            current_events: list[tuple[dict, Path]] = []
            current_inventory: dict[str, str] = {}
            event_root = vault / "events"
            for path in sorted(event_root.rglob("*.json")) if event_root.is_dir() else []:
                if path_has_reparse_component(root, path):
                    return blocked("BLOCKED_REPARSE_PATH", "an event path crosses a reparse component")
                relative = repository_relative_path(root, path)
                raw = read_regular_file_snapshot(root, path)[0]
                event = json.loads(raw.decode("utf-8-sig", errors="strict"))
                if not isinstance(event, dict):
                    return blocked("BLOCKED_EVENT_INVALID", "an event record is not an object")
                current_inventory[relative] = hashlib.sha256(raw).hexdigest()
                current_events.append((event, path))
            if current_inventory != event_inventory:
                return blocked("BLOCKED_CONCURRENT_CONTENT_CHANGE", "the event set changed before recovery")
            if any(proposal_id in (event.get("proposal_ids") or []) for event, _ in current_events):
                return blocked("BLOCKED_PROPOSAL_EVENT_CARDINALITY", "an event appeared before recovery")
            for source_id in source_ids:
                associated_stage = [
                    event for event, _ in current_events
                    if event.get("action") == "stage-source"
                    and source_id in (event.get("source_ids") or [])
                ]
                if len(associated_stage) != 1:
                    return blocked("BLOCKED_SOURCE_EVENT_CARDINALITY", "source stage-event set changed before recovery")
            current_snapshot = capture_candidate_bytes(root, relative_paths)
            if snapshot_fingerprint(current_snapshot) != snapshot_fingerprint(candidate_snapshot):
                return blocked("BLOCKED_CONCURRENT_CONTENT_CHANGE", "handoff evidence changed before recovery")
            if git_operation_in_progress(root) or not index_is_clean(root):
                return blocked("BLOCKED_CONCURRENT_GIT_STATE", "Git state changed before event recovery")
            if run_git(root, ["rev-parse", "--verify", "HEAD"]).stdout.strip() != head_before:
                return blocked("BLOCKED_HEAD_CHANGED", "HEAD changed before event recovery")

            now = now_utc()
            event_id = f"evt-{id_time(now)}-{uuid.uuid4().hex[:8]}"
            event_path = event_root / now.strftime("%Y") / now.strftime("%m") / f"{event_id}.json"
            event_dir = event_path.parent
            event_dir.mkdir(parents=True, exist_ok=True)
            if path_has_reparse_component(root, event_dir) or event_path.exists():
                return blocked("BLOCKED_REPARSE_PATH", "the new event destination is unsafe or occupied")
            evidence_rows = [
                {
                    "path": relative,
                    "sha256": hashlib.sha256(candidate_snapshot[relative]).hexdigest(),
                    "byte_length": len(candidate_snapshot[relative]),
                }
                for relative in relative_paths
            ]
            evidence_sha256 = canonical_json_sha256({
                "proposal_id": proposal_id,
                "producer_agent": producer,
                "files": evidence_rows,
            })
            recovered_at = iso_time(now)
            event = {
                "event_id": event_id,
                "occurred_at": recovered_at,
                "agent": producer,
                "action": "new-proposal",
                "proposal_ids": [proposal_id],
                "pages_changed": [],
                "source_ids": source_ids,
                "summary": f"Recovered missing new-proposal receipt for {proposal_id}; this is a new recovery event.",
                "recovery_provenance": {
                    "kind": "missing-new-proposal-event-recovery",
                    "recovered_at": recovered_at,
                    "recovered_by": local_machine_name(),
                    "repair_actor": "llm-wiki.checkpoint_intake",
                    "component": "checkpoint_intake",
                    "producer_agent": producer,
                    "proposal_evidence_sha256": evidence_sha256,
                    "evidence_files": evidence_rows,
                },
            }
            if path_has_reparse_component(root, event_dir):
                return blocked("BLOCKED_REPARSE_PATH", "the new event directory changed during recovery")
            write_json(event_path, event, exclusive=True)
            written = read_regular_file_snapshot(root, event_path)[0]
            if json.loads(written.decode("utf-8-sig", errors="strict")) != event:
                return blocked("BLOCKED_CONCURRENT_CONTENT_CHANGE", "recovery event did not round-trip")
            return {"status": "RECOVERED", "event": event, "path": event_path}
    except (GatewayError, OSError, UnicodeError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        return blocked("BLOCKED_PROPOSAL_EVENT_RECOVERY", f"event fence or write failed: {type(exc).__name__}")


def checkpoint_intake(
    vault: Path,
    proposal_id: str,
    *,
    recover_stale_lock: bool = False,
    recover_partial_pair: bool = False,
    retained_checkpoint_lock: Path | None = None,
) -> dict:
    policy = checkpoint_policy(vault)
    if not policy["enabled"]:
        return checkpoint_result("DISABLED", policy, proposal_id=proposal_id)
    if policy["mode"] != CHECKPOINT_MODE:
        return checkpoint_result(
            "BLOCKED_UNSUPPORTED_MODE",
            policy,
            proposal_id=proposal_id,
            reason=f"checkpoint_mode must be {CHECKPOINT_MODE}",
        )
    if policy.get("machine_identity_error"):
        return checkpoint_result(
            "BLOCKED_MACHINE_IDENTITY",
            policy,
            proposal_id=proposal_id,
            reason="Windows GetComputerNameW did not return a machine identity.",
        )
    if not policy["curator_machine"]:
        return checkpoint_result("BLOCKED_OWNER_NOT_CONFIGURED", policy, proposal_id=proposal_id)
    if not policy["owner_machine"]:
        return checkpoint_result("NOT_OWNER", policy, proposal_id=proposal_id)

    root = find_git_root(vault)
    if root is None:
        return checkpoint_result("BLOCKED_NO_GIT_ROOT", policy, proposal_id=proposal_id)
    try:
        root = root.resolve(strict=True)
        vault_relative = vault.resolve(strict=True).relative_to(root).as_posix()
    except (OSError, ValueError):
        return checkpoint_result("BLOCKED_VAULT_OUTSIDE_REPOSITORY", policy, proposal_id=proposal_id)
    manual_block = checkpoint_manual_block(root)
    if manual_block is not None:
        return checkpoint_result(
            "BLOCKED_MANUAL_RECOVERY",
            policy,
            proposal_id=proposal_id,
            manual_block_id=manual_block.get("block_id"),
            manual_status=manual_block.get("status"),
            manual_commit=manual_block.get("commit"),
        )
    filter_files = external_filter_attribute_files(root)
    if filter_files:
        return checkpoint_result(
            "BLOCKED_EXTERNAL_GIT_FILTER",
            policy,
            proposal_id=proposal_id,
            filter_file_count=len(filter_files),
        )

    proposal_path = proposal_path_for_id(vault, proposal_id)
    proposal_content = read_text(proposal_path)
    source_ids = fm_list(proposal_content, "source_ids")
    source_map = {
        str(metadata.get("source_id")): (metadata, metadata_path)
        for metadata, metadata_path in source_records(vault)
        if metadata
    }
    event_records: list[tuple[dict, Path]] = []
    event_root = vault / "events"
    for event_path in sorted(event_root.rglob("*.json")) if event_root.is_dir() else []:
        event_records.append((json.loads(read_text(event_path)), event_path))
    for event, event_path in event_records:
        if not isinstance(event, dict):
            return checkpoint_result(
                "BLOCKED_EVENT_INVALID", policy, proposal_id=proposal_id,
                reason=f"event is not a JSON object: {event_path}",
            )
        if any(
            not isinstance(event.get(field, []), list)
            or any(not isinstance(item, str) for item in event.get(field, []))
            for field in ("proposal_ids", "source_ids")
        ):
            return checkpoint_result(
                "BLOCKED_EVENT_INVALID", policy, proposal_id=proposal_id,
                reason=f"event identity lists are malformed: {event_path}",
            )

    proposal_events = [
        (event, path)
        for event, path in event_records
        if event.get("action") == "new-proposal"
        and proposal_id in (event.get("proposal_ids") or [])
        and sorted(event.get("source_ids") or []) == sorted(source_ids)
    ]
    if len(proposal_events) > 1 or (not proposal_events and not recover_partial_pair):
        return checkpoint_result(
            "BLOCKED_PROPOSAL_EVENT_CARDINALITY",
            policy,
            proposal_id=proposal_id,
            reason=f"expected one proposal event, found {len(proposal_events)}",
        )

    recovered_missing_proposal_event = not proposal_events
    candidate_paths: list[Path] = [proposal_path]
    receipt_event_ids: list[str] = []
    if proposal_events:
        candidate_paths.append(proposal_events[0][1])
        proposal_event = proposal_events[0][0]
        if str(proposal_event.get("event_id") or ""):
            receipt_event_ids.append(str(proposal_event["event_id"]))
    source_groups: list[list[Path]] = []
    for source_id in source_ids:
        if source_id not in source_map:
            return checkpoint_result("BLOCKED_UNKNOWN_SOURCE", policy, proposal_id=proposal_id, source_id=source_id)
        metadata, metadata_path = source_map[source_id]
        package = metadata_path.parent
        original_path = package / str(metadata.get("original_file") or "")
        if not original_path.is_file():
            return checkpoint_result("BLOCKED_MISSING_ORIGINAL", policy, proposal_id=proposal_id, source_id=source_id)
        package_files = sorted(path for path in package.iterdir() if path.is_file())
        if set(package_files) != {metadata_path, original_path}:
            return checkpoint_result("BLOCKED_SOURCE_PACKAGE_SHAPE", policy, proposal_id=proposal_id, source_id=source_id)
        if sha256_file(original_path) != metadata.get("sha256") or original_path.stat().st_size != metadata.get("byte_length"):
            return checkpoint_result("BLOCKED_SOURCE_INTEGRITY", policy, proposal_id=proposal_id, source_id=source_id)
        stage_events = [
            (event, path)
            for event, path in event_records
            if event.get("action") == "stage-source" and event.get("source_ids") == [source_id]
        ]
        if len(stage_events) != 1:
            return checkpoint_result(
                "BLOCKED_SOURCE_EVENT_CARDINALITY",
                policy,
                proposal_id=proposal_id,
                source_id=source_id,
                reason=f"expected one stage event, found {len(stage_events)}",
            )
        stage_event = stage_events[0][0]
        if str(stage_event.get("event_id") or ""):
            receipt_event_ids.append(str(stage_event["event_id"]))
        group = [metadata_path, original_path, stage_events[0][1]]
        source_groups.append(group)
        candidate_paths.extend(group)

    if recovered_missing_proposal_event:
        recovery = _recover_missing_critical_proposal_event(
            vault, root, proposal_id, proposal_path, proposal_content,
            source_ids, source_groups, source_map, event_records,
        )
        if recovery.get("status") != "RECOVERED":
            return checkpoint_result(
                recovery.get("status") or "BLOCKED_PROPOSAL_EVENT_RECOVERY",
                policy,
                proposal_id=proposal_id,
                reason=recovery.get("reason"),
            )
        proposal_event = recovery["event"]
        proposal_event_path = recovery["path"]
        proposal_events = [(proposal_event, proposal_event_path)]
        event_records.append((proposal_event, proposal_event_path))
        candidate_paths.append(proposal_event_path)
        receipt_event_ids.append(str(proposal_event["event_id"]))

    if any(path_has_reparse_component(root, path) for path in candidate_paths):
        return checkpoint_result("BLOCKED_REPARSE_PATH", policy, proposal_id=proposal_id)
    try:
        relative_paths = sorted(set(repository_relative_path(root, path) for path in candidate_paths))
    except GatewayError as exc:
        return checkpoint_result("BLOCKED_PATH_ESCAPE", policy, proposal_id=proposal_id, reason=str(exc))
    allowed_prefixes = (
        f"{vault_relative}/raw/sources/",
        f"{vault_relative}/proposals/",
        f"{vault_relative}/events/",
    )
    if any(not path.startswith(allowed_prefixes) for path in relative_paths):
        return checkpoint_result("BLOCKED_PATH_SCOPE", policy, proposal_id=proposal_id)
    if any(path_has_reparse_component(root, root / path) for path in relative_paths):
        return checkpoint_result("BLOCKED_REPARSE_PATH", policy, proposal_id=proposal_id)

    statuses = git_status_map(root, relative_paths)
    if set(statuses) != set(relative_paths):
        return checkpoint_result("BLOCKED_MISSING_CANDIDATE", policy, proposal_id=proposal_id)
    if git_operation_in_progress(root):
        return checkpoint_result("BLOCKED_GIT_OPERATION", policy, proposal_id=proposal_id)
    index_recovery = False
    recovered_staged_blobs: dict[str, str] = {}
    if not index_is_clean(root):
        if not recover_stale_lock:
            return checkpoint_result("BLOCKED_DIRTY_INDEX", policy, proposal_id=proposal_id)
        recovery_valid, recovery_reason, recovered_staged_blobs = _validate_partial_index_recovery(
            root, relative_paths
        )
        if not recovery_valid:
            return checkpoint_result(
                "BLOCKED_DIRTY_INDEX",
                policy,
                proposal_id=proposal_id,
                reason=recovery_reason,
            )
        index_recovery = True
    allowed_statuses = {"??", "IN", "STAGED"} if index_recovery else {"??", "IN"}
    if any(status not in allowed_statuses for status in statuses.values()):
        return checkpoint_result(
            "BLOCKED_UNSUPPORTED_PATH_STATUS",
            policy,
            proposal_id=proposal_id,
            path_status=statuses,
        )
    if index_recovery:
        staged_candidates = set(recovered_staged_blobs)
        if {path for path, status in statuses.items() if status == "STAGED"} != staged_candidates:
            return checkpoint_result(
                "BLOCKED_DIRTY_INDEX",
                policy,
                proposal_id=proposal_id,
                reason="staged candidate set changed during validation",
            )
        if any(statuses[path] != "??" for path in set(relative_paths) - staged_candidates):
            return checkpoint_result(
                "BLOCKED_DIRTY_INDEX",
                policy,
                proposal_id=proposal_id,
                reason="unstaged candidate remainder is not fully untracked",
            )
        changed_paths = list(relative_paths)
        rollback_paths = sorted(set(relative_paths) - staged_candidates)
    else:
        changed_paths = sorted(path for path, status in statuses.items() if status == "??")
        rollback_paths = changed_paths
    if not changed_paths:
        current_lock_path = git_internal_path(root, CHECKPOINT_LOCK_NAME)
        current_lock = acquire_checkpoint_lock(
            current_lock_path,
            policy,
            recover_stale_lock=recover_stale_lock,
        )
        if current_lock is None:
            return checkpoint_result("BLOCKED_CHECKPOINT_LOCK", policy, proposal_id=proposal_id)
        current_lock_id, current_lock_identity, stale_lock_recovered = current_lock
        try:
            manual_block = checkpoint_manual_block(root)
            if manual_block is not None:
                current_result = checkpoint_result(
                    "BLOCKED_MANUAL_RECOVERY",
                    policy,
                    proposal_id=proposal_id,
                    manual_block_id=manual_block.get("block_id"),
                    manual_status=manual_block.get("status"),
                    manual_commit=manual_block.get("commit"),
                )
            elif git_operation_in_progress(root) or not index_is_clean(root):
                current_result = checkpoint_result(
                    "BLOCKED_CONCURRENT_GIT_STATE",
                    policy,
                    proposal_id=proposal_id,
                )
            elif git_status_map(root, relative_paths) != statuses:
                current_result = checkpoint_result(
                    "BLOCKED_CONCURRENT_PATH_CHANGE",
                    policy,
                    proposal_id=proposal_id,
                )
            elif any(path_has_reparse_component(root, root / path) for path in relative_paths):
                current_result = checkpoint_result("BLOCKED_REPARSE_PATH", policy, proposal_id=proposal_id)
            else:
                current_snapshot = capture_candidate_bytes(root, relative_paths)
                verified, introducing_commit, reason = verify_current_checkpoint_receipt(
                    root,
                    relative_paths,
                    current_snapshot,
                )
                if not verified:
                    current_result = checkpoint_result(
                        "BLOCKED_CURRENT_UNPROVEN",
                        policy,
                        proposal_id=proposal_id,
                        commit=introducing_commit,
                        reason=reason,
                    )
                else:
                    current_result = checkpoint_result(
                        "CURRENT",
                        policy,
                        proposal_id=proposal_id,
                        commit=introducing_commit,
                        path_count=len(relative_paths),
                        receipt_path_count=len(relative_paths),
                        receipt_sha256=receipt_content_digest(current_snapshot),
                        stale_lock_recovered=stale_lock_recovered,
                    )
        except (GatewayError, OSError, subprocess.SubprocessError) as exc:
            current_result = checkpoint_result(
                "BLOCKED_CURRENT_UNPROVEN",
                policy,
                proposal_id=proposal_id,
                reason=str(exc),
            )
        released = remove_owned_checkpoint_lock(
            current_lock_path,
            current_lock_id,
            current_lock_identity,
        )
        if not released:
            return checkpoint_result(
                "BLOCKED_CHECKPOINT_LOCK_RELEASE",
                policy,
                proposal_id=proposal_id,
            )
        return current_result

    partial_recovery = recovered_missing_proposal_event
    proposal_group = {
        repository_relative_path(root, proposal_path),
        repository_relative_path(root, proposal_events[0][1]),
    }
    proposal_touched = proposal_group & set(changed_paths)
    source_partial = False
    for group in source_groups:
        group_paths = {repository_relative_path(root, path) for path in group}
        touched = group_paths & set(changed_paths)
        if touched and touched != group_paths:
            source_partial = True
            break
    is_partial = bool(proposal_touched and proposal_touched != proposal_group) or source_partial
    if recovered_missing_proposal_event and source_partial:
        return checkpoint_result(
            "BLOCKED_PARTIAL_RECOVERY_VALIDATION",
            policy,
            proposal_id=proposal_id,
            reason="missing proposal event cannot be combined with an incomplete source package",
        )
    if is_partial and not recovered_missing_proposal_event:
        if not recover_partial_pair:
            if proposal_touched and proposal_touched != proposal_group:
                return checkpoint_result("BLOCKED_PARTIAL_PROPOSAL", policy, proposal_id=proposal_id)
            return checkpoint_result("BLOCKED_PARTIAL_SOURCE", policy, proposal_id=proposal_id)
        valid, reason = _validate_partial_pair_recovery(
            root,
            statuses,
            proposal_path,
            proposal_events[0][1],
            source_groups,
        )
        if not valid:
            return checkpoint_result(
                "BLOCKED_PARTIAL_RECOVERY_VALIDATION",
                policy,
                proposal_id=proposal_id,
                reason=reason,
            )
        partial_recovery = True

    lint_result = lint(vault)
    if lint_result["error_count"]:
        return checkpoint_result(
            "BLOCKED_LINT",
            policy,
            proposal_id=proposal_id,
            lint_error_count=lint_result["error_count"],
            lint_warning_count=lint_result["warning_count"],
        )
    candidate_receipt_sha256 = receipt_content_digest(capture_candidate_bytes(root, relative_paths))
    retained_proof = None
    if index_recovery:
        if retained_checkpoint_lock is not None:
            retained_proof = _retained_checkpoint_lock_proof(
                root, retained_checkpoint_lock, policy, proposal_id, candidate_receipt_sha256,
                legacy_explicit=True,
            )
            if retained_proof is None:
                return checkpoint_result("BLOCKED_DIRTY_INDEX", policy, proposal_id=proposal_id,
                                         reason="unproven retained checkpoint lock")
        else:
            retained_proof = _find_retained_pair_lock(root, policy, proposal_id, candidate_receipt_sha256)
    lock_path = git_internal_path(root, CHECKPOINT_LOCK_NAME)
    acquired_lock = acquire_checkpoint_lock(
        lock_path,
        policy,
        recover_stale_lock=recover_stale_lock,
        intake_proposal_id=proposal_id,
        intake_receipt_sha256=candidate_receipt_sha256,
    )
    lock_fd: int | None = None
    owns_lock = acquired_lock is not None
    lock_id = acquired_lock[0] if acquired_lock is not None else ""
    lock_identity = acquired_lock[1] if acquired_lock is not None else None
    stale_lock_recovered = acquired_lock[2] if acquired_lock is not None else False
    staged = False
    commit_created = False
    commit_sha = ""
    head_before = ""
    try:
        if acquired_lock is None:
            return checkpoint_result("BLOCKED_CHECKPOINT_LOCK", policy, proposal_id=proposal_id)

        manual_block = checkpoint_manual_block(root)
        if manual_block is not None:
            return checkpoint_result(
                "BLOCKED_MANUAL_RECOVERY",
                policy,
                proposal_id=proposal_id,
                manual_block_id=manual_block.get("block_id"),
                manual_status=manual_block.get("status"),
                manual_commit=manual_block.get("commit"),
            )
        if index_recovery and not stale_lock_recovered and retained_proof is not None:
            rechecked = _retained_checkpoint_lock_proof(
                root, Path(retained_proof[0]), policy, proposal_id, candidate_receipt_sha256,
                legacy_explicit=retained_checkpoint_lock is not None,
            )
            if rechecked != retained_proof:
                return checkpoint_result("BLOCKED_CONCURRENT_GIT_STATE", policy, proposal_id=proposal_id)
        if index_recovery and not stale_lock_recovered and retained_proof is None:
            return checkpoint_result(
                "BLOCKED_DIRTY_INDEX",
                policy,
                proposal_id=proposal_id,
                reason="no proven abandoned same-host checkpoint lock",
            )
        if git_operation_in_progress(root):
            return checkpoint_result("BLOCKED_CONCURRENT_GIT_STATE", policy, proposal_id=proposal_id)
        if index_recovery:
            still_valid, _recheck_reason, current_staged_blobs = _validate_partial_index_recovery(
                root, relative_paths
            )
            if not still_valid or current_staged_blobs != recovered_staged_blobs:
                return checkpoint_result("BLOCKED_CONCURRENT_GIT_STATE", policy, proposal_id=proposal_id)
        elif not index_is_clean(root):
            return checkpoint_result("BLOCKED_CONCURRENT_GIT_STATE", policy, proposal_id=proposal_id)
        head_before = run_git(root, ["rev-parse", "--verify", "HEAD"]).stdout.strip()
        current_statuses = git_status_map(root, relative_paths)
        if current_statuses != statuses:
            return checkpoint_result("BLOCKED_CONCURRENT_PATH_CHANGE", policy, proposal_id=proposal_id)
        if any(path_has_reparse_component(root, root / path) for path in changed_paths):
            return checkpoint_result("BLOCKED_REPARSE_PATH", policy, proposal_id=proposal_id)
        snapshot_before = capture_candidate_bytes(root, changed_paths)
        lint_result = lint(vault)
        if lint_result["error_count"]:
            return checkpoint_result("BLOCKED_LINT", policy, proposal_id=proposal_id, lint_error_count=lint_result["error_count"])
        snapshot_after = capture_candidate_bytes(root, changed_paths)
        if snapshot_fingerprint(snapshot_before) != snapshot_fingerprint(snapshot_after):
            return checkpoint_result("BLOCKED_CONCURRENT_CONTENT_CHANGE", policy, proposal_id=proposal_id)
        if run_git(root, ["rev-parse", "--verify", "HEAD"]).stdout.strip() != head_before:
            return checkpoint_result("BLOCKED_HEAD_CHANGED", policy, proposal_id=proposal_id)

        expected_blobs: dict[str, str] = {}
        index_entries = []
        for relative in changed_paths:
            blob = write_git_blob(root, relative, snapshot_after[relative])
            expected_blobs[relative] = blob
            index_entries.append(f"100644 {blob}\t{relative}\0")
        # One Git index transaction for the whole validated pair. A worker
        # interruption between blob writes no longer leaves 1..4 paths staged.
        staged = True
        run_git(root, ["update-index", "-z", "--index-info"], input_text="".join(index_entries))
        cached_paths = sorted(
            path.replace("\\", "/")
            for path in run_git(root, ["diff", "--cached", "--name-only"]).stdout.splitlines()
            if path
        )
        if cached_paths != changed_paths:
            raise GatewayError("Checkpoint index scope differs from the validated append-only path set.")
        cached_status = [line for line in run_git(root, ["diff", "--cached", "--name-status"]).stdout.splitlines() if line]
        if any(not line.startswith("A\t") for line in cached_status):
            raise GatewayError("Checkpoint index contains a non-addition.")
        staged_entries = {}
        for line in run_git(root, ["ls-files", "--stage", "--", *changed_paths]).stdout.splitlines():
            metadata, relative = line.split("\t", 1)
            staged_entries[relative.replace("\\", "/")] = metadata.split()[1]
        if staged_entries != expected_blobs:
            raise GatewayError("Checkpoint index blob IDs differ from the validated byte snapshot.")
        if snapshot_fingerprint(capture_candidate_bytes(root, changed_paths)) != snapshot_fingerprint(snapshot_after):
            raise GatewayError("Checkpoint candidates changed after their validated blobs were staged.")
        if run_git(root, ["rev-parse", "--verify", "HEAD"]).stdout.strip() != head_before:
            raise GatewayError("Repository HEAD changed after checkpoint staging.")

        commit_message = f"knowledge: checkpoint {proposal_id}"
        receipt_path_count: int | None = None
        if partial_recovery:
            full_snapshot = capture_candidate_bytes(root, relative_paths)
            receipt_digest = receipt_content_digest(full_snapshot)
            receipt_event_ids: list[str] = []
            for event, _event_path in proposal_events:
                if str(event.get("event_id") or ""):
                    receipt_event_ids.append(str(event["event_id"]))
            for group in source_groups:
                _meta, _orig, stage_path = group
                for event, event_path in event_records:
                    if event_path == stage_path and str(event.get("event_id") or ""):
                        receipt_event_ids.append(str(event["event_id"]))
            receipt_path_count = len(relative_paths)
            commit_message = (
                f"knowledge: checkpoint {proposal_id}\n"
                f"\n"
                f"{RECEIPT_DIGEST_TRAILER}: {receipt_digest}\n"
                f"{RECEIPT_EVENTS_TRAILER}: {','.join(sorted(receipt_event_ids))}\n"
                f"{RECEIPT_PATHS_TRAILER}: {receipt_path_count}\n"
            )

        commit = run_git_without_hooks(
            root,
            [
                "commit",
                "--no-verify",
                "--no-gpg-sign",
                "-m",
                commit_message,
            ],
            timeout=60,
        )
        commit_created = True
        staged = False
        commit_sha = run_git(root, ["rev-parse", "HEAD"]).stdout.strip()
        committed_paths = sorted(
            path.replace("\\", "/")
            for path in run_git(root, ["show", "--format=", "--name-only", commit_sha]).stdout.splitlines()
            if path
        )
        committed_entries = {}
        for line in run_git(root, ["ls-tree", "-r", commit_sha, "--", *changed_paths]).stdout.splitlines():
            metadata, relative = line.split("\t", 1)
            committed_entries[relative.replace("\\", "/")] = metadata.split()[2]
        parent_sha = run_git(root, ["rev-parse", f"{commit_sha}^"]).stdout.strip()
        if committed_paths != changed_paths or committed_entries != expected_blobs or parent_sha != head_before:
            if rollback_checkpoint_commit(root, commit_sha, head_before, rollback_paths):
                return checkpoint_result(
                    "BLOCKED_POST_COMMIT_VERIFICATION_ROLLED_BACK",
                    policy,
                    proposal_id=proposal_id,
                    stale_lock_recovered=stale_lock_recovered,
                )
            manual_block = persist_checkpoint_manual_block(
                root,
                status="BLOCKED_POST_COMMIT_VERIFICATION_MANUAL",
                proposal_id=proposal_id,
                commit_sha=commit_sha,
                head_before=head_before,
                changed_paths=changed_paths,
                reason="Post-commit verification failed and automatic rollback did not complete.",
            )
            return checkpoint_result(
                "BLOCKED_POST_COMMIT_VERIFICATION_MANUAL",
                policy,
                proposal_id=proposal_id,
                commit=commit_sha,
                manual_block_id=manual_block.get("block_id"),
                manual_block_recorded=bool(manual_block.get("block_id")),
                stale_lock_recovered=stale_lock_recovered,
            )
        result_extra: dict[str, object] = {}
        if index_recovery:
            result_extra["staged_index_recovered"] = True
            result_extra["retained_lock_sha256"] = retained_proof[1] if retained_proof else None
        if partial_recovery:
            result_extra["partial_recovery"] = True
            result_extra["receipt_path_count"] = receipt_path_count
        return checkpoint_result(
            "CHECKPOINTED",
            policy,
            proposal_id=proposal_id,
            commit=commit_sha,
            path_count=len(changed_paths),
            git_summary=commit.stdout.splitlines()[0] if commit.stdout.splitlines() else "",
            lint_error_count=lint_result["error_count"],
            lint_warning_count=lint_result["warning_count"],
            stale_lock_recovered=stale_lock_recovered,
            **result_extra,
        )
    except (GatewayError, OSError, subprocess.SubprocessError) as exc:
        if commit_created:
            if not commit_sha:
                try:
                    commit_sha = run_git(root, ["rev-parse", "HEAD"]).stdout.strip()
                except (GatewayError, OSError, subprocess.SubprocessError):
                    commit_sha = ""
            if commit_sha and head_before and rollback_checkpoint_commit(root, commit_sha, head_before, rollback_paths):
                return checkpoint_result(
                    "BLOCKED_COMMIT_FAILURE_ROLLED_BACK",
                    policy,
                    proposal_id=proposal_id,
                    reason=str(exc),
                )
            manual_block = persist_checkpoint_manual_block(
                root,
                status="BLOCKED_COMMIT_FAILURE_MANUAL",
                proposal_id=proposal_id,
                commit_sha=commit_sha or None,
                head_before=head_before or None,
                changed_paths=changed_paths,
                reason=str(exc),
            )
            return checkpoint_result(
                "BLOCKED_COMMIT_FAILURE_MANUAL",
                policy,
                proposal_id=proposal_id,
                commit=commit_sha or None,
                reason=str(exc),
                manual_block_id=manual_block.get("block_id"),
                manual_block_recorded=bool(manual_block.get("block_id")),
            )
        if staged:
            if not unstage_checkpoint_paths(root, rollback_paths):
                manual_block = persist_checkpoint_manual_block(
                    root,
                    status="BLOCKED_COMMIT_FAILURE_MANUAL",
                    proposal_id=proposal_id,
                    commit_sha=None,
                    head_before=head_before or None,
                    changed_paths=changed_paths,
                    reason=str(exc),
                )
                return checkpoint_result(
                    "BLOCKED_COMMIT_FAILURE_MANUAL",
                    policy,
                    proposal_id=proposal_id,
                    commit=None,
                    reason=str(exc),
                    manual_block_id=manual_block.get("block_id"),
                    manual_block_recorded=bool(manual_block.get("block_id")),
                )
        return checkpoint_result("BLOCKED_COMMIT_FAILURE", policy, proposal_id=proposal_id, reason=str(exc))
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if owns_lock and lock_identity is not None:
            remove_owned_checkpoint_lock(lock_path, lock_id, lock_identity)


def bounded_completion_allowed(
    vault: Path, source_ids: list[str], agent: str
) -> tuple[bool, str]:
    if len(source_ids) != 1:
        return False, "blocked-tree completion requires exactly one staged source"
    source_id = source_ids[0]
    agent_name = slug(agent)
    selected: tuple[dict, Path] | None = None
    for metadata, metadata_path in source_records(vault):
        if metadata and str(metadata.get("source_id")) == source_id:
            selected = metadata, metadata_path
            break
    if selected is None:
        return False, "completion source is unknown"
    metadata, metadata_path = selected
    if str(metadata.get("captured_by") or "") != agent_name:
        return False, "completion source belongs to another agent"
    git_root = find_git_root(vault)
    if git_root is None:
        return False, "completion source is outside a Git worktree"
    changed_paths = git_uncommitted_paths(git_root)
    try:
        metadata_relative = metadata_path.resolve().relative_to(git_root.resolve()).as_posix()
    except ValueError:
        return False, "completion source escapes the Knowledge Hub worktree"
    if metadata_relative not in changed_paths:
        return False, "completion source is not an uncommitted in-flight source"
    for content, _ in proposal_records(vault):
        if source_id in fm_list(content, "source_ids"):
            return False, "completion source already has a proposal"
    event_root = vault / "events"
    for event_path in event_root.rglob("*.json") if event_root.is_dir() else []:
        try:
            event = json.loads(read_text(event_path))
            event_relative = event_path.resolve().relative_to(git_root.resolve()).as_posix()
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            continue
        if (
            event_relative in changed_paths
            and event.get("action") == "stage-source"
            and str(event.get("agent") or "") == agent_name
            and source_id in (event.get("source_ids") or [])
        ):
            return True, "uncommitted source and stage event belong to the same agent"
    return False, "completion source has no matching uncommitted stage event"


def assert_git_backlog_writable(
    vault: Path,
    operation: str,
    expected_additional_files: int = 0,
    completion_source_ids: list[str] | None = None,
    completion_agent: str = "",
) -> dict:
    assessment = git_dirty_assessment(find_git_root(vault))
    if assessment["state"] == "unavailable":
        raise GatewayError(
            f"Knowledge Hub write blocked for {operation} because Git hygiene is unavailable: "
            f"{assessment.get('reason') or 'unknown reason'}."
        )
    projected_count = int(assessment["changed_file_count"] or 0) + expected_additional_files
    count_would_block = projected_count >= int(assessment["policy"]["block_count"])
    oldest_age = assessment["oldest_age_hours"]
    age_would_block = oldest_age is not None and oldest_age >= assessment["policy"]["block_age_hours"]
    blocked = assessment["write_blocked"] or count_would_block or age_would_block
    completion_allowed = False
    completion_reason = "no bounded completion source was supplied"
    if blocked and completion_source_ids:
        completion_allowed, completion_reason = bounded_completion_allowed(
            vault, completion_source_ids, completion_agent
        )
    if blocked and not completion_allowed:
        raise GatewayError(
            f"Knowledge Hub write blocked for {operation}: dirty tree has "
            f"{assessment['changed_file_count']} changed files, oldest known age is "
            f"{assessment['oldest_age_hours']} h; {completion_reason}. "
            "Reconcile and commit the owner worktree before adding more knowledge."
        )
    return assessment


def collect_disposition_state(vault: Path) -> dict:
    """Raw ledger dispositions plus validated append-only resolution records.

    Pairs are the (curation event ID, disposition) tuples each proposal gathered
    across historical ledgers. A proposal with more than one pair is duplicated;
    one with more than one distinct disposition value is a raw conflict. Valid
    resolution records reconcile a proposal without touching the old ledgers:
    the raw history stays visible while the effective conflict clears. An
    unreconciled raw conflict never auto-selects a closed disposition - its
    effective disposition is unresolved so open matters stay open.
    """
    proposal_paths = sorted((vault / "proposals").rglob("*.md"))
    proposal_statuses = {
        str(fm_value(content, "id") or ""): fm_value(content, "status")
        for content in (read_text(path) for path in proposal_paths)
    }
    proposal_ids = set(proposal_statuses) - {""}
    source_ids = {str(metadata["source_id"]) for metadata, _ in source_records(vault)
                  if metadata and metadata.get("source_id")}
    events = [event for event, _ in readable_event_records(vault)
              if event.get("action") == "record-curation"]
    recorded_proposals = {item for event in events for item in event.get("proposal_ids", [])}
    recorded_sources = {item for event in events for item in event.get("source_ids", [])}
    accounted = proposal_ids & recorded_proposals
    accounted_sources = source_ids & recorded_sources
    event_index = {event.get("event_id"): event for event in events}
    dispositions: dict[str, dict[str, set[str]]] = {"proposals": {}, "sources": {}}
    pairs: dict[str, dict[str, set[tuple[str, str]]]] = {"proposals": {}, "sources": {}}
    allowed = DISPOSITION_VALUES
    ledger_count = 0
    for path in sorted((vault / "governance" / "curations").glob("*.json")):
        record = json.loads(read_regular_file_snapshot(vault, path)[0].decode("utf-8-sig"))
        if not isinstance(record, dict):
            raise GatewayError("Invalid curation ledger metadata.")
        ledger_event_id = record.get("event_id")
        event = event_index.get(ledger_event_id)
        if event is None:
            continue
        ledger = record.get("disposition_ledger", record)
        if not isinstance(ledger, dict):
            raise GatewayError("Invalid disposition ledger metadata.")
        ledger_count += 1
        for kind, id_key in (("proposals", "proposal_id"), ("sources", "source_id")):
            known_ids = accounted if kind == "proposals" else accounted_sources
            event_ids = set(event.get(id_key + "s", []))
            rows = ledger.get(kind, [])
            if not isinstance(rows, list):
                raise GatewayError("Invalid disposition ledger rows.")
            for row in rows:
                if not isinstance(row, dict):
                    raise GatewayError("Invalid disposition ledger row.")
                item_id, disposition = row.get(id_key), row.get("disposition")
                if item_id in known_ids and item_id in event_ids and disposition in allowed:
                    dispositions[kind].setdefault(item_id, set()).add(disposition)
                    if isinstance(ledger_event_id, str) and ledger_event_id:
                        pairs[kind].setdefault(item_id, set()).add((ledger_event_id, disposition))
    resolution_errors: list[str] = []
    valid_resolutions: dict[str, dict] = {}
    seen_resolution_ids: set[str] = set()
    tainted_proposals: set[str] = set()
    tainted_resolution_ids: set[str] = set()
    taint_all = False
    resolve_events = [event for event, _ in readable_event_records(vault)
                      if event.get("action") == "resolve-disposition"]
    resolutions_root = vault / "governance" / "resolutions"
    for path in sorted(resolutions_root.glob("*.json")) if resolutions_root.is_dir() else []:
        label = path.relative_to(vault).as_posix()
        if path_has_reparse_component(vault, path):
            resolution_errors.append(f"Resolution record is not a regular vault path: {label}")
            taint_all = True
            continue
        try:
            record = json.loads(read_regular_file_snapshot(vault, path)[0].decode("utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError, GatewayError) as exc:
            resolution_errors.append(f"Unreadable resolution record: {label} ({exc})")
            taint_all = True
            continue
        rid = record.get("resolution_id") if isinstance(record, dict) and isinstance(record.get("resolution_id"), str) else ""
        pid = record.get("proposal_id") if isinstance(record, dict) and isinstance(record.get("proposal_id"), str) else ""
        errors = validate_resolution_record(
            record,
            vault=vault,
            pairs=pairs["proposals"],
            path_label=label,
            seen_resolution_ids=seen_resolution_ids | tainted_resolution_ids,
            seen_proposals=set(valid_resolutions) | tainted_proposals,
            resolve_events=resolve_events,
        )
        if errors:
            resolution_errors.extend(errors)
            if pid:
                tainted_proposals.add(pid)
            else:
                taint_all = True
            if rid:
                tainted_resolution_ids.add(rid)
            continue
        seen_resolution_ids.add(rid)
        valid_resolutions[pid] = record
    # Fail-closed: any error for a proposal or resolution ID invalidates all its resolutions.
    for pid in list(valid_resolutions):
        rec = valid_resolutions[pid]
        if taint_all or pid in tainted_proposals or str(rec.get("resolution_id", "")) in tainted_resolution_ids:
            del valid_resolutions[pid]
    counts = {
        kind: {label: sum(values == {label} for values in rows.values()) for label in sorted(allowed)}
        for kind, rows in dispositions.items()
    }
    accepted = {item_id for item_id, state in proposal_statuses.items() if item_id and state == "accepted"}
    raw_conflict_ids = {item_id for item_id, values in dispositions["proposals"].items() if len(values) > 1}
    duplicated_ids = {item_id for item_id, rows in pairs["proposals"].items() if len(rows) > 1}
    resolved_conflict_ids = raw_conflict_ids & set(valid_resolutions)
    effective_unresolved = raw_conflict_ids - set(valid_resolutions)
    effective_dispositions: dict[str, str] = {}
    for item_id, values in dispositions["proposals"].items():
        if item_id in valid_resolutions:
            effective_dispositions[item_id] = str(valid_resolutions[item_id]["effective_disposition"])
        elif len(values) > 1:
            effective_dispositions[item_id] = "unresolved"
        else:
            effective_dispositions[item_id] = next(iter(values))
    effective_counts = {
        label: sum(value == label for value in effective_dispositions.values())
        for label in sorted(allowed)
    }
    return {
        "proposal_statuses": proposal_statuses,
        "proposal_ids": proposal_ids,
        "source_ids": source_ids,
        "events": events,
        "resolve_events": resolve_events,
        "recorded_proposals": recorded_proposals,
        "recorded_sources": recorded_sources,
        "accounted": accounted,
        "accounted_sources": accounted_sources,
        "dispositions": dispositions,
        "pairs": pairs,
        "ledger_count": ledger_count,
        "counts": counts,
        "accepted": accepted,
        "raw_conflict_ids": raw_conflict_ids,
        "duplicated_ids": duplicated_ids,
        "resolved_conflict_ids": resolved_conflict_ids,
        "effective_unresolved_conflict_ids": effective_unresolved,
        "valid_resolutions": valid_resolutions,
        "resolution_errors": resolution_errors,
        "effective_dispositions": effective_dispositions,
        "effective_counts": effective_counts,
    }


def _verify_resolution_evidence(vault: Path, path_str: str, expected_sha256: str,
                                label: str, path_label: str) -> list[str]:
    """Verify a portable citation to a staged, append-only shared source."""
    errors: list[str] = []
    parts = path_str.split("/")
    if (not path_str or Path(path_str).is_absolute() or "\\" in path_str
            or ":" in path_str or any(part in {"", ".", ".."} for part in parts)):
        errors.append(f"Non-portable {label} path: {path_label}")
        return errors
    if (len(parts) != 6 or parts[:2] != ["raw", "sources"]
            or not re.fullmatch(r"\d{4}", parts[2])
            or not re.fullmatch(r"\d{2}", parts[3])
            or not SOURCE_ID_PATTERN.fullmatch(parts[4])
            or not re.fullmatch(r"original\.[A-Za-z0-9]+", parts[5])):
        errors.append(f"Non-immutable {label} path (expected staged raw source): {path_label}")
        return errors
    evidence_path = vault.joinpath(*parts)
    metadata_path = evidence_path.parent / "source.json"
    try:
        content = read_regular_file_snapshot(vault, evidence_path, max_bytes=5 * 1024 * 1024)[0]
        metadata = json.loads(read_regular_file_snapshot(vault, metadata_path, max_bytes=64 * 1024)[0])
    except (OSError, GatewayError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"Missing or unsafe {label} file: {path_label} ({exc})")
        return errors
    if (not isinstance(metadata, dict) or metadata.get("source_id") != parts[4]
            or metadata.get("original_file") != parts[5]
            or metadata.get("sha256") != expected_sha256
            or metadata.get("classification") not in {"public", "internal"}
            or metadata.get("local_only") is not False):
        errors.append(f"Invalid staged-source metadata for {label}: {path_label}")
        return errors
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        errors.append(f"Forged {label} hash (content mismatch): {path_label}")
    return errors


def validate_resolution_record(
    record: object,
    *,
    vault: Path,
    pairs: dict[str, set[tuple[str, str]]],
    path_label: str,
    seen_resolution_ids: set[str],
    seen_proposals: set[str],
    resolve_events: list[dict],
) -> list[str]:
    """Fail-closed structural and referential checks for one written resolution."""
    errors: list[str] = []
    if not isinstance(record, dict) or set(record) != RESOLUTION_RECORD_KEYS:
        return [f"Resolution record schema mismatch: {path_label}"]
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        errors.append(f"Resolution record schema_version must be 1: {path_label}")
    resolution_id = record.get("resolution_id")
    if not isinstance(resolution_id, str) or not RESOLUTION_ID_PATTERN.fullmatch(resolution_id):
        errors.append(f"Invalid resolution ID: {path_label}")
    else:
        if Path(path_label).stem != resolution_id:
            errors.append(f"Resolution filename does not match resolution ID: {path_label}")
        if resolution_id in seen_resolution_ids:
            errors.append(f"Duplicate resolution ID: {resolution_id}")
    proposal_id = record.get("proposal_id")
    if not isinstance(proposal_id, str) or not PROPOSAL_ID_PATTERN.fullmatch(proposal_id):
        errors.append(f"Invalid resolution proposal ID: {path_label}")
    elif proposal_id in seen_proposals:
        errors.append(f"Duplicate resolution for proposal: {proposal_id}")
    elif proposal_id not in pairs:
        errors.append(f"Resolution proposal has no accounted disposition pairs: {path_label}")
    supersedes = record.get("supersedes")
    listed: set[tuple[str, str]] = set()
    if not isinstance(supersedes, list) or not supersedes:
        errors.append(f"Resolution supersedes must be a non-empty list: {path_label}")
        supersedes = []
    for row in supersedes:
        if not isinstance(row, dict) or set(row) != {"event_id", "disposition"}:
            errors.append(f"Resolution supersedes row schema mismatch: {path_label}")
            continue
        event_id, disposition = row.get("event_id"), row.get("disposition")
        if not isinstance(event_id, str) or not event_id or not isinstance(disposition, str):
            errors.append(f"Invalid resolution supersedes row: {path_label}")
            continue
        if disposition not in DISPOSITION_VALUES:
            errors.append(f"Invalid superseded disposition: {path_label}")
            continue
        pair = (event_id, disposition)
        if pair in listed:
            errors.append(f"Ambiguous duplicate supersedes pair {event_id}/{disposition}: {path_label}")
            continue
        listed.add(pair)
    known = pairs.get(proposal_id, set()) if isinstance(proposal_id, str) else set()
    if isinstance(proposal_id, str) and proposal_id in pairs and len(known) < 2:
        errors.append(f"Resolution requires at least two curation disposition pairs: {path_label}")
    if supersedes and known and listed != known:
        missing = sorted(known - listed)
        extra = sorted(listed - known)
        if missing:
            errors.append(f"Partial resolution supersedes (missing {missing}): {path_label}")
        if extra:
            errors.append(f"Stale or mismatched supersedes (unexpected {extra}): {path_label}")
    effective = record.get("effective_disposition")
    if not isinstance(effective, str) or effective not in DISPOSITION_VALUES:
        errors.append(f"Invalid effective disposition: {path_label}")
    reason = record.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        errors.append(f"Resolution reason must be non-empty text: {path_label}")
    else:
        risk = risk_assessment(reason)
        if risk["secret_hits"] or risk["injection_hits"]:
            errors.append(f"Unsafe resolution reason: {path_label}")
    evidence = record.get("evidence")
    if (not isinstance(evidence, dict) or set(evidence) != {"path", "sha256"}
            or not isinstance(evidence.get("path"), str) or not evidence.get("path")
            or not isinstance(evidence.get("sha256"), str)
            or not SHA256_HEX_PATTERN.fullmatch(evidence.get("sha256") or "")):
        errors.append(f"Invalid resolution evidence citation: {path_label}")
    else:
        errors.extend(_verify_resolution_evidence(vault, evidence["path"], evidence["sha256"], "resolution evidence", path_label))
    resolution_sha256 = record.get("resolution_sha256")
    if not isinstance(resolution_sha256, str) or not SHA256_HEX_PATTERN.fullmatch(resolution_sha256):
        errors.append(f"Invalid resolution document binding: {path_label}")
    else:
        payload = {key: record[key] for key in RESOLUTION_BINDING_FIELDS}
        if resolution_sha256 != resolution_binding_sha256(payload):
            errors.append(f"Forged resolution_sha256 (document content mismatch): {path_label}")
    review = record.get("review")
    agent = record.get("agent")
    if not isinstance(review, dict) or set(review) != RESOLUTION_REVIEW_KEYS:
        errors.append(f"Resolution review schema mismatch: {path_label}")
        review = {}
    else:
        if type(review.get("schema_version")) is not int or review["schema_version"] != 1 or review.get("verdict") != "PASS":
            errors.append(f"Unsupported resolution review schema or verdict: {path_label}")
        maker = review.get("maker")
        reviewer = review.get("reviewer")
        if not isinstance(maker, str) or not maker.strip():
            errors.append(f"Resolution review maker is required: {path_label}")
        if not isinstance(reviewer, dict) or set(reviewer) != {"id", "lineage"}:
            errors.append(f"Resolution reviewer schema mismatch: {path_label}")
        else:
            identity = reviewer.get("id")
            if not isinstance(identity, str) or not identity.strip() or not isinstance(reviewer.get("lineage"), str) or not reviewer.get("lineage"):
                errors.append(f"Resolution reviewer identity is required: {path_label}")
            elif isinstance(maker, str) and identity.casefold() in {maker.casefold(), str(agent or "").casefold()}:
                errors.append(f"Forged resolution review identity (reviewer must be distinct): {path_label}")
        digest = review.get("resolution_sha256")
        if not isinstance(digest, str) or not SHA256_HEX_PATTERN.fullmatch(digest):
            errors.append(f"Invalid resolution review binding: {path_label}")
        elif isinstance(resolution_sha256, str) and SHA256_HEX_PATTERN.fullmatch(resolution_sha256) and digest != resolution_sha256:
            errors.append(f"Forged resolution review binding (review not bound to this resolution): {path_label}")
        review_evidence = review.get("evidence")
        if (not isinstance(review_evidence, dict) or set(review_evidence) != {"path", "sha256"}
                or not isinstance(review_evidence.get("path"), str) or not review_evidence.get("path")
                or not isinstance(review_evidence.get("sha256"), str)
                or not SHA256_HEX_PATTERN.fullmatch(review_evidence.get("sha256") or "")):
            errors.append(f"Invalid resolution review evidence citation: {path_label}")
        else:
            errors.extend(_verify_resolution_evidence(vault, review_evidence["path"], review_evidence["sha256"], "review evidence", path_label))
    review_sha256 = record.get("review_sha256")
    if not isinstance(review_sha256, str) or not SHA256_HEX_PATTERN.fullmatch(review_sha256):
        errors.append(f"Invalid resolution review_sha256: {path_label}")
    elif review:
        try:
            if canonical_json_sha256(review) != review_sha256:
                errors.append(f"Forged resolution review_sha256 (review content mismatch): {path_label}")
        except (TypeError, ValueError):
            errors.append(f"Unserializable resolution review: {path_label}")
    recorded_at = record.get("recorded_at")
    if not isinstance(recorded_at, str) or parse_recorded_time(recorded_at) is None:
        errors.append(f"Invalid resolution recorded_at: {path_label}")
    elif recorded_at != iso_time(parse_recorded_time(recorded_at)):
        errors.append(f"Non-canonical resolution recorded_at: {path_label}")
    if not isinstance(agent, str) or not AGENT_PATTERN.fullmatch(agent):
        errors.append(f"Invalid resolution agent: {path_label}")
    event_id = record.get("event_id")
    if not isinstance(event_id, str) or not EVENT_ID_PATTERN.fullmatch(event_id):
        errors.append(f"Invalid resolution event ID: {path_label}")
    else:
        matching = [event for event in resolve_events if event.get("event_id") == event_id]
        if len(matching) != 1:
            errors.append(f"Resolution event cardinality mismatch: {path_label}")
        elif matching[0].get("resolution_id") != resolution_id or matching[0].get("proposal_ids") != [proposal_id]:
            errors.append(f"Resolution event does not reference this resolution: {path_label}")
    return errors


def curation_health(vault: Path) -> dict:
    state = collect_disposition_state(vault)
    dispositions = state["dispositions"]
    proposal_paths = sorted((vault / "proposals").rglob("*.md"))
    counts = state["counts"]
    conflicting = len(state["raw_conflict_ids"])
    effective_unresolved = len(state["effective_unresolved_conflict_ids"])
    return {
        "total_proposals": len(proposal_paths),
        "accounted_proposals": len(state["accounted"]),
        "pending_unaccounted": len(proposal_paths) - len(state["accounted"]),
        "record_curation_events": len(state["events"]),
        "record_curation_proposal_ids": len(state["recorded_proposals"]),
        "disposition_ledger_events": state["ledger_count"],
        "disposition_counts": counts,
        **{label + "_proposals": counts["proposals"][label] for label in sorted(DISPOSITION_VALUES)},
        "conflicting_disposition_proposals": conflicting,
        "raw_conflict_proposals": conflicting,
        "effective_unresolved_conflict_proposals": effective_unresolved,
        "resolved_conflict_proposals": len(state["resolved_conflict_ids"]),
        "duplicated_disposition_proposals": len(state["duplicated_ids"]),
        "resolved_disposition_proposals": len(state["valid_resolutions"]),
        "resolution_record_errors": len(state["resolution_errors"]),
        "resolve_disposition_events": len(state["resolve_events"]),
        "effective_disposition_counts": state["effective_counts"],
        "accounted_without_disposition": len(state["accounted"] - set(dispositions["proposals"])),
        "total_sources": len(state["source_ids"]),
        "accounted_sources": len(state["accounted_sources"]),
        "pending_unaccounted_sources": len(state["source_ids"] - state["accounted_sources"]),
        "accepted_proposals": len(state["accepted"]),
        "curated_accepted_proposals": len(state["accepted"] & state["accounted"]),
        "pending_proposals": sum(status == "proposed" for status in state["proposal_statuses"].values()),
        "uncurated_accepted_proposals": len(state["accepted"] - state["accounted"]),
        "coverage": round(len(state["accounted"]) / len(proposal_paths), 6) if proposal_paths else None,
        "accounted_implies_semantic_review": False,
        "basis": (
            "record-curation coverage and disposition ledger accounting; original proposal status is preserved; "
            "accounting does not prove semantic review or live verification; conflicting_disposition_proposals is "
            "raw history and is preserved; effective_unresolved_conflict_proposals excludes only valid reviewed "
            "append-only resolutions and never auto-selects a closed disposition for an open matter"
        ),
    }


MAX_CARETAKER_STATUS_BYTES = 256 * 1024
CARETAKER_THRESHOLD_MINUTES = 60


def resolve_caretaker_status_path(vault: Path) -> Path | None:
    # a) environment variable KNOWLEDGE_CARETAKER_STATUS_PATH
    env_path = os.environ.get("KNOWLEDGE_CARETAKER_STATUS_PATH")
    if env_path is not None:
        if not env_path.strip():
            return None
        try:
            candidate = Path(env_path.strip()).expanduser()
            if not candidate.is_absolute():
                return None
            candidate = candidate.resolve()
            if candidate.is_file():
                return candidate
        except (OSError, ValueError, RuntimeError):
            return None
        return None

    # b) selected config: AGENT_KNOWLEDGE_CONFIG override or installed default
    try:
        _override_path, _override_config = selected_knowledge_config()
    except GatewayError:
        return None
    except Exception:
        # A provided config binding that cannot even be selected (for example
        # a parser stack overflow on hostile input) must fail closed: never
        # reset the selection and fall through to another profile's config.
        if (os.environ.get("AGENT_KNOWLEDGE_CONFIG") or "").strip():
            return None
        _override_path, _override_config = None, None
    if _override_path is not None:
        _is_mapping = isinstance(_override_config, dict)
        _path_str = _override_config.get("caretaker_status_path") if _is_mapping else None
        if _is_mapping and "caretaker_status_path" in _override_config:
            if not isinstance(_path_str, str) or not _path_str.strip():
                return None
            try:
                _candidate = Path(_path_str.strip()).expanduser().resolve()
            except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
                return None
            return _candidate if _candidate.is_file() else None
        # A selected config binding without a usable caretaker_status_path
        # ends caretaker resolution here: never fall through to another
        # profile's home config or to the ancestor search below.
        return None
    try:
        config_path = Path.home() / ".agents" / "knowledge-config.json"
        if config_path.is_file():
            config = json.loads(read_text(config_path))
            if isinstance(config, dict):
                path_str = config.get("caretaker_status_path")
                if isinstance(path_str, str) and path_str.strip():
                    candidate = Path(path_str.strip()).expanduser().resolve()
                    if candidate.is_file():
                        return candidate
    except Exception:
        pass

    # c) default <vault>/../../../../openclaw-data/hub-caretaker/latest.json
    try:
        resolved = vault.resolve()
        if len(resolved.parents) >= 4:
            cand = resolved.parents[3] / "openclaw-data" / "hub-caretaker" / "latest.json"
            if cand.is_file():
                return cand
    except Exception:
        pass

    return None


def caretaker_freshness(vault: Path, now: dt.datetime | None = None) -> dict:
    unknown_default = {
        "state": "UNKNOWN",
        "age_minutes": None,
        "observed_at": None,
        "tick_status": None,
        "source_path": None,
        "threshold_minutes": CARETAKER_THRESHOLD_MINUTES,
    }
    try:
        if now is None:
            now_dt = now_utc()
        elif now.tzinfo is None:
            now_dt = now.replace(tzinfo=dt.timezone.utc)
        else:
            now_dt = now.astimezone(dt.timezone.utc)

        target_path = resolve_caretaker_status_path(vault)
        if target_path is None:
            return unknown_default

        source_path = str(target_path)
        unknown_with_path = {
            "state": "UNKNOWN",
            "age_minutes": None,
            "observed_at": None,
            "tick_status": None,
            "source_path": source_path,
            "threshold_minutes": CARETAKER_THRESHOLD_MINUTES,
        }

        try:
            stat_result = target_path.stat()
        except OSError:
            return unknown_default

        if stat_result.st_size > MAX_CARETAKER_STATUS_BYTES:
            return unknown_with_path

        try:
            with target_path.open("rb") as handle:
                raw_bytes = handle.read(MAX_CARETAKER_STATUS_BYTES + 1)
        except OSError:
            return unknown_with_path

        if len(raw_bytes) > MAX_CARETAKER_STATUS_BYTES or not raw_bytes:
            return unknown_with_path

        try:
            payload = json.loads(raw_bytes.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError):
            return unknown_with_path

        if not isinstance(payload, dict):
            return unknown_with_path

        raw_status = payload.get("status")
        tick_status = str(raw_status)[:40] if isinstance(raw_status, str) else None

        raw_observed = payload.get("observed_at")
        if raw_observed is None:
            mtime = stat_result.st_mtime
            observed_dt = dt.datetime.fromtimestamp(mtime, tz=dt.timezone.utc)
            observed_at_str = iso_time(observed_dt)
        elif isinstance(raw_observed, str):
            try:
                parsed = dt.datetime.fromisoformat(raw_observed.replace("Z", "+00:00"))
            except ValueError:
                return {**unknown_with_path, "tick_status": tick_status}
            if parsed.tzinfo is None:
                return {**unknown_with_path, "tick_status": tick_status}
            observed_dt = parsed.astimezone(dt.timezone.utc)
            observed_at_str = raw_observed
        else:
            return {**unknown_with_path, "tick_status": tick_status}

        delta_seconds = (now_dt - observed_dt).total_seconds()
        if delta_seconds < -300.0:
            return {**unknown_with_path, "tick_status": tick_status}

        age_minutes = round(max(0.0, delta_seconds / 60.0), 1)
        state = "FRESH" if age_minutes <= float(CARETAKER_THRESHOLD_MINUTES) else "STALE"

        return {
            "state": state,
            "age_minutes": age_minutes,
            "observed_at": observed_at_str,
            "tick_status": tick_status,
            "source_path": source_path,
            "threshold_minutes": CARETAKER_THRESHOLD_MINUTES,
        }
    except Exception:
        return unknown_default


def status(vault: Path, *, write_health_only: bool = False) -> dict:
    config = json.loads(read_text(vault / "knowledge-vault.json"))
    if write_health_only:
        git_assessment = git_dirty_assessment(find_git_root(vault))
        stage_admission = backlog_admission(git_assessment, STAGE_SOURCE_EXPECTED_ADDITIONAL_FILES)
        curator_lock_path = vault / "governance" / "curator.lock"
        curator_locked = os.path.lexists(curator_lock_path)
        # The caretaker's write gate only needs canonical Git admission and
        # curator metadata. Keep this as a separate opt-in response so the
        # default status contract and its full census remain unchanged.
        return {
            "action": "status",
            "gateway_mode": "owner-checkpoint-gated",
            "vault_path": str(vault),
            "curator_lock": curator_locked,
            "curator_lock_owner": curator_lock_owner_state(curator_lock_path) if curator_locked else None,
            "caretaker_freshness": caretaker_freshness(vault),
            "curator_machine": str(config.get("curator_machine") or ""),
            "curation_allowed": False,
            "machine": local_machine_name(),
            "git_root": git_assessment["git_root"],
            "git_change_count": git_assessment["change_count"],
            "git_changed_file_count": git_assessment["changed_file_count"],
            "dirty_tree_state": git_assessment["state"],
            "dirty_tree_oldest_age_hours": git_assessment["oldest_age_hours"],
            "dirty_tree_age_unknown_count": git_assessment["age_unknown_count"],
            "dirty_tree_write_blocked": git_assessment["write_blocked"],
            "dirty_tree_reason": git_assessment.get("reason"),
            "dirty_tree_policy": git_assessment["policy"],
            "stage_source_write_blocked": stage_admission["blocked"],
            "prospective_changed_file_count": stage_admission["prospective_changed_file_count"],
            "projected_changed_file_count": stage_admission["projected_changed_file_count"],
            "stage_source_write_blocked_reason": stage_admission["reason"],
        }
    proposals = list((vault / "proposals").rglob("*.md")) if (vault / "proposals").is_dir() else []
    wiki = [path for path in (vault / "wiki").rglob("*.md") if path.name != "index.md"] if (vault / "wiki").is_dir() else []
    events = list((vault / "events").rglob("*.json")) if (vault / "events").is_dir() else []
    git_assessment = git_dirty_assessment(find_git_root(vault))
    stage_admission = backlog_admission(git_assessment, STAGE_SOURCE_EXPECTED_ADDITIONAL_FILES)
    curator_lock_path = vault / "governance" / "curator.lock"
    curator_locked = os.path.lexists(curator_lock_path)
    return {
        "action": "status",
        "gateway_mode": "owner-checkpoint-gated",
        "vault_path": str(vault),
        "sources": sum(1 for metadata, _ in source_records(vault) if metadata),
        "proposals": len(proposals),
        "wiki_pages": len(wiki),
        "events": len(events),
        "curator_lock": curator_locked,
        "curator_lock_owner": curator_lock_owner_state(curator_lock_path) if curator_locked else None,
        "caretaker_freshness": caretaker_freshness(vault),
        "curator_machine": str(config.get("curator_machine") or ""),
        "curation_allowed": False,
        "machine": local_machine_name(),
        "git_root": git_assessment["git_root"],
        "git_change_count": git_assessment["change_count"],
        "git_changed_file_count": git_assessment["changed_file_count"],
        "dirty_tree_state": git_assessment["state"],
        "dirty_tree_oldest_age_hours": git_assessment["oldest_age_hours"],
        "dirty_tree_age_unknown_count": git_assessment["age_unknown_count"],
        "dirty_tree_write_blocked": git_assessment["write_blocked"],
        "dirty_tree_reason": git_assessment.get("reason"),
        "dirty_tree_policy": git_assessment["policy"],
        "stage_source_write_blocked": stage_admission["blocked"],
        "prospective_changed_file_count": stage_admission["prospective_changed_file_count"],
        "projected_changed_file_count": stage_admission["projected_changed_file_count"],
        "stage_source_write_blocked_reason": stage_admission["reason"],
        "curation_health": curation_health(vault),
    }


def _event_write_fence(vault: Path):
    """Shared native writer fence from the reviewed sibling module.

    Loaded by absolute sibling path so the gateway works both as a script and
    as a spec-loaded module. A missing fence module is a hard error, never a
    silent skip: the canonical publisher relies on contributor cooperation.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "event_write_lock", Path(__file__).resolve().parent / "event_write_lock.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.event_write_fence(vault)


def write_event(vault: Path, agent: str, action: str, summary: str, source_ids: list[str] | None = None, proposal_ids: list[str] | None = None) -> Path:
    assert_safe("Event summary", summary)
    agent_name = slug(agent)
    if agent_name == "unknown":
        raise GatewayError("Event agent must be identified.")
    # The fence is held only for the identity choice and exclusive create, so a
    # canonical publisher mid-commit sees either the whole event or nothing.
    with _event_write_fence(vault):
        now = now_utc()
        event_id = f"evt-{id_time(now)}-{uuid.uuid4().hex[:8]}"
        path = vault / "events" / now.strftime("%Y") / now.strftime("%m") / f"{event_id}.json"
        write_json(path, {
            "event_id": event_id,
            "occurred_at": iso_time(now),
            "agent": agent_name,
            "action": action,
            "proposal_ids": proposal_ids or [],
            "pages_changed": [],
            "source_ids": source_ids or [],
            "summary": summary,
        }, exclusive=True)
    return path


def stage_source(vault: Path, args: argparse.Namespace) -> dict:
    if not args.agent.strip() or slug(args.agent) == "unknown":
        raise GatewayError("stage-source requires an identified --agent.")
    if "\n" in args.title or "\r" in args.title:
        raise GatewayError("Source title must be one line.")
    assert_safe("Source title", args.title)
    assert_safe("Source origin", args.origin)
    source = Path(args.path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise GatewayError("stage-source accepts one file, not a directory.")
    if args.classification in {"confidential", "restricted"} and not args.local_only:
        raise GatewayError(f"{args.classification} sources require --local-only.")
    project_slug = getattr(args, "project_slug", "") or ""
    state_summary = getattr(args, "state_summary", "") or ""
    knowledge_kind = getattr(args, "knowledge_kind", "general") or "general"
    structured_source = bool(project_slug or state_summary or knowledge_kind != "general")
    if structured_source:
        project_slug = canonical_project_slug(project_slug)
        state_summary = one_line_state_summary(state_summary)
        if knowledge_kind not in KNOWLEDGE_KINDS:
            raise GatewayError(f"Unsupported knowledge kind: {knowledge_kind}")
    digest = sha256_file(source)
    if not args.local_only:
        for metadata, metadata_path in source_records(vault):
            same_payload = bool(
                metadata
                and metadata.get("sha256") == digest
                and metadata.get("classification") == args.classification
            )
            same_structured_identity = bool(
                not structured_source
                or (
                    metadata.get("project_slug") == project_slug
                    and metadata.get("knowledge_kind") == knowledge_kind
                    and metadata.get("state_summary") == state_summary
                )
            ) if metadata else False
            if same_payload and same_structured_identity:
                return {"action": "stage-source", "duplicate": True, "source_id": metadata.get("source_id"), "metadata_path": str(metadata_path)}
        assert_git_backlog_writable(vault, "stage-source", expected_additional_files=4)
    extension = source.suffix.lower()
    risk_flags: list[str] = []
    if extension in TEXT_EXTENSIONS:
        if source.stat().st_size > 5 * 1024 * 1024:
            raise GatewayError("Text source exceeds the 5 MB safety scan limit.")
        risk = risk_assessment(read_text(source))
        if risk["secret_hits"]:
            raise GatewayError(f"Secret indicators detected: {', '.join(risk['secret_hits'])}.")
        risk_flags.extend(risk["injection_hits"])
    else:
        if not args.local_only:
            raise GatewayError("Binary sources cannot enter shared Git; use --local-only.")
        risk_flags.append("binary-content-not-text-scanned-local-only")
    now = now_utc()
    source_id = f"src-{id_time(now)}-{slug(args.title)}-{digest[:8]}"
    base = Path(".local/private-sources") if args.local_only else Path("raw/sources")
    package = vault / base / now.strftime("%Y") / now.strftime("%m") / source_id
    original_name = "original" + extension
    original_path = package / original_name
    metadata_path = package / "source.json"
    event_path = None
    preserve_artifacts = bool(getattr(args, "preserve_artifacts_on_error", False))
    try:
        package.mkdir(parents=True, exist_ok=False)
        shutil.copy2(source, original_path)
        if sha256_file(original_path) != digest:
            raise GatewayError("Source changed while it was being staged; no package was retained.")
        metadata = {
            "schema_version": 1,
            "source_id": source_id,
            "title": args.title,
            "origin": args.origin,
            "captured_at": iso_time(now),
            "captured_by": slug(args.agent),
            "classification": args.classification,
            "local_only": bool(args.local_only),
            "original_file": original_name,
            "media_type": extension.lstrip("."),
            "byte_length": source.stat().st_size,
            "sha256": digest,
            "trust": "untrusted",
            "risk_flags": sorted(set(risk_flags)),
            "reviewed_by": None,
            "reviewed_at": None,
        }
        if structured_source:
            metadata.update(
                {
                    "project_slug": project_slug,
                    "knowledge_kind": knowledge_kind,
                    "state_summary": state_summary,
                }
            )
        write_json(metadata_path, metadata)
        if not args.local_only:
            event_path = write_event(vault, args.agent, "stage-source", f"Staged source: {args.title}", source_ids=[source_id])
    except Exception as exc:
        if preserve_artifacts:
            setattr(exc, "_record_handoff_artifacts", {
                "kind": "source",
                "agent": slug(args.agent),
                "source_id": source_id,
                "source_package_path": str(package),
                "source_metadata_path": str(metadata_path),
                "source_original_path": str(original_path),
                "source_event_path": str(event_path) if event_path else None,
            })
        elif package.is_dir():
            shutil.rmtree(package)
        raise
    return {
        "action": "stage-source", "duplicate": False, "source_id": source_id,
        "metadata_path": str(metadata_path), "original_path": str(original_path),
        "event_path": str(event_path) if event_path else None,
        "risk_flags": sorted(set(risk_flags)), "local_only": bool(args.local_only),
    }


def new_proposal(vault: Path, args: argparse.Namespace, *, completion_body: str | None = None) -> dict:
    if not args.agent.strip() or slug(args.agent) == "unknown":
        raise GatewayError("new-proposal requires an identified --agent.")
    if "\n" in args.title or "\r" in args.title:
        raise GatewayError("Proposal title must be one line.")
    assert_safe("Proposal title", args.title)
    if args.classification in {"confidential", "restricted"}:
        raise GatewayError("Confidential and restricted proposals require a separately approved vault.")
    known = {str(metadata.get("source_id")) for metadata, _ in source_records(vault) if metadata}
    source_ids = sorted(set(args.source_id or []))
    for source_id in source_ids:
        if source_id not in known:
            raise GatewayError(f"Unknown source ID: {source_id}")
    agent_name = slug(args.agent)
    assert_git_backlog_writable(
        vault,
        "new-proposal",
        expected_additional_files=2,
        completion_source_ids=source_ids,
        completion_agent=agent_name,
    )
    now = now_utc()
    proposal_id = f"prop-{id_time(now)}-{uuid.uuid4().hex[:8]}"
    path = vault / "proposals" / agent_name / f"{proposal_id}-{slug(args.title)}.md"
    source_block = "source_ids: []" if not source_ids else "source_ids:\n" + "\n".join(f"  - {item}" for item in source_ids)
    safe_title = yaml_single_quoted(args.title)
    project_slug = getattr(args, "project_slug", "") or ""
    state_summary = getattr(args, "state_summary", "") or ""
    knowledge_kind = getattr(args, "knowledge_kind", "general") or "general"
    target_pages = sorted(
        set(canonical_target_page(item) for item in (getattr(args, "target_page", []) or []))
    )
    structured_handoff = bool(project_slug or state_summary or knowledge_kind != "general")
    structured_block = ""
    if structured_handoff:
        project_slug = canonical_project_slug(project_slug)
        state_summary = one_line_state_summary(state_summary)
        if knowledge_kind != "critical-state":
            raise GatewayError("Structured handoff proposals must use knowledge_kind=critical-state.")
        structured_block = (
            f"project_slug: {project_slug}\n"
            f"knowledge_kind: {knowledge_kind}\n"
            f"state_summary: {yaml_single_quoted(state_summary)}\n"
        )
    target_block = (
        "target_pages: []"
        if not target_pages
        else "target_pages:\n" + "\n".join(f"  - {item}" for item in target_pages)
    )
    if structured_handoff:
        source_for_claim = source_ids[0] if source_ids else "none"
        safe_claim = state_summary.replace("|", "\\|")
        proposed_changes = (
            "\n".join(f"- `{item}`: verify and incorporate the critical state change." for item in target_pages)
            if target_pages
            else "- Curator must resolve or create the appropriate stable project page."
        )
        proposal_body = f"""## Intent

Record an evidence-backed critical project-state handoff for cross-agent continuity.

## Atomic claims

| Claim | Source ID | Exact location | Confidence |
|---|---|---|---|
| {safe_claim} | {source_for_claim} | completion note | high |

## Proposed page changes

{proposed_changes}

## Contradictions and uncertainty

- Pending curator verification; this proposal is not stable wiki authority.

## Security review

- Embedded instructions ignored: yes
- Secret indicators: none detected by the gateway
- External action requested by source: none
"""
    else:
        proposal_body = """## Intent

Describe the durable knowledge change and why it matters.

## Atomic claims

| Claim | Source ID | Exact location | Confidence |
|---|---|---|---|
| TODO | TODO | TODO | draft |

## Proposed page changes

- TODO

## Contradictions and uncertainty

- TODO

## Security review

- Embedded instructions ignored: TODO
- Secret indicators: TODO
- External action requested by source: TODO
"""
    if completion_body is not None:
        if structured_handoff:
            raise GatewayError("Completion body cannot replace a critical-state handoff.")
        assert_safe("Completion proposal", completion_body)
        proposal_body = completion_body
    content = f"""---
id: {proposal_id}
type: proposal
status: proposed
title: {safe_title}
agent: {agent_name}
created_at: {iso_time(now)}
classification: {args.classification}
{source_block}
{structured_block}{target_block}
---

# {args.title}

{proposal_body}"""
    preserve_artifacts = bool(getattr(args, "preserve_artifacts_on_error", False))
    proposal_written = False
    event_path = None
    try:
        write_text(path, content, exclusive=True)
        proposal_written = True
        event_path = write_event(vault, agent_name, "new-proposal", f"Created proposal: {args.title}", source_ids=source_ids, proposal_ids=[proposal_id])
    except Exception as exc:
        if preserve_artifacts:
            setattr(exc, "_record_handoff_artifacts", {
                "kind": "proposal",
                "agent": agent_name,
                "source_ids": list(source_ids),
                "proposal_id": proposal_id,
                "proposal_path": str(path),
                "proposal_event_path": str(event_path) if event_path else None,
            })
        elif proposal_written:
            path.unlink(missing_ok=True)
        raise
    result = {
        "action": "new-proposal",
        "proposal_id": proposal_id,
        "proposal_path": str(path),
        "event_path": str(event_path),
        "agent": agent_name,
    }
    if not bool(getattr(args, "defer_checkpoint", False)):
        lint_result = lint(vault)
        if lint_result["error_count"]:
            policy = checkpoint_policy(vault)
            result["checkpoint"] = checkpoint_result(
                "BLOCKED_LINT",
                policy,
                proposal_id=proposal_id,
                lint_error_count=lint_result["error_count"],
                lint_warning_count=lint_result["warning_count"],
            )
        else:
            result["checkpoint"] = checkpoint_intake(vault, proposal_id, recover_stale_lock=True, recover_partial_pair=True)
    return result


def proposal_records(vault: Path) -> list[tuple[str, Path]]:
    root = vault / "proposals"
    records: list[tuple[str, Path]] = []
    for path in sorted(root.rglob("*.md")) if root.is_dir() else []:
        try:
            records.append((read_text(path), path))
        except (OSError, UnicodeError):
            continue
    return records


def readable_event_records(vault: Path) -> list[tuple[dict, Path]]:
    root = vault / "events"
    records: list[tuple[dict, Path]] = []
    for path in sorted(root.rglob("*.json")) if root.is_dir() else []:
        try:
            records.append((json.loads(read_text(path)), path))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
    return records


def _collect_known_project_slugs(vault: Path) -> set[str]:
    candidates: set[str] = set()

    def add(candidate: str | None) -> None:
        value = str(candidate or "")
        if PROJECT_SLUG_PATTERN.fullmatch(value) and slug(value) == value:
            candidates.add(value)

    project_root = vault / "wiki" / "projects"
    seen_projects: set[str] = set()
    for path, content, _complete in _wiki_prefix_rows(vault, strict=False):
        explicit = fm_value(content, "project_slug")
        if path.parent == project_root:
            seen_projects.add(_path_key(path))
            if explicit or fm_value(content, "type") != "evidence-ledger":
                add(explicit or path.stem)
        add(explicit)
    if project_root.is_dir():
        for path in project_root.glob("*.md"):
            if _path_key(path) in seen_projects:
                continue
            try:
                content = read_text(path)
            except (OSError, UnicodeError):
                continue
            explicit = fm_value(content, "project_slug")
            if explicit or fm_value(content, "type") != "evidence-ledger":
                add(explicit or path.stem)
    for content, _path, _complete in _proposal_prefix_rows(vault):
        add(fm_value(content, "project_slug"))
    for metadata, _path in _source_metadata_index(vault):
        if metadata:
            add(str(metadata.get("project_slug") or ""))
    return candidates


def known_project_slugs(vault: Path) -> set[str]:
    scope = _active_scope(vault)
    key = _path_key(vault)
    if scope is not None and key in scope["known_slugs"]:
        return set(scope["known_slugs"][key])
    candidates = _collect_known_project_slugs(vault)
    if scope is not None:
        scope["known_slugs"][key] = set(candidates)
        scope["counts"]["known_slug_builds"] += 1
    return candidates


def load_project_registry(vault: Path) -> tuple[dict, str | None]:
    """Read the curator's navigation map without making it a gateway dependency."""
    root = vault.resolve()
    path = root / "wiki" / "registry.md"
    risk_warning = "Project registry has risk indicators; using existing project resolution."
    try:
        if not path.exists():
            return {}, "Project registry is missing."
        snapshot, _ = read_regular_file_snapshot(root, path, max_bytes=2 * 1024 * 1024)
        content = snapshot.decode("utf-8-sig")
    except (OSError, UnicodeError, GatewayError):
        return {}, "Project registry cannot be read."
    if any(risk_assessment(content).values()):
        return {}, risk_warning
    block = re.search(r"^```json[ \t]*\r?\n(.*?)^```[ \t]*\r?$", content, re.MULTILINE | re.DOTALL)
    if block is None:
        return {}, "Project registry has no JSON block."
    try:
        registry = json.loads(block.group(1))
        # JSON escapes can hide indicators in the raw page but reveal them in output.
        values = [registry]
        while values:
            value = values.pop()
            if isinstance(value, str) and any(risk_assessment(value).values()):
                return {}, risk_warning
            if isinstance(value, dict):
                values.extend(value.keys())
                values.extend(value.values())
            elif isinstance(value, list):
                values.extend(value)
        if not isinstance(registry, dict) or type(registry.get("schema_version")) is not int or registry["schema_version"] != 1:
            raise ValueError("schema_version")
        navigation_page = registry.get("navigation_page")
        if not isinstance(navigation_page, str):
            raise ValueError("navigation_page")
        registry["navigation_page"] = canonical_target_page(navigation_page)
        projects, aliases, families = registry.get("projects"), registry.get("aliases"), registry.get("families")
        if not isinstance(projects, dict) or not isinstance(aliases, dict) or not isinstance(families, list):
            raise ValueError("registry sections")
        for family in families:
            if (not isinstance(family, dict) or not isinstance(family.get("id"), str)
                    or not isinstance(family.get("title"), str) or not isinstance(family.get("projects"), list)
                    or any(not isinstance(project, str) for project in family["projects"])):
                raise ValueError("family")
        for project, entry in projects.items():
            if (not isinstance(project, str) or not PROJECT_SLUG_PATTERN.fullmatch(project)
                    or slug(project) != project or not isinstance(entry, dict)
                    or not {"title", "family", "main_page", "latest_page", "aliases", "related", "folders", "current_state_source"} <= entry.keys()
                    or not isinstance(entry.get("title"), str) or not isinstance(entry.get("family"), str)):
                raise ValueError("project")
            for field in ("aliases", "related", "folders"):
                if not isinstance(entry.get(field), list) or any(not isinstance(item, str) for item in entry[field]):
                    raise ValueError(field)
            for field in ("main_page", "latest_page"):
                page = entry.get(field)
                if page is not None:
                    if not isinstance(page, str):
                        raise ValueError(field)
                    entry[field] = canonical_target_page(page)
            if entry.get("current_state_source") is not None and not isinstance(entry["current_state_source"], str):
                raise ValueError("current_state_source")
        for alias, project in aliases.items():
            if (not isinstance(alias, str) or not PROJECT_SLUG_PATTERN.fullmatch(alias)
                    or slug(alias) != alias or alias in projects or project not in projects):
                raise ValueError("alias")
    except (ValueError, TypeError, GatewayError, json.JSONDecodeError):
        return {}, "Project registry is invalid; using existing project resolution."
    return registry, None


def registry_project_slugs(registry: dict, project_slug: str) -> set[str]:
    """Only proven aliases share a registered project's context."""
    canonical = registry.get("aliases", {}).get(project_slug, project_slug)
    if canonical not in registry.get("projects", {}):
        return {project_slug}
    return {canonical, *(alias for alias, target in registry["aliases"].items() if target == canonical)}


def project_related_candidates(vault: Path, normalized: str) -> list[str]:
    compact = compact_project_key(normalized)
    return sorted(
        candidate
        for candidate in known_project_slugs(vault)
        if compact_project_key(candidate) == compact
    )


def resolve_project_reference(vault: Path, value: str) -> tuple[str, str, str]:
    requested, normalized = normalized_project_reference(value)
    registry, _ = load_project_registry(vault)
    if normalized in registry.get("aliases", {}):
        return requested, registry["aliases"][normalized], "registry-alias"
    if normalized in registry.get("projects", {}):
        return requested, normalized, "exact"
    known = known_project_slugs(vault)
    if requested == normalized and normalized in known:
        return requested, normalized, "exact"
    if normalized in known:
        return requested, normalized, "unique-normalized-alias"

    compact = compact_project_key(normalized)
    matches = project_related_candidates(vault, normalized)
    if len(matches) == 1:
        return requested, matches[0], "unique-normalized-alias"
    if len(matches) > 1:
        raise GatewayError(
            f"Ambiguous project reference '{requested}' matches: {', '.join(matches)}. Use the exact canonical slug."
        )
    return requested, normalized, "unresolved-normalized"


_CRITICAL_EVENT_LOOKUP_TOKEN = object()


def critical_handoff_event_lookup(event_records: list[tuple[dict, Path]]) -> dict:
    """Index one already-loaded event list for a single lint or integrity call.

    The result is not a cache: it stays valid only for this list object and length.
    Malformed proposal or stage fields stay in fallback lists and use the original
    membership predicates. Duplicate records are kept; a repeated proposal id on
    one event is stored once.
    """
    new_by_proposal: dict[str, list[int]] = {}
    stage_by_source: dict[str, list[int]] = {}
    fallback_new: list[int] = []
    fallback_stage: list[int] = []
    for index, (event, _path) in enumerate(event_records):
        if not isinstance(event, dict):
            fallback_new.append(index)
            fallback_stage.append(index)
            continue
        action = event.get("action")
        if action == "new-proposal":
            proposal_ids = event.get("proposal_ids")
            if isinstance(proposal_ids, list) and all(isinstance(item, str) for item in proposal_ids):
                for proposal_id in dict.fromkeys(proposal_ids):
                    new_by_proposal.setdefault(proposal_id, []).append(index)
            else:
                fallback_new.append(index)
        elif action == "stage-source":
            source_ids = event.get("source_ids")
            if (
                isinstance(source_ids, list)
                and len(source_ids) == 1
                and isinstance(source_ids[0], str)
            ):
                stage_by_source.setdefault(source_ids[0], []).append(index)
            else:
                fallback_stage.append(index)
    return {
        "_token": _CRITICAL_EVENT_LOOKUP_TOKEN,
        "_records": event_records,
        "_records_len": len(event_records),
        "new_by_proposal": {key: tuple(value) for key, value in new_by_proposal.items()},
        "stage_by_source": {key: tuple(value) for key, value in stage_by_source.items()},
        "fallback_new": tuple(fallback_new),
        "fallback_stage": tuple(fallback_stage),
    }


def _reusable_critical_event_lookup(event_lookup: object, event_records: list) -> bool:
    return (
        isinstance(event_lookup, dict)
        and event_lookup.get("_token") is _CRITICAL_EVENT_LOOKUP_TOKEN
        and event_lookup.get("_records") is event_records
        and event_lookup.get("_records_len") == len(event_records)
    )


def _critical_new_proposal_events(lookup: dict, proposal_id: str, source_ids: list[str]) -> list[dict]:
    records = lookup["_records"]
    matched: list[dict] = []
    positions = lookup["new_by_proposal"].get(proposal_id, ())
    for index in (*positions, *lookup["fallback_new"]):
        event = records[index][0]
        if (
            event.get("action") == "new-proposal"
            and proposal_id in (event.get("proposal_ids") or [])
            and sorted(event.get("source_ids") or []) == source_ids
        ):
            matched.append(event)
    return matched


def _critical_stage_source_events(lookup: dict, source_id: str) -> list[dict]:
    records = lookup["_records"]
    matched: list[dict] = []
    for index in (*lookup["stage_by_source"].get(source_id, ()), *lookup["fallback_stage"]):
        event = records[index][0]
        if event.get("action") == "stage-source" and event.get("source_ids") == [source_id]:
            matched.append(event)
    return matched


def critical_handoff_integrity(
    vault: Path,
    proposal_content: str,
    *,
    sources: dict[str, tuple[dict, Path]] | None = None,
    events: list[tuple[dict, Path]] | None = None,
    event_lookup: dict | None = None,
) -> dict:
    source_index = sources or {
        str(metadata.get("source_id")): (metadata, path)
        for metadata, path in source_records(vault)
        if metadata
    }
    event_records = events if events is not None else readable_event_records(vault)
    if not _reusable_critical_event_lookup(event_lookup, event_records):
        event_lookup = critical_handoff_event_lookup(event_records)
    proposal_id = str(fm_value(proposal_content, "id") or "")
    project_slug = str(fm_value(proposal_content, "project_slug") or "")
    state_summary = str(fm_value(proposal_content, "state_summary") or "")
    agent = str(fm_value(proposal_content, "agent") or "")
    source_ids = sorted(set(fm_list(proposal_content, "source_ids")))
    issues: list[str] = []

    if not source_ids:
        issues.append("missing-source")
    proposal_events = _critical_new_proposal_events(event_lookup, proposal_id, source_ids)
    if len(proposal_events) != 1:
        issues.append(f"proposal-event-cardinality:{len(proposal_events)}")

    for source_id in source_ids:
        source_record = source_index.get(source_id)
        if source_record is None:
            issues.append(f"missing-source:{source_id}")
            continue
        metadata, metadata_path = source_record
        if (
            metadata.get("project_slug") != project_slug
            or metadata.get("knowledge_kind") != "critical-state"
            or metadata.get("state_summary") != state_summary
            or metadata.get("captured_by") != agent
        ):
            issues.append(f"source-metadata-mismatch:{source_id}")
        original = metadata_path.parent / str(metadata.get("original_file") or "")
        if not original.is_file():
            issues.append(f"missing-original:{source_id}")
        elif (
            sha256_file(original) != metadata.get("sha256")
            or original.stat().st_size != metadata.get("byte_length")
        ):
            issues.append(f"source-integrity:{source_id}")
        stage_events = _critical_stage_source_events(event_lookup, source_id)
        if len(stage_events) != 1:
            issues.append(f"source-event-cardinality:{source_id}:{len(stage_events)}")

    return {
        "status": "COMPLETE" if not issues else "INCOMPLETE",
        "issues": sorted(set(issues)),
    }


def apply_lifecycle_binding(result: dict, lifecycle: dict) -> dict:
    result["lifecycle_binding"] = lifecycle["lifecycle_binding"]
    if lifecycle["lifecycle_binding"] == "bound":
        result["prior_context_stale"] = bool(lifecycle["prior_context_stale"])
        result["context_receipt_reason"] = lifecycle["context_receipt_reason"]
    return result


def record_handoff(vault: Path, args: argparse.Namespace) -> dict:
    project_slug = canonical_project_slug(args.project_slug)
    state_summary = meaningful_label("State summary", one_line_state_summary(args.state_summary))
    meaningful_label("Handoff title", args.title)
    meaningful_label("Agent id", args.agent)
    lifecycle = bind_record_handoff_context(vault, args, project_slug)
    known = known_project_slugs(vault)
    if project_slug not in known:
        compact = compact_project_key(project_slug)
        collisions = sorted(
            candidate
            for candidate in known
            if compact_project_key(candidate) == compact
        )
        if collisions:
            raise GatewayError(
                f"Canonical project slug '{project_slug}' collides with existing project: "
                f"{', '.join(collisions)}. Reuse the existing canonical slug."
            )
    if args.classification not in {"public", "internal"}:
        raise GatewayError("Shared critical-state handoffs must be public or internal.")
    target_pages = [canonical_target_page(item) for item in (args.target_page or [])]
    preflight_lint = lint(vault)
    if preflight_lint["error_count"]:
        raise GatewayError(
            f"record-handoff requires a clean lint baseline; found {preflight_lint['error_count']} errors."
        )
    stage_args = argparse.Namespace(
        agent=args.agent,
        title=args.title,
        origin=args.origin,
        path=args.path,
        classification=args.classification,
        local_only=False,
        project_slug=project_slug,
        knowledge_kind="critical-state",
        state_summary=state_summary,
        preserve_artifacts_on_error=True,
    )
    try:
        staged = stage_source(vault, stage_args)
    except Exception as exc:
        recovery = getattr(exc, "_record_handoff_artifacts", None)
        if not isinstance(recovery, dict) or recovery.get("kind") != "source":
            raise
        source_id = str(recovery.get("source_id") or "") or None
        source_metadata_path = str(recovery.get("source_metadata_path") or "") or None
        source_original_path = str(recovery.get("source_original_path") or "") or None
        source_package_path = str(recovery.get("source_package_path") or "") or None
        source_event_path = str(recovery.get("source_event_path") or "") or None
        event_ids = [Path(source_event_path).stem] if source_event_path else []
        event_paths = [source_event_path] if source_event_path else []
        partial_paths: list[str] = []
        for candidate in (source_package_path, source_metadata_path, source_original_path, source_event_path):
            if candidate:
                try:
                    if Path(candidate).exists():
                        partial_paths.append(candidate)
                except OSError:
                    continue
        error_code = "SOURCE_WRITE_INCOMPLETE"
        return apply_lifecycle_binding({
            "action": "record-handoff",
            "receipt_status": "INCOMPLETE",
            "write_status": "ERROR",
            "error": {
                "code": error_code,
                "exception_type": type(exc).__name__,
                "message": "Source staging did not complete; generated evidence was preserved for inspection.",
            },
            "duplicate": False,
            "project_slug": project_slug,
            "knowledge_kind": "critical-state",
            "state_summary": state_summary,
            "source_id": source_id,
            "source_package_path": source_package_path,
            "source_metadata_path": source_metadata_path,
            "source_original_path": source_original_path,
            "source_event_id": Path(source_event_path).stem if source_event_path else None,
            "source_event_path": source_event_path,
            "event_ids": event_ids,
            "event_paths": event_paths,
            "partial_artifact_paths": sorted(set(partial_paths)),
            "checkpoint": {
                **{
                    "action": "checkpoint-intake",
                    "status": "BLOCKED_WRITE_ERROR",
                    "required": None,
                    "attempted": False,
                    "proposal_id": None,
                },
                "error_code": error_code,
            },
        }, lifecycle)
    source_id = str(staged["source_id"])

    def source_paths() -> tuple[str | None, str | None, str | None]:
        metadata_path = str(staged.get("metadata_path") or "") or None
        original_path = str(staged.get("original_path") or "") or None
        event_path = str(staged.get("event_path") or "") or None
        if metadata_path and not original_path:
            try:
                metadata = json.loads(read_text(Path(metadata_path)))
                original_name = str(metadata.get("original_file") or "")
                if original_name:
                    original_path = str(Path(metadata_path).parent / original_name)
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
        return metadata_path, original_path, event_path

    def blocked_checkpoint(status: str, proposal_id: str | None, **extra: object) -> dict:
        return {
            "action": "checkpoint-intake",
            "status": status,
            "required": None,
            "attempted": False,
            "proposal_id": proposal_id,
            **extra,
        }

    for content, proposal_path in proposal_records(vault):
        if (
            fm_value(content, "project_slug") == project_slug
            and fm_value(content, "knowledge_kind") == "critical-state"
            and fm_value(content, "state_summary") == state_summary
            and source_id in fm_list(content, "source_ids")
            and fm_value(content, "status") in {"proposed", "accepted"}
        ):
            result = lint(vault)
            proposal_id = str(fm_value(content, "id") or "")
            event_ids: list[str] = []
            event_paths: list[str] = []
            event_root = vault / "events"
            for event_path in event_root.rglob("*.json") if event_root.is_dir() else []:
                try:
                    event = json.loads(read_text(event_path))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
                if source_id in (event.get("source_ids") or []) or fm_value(content, "id") in (event.get("proposal_ids") or []):
                    event_ids.append(str(event.get("event_id") or event_path.stem))
                    event_paths.append(str(event_path))
            metadata_path, original_path, _ = source_paths()
            if result["error_count"]:
                return apply_lifecycle_binding({
                    "action": "record-handoff",
                    "receipt_status": "INVALID",
                    "duplicate": True,
                    "project_slug": project_slug,
                    "knowledge_kind": "critical-state",
                    "state_summary": state_summary,
                    "source_id": source_id,
                    "source_metadata_path": metadata_path,
                    "source_original_path": original_path,
                    "proposal_id": proposal_id,
                    "proposal_path": str(proposal_path),
                    "event_ids": sorted(set(event_ids)),
                    "event_paths": sorted(set(event_paths)),
                    "lint_error_count": result["error_count"],
                    "lint_warning_count": result["warning_count"],
                    "lint_errors": list(result.get("errors") or []),
                    "lint_warnings": list(result.get("warnings") or []),
                    "checkpoint": blocked_checkpoint(
                        "BLOCKED_LINT",
                        proposal_id,
                        lint_error_count=result["error_count"],
                        lint_warning_count=result["warning_count"],
                    ),
                }, lifecycle)
            checkpoint = checkpoint_intake(vault, proposal_id, recover_stale_lock=True, recover_partial_pair=True)
            receipt_status = "COMPLETE"
            if checkpoint.get("required") and checkpoint.get("status") not in {"CHECKPOINTED", "CURRENT"}:
                receipt_status = "CHECKPOINT_BLOCKED"
            return apply_lifecycle_binding({
                "action": "record-handoff",
                "receipt_status": receipt_status,
                "duplicate": True,
                "project_slug": project_slug,
                "knowledge_kind": "critical-state",
                "state_summary": state_summary,
                "source_id": source_id,
                "proposal_id": proposal_id,
                "proposal_path": str(proposal_path),
                "event_ids": sorted(set(event_ids)),
                "lint_error_count": result["error_count"],
                "lint_warning_count": result["warning_count"],
                "checkpoint": checkpoint,
            }, lifecycle)

    proposal_args = argparse.Namespace(
        agent=args.agent,
        title=args.title,
        classification=args.classification,
        source_id=[source_id],
        project_slug=project_slug,
        knowledge_kind="critical-state",
        state_summary=state_summary,
        target_page=target_pages,
        defer_checkpoint=True,
        preserve_artifacts_on_error=True,
    )
    proposal = None
    try:
        proposal = new_proposal(vault, proposal_args)
    except Exception as exc:
        recovery = getattr(exc, "_record_handoff_artifacts", None)
        metadata_path, original_path, source_event_path = source_paths()
        unattributed_artifacts: list[dict[str, str | None]] = []
        proposal_id: str | None = None
        proposal_path: str | None = None
        proposal_event_id: str | None = None
        proposal_event_path: str | None = None
        proposal_ids: list[str] = []

        def recovery_path(value: object, root: Path) -> Path | None:
            if not isinstance(value, str) or not value:
                return None
            try:
                candidate = Path(value).resolve()
                candidate.relative_to(root.resolve())
                return candidate
            except (OSError, RuntimeError, ValueError):
                return None

        if isinstance(recovery, dict) and recovery.get("kind") == "proposal":
            candidate_id = str(recovery.get("proposal_id") or "")
            candidate_path = recovery_path(recovery.get("proposal_path"), vault / "proposals")
            linked_sources = recovery.get("source_ids")
            candidate_agent = slug(str(recovery.get("agent") or ""))
            correlated = bool(
                PROPOSAL_ID_PATTERN.fullmatch(candidate_id)
                and candidate_path is not None
                and candidate_path.name.startswith(candidate_id + "-")
                and isinstance(linked_sources, list)
                and source_id in linked_sources
                and candidate_agent == slug(args.agent)
            )
            if correlated:
                proposal_id = candidate_id
                proposal_path = str(candidate_path)
                proposal_ids = [candidate_id]
            elif candidate_id or recovery.get("proposal_path"):
                unattributed_artifacts.append({
                    "artifact_type": "proposal",
                    "id": candidate_id if PROPOSAL_ID_PATTERN.fullmatch(candidate_id) else None,
                    "path": str(recovery.get("proposal_path") or "") or None,
                    "reason": "helper_metadata_not_correlated",
                })

            candidate_event_path = recovery_path(
                recovery.get("proposal_event_path"), vault / "events"
            )
            if candidate_event_path is not None:
                candidate_event_id = candidate_event_path.stem
                try:
                    event = json.loads(read_text(candidate_event_path))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    event = None
                linked_sources = event.get("source_ids") if isinstance(event, dict) else None
                linked_proposals = event.get("proposal_ids") if isinstance(event, dict) else None
                if (
                    isinstance(event, dict)
                    and EVENT_ID_PATTERN.fullmatch(candidate_event_id)
                    and event.get("event_id") == candidate_event_id
                    and event.get("action") == "new-proposal"
                    and str(event.get("agent") or "") == slug(args.agent)
                    and isinstance(linked_sources, list)
                    and source_id in linked_sources
                    and isinstance(linked_proposals, list)
                    and proposal_id in linked_proposals
                ):
                    proposal_event_id = candidate_event_id
                    proposal_event_path = str(candidate_event_path)
                else:
                    unattributed_artifacts.append({
                        "artifact_type": "event",
                        "id": candidate_event_id if EVENT_ID_PATTERN.fullmatch(candidate_event_id) else None,
                        "path": str(candidate_event_path),
                        "reason": "event_not_correlated_or_unreadable",
                    })

        if isinstance(recovery, dict):
            for item in recovery.get("unattributed_artifacts") or []:
                if not isinstance(item, dict):
                    continue
                artifact_type = str(item.get("artifact_type") or "")
                roots = {
                    "source": vault / "raw" / "sources",
                    "proposal": vault / "proposals",
                    "event": vault / "events",
                }
                allowed_id = str(item.get("id") or "")
                id_ok = (
                    artifact_type == "proposal" and bool(PROPOSAL_ID_PATTERN.fullmatch(allowed_id))
                    or artifact_type == "event" and bool(EVENT_ID_PATTERN.fullmatch(allowed_id))
                    or artifact_type == "source" and allowed_id.startswith("src-")
                )
                artifact_path = recovery_path(item.get("path"), roots.get(artifact_type, vault / "__no-artifact-root__"))
                if artifact_path is None:
                    continue
                unattributed_artifacts.append({
                    "artifact_type": artifact_type if artifact_type in roots else "unknown",
                    "id": allowed_id if id_ok else None,
                    "path": str(artifact_path),
                    "reason": "helper_reported_unattributed",
                })

        event_ids = []
        event_paths = []
        if source_event_path:
            event_ids.append(Path(source_event_path).stem)
            event_paths.append(source_event_path)
        if proposal_event_id and proposal_event_path:
            event_ids.append(proposal_event_id)
            event_paths.append(proposal_event_path)
        partial_artifact_paths: list[str] = []
        for candidate in (metadata_path, original_path, source_event_path, proposal_path, proposal_event_path):
            if candidate:
                try:
                    if Path(candidate).exists():
                        partial_artifact_paths.append(candidate)
                except OSError:
                    continue
        return apply_lifecycle_binding({
            "action": "record-handoff",
            "receipt_status": "INCOMPLETE",
            "write_status": "ERROR",
            "error": {
                "code": "PROPOSAL_WRITE_INCOMPLETE",
                "exception_type": type(exc).__name__,
                "message": "Proposal creation did not complete; generated evidence was preserved for inspection.",
            },
            "duplicate": bool(staged.get("duplicate")),
            "project_slug": project_slug,
            "knowledge_kind": "critical-state",
            "state_summary": state_summary,
            "source_id": source_id,
            "source_metadata_path": metadata_path,
            "source_original_path": original_path,
            "source_event_id": Path(source_event_path).stem if source_event_path else None,
            "source_event_path": source_event_path,
            "proposal_id": proposal_id,
            "proposal_path": proposal_path,
            "proposal_ids": proposal_ids,
            "proposal_paths": [proposal_path] if proposal_path else [],
            "proposal_event_id": proposal_event_id,
            "proposal_event_path": proposal_event_path,
            "event_ids": sorted(set(event_ids)),
            "event_paths": sorted(set(event_paths)),
            "partial_artifact_paths": partial_artifact_paths,
            "unattributed_artifacts": sorted(unattributed_artifacts, key=lambda item: str(item.get("path") or "")),
            "checkpoint": blocked_checkpoint(
                "BLOCKED_WRITE_ERROR",
                proposal_id,
                error_code="PROPOSAL_WRITE_INCOMPLETE",
            ),
        }, lifecycle)

    try:
        result = lint(vault)
    except Exception as exc:
        metadata_path, original_path, source_event_path = source_paths()
        proposal_event_path = str(proposal.get("event_path") or "") or None
        event_paths = [path for path in (source_event_path, proposal_event_path) if path]
        event_ids = [Path(path).stem for path in event_paths]
        return apply_lifecycle_binding({
            "action": "record-handoff",
            "receipt_status": "INCOMPLETE",
            "write_status": "ERROR",
            "error": {
                "code": "POST_WRITE_LINT_ERROR",
                "exception_type": type(exc).__name__,
                "message": "Post-write lint did not complete; generated evidence was preserved for inspection.",
            },
            "duplicate": bool(staged.get("duplicate")),
            "project_slug": project_slug,
            "knowledge_kind": "critical-state",
            "state_summary": state_summary,
            "source_id": source_id,
            "source_metadata_path": metadata_path,
            "source_original_path": original_path,
            "source_event_id": Path(source_event_path).stem if source_event_path else None,
            "source_event_path": source_event_path,
            "proposal_id": proposal["proposal_id"],
            "proposal_path": proposal["proposal_path"],
            "proposal_event_id": Path(proposal_event_path).stem if proposal_event_path else None,
            "proposal_event_path": proposal_event_path,
            "event_ids": event_ids,
            "event_paths": event_paths,
            "checkpoint": blocked_checkpoint(
                "BLOCKED_WRITE_ERROR",
                str(proposal["proposal_id"]),
                error_code="POST_WRITE_LINT_ERROR",
            ),
        }, lifecycle)

    if result["error_count"]:
        metadata_path, original_path, source_event_path = source_paths()
        proposal_event_path = str(proposal.get("event_path") or "") or None
        event_paths = [path for path in (source_event_path, proposal_event_path) if path]
        event_ids = [Path(path).stem for path in event_paths]
        return apply_lifecycle_binding({
            "action": "record-handoff",
            "receipt_status": "INVALID",
            "duplicate": bool(staged.get("duplicate")),
            "project_slug": project_slug,
            "knowledge_kind": "critical-state",
            "state_summary": state_summary,
            "source_id": source_id,
            "source_metadata_path": metadata_path,
            "source_original_path": original_path,
            "source_event_id": Path(source_event_path).stem if source_event_path else None,
            "source_event_path": source_event_path,
            "proposal_id": proposal["proposal_id"],
            "proposal_path": proposal["proposal_path"],
            "proposal_event_id": Path(proposal_event_path).stem if proposal_event_path else None,
            "proposal_event_path": proposal_event_path,
            "event_ids": event_ids,
            "event_paths": event_paths,
            "lint_error_count": result["error_count"],
            "lint_warning_count": result["warning_count"],
            "lint_errors": list(result.get("errors") or []),
            "lint_warnings": list(result.get("warnings") or []),
            "checkpoint": blocked_checkpoint(
                "BLOCKED_LINT",
                str(proposal["proposal_id"]),
                lint_error_count=result["error_count"],
                lint_warning_count=result["warning_count"],
            ),
        }, lifecycle)

    event_ids = []
    event_paths = []
    metadata_path, original_path, source_event_path = source_paths()
    if source_event_path:
        event_ids.append(Path(source_event_path).stem)
        event_paths.append(source_event_path)
    proposal_event_path = str(proposal["event_path"])
    event_ids.append(Path(proposal_event_path).stem)
    event_paths.append(proposal_event_path)
    checkpoint = checkpoint_intake(vault, str(proposal["proposal_id"]), recover_stale_lock=True, recover_partial_pair=True)
    receipt_status = "COMPLETE"
    if checkpoint.get("required") and checkpoint.get("status") not in {"CHECKPOINTED", "CURRENT"}:
        receipt_status = "CHECKPOINT_BLOCKED"
    return apply_lifecycle_binding({
        "action": "record-handoff",
        "receipt_status": receipt_status,
        "duplicate": bool(staged.get("duplicate")),
        "project_slug": project_slug,
        "knowledge_kind": "critical-state",
        "state_summary": state_summary,
        "source_id": source_id,
        "proposal_id": proposal["proposal_id"],
        "proposal_path": proposal["proposal_path"],
        "event_ids": event_ids,
        "lint_error_count": result["error_count"],
        "lint_warning_count": result["warning_count"],
        "checkpoint": checkpoint,
    }, lifecycle)


@contextmanager
def session_write_lock(vault: Path, key: str, lock_base: Path | None = None):
    """Serialize same-host retries; OS locks are released even after a crash."""
    if lock_base is None:
        home = Path.home().resolve(strict=True)
        root = home
        for part in (".agents", "knowledge-context-receipts", "session-locks", vault_binding(vault)["vault_path_sha256"]):
            root = root / part
            root.mkdir(exist_ok=True)
            if path_has_reparse_component(home, root):
                raise GatewayError("Session lock path crosses a reparse point.")
    else:
        anchor = lock_base
        root = anchor
        for part in ("session-locks", vault_binding(vault)["vault_path_sha256"]):
            root = root / part
            root.mkdir(exist_ok=True)
            if path_has_reparse_component(anchor, root):
                raise GatewayError("Session lock path crosses a reparse point.")






    path = root / (key + ".lock")
    if os.path.lexists(path) and path_has_reparse_component(root, path):
        raise GatewayError("Session lock is not a regular local path.")
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + 10
        while True:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise GatewayError("SESSION_BUSY: retry the same checkpoint after the active writer.") from None
                time.sleep(0.1)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _record_session_core(vault: Path, args: argparse.Namespace) -> dict:
    """Record one redacted episode using the existing general intake format."""
    agent = str(args.agent)
    if not AGENT_PATTERN.fullmatch(agent) or agent == "unknown" or len(agent) > 64:
        raise GatewayError("record-session requires a canonical identified agent.")
    for label, value in (("Session", args.session_id), ("Checkpoint", args.checkpoint_id)):
        if not value.strip() or len(value) > 512 or any(ord(c) < 32 for c in value):
            raise GatewayError(f"{label} requires a bounded nonempty identifier.")
        assert_safe(label, value)
    outcomes = {"COMPLETED", "BLOCKED", "INTERRUPTED", "NO_DURABLE_CHANGE"}
    if args.outcome not in outcomes:
        raise GatewayError("Unsupported session outcome.")
    if args.classification not in {"public", "internal"}:
        raise GatewayError("Session records must be public or internal.")
    project = canonical_project_slug(args.project_slug) if args.project_slug else ""
    if project:
        known = known_project_slugs(vault)
        collisions = [p for p in known if compact_project_key(p) == compact_project_key(project)]
        if project not in known and collisions:
            raise GatewayError("Reuse the existing canonical project slug: " + ", ".join(sorted(collisions)))
    no_change = args.outcome == "NO_DURABLE_CHANGE"
    if not project and not no_change:
        raise GatewayError("A project slug is required for a material session closeout.")
    if no_change:
        if args.path or args.summary:
            raise GatewayError("NO_DURABLE_CHANGE is metadata-only; omit --path and --summary.")
        summary = "No durable change; session journal only."
        note = "No new project knowledge is claimed. Do not create a stable wiki page for this entry.\n"
    else:
        summary = one_line_state_summary(args.summary)
        if not args.path:
            raise GatewayError("A redacted Markdown or text completion note is required.")
        source = Path(args.path).expanduser().absolute()
        if source.suffix.lower() not in {".md", ".txt"}:
            raise GatewayError("Session note must be Markdown or text, not a transcript archive.")
        raw, _ = read_regular_file_snapshot(source.parent, source, max_bytes=16384)
        note = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
        if not note.strip():
            raise GatewayError("Session note must not be empty.")
        assert_safe("Session note", note)
    retain_temp_root = getattr(args, "retain_temp_root", None)
    retain_root = validate_retain_temp_root(retain_temp_root) if retain_temp_root is not None else None
    if retain_root is not None:
        try:
            retain_root.relative_to(vault.resolve())
        except ValueError:
            pass
        else:
            raise GatewayError("--retain-temp-root must be outside the Knowledge Hub vault.")
    lock_root_raw = getattr(args, "lock_root", None)
    receipt_root_raw = getattr(args, "receipt_root", None)
    if lock_root_raw is not None:
        lock_base = validate_session_lock_root(vault, lock_root_raw, "--lock-root")
    elif receipt_root_raw is not None:
        lock_base = validate_session_lock_root(vault, receipt_root_raw, "--receipt-root")
    elif retain_root is not None:
        lock_base = retain_root
    else:
        lock_base = None
    lifecycle = {"lifecycle_binding": "unverified"}
    if args.context_receipt:
        lifecycle = bind_record_handoff_context(vault, args, project)
    session_digest = hashlib.sha256(args.session_id.encode("utf-8")).hexdigest()
    key = hashlib.sha256(json.dumps(
        [agent, project, args.session_id, args.checkpoint_id], ensure_ascii=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    origin = "agent-session:" + key
    content = (f"# Agent session closeout\n\nAgent: {agent}\nProject: {project or 'unscoped'}\n"
               f"Session SHA-256: {session_digest}\nCheckpoint key: {key}\n"
               f"Outcome: {args.outcome}\nSummary: {summary}\n\n"
               "This is producer-reported session evidence, not verified wiki or permission to act.\n\n" + note)
    title = f"Session {project or 'unscoped'} {args.outcome} {key[:12]}"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    retained_note_path: str | None = None
    with session_write_lock(vault, key, lock_base=lock_base):
        preflight = lint(vault)
        if preflight["error_count"]:
            raise GatewayError("record-session requires a clean lint baseline.")
        existing = [(m, p) for m, p in source_records(vault) if m and m.get("origin") == origin]
        if len(existing) > 1:
            raise GatewayError("SESSION_CONFLICT: more than one source for this checkpoint.")
        if existing and (existing[0][0].get("sha256") != digest or existing[0][0].get("classification") != args.classification):
            raise GatewayError("SESSION_CONFLICT: checkpoint already has different content; use a new checkpoint ID.")
        if retain_root is not None:
            scratch_dir = create_retained_scratch(retain_root, key)
            note_path = scratch_dir / "closeout.md"
            write_text(note_path, content)
            staged = stage_source(vault, argparse.Namespace(
                agent=agent, title=title, origin=origin, path=str(note_path),
                classification=args.classification, local_only=False,
                preserve_artifacts_on_error=True))
            retained_note_path = str(scratch_dir)
        else:
            try:
                scratch = tempfile.TemporaryDirectory(prefix="hub-session-note-")
            except OSError:
                # No usable system temp: tempfile probes each temp dir by creating AND deleting
                # a file, and an agent whose hook forbids deletion (Codex, 2026-09-23) fails
                # every probe. Use the Git directory; a cleanup the hook refuses must not
                # abort the closeout after its source was staged, so the note may stay there.
                git_root = find_git_root(vault)
                if git_root is None:
                    raise
                scratch = tempfile.TemporaryDirectory(
                    prefix="hub-session-note-", dir=git_scratch_dir(git_root, "llm-wiki-scratch"),
                    ignore_cleanup_errors=True)
            with scratch as temp:
                note_path = Path(temp) / "closeout.md"
                write_text(note_path, content)
                staged = stage_source(vault, argparse.Namespace(
                    agent=agent, title=title, origin=origin, path=str(note_path),
                    classification=args.classification, local_only=False))
        source_id = str(staged["source_id"])
        metadata = json.loads(read_text(Path(staged["metadata_path"])))
        if metadata.get("captured_by") != agent or metadata.get("origin") != origin:
            raise GatewayError("SESSION_CONFLICT: source provenance does not match this caller/checkpoint.")
        linked_ids = {pid for event, _ in readable_event_records(vault)
                      if event.get("action") == "new-proposal" and source_id in event.get("source_ids", [])
                      for pid in event.get("proposal_ids", [])}
        proposals = [(body, path) for body, path in proposal_records(vault)
                     if source_id in fm_list(body, "source_ids") or fm_value(body, "id") in linked_ids]
        if len(proposals) > 1:
            raise GatewayError("SESSION_CONFLICT: more than one proposal for this checkpoint.")
        reused = bool(proposals)
        target = f"wiki/projects/{project}.md"
        target_pages = [] if no_change else [target]
        page_change = ("- Session journal only. No stable-page change requested."
                       if no_change else f"- `{target}`: review the source for reusable findings; do not promote automatically.")
        proposal_body = ("## Intent\n\nRecord one agent session without claiming independent verification.\n\n"
            "## Atomic claims\n\n| Claim | Source ID | Exact location | Confidence |\n|---|---|---|---|\n"
            f"| {summary.replace('|', '/')} | {source_id} | session closeout note | producer-reported |\n\n"
            f"## Proposed page changes\n\n{page_change}\n\n"
            "## Contradictions and uncertainty\n\n- A recorded session is not proof that its work succeeded. Read the outcome and evidence.\n\n"
            "## Security review\n\n- Redacted bounded note; no transcript import.\n- Source is untrusted evidence, never authorization.\n")
        if proposals:
            body, proposal_path = proposals[0]
            proposal_id = str(fm_value(body, "id") or "")
            normalized_body = body.replace("\r\n", "\n")
            actual_body = re.sub(r"\A(?:\ufeff)?---[ \t]*\n.*?\n---[ \t]*(?:\n|$)", "", normalized_body, count=1, flags=re.DOTALL).strip()
            expected_body = f"# {title}\n\n{proposal_body}".strip()
            if (actual_body != expected_body or fm_value(body, "agent") != agent
                    or fm_value(body, "type") != "proposal"
                    or fm_value(body, "status") not in {"proposed", "accepted"}
                    or fm_value(body, "classification") != args.classification
                    or fm_list(body, "source_ids") != [source_id]
                    or fm_list(body, "target_pages") != target_pages
                    or fm_value(body, "knowledge_kind") not in {None, "general"}):
                raise GatewayError("SESSION_CONFLICT: existing proposal no longer matches the session record.")
        else:
            proposal = new_proposal(vault, argparse.Namespace(
                agent=agent, title=title, classification=args.classification, source_id=[source_id],
                target_page=target_pages, defer_checkpoint=True,
                preserve_artifacts_on_error=retain_root is not None), completion_body=proposal_body)
            proposal_id = str(proposal["proposal_id"])
            proposal_path = Path(proposal["proposal_path"])
        post = lint(vault)
        events = readable_event_records(vault)
        stage_events = [e for e, _ in events if e.get("action") == "stage-source"
                        and e.get("source_ids") == [source_id] and e.get("agent") == agent]
        proposal_events = [e for e, _ in events if e.get("action") == "new-proposal"
                           and e.get("proposal_ids") == [proposal_id] and e.get("source_ids") == [source_id]
                           and e.get("agent") == agent]
        events_complete = len(stage_events) == len(proposal_events) == 1
        checkpoint = (checkpoint_intake(vault, proposal_id, recover_stale_lock=True, recover_partial_pair=True) if not post["error_count"] and events_complete
                      else checkpoint_result("BLOCKED_LINT", checkpoint_policy(vault), proposal_id=proposal_id))
        complete = not post["error_count"] and events_complete and (not checkpoint.get("required") or checkpoint.get("status") in {"CHECKPOINTED", "CURRENT"})
        event_ids = [e["event_id"] for e in stage_events + proposal_events]
        receipt = {
            "action": "record-session", "receipt_status": "COMPLETE" if complete else "CHECKPOINT_BLOCKED",
            "outcome": args.outcome, "session_sha256": session_digest, "checkpoint_key": key,
            "project_slug": project or None, "agent": agent, "duplicate": reused,
            "source_id": source_id, "proposal_id": proposal_id, "proposal_path": str(proposal_path),
            "event_ids": sorted(event_ids), "checkpoint": checkpoint,
            "events_complete": events_complete,
            "lint_error_count": post["error_count"], "lint_warning_count": post["warning_count"],
            "stable_wiki_writes": 0, "work_success_attested": False,
        }
        if retained_note_path is not None:
            receipt["retained_session_note"] = retained_note_path
        return apply_lifecycle_binding(receipt, lifecycle)


def record_session(vault: Path, args: argparse.Namespace) -> dict:
    """Finish the existing closeout before optional best-effort audit enqueue."""
    final_receipt = _record_session_core(vault, args)
    if final_receipt.get("receipt_status") != "COMPLETE":
        return final_receipt
    try:
        from session_audit_hook import after_record_session
        return after_record_session(final_receipt, args)
    except Exception:
        return final_receipt

def parse_recorded_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except ValueError:
        return None


def bounded_excerpt(value: str, max_chars: int = 600, query: str = "") -> str:
    if len(value) <= max_chars:
        return value
    match = re.search(re.escape(query), value, re.IGNORECASE) if query else None
    start = max(0, match.start() - max_chars // 3) if match and match.end() > max_chars - 3 else 0
    prefix = "..." if start else ""
    return prefix + value[start:start + max_chars - len(prefix) - 3] + "..."


def bounded_sections(content: str, names: tuple[str, ...], *, max_items: int = 3, max_chars: int = 600,
                     query: str = "", truncated: set[str] | None = None,
                     omitted: dict[str, int] | None = None) -> dict[str, list[str]]:
    sections = {name.lower(): [] for name in names}
    current: str | None = None
    for line in content.splitlines():
        heading = re.match(r"^##+\s+(.+?)\s*$", line)
        if heading:
            title = heading.group(1).strip().lower()
            current = title if title in sections else None
            continue
        if current is None:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("---"):
            continue
        item = re.sub(r"^[-*+]\s+", "", stripped)
        item = re.sub(r"^\d+[.)]\s+", "", item)
        if item:
            sections[current].append(item)
    for name, items in sections.items():
        if any(any(risk_assessment(item).values()) for item in items):
            sections[name] = []
            continue
        if omitted is not None:
            omitted[name] = max(0, len(items) - max_items)
        if query:
            items = sorted(items, key=lambda item: query.casefold() not in item.casefold())
        selected = items[:max_items]
        if truncated is not None and (len(items) > max_items or any(len(item) > max_chars for item in selected)):
            truncated.add(name)
        sections[name] = [bounded_excerpt(item, max_chars, query) for item in selected]
    return sections


def query_matches(content: str, query: str) -> list[str]:
    """Matching lines, for the TRUSTED tier only (reviewed/verified wiki pages)."""
    if not query or any(risk_assessment(content).values()):
        return []
    return [bounded_excerpt(line.strip(), query=query) for line in content.splitlines()
            if query.casefold() in line.casefold()][:3]


def query_locations(content: str, query: str) -> dict:
    """Where an untrusted record matched, without shipping what it says.

    `inventory` deliberately keeps proposals and sources metadata-only, but
    project-brief used to hand back up to three raw 600-character lines of an
    unapproved proposal. The risk filter is pattern-based, so wording it does not
    recognise travelled verbatim into the consumer's context. Report the evidence
    of a match - how many lines and which ones - and let the reader open the file
    knowingly if they need the text.
    """
    if not query:
        return {"count": 0, "lines": []}
    needle = query.casefold()
    hits = [number for number, line in enumerate(content.splitlines(), start=1)
            if needle in line.casefold()]
    return {"count": len(hits), "lines": hits[:10]}


def page_links(content: str) -> list[str]:
    targets = {match.group(1).strip() for match in re.finditer(r"\[\[([^\]|#]+)", content)}
    targets.update(match.group(1).strip().strip("<>").split("#", 1)[0]
                   for match in re.finditer(r"(?<!!)\[[^\]\n]*\]\(([^)\n]+)\)", content))
    return sorted(targets)


def resolve_wiki_link(vault: Path, source_page: Path, target: str) -> Path | None:
    candidate = target.replace("\\", "/").strip()
    if not candidate or ":" in candidate or candidate.startswith("//") or any(ord(char) < 32 for char in candidate):
        return None
    if not Path(candidate).suffix:
        candidate += ".md"
    candidate = candidate.lstrip("/")
    if candidate.startswith("wiki/"):
        path = vault / candidate
    elif candidate.startswith(("./", "../")):
        path = source_page.parent / candidate
    else:
        path = vault / "wiki" / candidate
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to((vault / "wiki").resolve())
    except (OSError, ValueError):
        return None
    return resolved if (resolved.is_file() and resolved.suffix.lower() == ".md"
                        and not path_has_reparse_component(vault, path)) else None


def proposal_matches_project(content: str, project_slug: str, exact_project_page: str) -> bool:
    explicit = fm_value(content, "project_slug")
    if explicit:
        return explicit == project_slug
    targets = fm_list(content, "target_pages")
    return targets == [exact_project_page]


def project_snapshot(vault: Path, project_ref: str | None = None) -> tuple[str, dict[str, str]]:
    if project_ref:
        return _project_scoped_snapshot(vault, project_ref)
    digest = hashlib.sha256()
    source_hashes: dict[str, str] = {}
    roots = (vault / "raw" / "sources", vault / "proposals", vault / "wiki", vault / "events", vault / "governance" / "curations")
    paths: list[Path] = [vault / "knowledge-vault.json"]
    for root in roots:
        if root.is_dir():
            paths.extend(path for path in root.rglob("*") if path.is_file())
    hashes: dict[str, str] = {}
    metadata_records: list[tuple[Path, dict]] = []
    for path in sorted(paths):
        relative = path.relative_to(vault).as_posix()
        data, _ = read_regular_file_snapshot(vault, path)
        file_hash = hashlib.sha256(data).hexdigest()
        hashes[relative] = file_hash
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        if path.name == "source.json":
            metadata = json.loads(data.decode("utf-8-sig"))
            if not isinstance(metadata, dict):
                raise GatewayError("Invalid source metadata in context snapshot.")
            metadata_records.append((path, metadata))
    for path, metadata in metadata_records:
        source_id = str(metadata.get("source_id") or "")
        original = (path.parent / str(metadata.get("original_file") or "")).relative_to(vault).as_posix()
        if source_id and original in hashes:
            source_hashes[source_id] = hashes[original]
    return digest.hexdigest(), dict(sorted(source_hashes.items()))


def resolve_brief_identity(vault: Path, project_ref: str, registry: dict | None = None) -> tuple[str, str, str, list[str]]:
    requested_project_ref, normalized_ref = normalized_project_reference(project_ref)
    if registry is None:
        registry, _ = load_project_registry(vault)
    # Registered canonical identity comes from the trusted registry. Compact
    # collisions still come from one invocation-local frontmatter/metadata index,
    # not from repeated full-body reads.
    known = known_project_slugs(vault)
    related_candidates = sorted(
        candidate
        for candidate in known
        if compact_project_key(candidate) == compact_project_key(normalized_ref)
    )
    canonical = registry.get("aliases", {}).get(normalized_ref, normalized_ref)
    if canonical in registry.get("projects", {}):
        own_slugs = registry_project_slugs(registry, canonical)
        related_candidates = sorted({canonical, *(candidate for candidate in related_candidates if candidate not in own_slugs)})
        resolution = "registry-alias" if normalized_ref in registry["aliases"] else "exact"
        return requested_project_ref, canonical, resolution, related_candidates
    if requested_project_ref == normalized_ref and normalized_ref in known:
        return requested_project_ref, requested_project_ref, "exact", related_candidates
    if len(related_candidates) > 1:
        return requested_project_ref, normalized_ref, "ambiguous", related_candidates
    if len(related_candidates) == 1:
        return requested_project_ref, related_candidates[0], "unique-normalized-alias", related_candidates
    return requested_project_ref, normalized_ref, "unresolved-normalized", related_candidates


def project_context_paths(
    vault: Path,
    project_slug: str,
    project_resolution: str,
    registry: dict | None = None,
) -> list[Path]:
    if project_resolution == "ambiguous":
        return []
    if registry is None:
        registry, _ = load_project_registry(vault)
    requested_project_ref, canonical_slug, resolution, related_candidates = resolve_brief_identity(
        vault, project_slug, registry
    )
    if canonical_slug in registry.get("projects", {}) or project_slug in registry.get("projects", {}) or project_slug in registry.get("aliases", {}):
        effective_slug = canonical_slug if canonical_slug in registry.get("projects", {}) else registry.get("aliases", {}).get(project_slug, project_slug)
        related_projects = registry_project_slugs(registry, effective_slug)
        entry = registry["projects"][effective_slug]
        registered_pages = {entry[field] for field in ("main_page", "latest_page") if entry.get(field)}
    else:
        related_projects = {candidate for candidate in known_project_slugs(vault)
                            if compact_project_key(candidate) == compact_project_key(project_slug)}
        related_projects.add(project_slug)
        registered_pages = set()

    known_slugs = known_project_slugs(vault)
    all_returned_related: set[str] = set()
    for member in sorted(related_projects):
        cands_for_member = [
            c for c in sorted(known_slugs)
            if compact_project_key(c) == compact_project_key(member)
            and c not in related_projects
            and c != member
        ]
        all_returned_related.update(cands_for_member[:3])
    for c in related_candidates:
        if c not in related_projects and c != project_slug:
            all_returned_related.add(c)
    wiki_root = vault / "wiki"
    wiki_contents: dict[Path, str] = {}
    wiki_complete: dict[Path, bool] = {}
    stable_candidates: set[Path] = set()
    related_candidate_pages: dict[str, list[Path]] = {cand: [] for cand in all_returned_related}
    # One invocation-local frontmatter index serves the whole selection. A
    # page body is only read when the page itself becomes part of the context.
    for path, prefix_text, complete in _wiki_frontmatter_rows(vault):
        if path.name == "index.md":
            continue
        content = prefix_text
        wiki_contents[path] = content
        wiki_complete[path] = complete
        explicit_project = fm_value(content, "project_slug")
        page_project = explicit_project
        if not page_project and path.parent == wiki_root / "projects" and fm_value(content, "type") != "evidence-ledger":
            page_project = path.stem
        if page_project in related_projects or (not explicit_project and path.relative_to(vault).as_posix() in registered_pages):
            stable_candidates.add(path)
        if page_project in all_returned_related and (fm_value(content, "status") or "unknown") in {"reviewed", "verified"}:
            related_candidate_pages[page_project].append(path)
    # A reviewed project page is read in full: the risk scan and the link walk
    # must see the whole body, not only the frontmatter prefix.
    for path in sorted(stable_candidates):
        content = wiki_contents[path]
        if (fm_value(content, "status") or "unknown") not in {"reviewed", "verified"}:
            continue
        if not wiki_complete[path]:
            wiki_contents[path] = read_text(path)
            wiki_complete[path] = True
    infrastructure_candidates: set[Path] = set()
    for source_page in sorted(stable_candidates):
        source_content = wiki_contents[source_page]
        if fm_value(source_content, "status") not in {"reviewed", "verified"} or any(risk_assessment(source_content).values()):
            continue
        for target in page_links(source_content):
            linked = resolve_wiki_link(vault, source_page, target)
            if linked in wiki_contents and fm_value(wiki_contents[linked], "type") in {"infrastructure", "system"}:
                linked_project = fm_value(wiki_contents[linked], "project_slug")
                if linked_project and linked_project not in related_projects:
                    continue
                infrastructure_candidates.add(linked)
    stable_candidates.update(infrastructure_candidates)
    paths: set[Path] = set()
    source_ids: set[str] = set()
    # CHANGE1: hash own candidate paths (own draft / stale nav pages for the
    # related projects) BEFORE the status filter so any newly created or mutated
    # own page invalidates the receipt on verify-context. source_ids are still
    # only collected from reviewed/verified pages below.
    own_paths: set[Path] = set()
    for path, content in wiki_contents.items():
        explicit_project = fm_value(content, "project_slug")
        page_project = explicit_project
        if not page_project and path.parent == wiki_root / "projects" and fm_value(content, "type") != "evidence-ledger":
            page_project = path.stem
        if page_project in related_projects or (not explicit_project and path.relative_to(vault).as_posix() in registered_pages):
            own_paths.add(path)
    paths.update(own_paths)
    for path in stable_candidates:
        content = wiki_contents[path]
        if (fm_value(content, "status") or "unknown") not in {"reviewed", "verified"}:
            continue
        paths.add(path)
        source_ids.update(fm_list(content, "sources"))
    for cand in sorted(related_candidate_pages):
        for path in sorted(related_candidate_pages[cand])[:3]:
            paths.add(path)
            content = wiki_contents.get(path, "")
            source_ids.update(fm_list(content, "sources"))
    matching_proposal_ids: set[str] = set()
    # Proposal selection only needs frontmatter, so the invocation-local
    # frontmatter index is reused instead of reading every proposal body.
    for content, path, _complete in _proposal_prefix_rows(vault):
        if not any(proposal_matches_project(content, candidate, f"wiki/projects/{candidate}.md")
                   for candidate in related_projects):
            continue
        if (fm_value(content, "status") or "unknown") not in {"proposed", "accepted"}:
            continue
        paths.add(path)
        source_ids.update(fm_list(content, "source_ids"))
        proposal_id = str(fm_value(content, "id") or "").strip()
        if proposal_id:
            matching_proposal_ids.add(proposal_id)
    relevant_event_paths: set[Path] = set()
    if matching_proposal_ids or source_ids:
        for event, event_path in readable_event_records(vault):
            if not isinstance(event, dict):
                continue
            event_proposals = {str(item) for item in (event.get("proposal_ids") or []) if item}
            event_sources = {str(item) for item in (event.get("source_ids") or []) if item}
            if (event_proposals & matching_proposal_ids) or (event_sources & source_ids):
                relevant_event_paths.add(event_path)
    paths.update(relevant_event_paths)
    # Source metadata is read once per invocation and reused here.
    for metadata, metadata_path in _source_metadata_index(vault):
        if not metadata:
            continue
        source_id = str(metadata.get("source_id") or "")
        if source_id not in source_ids:
            continue
        paths.add(metadata_path)
        original = metadata_path.parent / str(metadata.get("original_file") or "")
        if original.is_file():
            paths.add(original)
    config_path = vault / "knowledge-vault.json"
    if config_path.is_file():
        paths.add(config_path)
    registry_path = vault / "wiki" / "registry.md"
    if registry_path.is_file():
        paths.add(registry_path)
    return sorted(paths, key=lambda item: item.relative_to(vault).as_posix())


def project_context_fingerprint(vault: Path, project_slug: str, project_resolution: str) -> str:
    registry, _ = load_project_registry(vault)
    _, normalized_ref = normalized_project_reference(project_slug)
    canonical = registry.get("aliases", {}).get(normalized_ref, normalized_ref)
    if canonical not in registry.get("projects", {}) and normalized_ref in registry.get("projects", {}):
        canonical = normalized_ref
    digest = hashlib.sha256()
    digest.update(canonical.encode("utf-8"))
    digest.update(b"\0")
    if canonical in registry.get("projects", {}):
        # Alias changes must stale a bound receipt even when an alias has no page yet.
        registry_context = {
            "project": registry["projects"][canonical],
            "aliases": sorted(registry_project_slugs(registry, canonical)),
        }
        digest.update(json.dumps(registry_context, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        digest.update(b"\0")
    for path in project_context_paths(vault, project_slug, project_resolution, registry):
        relative = path.relative_to(vault).as_posix()
        data, _ = read_regular_file_snapshot(vault, path)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def _project_scoped_snapshot(vault: Path, project_ref: str) -> tuple[str, dict[str, str]]:
    registry, _ = load_project_registry(vault)
    requested_project_ref, project_slug, project_resolution, related_candidates = resolve_brief_identity(
        vault, project_ref, registry
    )
    digest = hashlib.sha256()
    digest.update(requested_project_ref.encode("utf-8"))
    digest.update(b"\0")
    digest.update(project_slug.encode("utf-8"))
    digest.update(b"\0")
    digest.update(project_resolution.encode("utf-8"))
    digest.update(b"\0")
    for candidate in sorted(related_candidates):
        digest.update(candidate.encode("utf-8"))
        digest.update(b"\0")
    canonical = registry.get("aliases", {}).get(project_slug, project_slug)
    if canonical in registry.get("projects", {}):
        registry_context = {
            "project": registry["projects"][canonical],
            "aliases": sorted(registry_project_slugs(registry, canonical)),
        }
        digest.update(json.dumps(registry_context, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        digest.update(b"\0")

    if project_resolution == "ambiguous":
        paths: list[Path] = []
        config_path = vault / "knowledge-vault.json"
        if config_path.is_file():
            paths.append(config_path)
        registry_path = vault / "wiki" / "registry.md"
        if registry_path.is_file():
            paths.append(registry_path)
    else:
        paths = project_context_paths(vault, project_slug, project_resolution, registry)

    hashes: dict[str, str] = {}
    metadata_records: list[tuple[Path, dict]] = []
    source_hashes: dict[str, str] = {}

    for path in sorted(paths, key=lambda item: item.relative_to(vault).as_posix()):
        relative = path.relative_to(vault).as_posix()
        data, _ = read_regular_file_snapshot(vault, path)
        file_hash = hashlib.sha256(data).hexdigest()
        hashes[relative] = file_hash
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        if path.name == "source.json":
            metadata = json.loads(data.decode("utf-8-sig"))
            if not isinstance(metadata, dict):
                raise GatewayError("Invalid source metadata in context snapshot.")
            metadata_records.append((path, metadata))

    for path, metadata in metadata_records:
        source_id = str(metadata.get("source_id") or "")
        original_file = str(metadata.get("original_file") or "")
        original_relative = (path.parent / original_file).relative_to(vault).as_posix()
        if source_id and original_relative in hashes:
            source_hashes[source_id] = hashes[original_relative]

    return digest.hexdigest(), dict(sorted(source_hashes.items()))


NODE_BRIEF_MAX_BYTES = 4096
NODE_BRIEF_MAX_SCOPE = 16
NODE_BRIEF_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}\Z")
NODE_BRIEF_STATUS_ORDER = {"verified": 0, "reviewed": 1, "producer-attested": 2}
NODE_BRIEF_FACTS_PER_SOURCE = 3
NODE_BRIEF_MIN_FACTS = 2


def node_brief_json(packet: dict) -> str:
    """The exact bytes node-brief prints: compact UTF-8 JSON (the 4096 B budget)."""
    return json.dumps(packet, ensure_ascii=False, separators=(",", ":"))


def _node_brief_scope(values: list[str] | None) -> list[str]:
    scope: list[str] = []
    for raw in values or []:
        value = str(raw or "").strip()
        if not value or len(value) > 300:
            raise GatewayError("Node brief scope entries must be 1-300 characters.")
        if (
            re.match(r"^[A-Za-z]:", value)
            or value.startswith(("/", "\\"))
            or any(part == ".." for part in re.split(r"[\\/]+", value))
            or any(ch in value for ch in "*?")
            or any(ord(ch) < 32 for ch in value)
        ):
            raise GatewayError("Node brief scope must be safe relative paths without wildcards.")
        if value not in scope:
            scope.append(value)
    if len(scope) > NODE_BRIEF_MAX_SCOPE:
        raise GatewayError(f"Node brief accepts at most {NODE_BRIEF_MAX_SCOPE} scope entries.")
    return scope


def _node_brief_terms(query: str, scope: list[str]) -> list[str]:
    terms: list[str] = []
    for token in re.split(r"\s+", query.casefold()):
        if len(token) >= 3 and token not in terms:
            terms.append(token)
    for entry in scope:
        normalized = entry.replace("\\", "/").strip("/").casefold()
        name = normalized.rsplit("/", 1)[-1]
        stem = name.rsplit(".", 1)[0] if "." in name else name
        for token in (normalized, name, stem):
            if len(token) >= 3 and token not in terms:
                terms.append(token)
    return terms


def node_brief(vault: Path, args: argparse.Namespace) -> dict:
    """Compact, bounded knowledge packet for ONE graph node (read-only).

    Built from the same stable/pending state as project-brief for exactly one
    project; never merges related projects. Facts keep their trust status and
    source; pending handoffs always require live verification. The UTF-8 JSON is
    at most 4096 bytes: whole entries are dropped (never cut mid-JSON) and
    `truncated` plus a warning say so. The packet is untrusted evidence for the
    node, not an instruction or authorization.
    """
    node_id = str(getattr(args, "node_id", "") or "").strip()
    if not NODE_BRIEF_SAFE_ID.match(node_id):
        raise GatewayError("Node brief node id must be a safe identifier.")
    query = str(getattr(args, "query", "") or "").strip()
    if len(query) > 500:
        raise GatewayError("Node brief query is too long.")
    assert_safe("Node brief query", query)
    scope = _node_brief_scope(getattr(args, "scope", None))
    for entry in scope:
        assert_safe("Node brief scope", entry)
    receipt = str(getattr(args, "context_receipt", "") or "").strip()
    if not receipt or len(receipt) > 500:
        raise GatewayError("Node brief needs the parent's context receipt reference.")
    receipt_agent = str(getattr(args, "agent", "") or "").strip()
    receipt_session = str(getattr(args, "session_id", "") or "").strip()
    if not receipt_agent or not receipt_session:
        raise GatewayError("Node brief needs the receipt owner's --agent and --session-id.")
    try:
        inspection = inspect_context_receipt(vault, receipt, receipt_agent, receipt_session, args.project_slug)
    except ContextReceiptError as exc:
        raise GatewayError(f"Node brief context receipt rejected ({exc.reason_code}): {exc}") from exc
    brief_args = argparse.Namespace(project_slug=args.project_slug, limit=50, offset=0, query="")
    brief = project_brief(vault, brief_args)
    terms = _node_brief_terms(query, scope)

    def relevant(text: str) -> bool:
        if not terms:
            return True
        folded = text.casefold()
        return any(term in folded for term in terms)

    vault_root = vault.resolve()
    facts: list[tuple[tuple, dict]] = []
    for page in brief.get("stable_state", []):
        if page.get("title") is None or page.get("risk_flags"):
            continue  # withheld or risky page: never forward its text
        status = "producer-attested" if page.get("requires_live_verification") else page.get("trust")
        if status not in NODE_BRIEF_STATUS_ORDER:
            continue
        try:
            source_ref = Path(page["path"]).resolve().relative_to(vault_root).as_posix()
        except (KeyError, ValueError, OSError):
            continue
        observed = page.get("last_verified") or None  # date-only stays date-only; never invented
        page_hit = relevant(f"{source_ref} {page.get('title') or ''}")
        texts: list[str] = []
        summary = page.get("summary") or ""
        if summary and (page_hit or relevant(summary)):
            texts.append(summary)
        for key in ("findings", "conflicts", "open_questions"):
            for item in page.get(key) or []:
                if isinstance(item, str) and item.strip() and (page_hit or relevant(item)):
                    texts.append(item)
        for order, text in enumerate(texts):
            facts.append((
                (0 if page_hit else 1, NODE_BRIEF_STATUS_ORDER[status], source_ref, order),
                {"text": bounded_excerpt(text, 300), "source_ref": source_ref, "status": status,
                 "observed_at": observed},
            ))
    facts.sort(key=lambda row: row[0])
    per_source: dict[str, int] = {}
    capped: list[tuple[tuple, dict]] = []
    for row in facts:
        ref = row[1]["source_ref"]
        if per_source.get(ref, 0) >= NODE_BRIEF_FACTS_PER_SOURCE:
            continue
        per_source[ref] = per_source.get(ref, 0) + 1
        capped.append(row)
    facts_dropped_per_source = len(facts) - len(capped)
    facts = capped

    pending: list[dict] = []
    withheld = 0
    for item in brief.get("pending_state", []):
        summary = item.get("state_summary") or item.get("title")
        if not summary:
            withheld += 1
            continue
        if not relevant(summary):
            continue
        pending.append({
            "summary": bounded_excerpt(summary, 200),
            "source_ref": str(item.get("proposal_id") or "unknown"),
            "handoff_integrity": str(item.get("handoff_integrity") or "UNKNOWN"),
            "live_verification_required": True,
        })

    warnings: list[str] = [str(reason) for reason in brief.get("live_verification_reasons") or []]
    if brief.get("related_candidates") and len(brief.get("related_candidates")) > 1:
        warnings.append("related_projects_not_merged")
    if withheld:
        warnings.append(f"pending_text_withheld:{withheld}")
    if not facts and not pending:
        warnings.append("no_matching_knowledge")
    if facts_dropped_per_source:
        warnings.append(f"facts_capped_per_source:{facts_dropped_per_source}")
    if not inspection.get("fresh"):
        warnings.append(f"context_receipt_stale:{inspection.get('freshness_reason')}")
    if any(fact["observed_at"] is None or len(str(fact["observed_at"])) <= 10 for fact in (row[1] for row in facts)):
        warnings.append("observed_at_limited_precision")

    packet = {
        "schema_version": 1,
        "project_slug": brief.get("project_slug"),
        "node_id": node_id,
        "query": query,
        "generated_at": now_utc().isoformat().replace("+00:00", "Z"),
        "context_receipt": receipt,
        "continuity_status": brief.get("continuity_status") or "MISSING",
        "facts": [row[1] for row in facts],
        "pending": pending,
        "warnings": warnings,
        "truncated": False,
    }
    if brief.get("project_resolution") == "registry-alias":
        packet["project_resolution"] = "registry-alias"

    def size(value: dict) -> int:
        return len((node_brief_json(value) + "\n").encode("utf-8"))

    if size(packet) > NODE_BRIEF_MAX_BYTES:
        packet["truncated"] = True
        packet["warnings"] = packet["warnings"] + ["truncated_to_4096_bytes"]
        # 1) lower-ranked facts beyond the top ones, 2) oldest pending (the list
        # is newest first), 3) the remaining facts - so the newest state survives.
        while size(packet) > NODE_BRIEF_MAX_BYTES and len(packet["facts"]) > NODE_BRIEF_MIN_FACTS:
            packet["facts"].pop()
        while size(packet) > NODE_BRIEF_MAX_BYTES and len(packet["pending"]) > 1:
            packet["pending"].pop()
        while size(packet) > NODE_BRIEF_MAX_BYTES and packet["facts"]:
            packet["facts"].pop()
        while size(packet) > NODE_BRIEF_MAX_BYTES and packet["pending"]:
            packet["pending"].pop()
        if size(packet) > NODE_BRIEF_MAX_BYTES:
            packet["warnings"] = [w[:120] for w in packet["warnings"][:8]]
            if "truncated_to_4096_bytes" not in packet["warnings"]:
                packet["warnings"].append("truncated_to_4096_bytes")
    return packet


def newest_project_pages(vault: Path, paths: set[Path], main_page: str | None) -> list[str]:
    """Navigation order is separate from the existing stable_state order."""
    relative_paths = {path.relative_to(vault).as_posix() for path in paths}

    def order(relative: str) -> tuple:
        if relative.startswith("wiki/history/"):
            return (3, 0, relative)
        continuation = re.search(r"-cont-(\d+)\.md$", relative)
        if continuation:
            return (0, -int(continuation.group(1)), relative)
        if relative == main_page:
            return (1, 0, relative)
        return (2, 0, relative)

    return sorted(relative_paths, key=order)


def project_brief(vault: Path, args: argparse.Namespace) -> dict:
    registry, registry_warning = load_project_registry(vault)
    requested_project_ref, project_slug, project_resolution, related_candidates = resolve_brief_identity(
        vault, args.project_slug, registry
    )
    limit = int(getattr(args, "limit", 10) or 10)
    if limit < 1 or limit > 50:
        raise GatewayError("Project brief limit must be between 1 and 50.")
    offset = int(getattr(args, "offset", 0) or 0)
    if offset < 0:
        raise GatewayError("Project brief offset must be zero or greater.")
    query = str(getattr(args, "query", "") or "").strip().casefold()
    if len(query) > 200:
        raise GatewayError("Project brief query is too long.")
    assert_safe("Project query", query)
    exact_project_page = f"wiki/projects/{project_slug}.md"
    registry_entry = registry.get("projects", {}).get(project_slug)
    member_slugs = registry_project_slugs(registry, project_slug) if registry_entry else {project_slug}
    registered_pages = ({registry_entry[field] for field in ("main_page", "latest_page")
                         if registry_entry.get(field)} if registry_entry else set())

    stable_candidates: set[Path] = set()
    wiki_root = vault / "wiki"
    related_ids = [candidate for candidate in related_candidates if candidate != project_slug]
    related_pages: dict[str, list[dict]] = {candidate: [] for candidate in related_ids[:3]}
    wiki_contents: dict[Path, str] = {}
    for path in sorted(wiki_root.rglob("*.md")) if wiki_root.is_dir() else []:
        if path.name == "index.md":
            continue
        content = read_text(path)
        wiki_contents[path] = content
        explicit_project = fm_value(content, "project_slug")
        page_project = explicit_project
        if not page_project and path.parent == wiki_root / "projects" and fm_value(content, "type") != "evidence-ledger":
            page_project = path.stem
        relative = path.relative_to(vault).as_posix()
        if project_resolution != "ambiguous" and (page_project in member_slugs or (not explicit_project and relative in registered_pages)):
            stable_candidates.add(path)
        if page_project in related_pages and fm_value(content, "status") in {"reviewed", "verified"}:
            risky = any(risk_assessment(content).values())
            related_pages[page_project].append({
                "path": str(path.resolve()),
                "summary": None if risky else bounded_excerpt(fm_value(content, "summary") or "", 300),
                "trust": fm_value(content, "status"),
                "verification_scope": fm_value(content, "verification_scope"),
                "source_ids": fm_list(content, "sources"),
                "text_withheld": risky,
            })
    related_context = [{
        "project_slug": candidate,
        "status": "NOT_MERGED",
        "identity_resolution": "UNRESOLVED",
        "warning": "Related identity is unresolved; these reports are not authority for the selected project or permission to act.",
        "requires_live_verification": True,
        "pages": pages[:3],
        "page_total": len(pages),
        "has_more": len(pages) > 3,
        "next_command": f"project-brief --project-slug {candidate} --require-context",
    } for candidate, pages in related_pages.items()]

    direct_project_pages = set(stable_candidates)
    # Infrastructure context is admitted only by explicit project metadata or a
    # verified wikilink from an already admitted project page.
    infrastructure_candidates: set[Path] = set()
    for source_page in sorted(stable_candidates):
        source_content = wiki_contents[source_page]
        if fm_value(source_content, "status") not in {"reviewed", "verified"} or any(risk_assessment(source_content).values()):
            continue
        for target in page_links(source_content):
            linked = resolve_wiki_link(vault, source_page, target)
            if linked in wiki_contents and fm_value(wiki_contents[linked], "type") in {"infrastructure", "system"}:
                linked_project = fm_value(wiki_contents[linked], "project_slug")
                if linked_project and linked_project not in member_slugs:
                    continue
                infrastructure_candidates.add(linked)
    stable_candidates.update(infrastructure_candidates)

    stable_state: list[dict] = []
    stable_times: list[dt.datetime] = []
    # Ages of pages that claim INDEPENDENT verification, kept apart from
    # producer-attested ones. See stable_age_freshness below.
    attested_times: list[dt.datetime] = []
    producer_page_count = 0
    producer_page_seen = False
    for path in sorted(stable_candidates):
        content = wiki_contents[path]
        status_value = fm_value(content, "status") or "unknown"
        if status_value not in {"reviewed", "verified"}:
            continue
        summary = fm_value(content, "summary") or ""
        page_risk = risk_assessment(content)
        unsafe_page = any(page_risk.values())
        if query and (unsafe_page or query not in content.casefold()):
            continue
        last_verified = fm_value(content, "last_verified")
        timestamp = parse_recorded_time(last_verified)
        if timestamp:
            stable_times.append(timestamp)
        page_type = fm_value(content, "type") or ""
        verification_scope = fm_value(content, "verification_scope")
        producer_report = verification_scope == "producer-reports" or page_type in {"producer-report", "producer-reports"}
        producer_page_seen = producer_page_seen or producer_report
        if producer_report:
            producer_page_count += 1
        elif timestamp:
            attested_times.append(timestamp)
        truncated: set[str] = set()
        omitted: dict[str, int] = {}
        excerpts = bounded_sections("" if unsafe_page else content, ("findings", "conflicts", "open questions"),
                                    query=query, truncated=truncated, omitted=omitted)
        age_days = None
        if timestamp:
            age_days = round(max(0.0, (now_utc() - timestamp).total_seconds() / 86400), 3)
        stable_state.append(
            {
                "title": None if unsafe_page else fm_value(content, "title") or path.stem,
                "status": status_value,
                "trust": status_value,
                "summary": None if unsafe_page else bounded_excerpt(summary),
                "risk_flags": sorted(page_risk["secret_hits"] + page_risk["injection_hits"]),
                "last_verified": last_verified,
                "age_days": age_days,
                "source_ids": fm_list(content, "sources"),
                "path": str(path.resolve()),
                "type": page_type or None,
                "verification_scope": verification_scope,
                "requires_live_verification": producer_report,
                "findings": excerpts["findings"],
                "conflicts": excerpts["conflicts"],
                "open_questions": excerpts["open questions"],
                "excerpts_truncated": sorted(truncated),
                "excerpt_omitted": omitted,
                "query_matches": query_matches(content, query),
            }
        )

    pending_state: list[dict] = []
    pending_times: list[dt.datetime] = []
    source_index = {
        str(metadata.get("source_id")): (metadata, path)
        for metadata, path in source_records(vault)
        if metadata
    }
    event_records = readable_event_records(vault)
    event_lookup = critical_handoff_event_lookup(event_records)
    for content, path in ([] if project_resolution == "ambiguous" else proposal_records(vault)):
        if not any(proposal_matches_project(content, member, f"wiki/projects/{member}.md")
                   for member in member_slugs):
            continue
        proposal_status = fm_value(content, "status") or "unknown"
        if proposal_status not in {"proposed", "accepted"}:
            continue
        summary = fm_value(content, "state_summary") or ""
        proposal_risk = risk_assessment(content)
        unsafe_proposal = any(proposal_risk.values())
        if query and (unsafe_proposal or query not in content.casefold()):
            continue
        created_at = fm_value(content, "created_at")
        timestamp = parse_recorded_time(created_at)
        if timestamp:
            pending_times.append(timestamp)
        is_critical = fm_value(content, "knowledge_kind") == "critical-state"
        if is_critical:
            integrity = critical_handoff_integrity(
                vault,
                content,
                sources=source_index,
                events=event_records,
                event_lookup=event_lookup,
            )
            handoff_integrity = integrity["status"]
            trust = (
                "producer-attested-handoff"
                if handoff_integrity == "COMPLETE"
                else "unverified-proposal"
            )
            integrity_issues = integrity["issues"]
        else:
            handoff_integrity = "NOT_APPLICABLE"
            trust = "unverified-proposal"
            integrity_issues = []
        pending_state.append(
            {
                "proposal_id": fm_value(content, "id"),
                "title": None if unsafe_proposal else fm_value(content, "title"),
                "kind": fm_value(content, "knowledge_kind") or "general",
                "status": proposal_status,
                "trust": trust,
                "handoff_integrity": handoff_integrity,
                "integrity_issues": integrity_issues,
                "state_summary": None if unsafe_proposal else bounded_excerpt(summary),
                "risk_flags": sorted(proposal_risk["secret_hits"] + proposal_risk["injection_hits"]),
                # Never the text of an unapproved proposal - only that it matched
                # and where, matching the metadata-only boundary inventory keeps.
                "query_matches": None,
                "query_match_locations": query_locations(content, query),
                "created_at": created_at,
                "agent": fm_value(content, "agent"),
                "source_ids": fm_list(content, "source_ids"),
                "target_pages": fm_list(content, "target_pages"),
                "path": str(path.resolve()),
            }
        )
    stable_state.sort(key=lambda item: (item.get("path") or "", item.get("title") or ""))
    pending_state.sort(key=lambda item: (item.get("created_at") or "", item.get("proposal_id") or ""), reverse=True)
    all_pending_count = len(pending_state)
    pending_state = pending_state[offset:offset + limit]
    all_stable_count = len(stable_state)
    # stable_state is deliberately NOT paginated by offset/limit: those flags walk
    # the pending handoff queue, and the small trusted context must survive that
    # walk. Slicing it here emptied stable_state at high offsets and dropped the
    # project page - caught by test_brief_paginates_old_ordinary_proposals_and_
    # verified_infrastructure. Bounding stable volume needs its own knob.

    if all_pending_count and not all_stable_count:
        continuity_status = "PENDING_ONLY"
    elif all_stable_count and not all_pending_count:
        continuity_status = "STABLE_ONLY"
    elif not all_stable_count and not all_pending_count:
        continuity_status = "MISSING"
    elif pending_times and (not stable_times or max(pending_times) > max(stable_times)):
        continuity_status = "PENDING_NEWER_STATE"
    else:
        continuity_status = "STABLE_WITH_PENDING"
    latest_stable = max(stable_times) if stable_times else None
    stable_age_days = (
        round(max(0.0, (now_utc() - latest_stable).total_seconds() / 86400), 3)
        if latest_stable
        else None
    )
    # The age clock is only meaningful for pages that claim INDEPENDENT
    # verification. Producer-attested pages say "not independently verified" in
    # the first place, so calling them STALE after 14 days asserts that something
    # once checked has drifted - it never was checked. On this vault 118 of 129
    # pages were backfilled on 2026-09-11/12, so they share a birthday and would
    # all flip on 2026-09-25/26, turning the freshness signal back into constant
    # noise. Producer attestation is reported through its own reason instead.
    latest_attested = max(attested_times) if attested_times else None
    attested_age_days = (
        round(max(0.0, (now_utc() - latest_attested).total_seconds() / 86400), 3)
        if latest_attested
        else None
    )
    # These verdicts describe the whole stable set, so they must be derived from
    # all_stable_count rather than the page slice above.
    stable_age_freshness = (
        "MISSING"
        if not all_stable_count
        else "UNVERIFIED_BASELINE"
        if not attested_times
        else "UNKNOWN"
        if attested_age_days is None
        else "STALE"
        if attested_age_days > 14
        else "FRESH"
    )
    stable_freshness = stable_age_freshness
    if producer_page_seen and all_stable_count:
        stable_freshness = "LIVE_VERIFICATION_REQUIRED"
    pending_has_more = offset + limit < all_pending_count
    # Derived from what is actually returned, so it stays truthful instead of
    # being a hardcoded constant, and stays correct if stable ever gets a knob.
    stable_has_more = len(stable_state) < all_stable_count
    live_verification_reasons: list[str] = []
    if continuity_status != "STABLE_ONLY":
        live_verification_reasons.append(f"continuity_status:{continuity_status}")
    if stable_age_freshness == "STALE":
        live_verification_reasons.append(f"independently_verified_pages_older_than_14_days:{attested_age_days}")
    elif stable_age_freshness in {"UNKNOWN", "MISSING", "UNVERIFIED_BASELINE"}:
        live_verification_reasons.append(f"stable_age:{stable_age_freshness}")
    if producer_page_seen and all_stable_count:
        live_verification_reasons.append("stable_pages_are_producer_attested_not_independently_verified")
    total = all_stable_count + all_pending_count
    result = {
        "action": "project-brief",
        "requested_project_ref": requested_project_ref,
        "project_slug": project_slug,
        "project_resolution": project_resolution,
        "registry": registry_entry,
        "registry_warning": registry_warning,
        "navigation_page": registry.get("navigation_page"),
        "pages_newest_first": newest_project_pages(vault, direct_project_pages, registry_entry.get("main_page") if registry_entry else exact_project_page),
        "related_candidates": related_candidates,
        "related_context": related_context,
        "related_context_total": len(related_ids),
        "related_context_has_more": len(related_ids) > len(related_context),
        "project_resolution_warning": (
            "Exact canonical slug selected; related compact candidates are surfaced and not merged."
            if project_resolution == "exact" and len(related_candidates) > 1
            else "Ambiguous compact alias; no canonical project was merged."
            if project_resolution == "ambiguous"
            else None
        ),
        "query": query,
        "offset": offset,
        "limit": limit,
        "total": total,
        "stable_total": all_stable_count,
        "pending_total": all_pending_count,
        "stable_has_more": stable_has_more,
        "pending_has_more": pending_has_more,
        "has_more": stable_has_more or pending_has_more,
        # offset/limit walk pending_state only; stable_state is returned whole on
        # every page. A consumer paging by `total` (stable+pending) would page past
        # the end and re-receive the same stable pages each time.
        "pagination_scope": "pending_state",
        "stable_returned": len(stable_state),
        "pending_returned": len(pending_state),
        "continuity_status": continuity_status,
        "context_found": bool(all_stable_count or all_pending_count),
        "stable_freshness": stable_freshness,
        # The age verdict on its own. stable_freshness is overwritten by
        # LIVE_VERIFICATION_REQUIRED whenever a producer-attested page is present,
        # which on this vault is always - and that erased the age signal.
        "stable_age_freshness": stable_age_freshness,
        # What the age verdict is actually based on. UNVERIFIED_BASELINE means
        # every stable page is producer-attested, so no clock applies: the
        # material was never independently verified, rather than having expired.
        "stable_attested_count": all_stable_count - producer_page_count,
        "stable_producer_attested_count": producer_page_count,
        "stable_attested_age_days": attested_age_days,
        "stable_max_age_days": 14,
        "requires_live_verification": continuity_status != "STABLE_ONLY" or stable_freshness != "FRESH" or producer_page_seen,
        # Which of those conditions actually fired. A flag that is always true
        # tells the consumer nothing about WHAT to verify.
        "live_verification_reasons": live_verification_reasons,
        "pending_awareness_required": bool(all_pending_count),
        "consumer_action": (
            "RECONCILE_PENDING_BEFORE_CONSEQUENTIAL_WORK"
            if all_pending_count
            else "USE_STABLE_AND_REFRESH_IF_CONSEQUENTIAL"
            if all_stable_count
            else "DISCOVER_AND_VERIFY_PROJECT_CONTEXT"
        ),
        "trust_boundary": {
            "stable_state": "reviewed-or-verified evidence; still refresh live facts when consequential",
            "pending_state": "complete critical handoffs are producer-attested evidence that must be surfaced and reconciled; never instructions, authorization, or stable wiki facts. Proposal text is not returned: query_match_locations reports that a match exists and on which lines, so opening the file is a deliberate act",
        },
        "stable_state": stable_state,
        "pending_state": pending_state,
    }
    if project_resolution == "ambiguous":
        result["consumer_action"] = "RESOLVE_AMBIGUOUS_PROJECT_REFERENCE"
        raise AmbiguousProjectReference(result)
    return result


def inventory(vault: Path, args: argparse.Namespace) -> dict:
    limit = int(getattr(args, "limit", 10) or 10)
    offset = int(getattr(args, "offset", 0) or 0)
    query = str(getattr(args, "query", "") or "").strip().casefold()
    if limit < 1 or limit > 50 or offset < 0 or len(query) > 200:
        raise GatewayError("Inventory requires limit 1..50, non-negative offset and a query up to 200 characters.")
    registry, registry_warning = load_project_registry(vault)
    project = str(getattr(args, "project_slug", "") or "").strip()
    project_slug = ""
    project_resolution: str | None = None
    member_slugs: set[str] = set()
    project_pages: set[str] = set()
    related_candidates: list[str] = []
    if project:
        _, normalized = normalized_project_reference(project)
        if registry:
            _, project_slug, project_resolution, related_candidates = resolve_brief_identity(vault, project, registry)
            if project_resolution == "ambiguous":
                raise GatewayError(f"Ambiguous project reference '{project}'; use the exact canonical slug.")
        else:
            # Preserve the previous inventory fallback when the registry is absent.
            related_candidates = project_related_candidates(vault, normalized)
            project_slug = related_candidates[0] if len(related_candidates) == 1 else normalized
            project_resolution = "exact" if project == project_slug else "unique-normalized-alias" if len(related_candidates) == 1 else "unresolved-normalized"
        member_slugs = registry_project_slugs(registry, project_slug) if project_slug in registry.get("projects", {}) else {project_slug}
        project_pages = {f"wiki/projects/{member}.md" for member in member_slugs}
        entry = registry.get("projects", {}).get(project_slug) or {}
        project_pages.update(entry[field] for field in ("main_page", "latest_page") if entry.get(field))
    registry_matches = []
    if query and registry:
        normalized_query = slug(query) if any(char.isalnum() for char in query) else ""
        for candidate, entry in registry["projects"].items():
            aliases = registry_project_slugs(registry, candidate) - {candidate}
            if any(query in value.casefold() or (normalized_query and normalized_query in slug(value))
                   for value in (candidate, entry["title"], *aliases)):
                registry_matches.append({
                    "slug": candidate,
                    "title": entry["title"],
                    "family": entry["family"],
                    "main_page": entry["main_page"],
                    "latest_page": entry["latest_page"],
                })
        registry_matches.sort(key=lambda item: (0 if query == item["slug"] else 1, item["slug"]))
    records: list[dict] = []

    for content, path in proposal_records(vault):
        explicit_project = fm_value(content, "project_slug") or ""
        target_project = (any(proposal_matches_project(content, member, f"wiki/projects/{member}.md")
                              for member in member_slugs) if project_slug else True)
        if not target_project:
            continue
        metadata = " ".join(
            [fm_value(content, "title") or "", explicit_project, fm_value(content, "id") or "", fm_value(content, "status") or ""]
        ).casefold()
        risk = risk_assessment(content)
        if query and query not in metadata:
            continue
        withheld = bool(any(risk.values()))
        records.append({
            "kind": "proposal",
            "id": fm_value(content, "id"),
            # A title is content too: a page titled "Leak ghp_..." shipped the whole
            # token while the same record advertised text_withheld: true.
            # project_brief already nulls the title of a risky page; inventory did not.
            "title": None if withheld else (fm_value(content, "title") or path.stem),
            "status": fm_value(content, "status") or "unknown",
            "project_slug": explicit_project or None,
            "knowledge_kind": fm_value(content, "knowledge_kind") or "general",
            "created_at": fm_value(content, "created_at"),
            "path": str(path.resolve()),
            "risk_flags": sorted(risk["secret_hits"] + risk["injection_hits"]),
            "text_withheld": bool(any(risk.values())),
        })

    for path in sorted((vault / "wiki").rglob("*.md")) if (vault / "wiki").is_dir() else []:
        if path.name == "index.md":
            continue
        try:
            content = read_text(path)
        except (OSError, UnicodeError):
            continue
        explicit_project = fm_value(content, "project_slug") or ""
        if project_slug and explicit_project not in member_slugs:
            relative = path.relative_to(vault).as_posix()
            if relative not in project_pages or (project_slug in registry.get("projects", {}) and explicit_project):
                continue
        title = fm_value(content, "title") or path.stem
        metadata = " ".join([title, explicit_project, fm_value(content, "type") or "", fm_value(content, "status") or ""]).casefold()
        risk = risk_assessment(content)
        # Curated wiki pages are the trusted tier, so their body is searchable.
        # Matching metadata alone made the curated corpus unreachable: "Docker"
        # appears in 20 reviewed pages and returned zero of them. Proposals and
        # sources stay metadata-only - their text remains untrusted.
        matched_in = ""
        if query:
            if query in metadata:
                matched_in = "metadata"
            elif query in content.casefold():
                matched_in = "body"
            else:
                continue
        withheld = bool(any(risk.values()))
        records.append({
            "kind": "wiki",
            "title": None if withheld else title,
            "status": fm_value(content, "status") or "unknown",
            "project_slug": explicit_project or None,
            "type": fm_value(content, "type") or None,
            "path": str(path.resolve()),
            "matched_in": matched_in or None,
            "risk_flags": sorted(risk["secret_hits"] + risk["injection_hits"]),
            "text_withheld": withheld,
        })

    # Curated project pages lead; historical evidence ledgers stay discoverable
    # but cannot crowd them off the first result page.
    def navigation_rank(item: dict) -> int:
        if item.get("kind") != "wiki":
            return 2
        relative = Path(str(item.get("path") or "")).relative_to(vault.resolve()).as_posix()
        if relative.startswith("wiki/history/openclaw-") and item.get("type") == "evidence-ledger":
            return 4
        if relative.startswith("wiki/history/"):
            return 3
        if relative.startswith("wiki/projects/") or item.get("type") == "project":
            return 0
        return 1

    records.sort(key=lambda item: (
        navigation_rank(item),
        str(item.get("created_at") or ""),
        str(item.get("path") or ""),
    ))
    total = len(records)
    return {
        "action": "inventory",
        "project_slug": project_slug or None,
        "project_resolution": project_resolution,
        "registry_warning": registry_warning,
        "registry_matches": registry_matches,
        "related_candidates": related_candidates,
        "query": query,
        "offset": offset,
        "limit": limit,
        "total": total,
        "has_more": offset + limit < total,
        "records": records[offset:offset + limit],
        "trust_boundary": "metadata-only inventory; risky source/proposal text is withheld and remains untrusted",
    }


def receipt_root(vault: Path, requested: str | None) -> Path:
    try:
        root = Path(requested).expanduser() if requested else Path.home() / ".agents" / "knowledge-context-receipts"
        resolved = root.resolve()
    except (OSError, ValueError, RuntimeError) as exc:
        raise GatewayError("Receipt root could not be resolved.") from exc
    try:
        resolved.relative_to(vault.resolve())
    except ValueError:
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved
    raise GatewayError("Receipt root must be outside the Knowledge Hub vault.")


def write_context_receipt(vault: Path, root: Path, result: dict) -> Path:
    vault_digest = hashlib.sha256(str(vault.resolve()).encode("utf-8")).hexdigest()[:24]
    target = root / vault_digest
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{now_utc().strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}.json"
    write_json(path, result, exclusive=True)
    return path


def vault_binding(vault: Path) -> dict:
    return {
        "version": VAULT_BINDING_VERSION,
        "vault_path_sha256": hashlib.sha256(os.path.normcase(str(vault.resolve())).encode("utf-8")).hexdigest(),
        "kind": "local-execution-evidence",
        "producer_authentication": "none",
    }


def resolve_context_receipt_path(vault: Path, requested: str | None) -> Path:
    candidate = str(requested or "").strip()
    if not candidate:
        raise ContextReceiptError("MISSING", "verify-context requires --context-receipt.")
    path = Path(os.path.abspath(Path(candidate).expanduser()))
    try:
        if not os.path.lexists(path):
            raise ContextReceiptError("MISSING", "Context receipt does not exist.")
        if path_has_reparse_component(Path(path.anchor), path):
            raise ContextReceiptError("UNSAFE_PATH", "Context receipt path contains a reparse component.")
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ContextReceiptError("MISSING", "Context receipt does not exist.") from exc
    if not resolved.is_file() or path.is_symlink():
        raise ContextReceiptError("UNSAFE_PATH", "Context receipt must be a regular file.")
    try:
        resolved.relative_to(vault.resolve())
    except ValueError:
        return resolved
    raise ContextReceiptError("UNSAFE_PATH", "Context receipt must be outside the Knowledge Hub vault.")


def context_receipt_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate context receipt key.")
        result[key] = value
    return result


def context_receipt_created_at(value: object) -> dt.datetime:
    if not isinstance(value, str):
        raise ContextReceiptError("MALFORMED", "Context receipt created_at is missing or invalid.")
    parsed = parse_recorded_time(value)
    if parsed is None:
        raise ContextReceiptError("MALFORMED", "Context receipt created_at is missing or invalid.")
    return parsed


def inspect_context_receipt(
    vault: Path,
    receipt_path: str,
    agent: str,
    session_id: str,
    project_ref: str,
) -> dict:
    path = resolve_context_receipt_path(vault, receipt_path)
    try:
        content, _ = read_regular_file_snapshot(Path(path.anchor), path, max_bytes=1024 * 1024)
        payload = json.loads(content.decode("utf-8-sig"), object_pairs_hook=context_receipt_object)
    except (OSError, UnicodeError, ValueError, GatewayError) as exc:
        raise ContextReceiptError("MALFORMED", "Context receipt is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise ContextReceiptError("MALFORMED", "Context receipt must be a JSON object.")
    schema_version = payload.get("schema_version")
    binding = payload.get("vault_binding")
    if type(schema_version) is not int or schema_version != CONTEXT_RECEIPT_SCHEMA_VERSION or not isinstance(binding, dict):
        raise ContextReceiptError(
            "LEGACY_UNBOUND",
            "Context receipt is not a versioned vault-bound begin-task receipt.",
        )
    execution = payload.get("execution")
    attempts = execution.get("attempts") if isinstance(execution, dict) else None
    if not isinstance(attempts, list) or not attempts or not isinstance(attempts[-1], dict):
        raise ContextReceiptError("MALFORMED", "Context receipt execution attempts are missing.")
    if payload.get("status") != "COMPLETE" or attempts[-1].get("status") != "COMPLETE":
        raise ContextReceiptError("EXECUTION_FAILED", "Context receipt did not complete successfully.")
    if (
        payload.get("action") != "begin-task"
        or type(binding.get("version")) is not int
        or binding.get("version") != VAULT_BINDING_VERSION
        or binding.get("kind") != "local-execution-evidence"
        or binding.get("producer_authentication") != "none"
        or not SHA256_HEX_PATTERN.fullmatch(str(binding.get("vault_path_sha256") or ""))
        or not SHA256_HEX_PATTERN.fullmatch(str(payload.get("session_sha256") or ""))
        or not SHA256_HEX_PATTERN.fullmatch(str(payload.get("context_fingerprint") or ""))
        or not isinstance(payload.get("health"), dict)
        or not isinstance(payload.get("execution"), dict)
        or not isinstance(payload.get("source_hashes"), dict)
        or not isinstance(payload.get("require_context"), bool)
        or not isinstance(payload.get("coherent_read"), bool)
    ):
        raise ContextReceiptError("MALFORMED", "Context receipt is missing required bound metadata.")
    expected_binding = vault_binding(vault)
    if binding.get("vault_path_sha256") != expected_binding["vault_path_sha256"]:
        raise ContextReceiptError("VAULT_MISMATCH", "Context receipt is bound to a different vault identity.")
    caller_agent = slug(agent)
    if not caller_agent or caller_agent == "unknown" or caller_agent != payload.get("agent"):
        raise ContextReceiptError("CALLER_MISMATCH", "Context receipt agent does not match the caller.")
    session_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    if not session_id.strip() or session_digest != payload.get("session_sha256"):
        raise ContextReceiptError("CALLER_MISMATCH", "Context receipt session does not match the caller.")
    requested, project_slug, project_resolution, related_candidates = resolve_brief_identity(vault, project_ref)
    if project_resolution == "ambiguous":
        raise ContextReceiptError(
            "PROJECT_AMBIGUOUS",
            f"Project reference '{requested}' is ambiguous: {', '.join(related_candidates)}.",
        )
    if project_slug != payload.get("project_slug") or not PROJECT_SLUG_PATTERN.fullmatch(str(payload.get("project_slug") or "")):
        raise ContextReceiptError("PROJECT_MISMATCH", "Context receipt project does not match the resolved caller project.")
    created_at = context_receipt_created_at(payload.get("created_at"))
    now = now_utc()
    if created_at - now > CONTEXT_RECEIPT_CLOCK_SKEW:
        raise ContextReceiptError("TIMESTAMP_FUTURE", "Context receipt timestamp is in the future.")
    attempts = payload.get("execution", {}).get("attempts")
    if not isinstance(attempts, list) or not attempts or not isinstance(attempts[-1], dict):
        raise ContextReceiptError("MALFORMED", "Context receipt execution attempts are missing.")
    final = attempts[-1]
    commands = ((final.get("execution") or {}).get("commands") if isinstance(final.get("execution"), dict) else None) or []
    revision_before = final.get("revision_before")
    revision_after = final.get("revision_after")
    health = payload["health"]
    if final.get("status") != "COMPLETE":
        raise ContextReceiptError("EXECUTION_FAILED", "Context receipt final attempt was not a complete execution.")
    if (
        not final.get("coherent_read")
        or payload.get("coherent_read") is not True
        or revision_before != revision_after
        or revision_after != payload.get("source_revision")
        or not SHA256_HEX_PATTERN.fullmatch(str(revision_before or ""))
    ):
        raise ContextReceiptError(
            "EXECUTION_INCOHERENT",
            "Context receipt does not record a coherent before/after revision on the successful final attempt.",
        )
    if (
        health.get("status") != "PASS"
        or health.get("lint") != "PASS"
        or type(health.get("lint_error_count")) is not int
        or health.get("lint_error_count") != 0
        or "status" not in commands
        or "lint" not in commands
        or "project-brief" not in commands
    ):
        raise ContextReceiptError("HEALTH_FAILED", "Context receipt does not record successful status, lint, and project-brief.")
    if payload.get("require_context") and not health.get("context_found"):
        raise ContextReceiptError("HEALTH_FAILED", "Context receipt required project context but did not record it.")
    try:
        current_fingerprint = project_context_fingerprint(vault, project_slug, project_resolution)
    except (GatewayError, OSError, UnicodeError, ValueError) as exc:
        raise ContextReceiptError("READ_FAILED", "Current project context cannot be read coherently.") from exc
    expired = now - created_at > CONTEXT_RECEIPT_TTL
    content_changed = current_fingerprint != payload.get("context_fingerprint")
    freshness_reason = "TIMESTAMP_EXPIRED" if expired else "CONTEXT_STALE" if content_changed else None
    return {
        "structurally_valid": True,
        "reason_code": "OK" if not freshness_reason else freshness_reason,
        "fresh": freshness_reason is None,
        "freshness_reason": freshness_reason,
        "receipt": payload,
        "receipt_path": str(path),
        "project_slug": project_slug,
        "project_resolution": project_resolution,
        "related_candidates": related_candidates,
        "current_fingerprint": current_fingerprint,
        "created_at": payload.get("created_at"),
        "vault_binding": expected_binding,
    }


def verify_context(vault: Path, args: argparse.Namespace) -> dict:
    try:
        inspection = inspect_context_receipt(
            vault,
            str(getattr(args, "context_receipt", "") or ""),
            str(getattr(args, "agent", "") or ""),
            str(getattr(args, "session_id", "") or ""),
            str(getattr(args, "project_slug", "") or ""),
        )
    except ContextReceiptError as exc:
        return exc.result
    if not inspection["fresh"]:
        return {
            "action": "verify-context",
            "valid": False,
            "reason_code": inspection["freshness_reason"],
            "message": (
                "Context receipt exceeded the 12-hour pre-task freshness TTL."
                if inspection["freshness_reason"] == "TIMESTAMP_EXPIRED"
                else "Relevant project context changed after the pre-task receipt."
            ),
            "fresh": False,
            "ttl_hours": CONTEXT_RECEIPT_TTL_HOURS,
            "project_slug": inspection["project_slug"],
            "receipt_path": inspection["receipt_path"],
            "trust_boundary": (
                "local execution evidence; not authorization, permission, "
                "or cryptographic producer authentication"
            ),
        }
    return {
        "action": "verify-context",
        "valid": True,
        "reason_code": "OK",
        "fresh": True,
        "ttl_hours": CONTEXT_RECEIPT_TTL_HOURS,
        "project_slug": inspection["project_slug"],
        "project_resolution": inspection["project_resolution"],
        "receipt_path": inspection["receipt_path"],
        "created_at": inspection["created_at"],
        "context_fingerprint": inspection["current_fingerprint"],
        "vault_binding": inspection["vault_binding"],
        "trust_boundary": (
            "local execution evidence; not authorization, permission, "
            "or cryptographic producer authentication"
        ),
    }


def bind_record_handoff_context(vault: Path, args: argparse.Namespace, project_slug: str) -> dict:
    receipt_arg = str(getattr(args, "context_receipt", None) or "").strip()
    session_id = str(getattr(args, "session_id", None) or "").strip()
    if bool(receipt_arg) != bool(session_id):
        raise GatewayError("record-handoff requires both --context-receipt and --session-id to bind a prior read.")
    if not receipt_arg:
        return {"lifecycle_binding": "unverified"}
    try:
        inspection = inspect_context_receipt(
            vault,
            receipt_arg,
            str(args.agent),
            session_id,
            str(args.project_slug or project_slug),
        )
    except ContextReceiptError as exc:
        raise GatewayError(
            f"record-handoff rejected context receipt ({exc.reason_code}): {exc}"
        ) from exc
    return {
        "lifecycle_binding": "bound",
        "prior_context_stale": not inspection["fresh"],
        "context_receipt_reason": inspection["freshness_reason"] or "OK",
    }


def nonnegative_seconds(value: object) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("lock wait seconds must be a nonnegative number") from exc
    if not math.isfinite(seconds) or seconds < 0:
        raise argparse.ArgumentTypeError("lock wait seconds must be a nonnegative number")
    return seconds


def begin_task(vault: Path, args: argparse.Namespace, *, sleep=None, clock=None) -> dict:
    if not args.agent.strip() or slug(args.agent) == "unknown":
        raise GatewayError("begin-task requires an identified --agent.")
    if not args.session_id.strip():
        raise GatewayError("begin-task requires --session-id.")
    assert_safe("Agent", args.agent)
    _, normalized_project = normalized_project_reference(args.project_slug)
    assert_safe("Project reference", args.project_slug)
    session_digest = hashlib.sha256(args.session_id.encode("utf-8")).hexdigest()
    root = receipt_root(vault, getattr(args, "receipt_root", None))
    attempts: list[dict] = []
    context: dict | None = None
    source_revision: str | None = None
    source_hashes: dict[str, str] = {}
    context_fingerprint: str | None = None
    health: dict = {}
    lock_path = vault / "governance" / "curator.lock"
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    lock_wait_limit = nonnegative_seconds(getattr(args, "lock_wait_seconds", os.environ.get("KNOWLEDGE_LOCK_WAIT_SECONDS", "1200")))
    lock_deadline = clock() + lock_wait_limit
    for attempt in range(1, 3):
        context_fingerprint = None
        health = {"status": "NOT_RUN", "lint": "NOT_RUN"}
        commands: list[str] = []
        attempt_result: dict = {
            "execution": {"attempt": attempt, "commands": commands},
            "status": "FAILED", "coherent_read": False, "retry_required": False,
            "lock_before": os.path.lexists(lock_path),
            "lock_wait_seconds": 0.0,
        }
        attempts.append(attempt_result)
        try:
            wait_started = clock()
            while os.path.lexists(lock_path) and lock_wait_limit > 0:
                owner = curator_lock_owner_state(lock_path)
                if owner["state"] == "dead":
                    attempt_result.update(failure_code="CURATOR_LOCK_STALE", retry_required=True,
                                          curator_lock_owner=owner)
                    break
                remaining = lock_deadline - clock()
                if remaining <= 0:
                    break
                sleep(min(5.0, remaining))
            attempt_result["lock_wait_seconds"] = round(max(0.0, clock() - wait_started), 3)
            if attempt_result.get("failure_code") == "CURATOR_LOCK_STALE":
                break
            if os.path.lexists(lock_path):
                attempt_result.update(failure_code="CURATOR_LOCK", retry_required=True)
                continue
            before_revision, _ = project_snapshot(vault, args.project_slug)
            source_revision = before_revision
            attempt_result["revision_before"] = before_revision
            commands.append("status")
            status_result = status(vault)
            health.update({
                "status": "FAILED" if status_result.get("dirty_tree_state") == "unavailable" else "PASS",
                "dirty_tree_state": status_result.get("dirty_tree_state"),
                "dirty_tree_write_blocked": status_result.get("dirty_tree_write_blocked"),
                "curation_health": status_result.get("curation_health"),
            })
            commands.append("lint")
            lint_result = lint(vault)
            health.update(lint="PASS" if lint_result["error_count"] == 0 else "FAILED",
                          lint_error_count=lint_result["error_count"],
                          lint_warning_count=lint_result["warning_count"])
            brief_result = None
            if not lint_result["error_count"] and health["status"] == "PASS":
                commands.append("project-brief")
                try:
                    brief_result = project_brief(vault, argparse.Namespace(
                        project_slug=args.project_slug, limit=getattr(args, "limit", 10),
                        offset=0, query="", require_context=bool(args.require_context),
                    ))
                except AmbiguousProjectReference as exc:
                    brief_result = exc.result
            attempt_fingerprint = None
            if brief_result is not None:
                attempt_fingerprint = project_context_fingerprint(
                    vault, brief_result["project_slug"], brief_result["project_resolution"]
                )
            after_revision, after_source_hashes = project_snapshot(vault, args.project_slug)
            source_revision = after_revision
            lock_after = os.path.lexists(lock_path)
            coherent = not status_result["curator_lock"] and not lock_after and before_revision == after_revision
            attempt_result.update({
                "revision_after": after_revision,
                "coherent_read": coherent,
                "lock_after": lock_after,
            })
            if not coherent:
                attempt_result.update(failure_code="CURATOR_LOCK" if lock_after or status_result["curator_lock"] else "REVISION_CHANGED",
                                      retry_required=True)
                continue
            if brief_result is None:
                attempt_result["failure_code"] = "LINT_FAILED" if lint_result["error_count"] else "STATUS_FAILED"
                break
            context = brief_result
            health.update({
                "context_found": bool(context["context_found"]),
                "project_resolution": context["project_resolution"],
                "related_candidates": context["related_candidates"],
                "project_resolution_warning": context["project_resolution_warning"],
            })
            referenced = {source_id for item in context["stable_state"] + context["pending_state"]
                          for source_id in item.get("source_ids", [])}
            referenced.update(source_id for group in context["related_context"] for page in group["pages"]
                              for source_id in page.get("source_ids", []))
            source_hashes = {source_id: after_source_hashes[source_id]
                             for source_id in sorted(referenced) if source_id in after_source_hashes}
            if context["project_resolution"] == "ambiguous" or (args.require_context and not context["context_found"]):
                attempt_result["failure_code"] = "AMBIGUOUS_PROJECT_REFERENCE" if context["project_resolution"] == "ambiguous" else "CONTEXT_REQUIRED"
                break
            attempt_result["status"] = "COMPLETE"
            context_fingerprint = attempt_fingerprint
            break
        except (GatewayError, OSError, UnicodeError, json.JSONDecodeError) as exc:
            attempt_result.update({
                "failure_code": "READ_FAILED",
                "error_class": type(exc).__name__,
                "lock_after": os.path.lexists(lock_path),
            })
            break
    final = attempts[-1]
    bound_project = context["project_slug"] if context else normalized_project
    bound_resolution = context["project_resolution"] if context else "unresolved-normalized"
    receipt = {
        "schema_version": CONTEXT_RECEIPT_SCHEMA_VERSION,
        "action": "begin-task",
        "status": final.get("status", "FAILED"),
        "agent": slug(args.agent),
        "session_sha256": session_digest,
        "project_slug": bound_project,
        "project_resolution": bound_resolution,
        "require_context": bool(args.require_context),
        "health": health,
        "source_revision": source_revision,
        "source_hashes": source_hashes,
        "context_fingerprint": context_fingerprint,
        "vault_binding": vault_binding(vault),
        "coherent_read": final["coherent_read"],
        "retry_required": final["retry_required"],
        "failure_code": final.get("failure_code"),
        "lock_wait_seconds": round(sum(item["lock_wait_seconds"] for item in attempts), 3),
        "execution": {"attempts": attempts, "comprehension": "not measured"},
        "created_at": iso_time(now_utc()),
    }
    receipt_path = write_context_receipt(vault, root, receipt)
    return {**receipt, "receipt_path": str(receipt_path), "context": context}


def lint(vault: Path, *, registry_stage: str = "F1") -> dict:
    from registry_v2_lint import validate_page

    if registry_stage not in {"F1", "F2"}:
        raise GatewayError("Registry lint stage must be F1 or F2.")
    errors: list[str] = []
    warnings: list[str] = []
    registry_path = vault.parent / "registry" / "registry-v2.json"
    registry_v2 = None
    try:
        registry_bytes, _ = read_regular_file_snapshot(
            vault.parent, registry_path, max_bytes=4 * 1024 * 1024
        )
        registry_v2 = json.loads(registry_bytes.decode("utf-8-sig"))
    except (OSError, UnicodeError, ValueError, GatewayError):
        pass  # The module reports unavailable registries for pages with references.
    try:
        config = json.loads(read_text(vault / "knowledge-vault.json"))
        if not str(config.get("curator_machine") or "").strip():
            errors.append("Vault curator_machine is not configured.")
        if config.get("curation_scope") != "designated-machine-only":
            errors.append("Vault curation_scope must be designated-machine-only.")
    except (OSError, UnicodeError, json.JSONDecodeError):
        config = {"curator_lock_minutes": 30}
        errors.append(f"Invalid vault configuration: {vault / 'knowledge-vault.json'}")

    source_map: dict[str, Path] = {}
    source_detail_map: dict[str, tuple[dict, Path]] = {}
    required_source = {"schema_version", "source_id", "title", "origin", "captured_at", "captured_by", "classification", "local_only", "original_file", "byte_length", "sha256", "trust", "risk_flags"}
    def check_source(metadata: dict | None, metadata_path: Path) -> None:
            if metadata is None:
                errors.append(f"Invalid source metadata JSON: {metadata_path}")
                return
            for field in sorted(required_source - set(metadata)):
                errors.append(f"Missing source metadata '{field}': {metadata_path}")
            source_id = str(metadata.get("source_id") or "")
            if not SOURCE_ID_PATTERN.fullmatch(source_id):
                errors.append(f"Invalid source ID: {source_id}")
            if source_id in source_map:
                errors.append(f"Duplicate source ID: {source_id}")
            source_map[source_id] = metadata_path
            source_detail_map[source_id] = (metadata, metadata_path)
            if metadata_path.parent.name != source_id:
                errors.append(f"Source directory does not match source ID: {source_id}")
            if metadata.get("classification") not in {"public", "internal"} or bool(metadata.get("local_only")):
                errors.append(f"Shared source has disallowed classification/local-only state: {source_id}")
            if not AGENT_PATTERN.fullmatch(str(metadata.get("captured_by") or "")):
                errors.append(f"Invalid source captured_by: {source_id}")
            source_project_slug = str(metadata.get("project_slug") or "")
            source_knowledge_kind = str(metadata.get("knowledge_kind") or "")
            source_state_summary = str(metadata.get("state_summary") or "")
            if source_project_slug or source_knowledge_kind or source_state_summary:
                if not PROJECT_SLUG_PATTERN.fullmatch(source_project_slug) or slug(source_project_slug) != source_project_slug:
                    errors.append(f"Invalid source project_slug: {source_id}")
                if source_knowledge_kind != "critical-state":
                    errors.append(f"Invalid source knowledge_kind: {source_id}")
                if not source_state_summary or len(source_state_summary) > 500 or "\n" in source_state_summary or "\r" in source_state_summary:
                    errors.append(f"Invalid source state_summary: {source_id}")
            digest = str(metadata.get("sha256") or "")
            original = metadata_path.parent / str(metadata.get("original_file") or "")
            if not re.fullmatch(r"[a-f0-9]{64}", digest):
                errors.append(f"Invalid source SHA-256: {source_id}")
            elif not original.is_file():
                errors.append(f"Missing source original: {source_id}")
            elif sha256_file(original) != digest:
                errors.append(f"Source hash changed: {source_id}")
            elif original.suffix.lower() not in TEXT_EXTENSIONS:
                errors.append(f"Non-text original found in shared source: {source_id}")
            elif original.stat().st_size > 5 * 1024 * 1024:
                errors.append(f"Shared source exceeds the 5 MB safety scan limit: {source_id}")
            else:
                source_risk = risk_assessment(read_text(original))
                if source_risk["secret_hits"]:
                    errors.append(f"Secret indicator in source original: {source_id}")
            risk = risk_assessment("\n".join(str(metadata.get(key) or "") for key in ("title", "origin", "captured_by", "project_slug", "state_summary")))
            if risk["secret_hits"] or risk["injection_hits"]:
                errors.append(f"Unsafe source metadata: {source_id}")
            if "binary-content-not-text-scanned-local-only" in (metadata.get("risk_flags") or []):
                errors.append(f"Unscanned binary found in shared source: {source_id}")

    def late_source(source_id: str) -> bool:
        """Accept a source package another session completed after the source scan started."""
        if source_id in source_map:
            return True
        if not SOURCE_ID_PATTERN.fullmatch(source_id):
            return False
        stamp = source_id[4:12]
        metadata_path = vault / "raw" / "sources" / stamp[:4] / stamp[4:6] / source_id / "source.json"
        if not metadata_path.is_file():
            return False
        try:
            metadata = json.loads(read_text(metadata_path))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        if not isinstance(metadata, dict) or str(metadata.get("source_id") or "") != source_id:
            return False
        check_source(metadata, metadata_path)
        return True

    for metadata, metadata_path in source_records(vault):
        check_source(metadata, metadata_path)

    wiki_root = vault / "wiki"
    all_wiki = sorted(wiki_root.rglob("*.md")) if wiki_root.is_dir() else []
    wiki_files = [path for path in all_wiki if path.name != "index.md"]
    page_keys = {path.stem.lower() for path in all_wiki}
    page_keys.update(path.relative_to(wiki_root).with_suffix("").as_posix().lower() for path in all_wiki)
    for path in wiki_files:
        content = read_text(path)
        for finding in validate_page(content, registry_v2, stage=registry_stage):
            target = errors if finding["severity"] == "error" else warnings
            target.append(f"Registry v2 {finding['code']}: {finding['message']}: {path}")
        for field in ("title", "type", "status", "summary", "last_verified", "sources"):
            if fm_value(content, field) is None:
                errors.append(f"Missing frontmatter '{field}': {path}")
        page_status = fm_value(content, "status") or ""
        if page_status and page_status not in {"draft", "reviewed", "verified", "stale", "archived"}:
            errors.append(f"Invalid status '{page_status}': {path}")
        page_type = fm_value(content, "type") or ""
        page_slug = fm_value(content, "project_slug") or ""
        if page_type == "project" and not page_slug:
            errors.append(f"Project page without project_slug: {path}")
        if page_slug and (not PROJECT_SLUG_PATTERN.fullmatch(page_slug) or slug(page_slug) != page_slug):
            errors.append(f"Invalid page project_slug '{page_slug}': {path}")
        page_sources = fm_list(content, "sources")
        if page_status == "verified" and fm_value(content, "type") != "system" and not page_sources:
            errors.append(f"Verified non-system page has no sources: {path}")
        for source_id in page_sources:
            if source_id not in source_map:
                errors.append(f"Unknown source ID '{source_id}': {path}")
        risk = risk_assessment(content)
        if risk["secret_hits"] or risk["injection_hits"]:
            errors.append(f"Unsafe content in stable wiki: {path}")
        for match in re.finditer(r"\[\[([^\]|#]+)", content):
            target = match.group(1).strip().replace("\\", "/").removesuffix(".md").lower()
            if target not in page_keys and Path(target).name not in page_keys:
                warnings.append(f"Unresolved wikilink '[[{match.group(1)}]]': {path}")

    proposal_map: dict[str, Path] = {}
    proposal_files = sorted((vault / "proposals").rglob("*.md")) if (vault / "proposals").is_dir() else []
    def check_proposal(path: Path) -> None:
            content = read_text(path)
            for field in ("id", "type", "status", "title", "agent", "created_at", "classification", "source_ids", "target_pages"):
                if fm_value(content, field) is None:
                    errors.append(f"Missing proposal frontmatter '{field}': {path}")
            proposal_id = fm_value(content, "id") or ""
            agent = fm_value(content, "agent") or ""
            if not PROPOSAL_ID_PATTERN.fullmatch(proposal_id) or not path.name.startswith(proposal_id + "-"):
                errors.append(f"Invalid proposal ID or filename: {path}")
            if proposal_id in proposal_map:
                errors.append(f"Duplicate proposal ID: {proposal_id}")
            proposal_map[proposal_id] = path
            if fm_value(content, "type") not in {None, "proposal"} or fm_value(content, "status") not in {"proposed", "accepted", "rejected", "superseded"}:
                errors.append(f"Invalid proposal type/status: {path}")
            if fm_value(content, "classification") not in {"public", "internal"}:
                errors.append(f"Invalid proposal classification: {path}")
            if agent and path.parent.name != agent:
                errors.append(f"Proposal directory does not match agent: {path}")
            proposal_project_slug = fm_value(content, "project_slug") or ""
            proposal_knowledge_kind = fm_value(content, "knowledge_kind") or ""
            proposal_state_summary = fm_value(content, "state_summary") or ""
            if proposal_project_slug or proposal_knowledge_kind or proposal_state_summary:
                if not PROJECT_SLUG_PATTERN.fullmatch(proposal_project_slug) or slug(proposal_project_slug) != proposal_project_slug:
                    errors.append(f"Invalid proposal project_slug: {path}")
                if proposal_knowledge_kind != "critical-state":
                    errors.append(f"Invalid proposal knowledge_kind: {path}")
                if not proposal_state_summary or len(proposal_state_summary) > 500 or "\n" in proposal_state_summary or "\r" in proposal_state_summary:
                    errors.append(f"Invalid proposal state_summary: {path}")
            for source_id in fm_list(content, "source_ids"):
                if not late_source(source_id):
                    errors.append(f"Unknown proposal source ID '{source_id}': {path}")
            placeholder_markers = (
                "Describe the durable knowledge change and why it matters.",
                "| TODO | TODO | TODO | draft |",
                "- Embedded instructions ignored: TODO",
                "- Secret indicators: TODO",
                "- External action requested by source: TODO",
            )
            for marker in placeholder_markers:
                if marker in content:
                    errors.append(f"Incomplete proposal placeholder '{marker}': {path}")
            if re.search(r"(?m)^- TODO$", content):
                errors.append(f"Incomplete proposal placeholder '- TODO': {path}")
            risk = risk_assessment(content)
            if risk["secret_hits"]:
                errors.append(f"Secret indicator in proposal: {path}")
            if risk["injection_hits"]:
                warnings.append(f"Injection indicator in proposal: {path}")

    def late_proposal(proposal_id: str) -> bool:
        """Accept a proposal another session completed after the proposal listing."""
        if proposal_id in proposal_map:
            return True
        if not PROPOSAL_ID_PATTERN.fullmatch(proposal_id):
            return False
        proposals_root = vault / "proposals"
        matches = sorted(proposals_root.glob(f"*/{proposal_id}-*.md")) if proposals_root.is_dir() else []
        if len(matches) != 1:
            return False
        try:
            if fm_value(read_text(matches[0]), "id") != proposal_id:
                return False
        except (OSError, UnicodeError):
            return False
        check_proposal(matches[0])
        return True

    for path in proposal_files:
        check_proposal(path)

    event_map: dict[str, Path] = {}
    valid_event_records: list[tuple[dict, Path]] = []
    event_files = sorted((vault / "events").rglob("*.json")) if (vault / "events").is_dir() else []
    required_event = {"event_id", "occurred_at", "agent", "action", "proposal_ids", "pages_changed", "source_ids", "summary"}
    for path in event_files:
        try:
            event = json.loads(read_text(path))
        except (OSError, UnicodeError, json.JSONDecodeError):
            errors.append(f"Invalid event JSON: {path}")
            continue
        valid_event_records.append((event, path))
        for field in sorted(required_event - set(event)):
            errors.append(f"Missing event field '{field}': {path}")
        event_id = str(event.get("event_id") or "")
        if event_id in event_map or path.stem != event_id:
            errors.append(f"Duplicate or mismatched event ID: {path}")
        event_map[event_id] = path
        if not AGENT_PATTERN.fullmatch(str(event.get("agent") or "")) or event.get("action") not in {"stage-source", "new-proposal", "record-curation", "resolve-disposition"}:
            errors.append(f"Invalid event agent/action: {path}")
        try:
            dt.datetime.fromisoformat(str(event.get("occurred_at") or "").replace("Z", "+00:00"))
        except ValueError:
            errors.append(f"Invalid event occurred_at: {path}")
        for source_id in event.get("source_ids") or []:
            if not late_source(str(source_id)):
                errors.append(f"Unknown event source ID '{source_id}': {path}")
        for proposal_id in event.get("proposal_ids") or []:
            if not late_proposal(str(proposal_id)):
                errors.append(f"Unknown event proposal ID '{proposal_id}': {path}")
        risk = risk_assessment(str(event.get("summary") or ""))
        if risk["secret_hits"] or risk["injection_hits"]:
            errors.append(f"Unsafe event summary: {path}")
        for page in event.get("pages_changed") or []:
            candidate = (wiki_root / str(page)).resolve()
            try:
                candidate.relative_to(wiki_root.resolve())
            except ValueError:
                errors.append(f"Invalid event page '{page}': {path}")
                continue
            if not candidate.is_file():
                errors.append(f"Invalid event page '{page}': {path}")

    critical_event_lookup = critical_handoff_event_lookup(valid_event_records)
    for path in proposal_files:
        content = read_text(path)
        if fm_value(content, "knowledge_kind") != "critical-state":
            continue
        integrity = critical_handoff_integrity(
            vault,
            content,
            sources=source_detail_map,
            events=valid_event_records,
            event_lookup=critical_event_lookup,
        )
        if integrity["status"] != "COMPLETE":
            errors.append(
                f"Incomplete critical handoff receipt: {path} ({', '.join(integrity['issues'])})"
            )

    lock_path = vault / "governance" / "curator.lock"
    if lock_path.is_file():
        try:
            lock = json.loads(read_text(lock_path))
            acquired = dt.datetime.fromisoformat(str(lock.get("acquired_at") or "").replace("Z", "+00:00"))
            if (now_utc() - acquired.astimezone(dt.timezone.utc)).total_seconds() / 60 > float(config.get("curator_lock_minutes", 30)):
                warnings.append(f"Curator lock is stale and requires human review: {lock_path}")
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            errors.append(f"Invalid curator lock: {lock_path}")

    freshness = caretaker_freshness(vault)
    if freshness.get("state") == "STALE":
        thresh = freshness.get("threshold_minutes", CARETAKER_THRESHOLD_MINUTES)
        warnings.append(
            f"Caretaker is stale: last tick {freshness.get('observed_at')} "
            f"({freshness.get('age_minutes')} min ago), expected a tick at least every {thresh} min"
        )

    disposition_state = None
    try:
        disposition_state = collect_disposition_state(vault)
    except GatewayError as exc:
        errors.append(f"Disposition state unavailable: {exc}")
    if disposition_state is not None:
        errors.extend(disposition_state["resolution_errors"])
        by_resolution_id = {
            str(record.get("resolution_id")): record
            for record in disposition_state["valid_resolutions"].values()
        }
        for event, path in valid_event_records:
            if event.get("action") != "resolve-disposition":
                continue
            referenced = event.get("resolution_id")
            if not isinstance(referenced, str) or referenced not in by_resolution_id:
                errors.append(f"resolve-disposition event references unknown or invalid resolution: {path}")
            elif by_resolution_id[referenced].get("event_id") != event.get("event_id"):
                errors.append(f"resolve-disposition event/resolution mismatch: {path}")

    raw_conflicts = len(disposition_state["raw_conflict_ids"]) if disposition_state else 0
    effective_unresolved = (
        len(disposition_state["effective_unresolved_conflict_ids"]) if disposition_state else 0
    )
    return {
        "action": "lint", "gateway_mode": "owner-checkpoint-gated", "vault_path": str(vault),
        "registry_stage": registry_stage, "registry_path": str(registry_path),
        "source_count": len(source_map), "proposal_count": len(proposal_map),
        "event_count": len(event_map), "wiki_page_count": len(wiki_files),
        "raw_conflict_proposals": raw_conflicts,
        "effective_unresolved_conflict_proposals": effective_unresolved,
        "resolution_count": len(disposition_state["valid_resolutions"]) if disposition_state else 0,
        "error_count": len(errors), "warning_count": len(warnings),
        "errors": errors, "warnings": warnings,
    }


def _filter_navigation_feed(module):
    """Apply the same resolved project/org intersection to topic feed records."""
    if not hasattr(module, "registry_facets") or not hasattr(module, "topic_feed_records"):
        return
    resolve = module.registry_facets
    read_feed = module.topic_feed_records
    selected = {}
    def facets(*args, **kwargs):
        result, warnings = resolve(*args, **kwargs)
        selected["projects"] = (result or {}).get("projects_filter")
        return result, warnings
    def feed(*args, **kwargs):
        records, note = read_feed(*args, **kwargs)
        projects = selected.get("projects")
        if projects is not None:
            records = [r for r in records if r.get("project_slug") in projects]
        return records, note
    module.registry_facets = facets
    module.topic_feed_records = feed


def _load_navigation_module():
    """Lazy trusted local import of navigation.py (owner: ZCode, contract I3)."""
    import importlib.util
    path = Path(__file__).resolve().with_name("navigation.py")
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("knowledge_navigation_i3", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bind_navigation_to_vault(module, vault: Path) -> None:
    """Point the I3 module at the resolved vault only.

    navigation.py hardcodes hub-wide registry and topic-feed paths. An alternate
    --vault must never fall back to those, or synthetic vaults would leak real
    registry/topic data into results. The canonical hub vault keeps module defaults.
    """
    module.VAULT = vault
    hub_root = getattr(module, "HUB_ROOT", None)
    canonical = (Path(hub_root) / "vault") if hub_root is not None else None
    is_canonical = False
    if canonical is not None:
        try:
            is_canonical = vault.resolve() == canonical.resolve()
        except OSError:
            is_canonical = os.path.normcase(str(vault)) == os.path.normcase(str(canonical))
    if is_canonical:
        return
    module.REGISTRY_LIVE = vault / "registry" / "registry-v2.json"
    module.REGISTRY_SEED = vault / "registry" / "I1-registry-v2.frozen-v0.json"
    module.TOPIC_FEEDS = vault / "feeds" / "KSEF-SCOUT-CONTRACT.json"


def _navigate_forwarded_argv(args) -> list[str]:
    forwarded = ["knowledge.py navigate"]
    for flag, value in (
        ("--topic", getattr(args, "topic", None)),
        ("--org", getattr(args, "org", None)),
        ("--project", getattr(args, "project", None)),
        ("--person", getattr(args, "person", None)),
    ):
        if value is not None:
            forwarded.extend([flag, str(value)])
    if getattr(args, "limit", None) is not None:
        forwarded.extend(["--limit", str(args.limit)])
    if getattr(args, "offset", None) is not None:
        forwarded.extend(["--offset", str(args.offset)])
    if getattr(args, "json", False):
        forwarded.append("--json")
    return forwarded


def navigate(vault: Path, args) -> int:
    """Read-only I3 navigate dispatcher (module owner: ZCode; semantics unchanged)."""
    module_path = Path(__file__).resolve().with_name("navigation.py")
    try:
        module = _load_navigation_module()
    except Exception as exc:
        print_json({
            "status": "ERROR",
            "code": "navigation_module_error",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "module_path": str(module_path),
        })
        return 2
    if module is None:
        print_json({
            "status": "UNAVAILABLE",
            "code": "navigation_module_unavailable",
            "result": "UNAVAILABLE",
            "module_path": str(module_path),
            "availability": {"unavailable": ["module:navigation"]},
            "records": [],
            "total": 0,
            "warnings": [],
        })
        return 2
    _bind_navigation_to_vault(module, vault)
    _filter_navigation_feed(module)
    previous_argv = sys.argv
    sys.argv = _navigate_forwarded_argv(args)
    try:
        code = module.main()
        return int(code) if code is not None else 0
    except SystemExit as exc:
        if exc.code is None:
            return 0
        if isinstance(exc.code, int):
            return exc.code
        print(exc.code, file=sys.stderr)
        return 1
    except Exception as exc:
        print_json({
            "status": "ERROR",
            "code": "navigation_module_error",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "module_path": str(module_path),
        })
        return 2
    finally:
        sys.argv = previous_argv


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Owner-checkpoint-gated shared knowledge gateway")
    commands = root.add_subparsers(dest="action", required=True)
    for name in ("status", "lint"):
        command = commands.add_parser(name)
        command.add_argument("--vault")
        if name == "lint":
            command.add_argument("--registry-stage", choices=("F1", "F2"), default="F1")
        else:
            command.add_argument("--write-health-only", action="store_true",
                                 help="Return the canonical write-health gate without the full status census.")
    stage = commands.add_parser("stage-source")
    stage.add_argument("--vault")
    stage.add_argument("--path", required=True)
    stage.add_argument("--title", required=True)
    stage.add_argument("--origin", default="")
    stage.add_argument("--agent", required=True)
    stage.add_argument("--classification", choices=("public", "internal", "confidential", "restricted"), default="internal")
    stage.add_argument("--local-only", action="store_true")
    proposal = commands.add_parser("new-proposal")
    proposal.add_argument("--vault")
    proposal.add_argument("--title", required=True)
    proposal.add_argument("--agent", required=True)
    proposal.add_argument("--classification", choices=("public", "internal", "confidential", "restricted"), default="internal")
    proposal.add_argument("--source-id", action="append", default=[])
    checkpoint = commands.add_parser("checkpoint-intake")
    checkpoint.add_argument("--vault")
    checkpoint.add_argument("--proposal-id", required=True)
    checkpoint.add_argument("--recover-stale-lock", action="store_true")
    checkpoint.add_argument("--recover-retained-lock", type=Path, help="Explicit preserved legacy checkpoint lock evidence; exact index/pair and live-lock guards still apply.")
    checkpoint.add_argument("--recover-partial-pair", action="store_true")
    handoff = commands.add_parser("record-handoff")
    handoff.add_argument("--vault")
    handoff.add_argument("--path", required=True)
    handoff.add_argument("--title", required=True)
    handoff.add_argument("--origin", default="")
    handoff.add_argument("--agent", required=True)
    handoff.add_argument("--classification", choices=("public", "internal"), default="internal")
    handoff.add_argument("--project-slug", required=True)
    handoff.add_argument("--state-summary", required=True)
    handoff.add_argument("--target-page", action="append", default=[])
    handoff.add_argument("--context-receipt", default=None)
    handoff.add_argument("--session-id", default=None)
    session = commands.add_parser("record-session")
    session.add_argument("--vault")
    session.add_argument("--agent", required=True)
    session.add_argument("--session-id", required=True)
    session.add_argument("--checkpoint-id", required=True)
    session.add_argument("--project-slug", default="")
    session.add_argument("--outcome", required=True, choices=("COMPLETED", "BLOCKED", "INTERRUPTED", "NO_DURABLE_CHANGE"))
    session.add_argument("--path", default=None)
    session.add_argument("--summary", default="")
    session.add_argument("--classification", choices=("public", "internal"), default="internal")
    session.add_argument("--context-receipt", default=None)
    session.add_argument("--retain-temp-root", default=None,
                         help="Optional absolute directory under which the bounded note is created without probing system TEMP or running any cleanup. Omission preserves the legacy TemporaryDirectory behaviour.")
    session.add_argument("--receipt-root", default=None)
    session.add_argument("--lock-root", default=None)
    brief = commands.add_parser("project-brief")
    brief.add_argument("--vault")
    brief.add_argument("--project-slug", required=True)
    brief.add_argument("--limit", type=int, default=10)
    brief.add_argument("--offset", type=int, default=0)
    brief.add_argument("--query", default="")
    brief.add_argument("--require-context", action="store_true")
    node = commands.add_parser("node-brief")
    node.add_argument("--vault")
    node.add_argument("--project-slug", required=True)
    node.add_argument("--node-id", required=True)
    node.add_argument("--scope", action="append", default=[])
    node.add_argument("--query", default="")
    node.add_argument("--context-receipt", required=True)
    node.add_argument("--agent", required=True, help="Agent that produced the context receipt (the parent).")
    node.add_argument("--session-id", required=True, help="Session that produced the context receipt.")
    inventory_command = commands.add_parser("inventory")
    inventory_command.add_argument("--vault")
    inventory_command.add_argument("--project-slug", default="")
    inventory_command.add_argument("--query", default="")
    inventory_command.add_argument("--limit", type=int, default=10)
    inventory_command.add_argument("--offset", type=int, default=0)
    begin = commands.add_parser("begin-task")
    begin.add_argument("--vault")
    begin.add_argument("--agent", required=True)
    begin.add_argument("--session-id", required=True)
    begin.add_argument("--project-slug", required=True)
    begin.add_argument("--require-context", action="store_true")
    begin.add_argument("--receipt-root", default=None)
    begin.add_argument("--limit", type=int, default=10)
    begin.add_argument("--lock-wait-seconds", type=nonnegative_seconds,
                       default=os.environ.get("KNOWLEDGE_LOCK_WAIT_SECONDS", "1200"))
    verify = commands.add_parser("verify-context")
    verify.add_argument("--vault")
    verify.add_argument("--context-receipt", required=True)
    verify.add_argument("--agent", required=True)
    verify.add_argument("--session-id", required=True)
    verify.add_argument("--project-slug", required=True)
    navigate_command = commands.add_parser("navigate")
    navigate_command.add_argument("--vault")
    navigate_command.add_argument("--topic", default=None)
    navigate_command.add_argument("--org", default=None)
    navigate_command.add_argument("--project", default=None)
    navigate_command.add_argument("--person", default=None)
    navigate_command.add_argument("--limit", type=int, default=None)
    navigate_command.add_argument("--offset", type=int, default=None)
    navigate_command.add_argument("--json", action="store_true")
    return root


def main() -> int:
    force_utf8_streams()
    args = parser().parse_args()
    try:
        vault = resolve_vault(args.vault)
        scope = invocation_read_scope(vault) if args.action in CLI_READ_SCOPE_ACTIONS else nullcontext()
        with scope:
            if args.action == "navigate":
                return navigate(vault, args)
            if args.action == "status":
                result = status(vault, write_health_only=bool(args.write_health_only))
            elif args.action == "lint":
                result = lint(vault, registry_stage=args.registry_stage)
            elif args.action == "stage-source":
                result = stage_source(vault, args)
            elif args.action == "new-proposal":
                result = new_proposal(vault, args)
            elif args.action == "checkpoint-intake":
                result = checkpoint_intake(
                    vault,
                    args.proposal_id,
                    recover_stale_lock=bool(args.recover_stale_lock),
                    recover_partial_pair=bool(args.recover_partial_pair),
                    retained_checkpoint_lock=args.recover_retained_lock,
                )
            elif args.action == "record-handoff":
                result = record_handoff(vault, args)
            elif args.action == "record-session":
                result = record_session(vault, args)
            elif args.action == "inventory":
                result = inventory(vault, args)
            elif args.action == "begin-task":
                result = begin_task(vault, args)
            elif args.action == "verify-context":
                result = verify_context(vault, args)
            elif args.action == "node-brief":
                result = node_brief(vault, args)
            else:
                result = project_brief(vault, args)
        if args.action in READ_TELEMETRY_ACTIONS and isinstance(result, dict):
            record_read_telemetry(vault, args.action, args, result)
        if args.action == "node-brief":
            sys.stdout.buffer.write((node_brief_json(result) + "\n").encode("utf-8"))
            sys.stdout.flush()
            return 0
        print_json(result)
        if args.action == "lint" and result["error_count"]:
            return 2
        if args.action == "verify-context" and not result.get("valid"):
            return 2
        if args.action in {"record-handoff", "record-session"} and result["receipt_status"] != "COMPLETE":
            return 2
        if args.action in {"new-proposal", "checkpoint-intake"}:
            checkpoint = result.get("checkpoint") if args.action == "new-proposal" else result
            if checkpoint and checkpoint.get("required") and checkpoint.get("status") not in {"CHECKPOINTED", "CURRENT"}:
                return 2
        if args.action == "project-brief" and args.require_context and not result["context_found"]:
            return 3
        if args.action == "begin-task" and result.get("status") != "COMPLETE":
            if args.require_context and result.get("failure_code") in {"CONTEXT_REQUIRED", "AMBIGUOUS_PROJECT_REFERENCE"}:
                return 3
            return 2
        return 0
    except AmbiguousProjectReference as exc:
        print_json(exc.result)
        return 3 if getattr(args, "require_context", False) else 2
    except (GatewayError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
