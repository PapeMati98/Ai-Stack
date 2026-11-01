"""Bounded durable work-journal bridge for Agent Office.

This module deliberately does not send Office messages, reserve work, or write to
Knowledge Hub.  It only stores a small, schema-shaped receipt stream beneath the
existing ``COORDFS_HOME`` runtime root.  A journal entry is append-only; an
immutable checkpoint is written once and then bound to a successor through its
content hash.

The public operations return result dictionaries instead of exposing whether a
filesystem operation was absent, rejected, or ambiguous through an exception.
In particular, a failed durable write is ``UNKNOWN`` and is never retried by
this bridge.
"""

from __future__ import annotations

import errno
import json
import os
import re
import time
import uuid
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from .atomic import exclusive_create, exclusive_mutex, replace_with_backoff
from .errors import CoordError
from .office import detect_secret
from .store import default_home


WORK_SCHEMA = "office-work.v1"
LOG_SCHEMA = "office-work-log.v1"
CHECKPOINT_SCHEMA = "office-work-checkpoint.v1"
CURSOR_SCHEMA = "office-work-cursor.v1"
AUDIT_SCHEMA = "office-work-audit.v1"

# The journal is durable rather than rotated: reaching a bound is visible and
# refuses a new record instead of silently dropping prior lineage.
MAX_RECORD_BYTES = 64 * 1024
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
MAX_JOURNAL_RECORDS = 4096
MAX_CHECKPOINTS_PER_LINEAGE = 1024
MAX_AUDIT_BYTES = 1024 * 1024

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/\-]{0,199}\Z")
_HASH_RE = re.compile(r"^[0-9a-f]{64}\Z")
_GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_NULL_REASONS = frozenset(
    {
        "not_null",
        "not_applicable",
        "not_provided",
        "not_observed",
        "unknown",
        "redacted",
        "computed_on_commit",
        "genesis",
    }
)
_SENSITIVE_KEYS = frozenset(
    {
        "transcript",
        "raw_transcript",
        "hidden_reasoning",
        "reasoning",
        "chain_of_thought",
        "cot",
        "prompt",
        "secret",
        "password",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "authorization",
        "api_key",
        "private_key",
        "access_token",
        "refresh_token",
        "customer",
        "customer_record",
        "customer_records",
        "customer_data",
        "client_record",
        "client_records",
        "client_data",
        "personal_data",
        "pii",
    }
)


class JournalValidationError(ValueError):
    """A record is unsafe or does not match the bounded work-record contract."""

    def __init__(self, message: str, *, code: str = "invalid_record", field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.field = field


def _result(status: str, code: str, **fields: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status, "code": code}
    result.update(fields)
    return result


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise JournalValidationError("record is not JSON serializable", field="record") from exc


def _json_copy(value: Any) -> Any:
    return json.loads(_canonical_bytes(value).decode("utf-8"))


def _hash_value(value: Any) -> str:
    return sha256(_canonical_bytes(value)).hexdigest()


def _nullable(value: Any, reason: str) -> dict[str, Any]:
    if value is None:
        return {"value": None, "null_reason": reason}
    return {"value": value, "null_reason": "not_null"}


def make_work_record(
    *,
    origin_id: str,
    origin_epoch: str,
    sequence: int,
    project_id: str,
    task_id: str,
    actor_id: str,
    actor_strength: str,
    provider_name: str,
    thread_id: str | None = None,
    run_id: str | None = None,
    provider_session_id: str | None = None,
    patch_hash: str | None = None,
    git_commit: str | None = None,
    git_state: str | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    checkpoint_id: str | None = None,
    checkpoint_sequence: int | None = None,
    predecessor_hash: str | None = None,
    checkpoint_hash: str | None = None,
    effect_state: str = "NONE",
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    duration_ms: int | None = None,
    cost_micros: int | None = None,
    blocker_state: str = "none",
    blocker_code: str | None = None,
    blocker_evidence_ref: str | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a strict, provider-neutral ``office-work.v1`` record.

    The helper exists so callers do not accidentally encode ``None`` as a
    meaningful zero/unknown value.  A checkpoint hash is intentionally null
    with ``computed_on_commit`` until :meth:`JournalBridge.commit_checkpoint`
    binds it to the complete record.
    """

    checkpoint_present = checkpoint_id is not None
    checkpoint_reason = "computed_on_commit" if checkpoint_present else "not_applicable"
    predecessor_reason = "genesis" if checkpoint_present and predecessor_hash is None else "not_applicable"
    return {
        "schema": WORK_SCHEMA,
        "origin": {"id": origin_id, "epoch": origin_epoch, "sequence": sequence},
        "lineage": {
            "project_id": project_id,
            "task_id": task_id,
            "thread_id": _nullable(thread_id, "not_provided"),
            "run_id": _nullable(run_id, "not_provided"),
        },
        "actor": {"id": actor_id, "strength": actor_strength},
        "provider": {
            "name": provider_name,
            "session": _nullable(provider_session_id, "not_provided"),
        },
        "artifact": {
            "patch_hash": _nullable(patch_hash, "not_observed"),
            "git": {
                "commit": _nullable(git_commit, "not_observed"),
                "state": _nullable(git_state, "not_observed"),
            },
            "items": artifacts or [],
        },
        "checkpoint": {
            "id": _nullable(checkpoint_id, "not_applicable"),
            "sequence": _nullable(checkpoint_sequence, "not_applicable"),
            "predecessor_hash": _nullable(predecessor_hash, predecessor_reason),
            "hash": _nullable(checkpoint_hash, checkpoint_reason),
            "effect_state": effect_state,
        },
        "usage": {
            "input_tokens": _nullable(input_tokens, "unknown"),
            "output_tokens": _nullable(output_tokens, "unknown"),
            "duration_ms": _nullable(duration_ms, "unknown"),
            "cost_micros": _nullable(cost_micros, "unknown"),
        },
        "blocker": {
            "state": blocker_state,
            "code": _nullable(blocker_code, "not_applicable"),
            "evidence_ref": _nullable(blocker_evidence_ref, "not_applicable"),
        },
        "evidence": evidence or [],
    }


def _expect_keys(value: Any, required: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != required:
        raise JournalValidationError(f"{label} has an unsupported shape", field=label)
    return value


def _validate_identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise JournalValidationError(f"{label} must be a bounded identifier", field=label)
    return value


def _validate_text(value: Any, label: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(char) < 32 for char in value):
        raise JournalValidationError(f"{label} must be bounded plain text", field=label)
    secret = detect_secret(value)
    if secret is not None:
        raise JournalValidationError(
            f"{label} matches a high-confidence secret pattern", code="sensitive_content", field=label
        )
    return value


def _validate_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise JournalValidationError(f"{label} must be a lowercase SHA-256", field=label)
    return value


def _validate_nullable(value: Any, label: str, kind: str) -> Any:
    nullable = _expect_keys(value, {"value", "null_reason"}, label)
    reason = nullable["null_reason"]
    if not isinstance(reason, str) or reason not in _NULL_REASONS:
        raise JournalValidationError(f"{label}.null_reason is invalid", field=f"{label}.null_reason")
    raw = nullable["value"]
    if raw is None:
        if reason == "not_null":
            raise JournalValidationError(f"{label} is null without a null reason", field=label)
        return None
    if reason != "not_null":
        raise JournalValidationError(f"{label} has a value but a null reason", field=label)
    if kind == "id":
        return _validate_identifier(raw, f"{label}.value")
    if kind == "hash":
        return _validate_hash(raw, f"{label}.value")
    if kind == "int":
        if type(raw) is not int or raw < 0:
            raise JournalValidationError(f"{label}.value must be a non-negative integer", field=label)
        return raw
    if kind == "text":
        return _validate_text(raw, f"{label}.value")
    raise RuntimeError(f"unsupported nullable kind: {kind}")


def _reject_sensitive_content(value: Any, label: str = "record") -> None:
    """Reject disallowed payload shapes before any durable path is opened."""

    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise JournalValidationError("record keys must be strings", field=label)
            normalized = key.lower().replace("-", "_").replace(" ", "_")
            if normalized in _SENSITIVE_KEYS:
                raise JournalValidationError(
                    f"{label}.{key} is prohibited from the work journal",
                    code="sensitive_content",
                    field=f"{label}.{key}",
                )
            _reject_sensitive_content(child, f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_content(child, f"{label}[{index}]")
    elif isinstance(value, str):
        secret = detect_secret(value)
        if secret is not None:
            raise JournalValidationError(
                f"{label} matches a high-confidence secret pattern ({secret})",
                code="sensitive_content",
                field=label,
            )


def validate_work_record(value: Mapping[str, Any], *, require_checkpoint_hash: bool = False) -> dict[str, Any]:
    """Validate and return a JSON-only copy of a work record.

    This is intentionally stricter than a permissive event envelope: unknown
    fields are refused, so a raw transcript, secret field, or customer record
    cannot silently become a future journal feature.
    """

    _reject_sensitive_content(value)
    encoded = _canonical_bytes(value)
    if len(encoded) > MAX_RECORD_BYTES:
        raise JournalValidationError("record exceeds the bounded journal size", field="record")
    record = json.loads(encoded.decode("utf-8"))
    top = _expect_keys(
        record,
        {
            "schema",
            "origin",
            "lineage",
            "actor",
            "provider",
            "artifact",
            "checkpoint",
            "usage",
            "blocker",
            "evidence",
        },
        "record",
    )
    if top["schema"] != WORK_SCHEMA:
        raise JournalValidationError("unsupported work record schema", field="schema")

    origin = _expect_keys(top["origin"], {"id", "epoch", "sequence"}, "origin")
    _validate_identifier(origin["id"], "origin.id")
    _validate_identifier(origin["epoch"], "origin.epoch")
    if type(origin["sequence"]) is not int or origin["sequence"] < 1:
        raise JournalValidationError("origin.sequence must be a positive integer", field="origin.sequence")

    lineage = _expect_keys(top["lineage"], {"project_id", "task_id", "thread_id", "run_id"}, "lineage")
    _validate_identifier(lineage["project_id"], "lineage.project_id")
    _validate_identifier(lineage["task_id"], "lineage.task_id")
    _validate_nullable(lineage["thread_id"], "lineage.thread_id", "id")
    _validate_nullable(lineage["run_id"], "lineage.run_id", "id")

    actor = _expect_keys(top["actor"], {"id", "strength"}, "actor")
    _validate_identifier(actor["id"], "actor.id")
    if actor["strength"] not in {"strong", "weak", "unknown"}:
        raise JournalValidationError("actor.strength must be strong, weak, or unknown", field="actor.strength")

    provider = _expect_keys(top["provider"], {"name", "session"}, "provider")
    _validate_identifier(provider["name"], "provider.name")
    _validate_nullable(provider["session"], "provider.session", "id")

    artifact = _expect_keys(top["artifact"], {"patch_hash", "git", "items"}, "artifact")
    _validate_nullable(artifact["patch_hash"], "artifact.patch_hash", "hash")
    git = _expect_keys(artifact["git"], {"commit", "state"}, "artifact.git")
    commit = _validate_nullable(git["commit"], "artifact.git.commit", "text")
    if commit is not None and not _GIT_COMMIT_RE.fullmatch(commit):
        raise JournalValidationError("artifact.git.commit must be a 40 or 64 char hex commit", field="artifact.git.commit")
    _validate_nullable(git["state"], "artifact.git.state", "text")
    if not isinstance(artifact["items"], list) or len(artifact["items"]) > 128:
        raise JournalValidationError("artifact.items must be a bounded array", field="artifact.items")
    seen_artifacts: set[str] = set()
    for index, item in enumerate(artifact["items"]):
        item_label = f"artifact.items[{index}]"
        item_value = _expect_keys(item, {"id", "kind", "sha256"}, item_label)
        item_id = _validate_identifier(item_value["id"], f"{item_label}.id")
        if item_id in seen_artifacts:
            raise JournalValidationError("artifact item ids must be unique", field=f"{item_label}.id")
        seen_artifacts.add(item_id)
        _validate_identifier(item_value["kind"], f"{item_label}.kind")
        _validate_nullable(item_value["sha256"], f"{item_label}.sha256", "hash")

    checkpoint = _expect_keys(
        top["checkpoint"],
        {"id", "sequence", "predecessor_hash", "hash", "effect_state"},
        "checkpoint",
    )
    checkpoint_id = _validate_nullable(checkpoint["id"], "checkpoint.id", "id")
    checkpoint_sequence = _validate_nullable(checkpoint["sequence"], "checkpoint.sequence", "int")
    predecessor = _validate_nullable(checkpoint["predecessor_hash"], "checkpoint.predecessor_hash", "hash")
    checkpoint_hash = _validate_nullable(checkpoint["hash"], "checkpoint.hash", "hash")
    if checkpoint_id is None:
        if checkpoint_sequence is not None or predecessor is not None or checkpoint_hash is not None:
            raise JournalValidationError("an absent checkpoint cannot carry checkpoint state", field="checkpoint")
        if checkpoint["effect_state"] != "NONE":
            raise JournalValidationError("an absent checkpoint must have effect_state NONE", field="checkpoint.effect_state")
    else:
        if checkpoint_sequence is None or checkpoint_sequence < 1:
            raise JournalValidationError("a checkpoint needs a positive sequence", field="checkpoint.sequence")
        if checkpoint["effect_state"] not in {"CONFIRMED", "UNKNOWN"}:
            raise JournalValidationError("checkpoint.effect_state is invalid", field="checkpoint.effect_state")
        if checkpoint_hash is None and checkpoint["hash"]["null_reason"] != "computed_on_commit":
            raise JournalValidationError("a checkpoint hash may only be absent before commit", field="checkpoint.hash")
        if require_checkpoint_hash and checkpoint_hash is None:
            raise JournalValidationError("a persisted checkpoint needs a hash", field="checkpoint.hash")
        if predecessor is None and checkpoint["predecessor_hash"]["null_reason"] not in {"genesis", "not_applicable"}:
            raise JournalValidationError("a null predecessor must declare genesis", field="checkpoint.predecessor_hash")

    usage = _expect_keys(top["usage"], {"input_tokens", "output_tokens", "duration_ms", "cost_micros"}, "usage")
    for name in ("input_tokens", "output_tokens", "duration_ms", "cost_micros"):
        _validate_nullable(usage[name], f"usage.{name}", "int")

    blocker = _expect_keys(top["blocker"], {"state", "code", "evidence_ref"}, "blocker")
    if blocker["state"] not in {"none", "blocked", "unknown"}:
        raise JournalValidationError("blocker.state is invalid", field="blocker.state")
    blocker_code = _validate_nullable(blocker["code"], "blocker.code", "id")
    _validate_nullable(blocker["evidence_ref"], "blocker.evidence_ref", "id")
    if blocker["state"] == "blocked" and blocker_code is None:
        raise JournalValidationError("a blocked record needs a blocker code", field="blocker.code")
    if blocker["state"] == "none" and blocker_code is not None:
        raise JournalValidationError("a clear record cannot carry a blocker code", field="blocker.code")

    if not isinstance(top["evidence"], list) or len(top["evidence"]) > 128:
        raise JournalValidationError("evidence must be a bounded array", field="evidence")
    seen_evidence: set[str] = set()
    for index, evidence in enumerate(top["evidence"]):
        evidence_label = f"evidence[{index}]"
        evidence_value = _expect_keys(evidence, {"id", "kind", "sha256"}, evidence_label)
        evidence_id = _validate_identifier(evidence_value["id"], f"{evidence_label}.id")
        if evidence_id in seen_evidence:
            raise JournalValidationError("evidence ids must be unique", field=f"{evidence_label}.id")
        seen_evidence.add(evidence_id)
        _validate_identifier(evidence_value["kind"], f"{evidence_label}.kind")
        _validate_hash(evidence_value["sha256"], f"{evidence_label}.sha256")
    return record


def _origin_key(record: Mapping[str, Any]) -> str:
    origin = record["origin"]
    return f"{origin['id']}\x1f{origin['epoch']}\x1f{origin['sequence']}"


def _stream_key(record: Mapping[str, Any]) -> str:
    origin = record["origin"]
    return f"{origin['id']}\x1f{origin['epoch']}"


def _lineage_key(record: Mapping[str, Any]) -> str:
    lineage = record["lineage"]
    return f"{lineage['project_id']}\x1f{lineage['task_id']}"


def _checkpoint_hash(record: Mapping[str, Any]) -> str:
    binding = _json_copy(record)
    binding["checkpoint"]["hash"] = {"value": None, "null_reason": "computed_on_commit"}
    return _hash_value(binding)


def _append_json_line(path: Path, payload: Mapping[str, Any]) -> int:
    """Append one fully encoded line and fsync it; partial failure stays ambiguous."""

    data = _canonical_bytes(payload) + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
    fd = os.open(str(path), flags, 0o600)
    try:
        written = 0
        view = memoryview(data)
        while written < len(view):
            count = os.write(fd, view[written:])
            if count <= 0:
                raise OSError(errno.ENOSPC, "journal write made no progress")
            written += count
        os.fsync(fd)
    finally:
        os.close(fd)
    return len(data)


class JournalBridge:
    """Filesystem-only work journal below ``COORDFS_HOME``.

    A single O_EXCL mutex serializes one journal root.  This keeps duplicate and
    successor decisions adjacent to the durable append without creating an
    Office inbox, transport, reservation, or lease.
    """

    def __init__(self, home: Path | None = None, *, mutex_timeout_s: float = 5.0) -> None:
        self.home = Path(home) if home is not None else default_home()
        self.mutex_timeout_s = mutex_timeout_s

    @property
    def root(self) -> Path:
        return self.home / "office-work"

    @property
    def journal_path(self) -> Path:
        return self.root / "journal.jsonl"

    @property
    def audit_path(self) -> Path:
        return self.root / "audit.jsonl"

    @property
    def mutex_path(self) -> Path:
        return self.root / "journal.lock"

    @property
    def cursors_dir(self) -> Path:
        return self.root / "cursors"

    @property
    def checkpoints_dir(self) -> Path:
        return self.root / "checkpoints"

    def cursor_path(self, origin_id: str, epoch: str) -> Path:
        digest = sha256(f"{origin_id}\x1f{epoch}".encode("utf-8")).hexdigest()
        return self.cursors_dir / f"{digest}.json"

    def _checkpoint_path(self, record: Mapping[str, Any], checkpoint_hash: str) -> Path:
        lineage_digest = sha256(_lineage_key(record).encode("utf-8")).hexdigest()
        return self.checkpoints_dir / lineage_digest / f"{checkpoint_hash}.json"

    def _ensure_layout(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.cursors_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)

    def _read_journal(self) -> dict[str, Any]:
        path = self.journal_path
        if not path.exists():
            return {"ok": True, "entries": [], "by_origin": {}, "streams": {}, "raw_hash": None,
                    "duplicate_entries": 0}
        try:
            raw = path.read_bytes()
        except OSError as exc:
            return {"ok": False, "code": "journal_unreadable", "error": f"{type(exc).__name__}: {exc}"}
        if len(raw) > MAX_JOURNAL_BYTES:
            return {"ok": False, "code": "journal_capacity_exceeded", "bytes": len(raw)}
        if raw and not raw.endswith(b"\n"):
            return {"ok": False, "code": "journal_torn", "line": raw.count(b"\n") + 1}
        entries: list[dict[str, Any]] = []
        by_origin: dict[str, dict[str, Any]] = {}
        streams: dict[str, list[dict[str, Any]]] = {}
        duplicate_entries = 0
        for line_number, raw_line in enumerate(raw.splitlines(), start=1):
            if line_number > MAX_JOURNAL_RECORDS:
                return {"ok": False, "code": "journal_capacity_exceeded", "records": line_number}
            if not raw_line.strip():
                return {"ok": False, "code": "journal_blank_line", "line": line_number}
            try:
                item = json.loads(raw_line.decode("utf-8"))
                item_value = _expect_keys(item, {"schema", "written_at", "record", "record_hash"}, "journal_entry")
                if item_value["schema"] != LOG_SCHEMA:
                    raise JournalValidationError("unsupported journal entry schema")
                _validate_text(item_value["written_at"], "journal_entry.written_at", maximum=64)
                record = validate_work_record(item_value["record"])
                record_hash = _validate_hash(item_value["record_hash"], "journal_entry.record_hash")
                if _hash_value(record) != record_hash:
                    raise JournalValidationError("journal record hash mismatch")
            except (UnicodeDecodeError, json.JSONDecodeError, JournalValidationError, TypeError, ValueError) as exc:
                return {
                    "ok": False,
                    "code": "journal_corrupt",
                    "line": line_number,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            entry = {"record": record, "record_hash": record_hash, "line": line_number}
            origin_key = _origin_key(record)
            existing = by_origin.get(origin_key)
            if existing is not None:
                if existing["record_hash"] != record_hash:
                    return {"ok": False, "code": "journal_origin_conflict", "line": line_number}
                duplicate_entries += 1
            else:
                by_origin[origin_key] = entry
            entries.append(entry)
            streams.setdefault(_stream_key(record), []).append(entry)
        return {
            "ok": True,
            "entries": entries,
            "by_origin": by_origin,
            "streams": streams,
            "raw_hash": sha256(raw).hexdigest(),
            "duplicate_entries": duplicate_entries,
        }

    def _read_cursor(self, origin_id: str, epoch: str) -> dict[str, Any]:
        path = self.cursor_path(origin_id, epoch)
        if not path.exists():
            return {"state": "missing", "path": path}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            cursor = _expect_keys(
                value,
                {"schema", "origin_id", "epoch", "sequence", "record_hash", "updated_at"},
                "cursor",
            )
            if cursor["schema"] != CURSOR_SCHEMA:
                raise JournalValidationError("unsupported cursor schema")
            if cursor["origin_id"] != origin_id or cursor["epoch"] != epoch:
                raise JournalValidationError("cursor identity mismatch")
            _validate_identifier(cursor["origin_id"], "cursor.origin_id")
            _validate_identifier(cursor["epoch"], "cursor.epoch")
            if type(cursor["sequence"]) is not int or cursor["sequence"] < 1:
                raise JournalValidationError("cursor sequence is invalid")
            _validate_hash(cursor["record_hash"], "cursor.record_hash")
            _validate_text(cursor["updated_at"], "cursor.updated_at", maximum=64)
            return {"state": "ok", "value": cursor, "path": path}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, JournalValidationError, TypeError, ValueError) as exc:
            return {"state": "corrupt", "path": path, "error": f"{type(exc).__name__}: {exc}"}

    def _cursor_health(self, scan: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any] | None:
        stream_entries = scan["streams"].get(_stream_key(record), [])
        if not stream_entries:
            return None
        origin = record["origin"]
        cursor = self._read_cursor(origin["id"], origin["epoch"])
        if cursor["state"] == "missing":
            return _result("unknown", "cursor_missing", cursor_path=str(cursor["path"]), retryable=False)
        if cursor["state"] != "ok":
            return _result(
                "unknown",
                "cursor_corrupt",
                cursor_path=str(cursor["path"]),
                retryable=False,
                evidence={"error": cursor.get("error", "unknown")},
            )
        expected = max(stream_entries, key=lambda item: item["record"]["origin"]["sequence"])
        value = cursor["value"]
        if value["sequence"] != expected["record"]["origin"]["sequence"] or value["record_hash"] != expected["record_hash"]:
            return _result(
                "unknown",
                "cursor_mismatch",
                retryable=False,
                evidence={
                    "expected_sequence": expected["record"]["origin"]["sequence"],
                    "expected_record_hash": expected["record_hash"],
                    "cursor_sequence": value["sequence"],
                    "cursor_record_hash": value["record_hash"],
                },
            )
        return None

    def _write_cursor(self, record: Mapping[str, Any], record_hash: str) -> None:
        origin = record["origin"]
        path = self.cursor_path(origin["id"], origin["epoch"])
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
        content = {
            "schema": CURSOR_SCHEMA,
            "origin_id": origin["id"],
            "epoch": origin["epoch"],
            "sequence": origin["sequence"],
            "record_hash": record_hash,
            "updated_at": _utc_now(),
        }
        exclusive_create(temporary, _canonical_bytes(content))
        if not replace_with_backoff(temporary, path):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise OSError("cursor replace remained busy")

    def _append_audit_locked(self, event: str, **fields: Any) -> str:
        payload = {"schema": AUDIT_SCHEMA, "ts": _utc_now(), "event": event, **fields}
        current_size = self.audit_path.stat().st_size if self.audit_path.exists() else 0
        if current_size + len(_canonical_bytes(payload)) + 1 > MAX_AUDIT_BYTES:
            raise OSError(errno.ENOSPC, "audit capacity reached")
        _append_json_line(self.audit_path, payload)
        return _hash_value(payload)

    def _conflict_locked(self, code: str, *, event: str, **evidence: Any) -> dict[str, Any]:
        try:
            audit_hash = self._append_audit_locked(event, code=code, **evidence)
        except OSError as exc:
            return _result(
                "unknown",
                "audit_unavailable",
                retryable=False,
                evidence={"conflict_code": code, "error": f"{type(exc).__name__}: {exc}"},
            )
        return _result("conflict", code, audit_hash=audit_hash, evidence=evidence)

    def _sequence_gap(self, scan: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, int] | None:
        entries = scan["streams"].get(_stream_key(record), [])
        received = record["origin"]["sequence"]
        if not entries:
            return {"expected": 1, "received": received} if received != 1 else None
        highest = max(item["record"]["origin"]["sequence"] for item in entries)
        if received > highest + 1:
            return {"expected": highest + 1, "received": received}
        return None

    def _existing_origin_result(self, scan: Mapping[str, Any], record: Mapping[str, Any], record_hash: str) -> dict[str, Any] | None:
        existing = scan["by_origin"].get(_origin_key(record))
        if existing is None:
            return None
        if existing["record_hash"] == record_hash:
            return _result(
                "ok",
                "duplicate",
                duplicate=True,
                record_hash=record_hash,
                evidence={"journal_line": existing["line"]},
            )
        return self._conflict_locked(
            "origin_payload_conflict",
            event="origin_payload_conflict",
            origin_id=record["origin"]["id"],
            epoch=record["origin"]["epoch"],
            sequence=record["origin"]["sequence"],
            existing_record_hash=existing["record_hash"],
            received_record_hash=record_hash,
        )

    def _append_record_locked(self, scan: Mapping[str, Any], record: Mapping[str, Any], record_hash: str) -> dict[str, Any]:
        existing = self._existing_origin_result(scan, record, record_hash)
        if existing is not None:
            return existing
        cursor_problem = self._cursor_health(scan, record)
        if cursor_problem is not None:
            return cursor_problem
        entries = scan["streams"].get(_stream_key(record), [])
        received = record["origin"]["sequence"]
        if entries:
            highest = max(item["record"]["origin"]["sequence"] for item in entries)
            if received <= highest:
                return self._conflict_locked(
                    "sequence_reuse",
                    event="sequence_reuse",
                    origin_id=record["origin"]["id"],
                    epoch=record["origin"]["epoch"],
                    sequence=received,
                    highest_sequence=highest,
                )
        gap = self._sequence_gap(scan, record)
        entry = {"schema": LOG_SCHEMA, "written_at": _utc_now(), "record": record, "record_hash": record_hash}
        if len(scan["entries"]) >= MAX_JOURNAL_RECORDS:
            return _result("error", "journal_capacity_reached", persisted=False)
        current_size = self.journal_path.stat().st_size if self.journal_path.exists() else 0
        if current_size + len(_canonical_bytes(entry)) + 1 > MAX_JOURNAL_BYTES:
            return _result("error", "journal_capacity_reached", persisted=False)
        _append_json_line(self.journal_path, entry)
        try:
            self._write_cursor(record, record_hash)
        except OSError as exc:
            return _result(
                "unknown",
                "cursor_persistence_unknown",
                retryable=False,
                record_hash=record_hash,
                evidence={"error": f"{type(exc).__name__}: {exc}", "journal_entry_committed": True},
            )
        if gap is not None:
            return _result("warning", "cursor_gap", record_hash=record_hash, cursor_gap=gap, persisted=True)
        return _result("ok", "exported", record_hash=record_hash, persisted=True)

    def export(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Append a non-checkpoint work record with at-least-once semantics."""

        try:
            checked = validate_work_record(record)
        except JournalValidationError as exc:
            return _result("error", exc.code, field=exc.field, message=str(exc), persisted=False)
        if checked["checkpoint"]["id"]["value"] is not None:
            return _result("error", "checkpoint_requires_commit", persisted=False)
        record_hash = _hash_value(checked)
        operation_result: dict[str, Any] | None = None
        try:
            self._ensure_layout()
            with exclusive_mutex(self.mutex_path, timeout_s=self.mutex_timeout_s):
                scan = self._read_journal()
                if not scan["ok"]:
                    operation_result = _result("unknown", scan["code"], retryable=False, evidence=scan)
                else:
                    operation_result = self._append_record_locked(scan, checked, record_hash)
                return operation_result
        except CoordError as exc:
            if operation_result is not None:
                return _result(
                    "unknown",
                    "mutex_cleanup_unknown",
                    retryable=False,
                    evidence={"prior_result": operation_result, "error": f"{type(exc).__name__}: {exc}"},
                )
            return _result("degraded", exc.code, retryable=False, message=str(exc))
        except OSError as exc:
            return _result(
                "unknown",
                "persistence_unknown",
                retryable=False,
                evidence={"error": f"{type(exc).__name__}: {exc}"},
            )

    def _materialize_checkpoint(self, record: Mapping[str, Any]) -> dict[str, Any]:
        checked = validate_work_record(record)
        checkpoint = checked["checkpoint"]
        if checkpoint["id"]["value"] is None:
            raise JournalValidationError("checkpoint commit requires a checkpoint id", field="checkpoint.id")
        if checkpoint["effect_state"] != "CONFIRMED":
            raise JournalValidationError(
                "an unconfirmed effect is UNKNOWN and cannot be auto-committed",
                code="unconfirmed_effect",
                field="checkpoint.effect_state",
            )
        computed = _checkpoint_hash(checked)
        supplied = checkpoint["hash"]["value"]
        if supplied is None:
            checkpoint["hash"] = {"value": computed, "null_reason": "not_null"}
        elif supplied != computed:
            raise JournalValidationError("checkpoint hash does not bind the full record", code="checkpoint_hash_mismatch")
        return validate_work_record(checked, require_checkpoint_hash=True)

    def _checkpoint_records(self, scan: Mapping[str, Any], record: Mapping[str, Any]) -> list[dict[str, Any]]:
        key = _lineage_key(record)
        result = [
            entry
            for entry in scan["entries"]
            if entry["record"]["checkpoint"]["id"]["value"] is not None and _lineage_key(entry["record"]) == key
        ]
        return sorted(result, key=lambda item: item["record"]["checkpoint"]["sequence"]["value"])

    def _read_checkpoint_file(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            checkpoint = _expect_keys(
                value,
                {"schema", "checkpoint_hash", "record_hash", "record", "written_at"},
                "checkpoint_file",
            )
            if checkpoint["schema"] != CHECKPOINT_SCHEMA:
                raise JournalValidationError("unsupported checkpoint file schema")
            checkpoint_hash = _validate_hash(checkpoint["checkpoint_hash"], "checkpoint_file.checkpoint_hash")
            record_hash = _validate_hash(checkpoint["record_hash"], "checkpoint_file.record_hash")
            record = validate_work_record(checkpoint["record"], require_checkpoint_hash=True)
            if _checkpoint_hash(record) != checkpoint_hash or record["checkpoint"]["hash"]["value"] != checkpoint_hash:
                raise JournalValidationError("checkpoint hash binding mismatch")
            if _hash_value(record) != record_hash:
                raise JournalValidationError("checkpoint record hash mismatch")
            return {"ok": True, "record": record, "checkpoint_hash": checkpoint_hash, "record_hash": record_hash}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, JournalValidationError, TypeError, ValueError) as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _chain_problem(self, checkpoint_entries: list[dict[str, Any]]) -> dict[str, Any] | None:
        previous_hash: str | None = None
        previous_sequence = 0
        ids: set[str] = set()
        for entry in checkpoint_entries:
            checkpoint = entry["record"]["checkpoint"]
            current_id = checkpoint["id"]["value"]
            current_sequence = checkpoint["sequence"]["value"]
            current_hash = checkpoint["hash"]["value"]
            if current_id in ids or current_sequence != previous_sequence + 1:
                return {"code": "checkpoint_sequence_conflict", "checkpoint_id": current_id}
            ids.add(current_id)
            predecessor = checkpoint["predecessor_hash"]["value"]
            if predecessor != previous_hash:
                return {"code": "checkpoint_successor_conflict", "checkpoint_id": current_id}
            previous_hash = current_hash
            previous_sequence = current_sequence
        return None

    def _checkpoint_file_matches(self, record: Mapping[str, Any], checkpoint_hash: str) -> dict[str, Any]:
        path = self._checkpoint_path(record, checkpoint_hash)
        if not path.is_file() or path.is_symlink():
            return {"ok": False, "code": "checkpoint_file_missing", "path": str(path)}
        value = self._read_checkpoint_file(path)
        if not value["ok"]:
            return {"ok": False, "code": "checkpoint_file_corrupt", "path": str(path), "error": value["error"]}
        if value["record_hash"] != _hash_value(record):
            return {"ok": False, "code": "checkpoint_file_conflict", "path": str(path)}
        return {"ok": True, "path": str(path), **value}

    def commit_checkpoint(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Write a checkpoint once, then append its bound receipt to the journal.

        A crash or disk-full after the immutable file but before the journal is
        deliberately returned as UNKNOWN.  Retrying it automatically could turn
        an unconfirmed external effect into a duplicated step.
        """

        try:
            checked = self._materialize_checkpoint(record)
        except JournalValidationError as exc:
            status = "unknown" if exc.code == "unconfirmed_effect" else "error"
            return _result(status, exc.code, field=exc.field, message=str(exc), persisted=False, retryable=False)
        record_hash = _hash_value(checked)
        checkpoint_hash = checked["checkpoint"]["hash"]["value"]
        checkpoint_written = False
        operation_result: dict[str, Any] | None = None
        try:
            self._ensure_layout()
            with exclusive_mutex(self.mutex_path, timeout_s=self.mutex_timeout_s):
                scan = self._read_journal()
                if not scan["ok"]:
                    operation_result = _result("unknown", scan["code"], retryable=False, evidence=scan)
                    return operation_result
                existing = self._existing_origin_result(scan, checked, record_hash)
                if existing is not None:
                    if existing["status"] != "ok":
                        operation_result = existing
                        return operation_result
                    verified = self._checkpoint_file_matches(checked, checkpoint_hash)
                    if not verified["ok"]:
                        operation_result = _result(
                            "unknown",
                            "checkpoint_ambiguous",
                            retryable=False,
                            checkpoint_hash=checkpoint_hash,
                            evidence=verified,
                        )
                        return operation_result
                    operation_result = {**existing, "checkpoint_hash": checkpoint_hash, "checkpoint_path": verified["path"]}
                    return operation_result
                cursor_problem = self._cursor_health(scan, checked)
                if cursor_problem is not None:
                    operation_result = cursor_problem
                    return operation_result
                origin_entries = scan["streams"].get(_stream_key(checked), [])
                if origin_entries:
                    highest_origin_sequence = max(
                        entry["record"]["origin"]["sequence"] for entry in origin_entries
                    )
                    if checked["origin"]["sequence"] <= highest_origin_sequence:
                        operation_result = self._conflict_locked(
                            "sequence_reuse",
                            event="sequence_reuse",
                            origin_id=checked["origin"]["id"],
                            epoch=checked["origin"]["epoch"],
                            sequence=checked["origin"]["sequence"],
                            highest_sequence=highest_origin_sequence,
                        )
                        return operation_result
                prior = self._checkpoint_records(scan, checked)
                chain_problem = self._chain_problem(prior)
                if chain_problem is not None:
                    operation_result = _result("unknown", chain_problem["code"], retryable=False, evidence=chain_problem)
                    return operation_result
                for entry in prior:
                    prior_hash = entry["record"]["checkpoint"]["hash"]["value"]
                    verified = self._checkpoint_file_matches(entry["record"], prior_hash)
                    if not verified["ok"]:
                        operation_result = _result(
                            "unknown",
                            "checkpoint_predecessor_unverifiable",
                            retryable=False,
                            evidence=verified,
                        )
                        return operation_result
                incoming = checked["checkpoint"]
                if prior:
                    predecessor = prior[-1]["record"]["checkpoint"]["hash"]["value"]
                    expected_sequence = prior[-1]["record"]["checkpoint"]["sequence"]["value"] + 1
                else:
                    predecessor = None
                    expected_sequence = 1
                if incoming["sequence"]["value"] != expected_sequence or incoming["predecessor_hash"]["value"] != predecessor:
                    operation_result = self._conflict_locked(
                        "checkpoint_successor_conflict",
                        event="checkpoint_successor_conflict",
                        checkpoint_id=incoming["id"]["value"],
                        checkpoint_hash=checkpoint_hash,
                        expected_sequence=expected_sequence,
                        expected_predecessor_hash=predecessor,
                        received_sequence=incoming["sequence"]["value"],
                        received_predecessor_hash=incoming["predecessor_hash"]["value"],
                    )
                    return operation_result
                if any(entry["record"]["checkpoint"]["id"]["value"] == incoming["id"]["value"] for entry in prior):
                    operation_result = self._conflict_locked(
                        "checkpoint_id_conflict",
                        event="checkpoint_id_conflict",
                        checkpoint_id=incoming["id"]["value"],
                        checkpoint_hash=checkpoint_hash,
                    )
                    return operation_result
                checkpoint_path = self._checkpoint_path(checked, checkpoint_hash)
                checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
                if len(list(checkpoint_path.parent.glob("*.json"))) >= MAX_CHECKPOINTS_PER_LINEAGE:
                    operation_result = _result("error", "checkpoint_capacity_reached", persisted=False)
                    return operation_result
                if checkpoint_path.exists():
                    known = self._checkpoint_file_matches(checked, checkpoint_hash)
                    code = "checkpoint_orphan_exists" if known.get("ok") else "checkpoint_hash_collision"
                    audit = self._conflict_locked(
                        code,
                        event=code,
                        checkpoint_id=incoming["id"]["value"],
                        checkpoint_hash=checkpoint_hash,
                    )
                    if audit["status"] == "conflict":
                        operation_result = _result(
                            "unknown",
                            code,
                            retryable=False,
                            checkpoint_hash=checkpoint_hash,
                            evidence={"audit_hash": audit["audit_hash"]},
                        )
                        return operation_result
                    operation_result = audit
                    return operation_result
                checkpoint_payload = {
                    "schema": CHECKPOINT_SCHEMA,
                    "checkpoint_hash": checkpoint_hash,
                    "record_hash": record_hash,
                    "record": checked,
                    "written_at": _utc_now(),
                }
                exclusive_create(checkpoint_path, _canonical_bytes(checkpoint_payload))
                checkpoint_written = True
                operation_result = self._append_record_locked(scan, checked, record_hash)
                if operation_result["status"] in {"ok", "warning"}:
                    operation_result.update(
                        {
                            "checkpoint_hash": checkpoint_hash,
                            "checkpoint_path": str(checkpoint_path),
                            "checkpoint_bound": True,
                        }
                    )
                    return operation_result
                operation_result = _result(
                    "unknown",
                    "checkpoint_commit_unknown",
                    retryable=False,
                    checkpoint_hash=checkpoint_hash,
                    evidence={"checkpoint_path": str(checkpoint_path), "journal_result": operation_result},
                )
                return operation_result
        except CoordError as exc:
            if checkpoint_written or operation_result is not None:
                return _result(
                    "unknown",
                    "mutex_cleanup_unknown",
                    retryable=False,
                    checkpoint_hash=checkpoint_hash,
                    evidence={"prior_result": operation_result, "checkpoint_written": checkpoint_written, "error": f"{type(exc).__name__}: {exc}"},
                )
            return _result("degraded", exc.code, retryable=False, message=str(exc))
        except OSError as exc:
            return _result(
                "unknown",
                "persistence_unknown",
                retryable=False,
                checkpoint_hash=checkpoint_hash,
                evidence={"error": f"{type(exc).__name__}: {exc}"},
            )

    def read_checkpoint(self, project_id: str, task_id: str, checkpoint_id: str | None = None) -> dict[str, Any]:
        """Return a read-only checkpoint transfer receipt; it never reserves work."""

        try:
            _validate_identifier(project_id, "project_id")
            _validate_identifier(task_id, "task_id")
            if checkpoint_id is not None:
                _validate_identifier(checkpoint_id, "checkpoint_id")
        except JournalValidationError as exc:
            return _result("error", exc.code, field=exc.field, message=str(exc))
        if not self.root.exists():
            return _result("ok", "checkpoint_absent", checkpoint=None, reservation_created=False)
        try:
            with exclusive_mutex(self.mutex_path, timeout_s=self.mutex_timeout_s):
                scan = self._read_journal()
                if not scan["ok"]:
                    return _result("unknown", scan["code"], retryable=False, evidence=scan)
                candidates = [
                    entry
                    for entry in scan["entries"]
                    if entry["record"]["lineage"]["project_id"] == project_id
                    and entry["record"]["lineage"]["task_id"] == task_id
                    and entry["record"]["checkpoint"]["id"]["value"] is not None
                ]
                if checkpoint_id is not None:
                    candidates = [
                        entry for entry in candidates if entry["record"]["checkpoint"]["id"]["value"] == checkpoint_id
                    ]
                if not candidates:
                    return _result("ok", "checkpoint_absent", checkpoint=None, reservation_created=False)
                selected = max(candidates, key=lambda item: item["record"]["checkpoint"]["sequence"]["value"])
                checkpoint_hash = selected["record"]["checkpoint"]["hash"]["value"]
                verified = self._checkpoint_file_matches(selected["record"], checkpoint_hash)
                if not verified["ok"]:
                    return _result("unknown", "checkpoint_unverifiable", retryable=False, evidence=verified)
                record = verified["record"]
                return _result(
                    "ok",
                    "checkpoint_read",
                    checkpoint=record,
                    receipt={
                        "checkpoint_hash": checkpoint_hash,
                        "patch_hash": record["artifact"]["patch_hash"],
                        "git_state": record["artifact"]["git"]["state"],
                        "lineage": record["lineage"],
                        "record_hash": verified["record_hash"],
                    },
                    transfer={
                        "mode": "read_only",
                        "decomposition": "not_performed",
                        "repeated_substep": False,
                        "reservation_created": False,
                    },
                    reservation_created=False,
                )
        except CoordError as exc:
            return _result("degraded", exc.code, retryable=False, message=str(exc))
        except OSError as exc:
            return _result(
                "unknown",
                "checkpoint_read_unknown",
                retryable=False,
                evidence={"error": f"{type(exc).__name__}: {exc}"},
            )

    def _checkpoint_mutation_rejected(self, action: str, checkpoint_hash: str, **details: Any) -> dict[str, Any]:
        try:
            _validate_hash(checkpoint_hash, "checkpoint_hash")
            for key, value in details.items():
                if key.endswith("hash"):
                    _validate_hash(value, key)
        except JournalValidationError as exc:
            return _result("error", exc.code, field=exc.field, message=str(exc))
        audit_hash: str | None = None
        try:
            self._ensure_layout()
            with exclusive_mutex(self.mutex_path, timeout_s=self.mutex_timeout_s):
                audit_hash = self._append_audit_locked(
                    "checkpoint_mutation_rejected",
                    action=action,
                    checkpoint_hash=checkpoint_hash,
                    **details,
                )
                return _result(
                    "conflict",
                    "checkpoint_write_once",
                    checkpoint_hash=checkpoint_hash,
                    action=action,
                    audit_hash=audit_hash,
                    persisted=False,
                )
        except CoordError as exc:
            if audit_hash is not None:
                return _result(
                    "unknown",
                    "mutex_cleanup_unknown",
                    retryable=False,
                    checkpoint_hash=checkpoint_hash,
                    evidence={"audit_hash": audit_hash, "error": f"{type(exc).__name__}: {exc}"},
                )
            return _result("degraded", exc.code, retryable=False, message=str(exc))
        except OSError as exc:
            return _result(
                "unknown",
                "audit_unavailable",
                retryable=False,
                evidence={"error": f"{type(exc).__name__}: {exc}"},
            )

    def overwrite_checkpoint(self, checkpoint_hash: str, replacement: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Reject overwrite attempts without reading or persisting replacement content."""

        return self._checkpoint_mutation_rejected(
            "overwrite", checkpoint_hash, replacement_supplied=replacement is not None
        )

    def partial_rewrite_checkpoint(self, checkpoint_hash: str, patch: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Reject partial rewrites; a fragment cannot preserve an immutable hash binding."""

        return self._checkpoint_mutation_rejected(
            "partial_rewrite", checkpoint_hash, replacement_supplied=patch is not None
        )

    def update_checkpoint_hash(self, checkpoint_hash: str, new_hash: str) -> dict[str, Any]:
        """Reject hash-only updates and retain the attempted target only as an audit hash."""

        return self._checkpoint_mutation_rejected("hash_only_update", checkpoint_hash, requested_hash=new_hash)

    def delete_checkpoint(self, checkpoint_hash: str) -> dict[str, Any]:
        """Reject deletion; immutable checkpoint bytes are never unlinked by the bridge."""

        return self._checkpoint_mutation_rejected("delete", checkpoint_hash)

    def audit_events(self) -> dict[str, Any]:
        """Read bounded audit events for verification; malformed audit is visible."""

        if not self.audit_path.exists():
            return _result("ok", "audit_empty", events=[])
        try:
            events: list[dict[str, Any]] = []
            for raw_line in self.audit_path.read_bytes().splitlines():
                item = json.loads(raw_line.decode("utf-8"))
                if not isinstance(item, dict) or item.get("schema") != AUDIT_SCHEMA:
                    raise JournalValidationError("unsupported audit schema")
                if not isinstance(item.get("event"), str) or not isinstance(item.get("ts"), str):
                    raise JournalValidationError("malformed audit event")
                events.append(item)
            return _result("ok", "audit_read", events=events)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, JournalValidationError, TypeError, ValueError) as exc:
            return _result("unknown", "audit_corrupt", retryable=False, evidence={"error": f"{type(exc).__name__}: {exc}"})

    def reconcile(self) -> dict[str, Any]:
        """Inspect journal, cursor, checkpoint, artifact, and usage consistency without repair."""

        if not self.root.exists():
            return _result(
                "ok",
                "reconciled_empty",
                auto_retry=False,
                reconciliation={"ids": 0, "sequence": [], "hashes": [], "state": "empty", "artifacts": [], "usage": {}},
            )
        try:
            with exclusive_mutex(self.mutex_path, timeout_s=self.mutex_timeout_s):
                scan = self._read_journal()
                if not scan["ok"]:
                    return _result("unknown", scan["code"], auto_retry=False, evidence=scan)
                issues: list[dict[str, Any]] = []
                sequence_summary: list[dict[str, Any]] = []
                for _stream, entries in sorted(scan["streams"].items()):
                    ordered = sorted(entries, key=lambda item: item["record"]["origin"]["sequence"])
                    origin = ordered[-1]["record"]["origin"]
                    cursor = self._read_cursor(origin["id"], origin["epoch"])
                    if cursor["state"] != "ok":
                        issues.append({"code": f"cursor_{cursor['state']}", "origin_id": origin["id"], "epoch": origin["epoch"]})
                    else:
                        expected = ordered[-1]
                        value = cursor["value"]
                        if value["sequence"] != expected["record"]["origin"]["sequence"] or value["record_hash"] != expected["record_hash"]:
                            issues.append({"code": "cursor_mismatch", "origin_id": origin["id"], "epoch": origin["epoch"]})
                    expected_sequence = 1
                    for entry in ordered:
                        actual = entry["record"]["origin"]["sequence"]
                        if actual != expected_sequence:
                            issues.append(
                                {
                                    "code": "cursor_gap",
                                    "origin_id": origin["id"],
                                    "epoch": origin["epoch"],
                                    "expected": expected_sequence,
                                    "received": actual,
                                }
                            )
                        expected_sequence = actual + 1
                    sequence_summary.append(
                        {
                            "origin_id": origin["id"],
                            "epoch": origin["epoch"],
                            "last_sequence": ordered[-1]["record"]["origin"]["sequence"],
                            "records": len(ordered),
                        }
                    )

                checkpoint_entries = [
                    entry for entry in scan["entries"] if entry["record"]["checkpoint"]["id"]["value"] is not None
                ]
                by_lineage: dict[str, list[dict[str, Any]]] = {}
                checkpoint_hashes: set[str] = set()
                for entry in checkpoint_entries:
                    record = entry["record"]
                    checkpoint_hash = record["checkpoint"]["hash"]["value"]
                    checkpoint_hashes.add(checkpoint_hash)
                    verified = self._checkpoint_file_matches(record, checkpoint_hash)
                    if not verified["ok"]:
                        issues.append({"code": verified["code"], "checkpoint_hash": checkpoint_hash})
                    by_lineage.setdefault(_lineage_key(record), []).append(entry)
                for lineage, entries in by_lineage.items():
                    issue = self._chain_problem(sorted(entries, key=lambda item: item["record"]["checkpoint"]["sequence"]["value"]))
                    if issue is not None:
                        issues.append({"lineage_hash": sha256(lineage.encode("utf-8")).hexdigest(), **issue})

                if scan["duplicate_entries"]:
                    issues.append({"code": "journal_duplicate_entries", "count": scan["duplicate_entries"]})

                actual_checkpoint_hashes: set[str] = set()
                if self.checkpoints_dir.exists():
                    seen_checkpoint_files = 0
                    for lineage_dir in sorted(self.checkpoints_dir.iterdir(), key=lambda path: path.name):
                        if not lineage_dir.is_dir() or lineage_dir.is_symlink():
                            issues.append({"code": "checkpoint_directory_invalid", "path": lineage_dir.name})
                            continue
                        for path in sorted(lineage_dir.glob("*.json"), key=lambda current: current.name):
                            seen_checkpoint_files += 1
                            if seen_checkpoint_files > MAX_JOURNAL_RECORDS:
                                issues.append({"code": "checkpoint_capacity_exceeded"})
                                break
                            if path.is_symlink():
                                issues.append({"code": "checkpoint_file_symlink", "path": path.name})
                                continue
                            value = self._read_checkpoint_file(path)
                            if not value["ok"]:
                                issues.append({"code": "checkpoint_file_corrupt", "path": path.name})
                                continue
                            actual_checkpoint_hashes.add(value["checkpoint_hash"])
                for orphan in sorted(actual_checkpoint_hashes - checkpoint_hashes):
                    issues.append({"code": "orphan_checkpoint", "checkpoint_hash": orphan})

                artifact_ids: list[str] = []
                usage_summary = {
                    name: {"known_total": 0, "unknown_count": 0}
                    for name in ("input_tokens", "output_tokens", "duration_ms", "cost_micros")
                }
                for entry in scan["entries"]:
                    record = entry["record"]
                    artifact_ids.extend(item["id"] for item in record["artifact"]["items"])
                    for name, summary in usage_summary.items():
                        value = record["usage"][name]["value"]
                        if value is None:
                            summary["unknown_count"] += 1
                        else:
                            summary["known_total"] += value
                reconciliation = {
                    "ids": len(scan["by_origin"]),
                    "sequence": sequence_summary,
                    "hashes": sorted(checkpoint_hashes),
                    "state": "consistent" if not issues else "UNKNOWN",
                    "artifacts": sorted(set(artifact_ids)),
                    "usage": usage_summary,
                    "journal_sha256": scan["raw_hash"],
                    "duplicate_entries": scan["duplicate_entries"],
                }
                return _result(
                    "ok" if not issues else "unknown",
                    "reconciled" if not issues else "reconciliation_unknown",
                    auto_retry=False,
                    reconciliation=reconciliation,
                    issues=issues,
                )
        except CoordError as exc:
            return _result("degraded", exc.code, retryable=False, message=str(exc))
        except OSError as exc:
            return _result(
                "unknown",
                "reconciliation_unknown",
                auto_retry=False,
                evidence={"error": f"{type(exc).__name__}: {exc}"},
            )
