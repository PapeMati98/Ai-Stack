"""Local, metadata-only incident records and offline recurring reports.

The module deliberately has no knowledge of Fleet launchers or model clients.
Callers provide one already-normalized metadata record at a lifecycle boundary.
The authoritative records are immutable JSON files.  ``index.json`` is a
bounded, rebuildable derived view and is never used as the source of truth.
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any, Iterable, Mapping, Sequence

import adaptive

SCHEMA_VERSION = 1
INCIDENTS_DIRNAME = "incidents"
EVENTS_DIRNAME = "events"
INDEX_FILENAME = "index.json"
LOCK_FILENAME = ".lock"

DEFAULT_MAX_EVENT_BYTES = 16 * 1024
DEFAULT_MAX_INDEX_BYTES = 256 * 1024
DEFAULT_MAX_INDEX_ENTRIES = 256
MAX_EVENT_BYTES = DEFAULT_MAX_EVENT_BYTES
MAX_INDEX_BYTES = DEFAULT_MAX_INDEX_BYTES
MAX_INDEX_ENTRIES = DEFAULT_MAX_INDEX_ENTRIES
# These aliases make the bound easy to inspect or override in a focused test.
INDEX_MAX_BYTES = DEFAULT_MAX_INDEX_BYTES
INDEX_MAX_ENTRIES = DEFAULT_MAX_INDEX_ENTRIES
MAX_IMPORT_BYTES = 8 * 1024 * 1024
MAX_COUNT = 10_000_000
MAX_ID_LENGTH = 128
MAX_LIST_ITEMS = 128

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,127}\Z")
_EVENT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+\-]{0,127}\Z")
_CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:\-]{0,95}\Z")
_HEX_RE = re.compile(r"[0-9a-fA-F]{7,128}\Z")

_ALLOWED_FIELDS = frozenset(
    {
        "schema_version",
        "event_id",
        "source",
        "event_source",
        "event_type",
        "kind",
        "occurred_at",
        "timestamp",
        "started_at",
        "finished_at",
        "task_id",
        "owner_task_id",
        "business_task_id",
        "root_task_id",
        "root_id",
        "run_id",
        "graph_id",
        "node_id",
        "gate_id",
        "gate_group_id",
        "group_id",
        "attempt_id",
        "attempt",
        "attempt_index",
        "worker_id",
        "worker_ids",
        "owner_task_known",
        "profile",
        "execution_profile",
        "model",
        "provider",
        "model_provider_id",
        "runtime",
        "runtime_id",
        "runtime_hash",
        "release",
        "release_id",
        "release_hash",
        "stage",
        "outcome",
        "status",
        "error_code",
        "classification",
        "failure_class",
        "severity",
        "retry",
        "retry_index",
        "retry_count",
        "recovery",
        "recovery_outcome",
        "duration_ms",
        "delivery_impact",
        "denominator",
        "frequency_denominator",
        "task_denominator",
        "attempt_denominator",
        "tokens",
        "usage",
        "token_usage",
        "token_count",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "issue",
        "issue_id",
        "issue_status",
        "evidence",
        "evidence_id",
        "evidence_ref",
        "evidence_refs",
        "fix_release",
        "fix_release_hash",
        "regression_test",
        "regression_test_id",
        "regression_status",
        "regression",
        "postdeploy",
        "postdeploy_status",
        "postdeploy_verification",
        "postdeploy_verified_at",
        "lifecycle",
        "metadata",
    }
)

_TOKEN_FIELDS = frozenset(
    {"input", "output", "total", "input_tokens", "output_tokens", "total_tokens", "known"}
)
_ISSUE_FIELDS = frozenset(
    {
        "issue_id",
        "status",
        "evidence",
        "evidence_id",
        "evidence_ref",
        "evidence_refs",
        "fix_release",
        "fix_release_hash",
        "regression_test",
        "regression_test_id",
        "regression_status",
        "regression",
        "postdeploy",
        "postdeploy_status",
        "postdeploy_verification",
        "postdeploy_verified_at",
    }
)
_POSTDEPLOY_FIELDS = frozenset(
    {"status", "verification", "release", "release_hash", "verified_at", "evidence_ref"}
)

_OUTCOMES = frozenset(
    {
        "accepted",
        "blocked",
        "cancelled",
        "failure",
        "missing_binding",
        "missing_input",
        "observed",
        "pending",
        "product_defect",
        "quota",
        "recovered",
        "regression",
        "success",
        "timeout",
        "unknown",
        "waiting",
    }
)
_CLASSIFICATIONS = frozenset(
    {
        "evidence_gap",
        "environment",
        "harness_transport",
        "not_failure",
        "product_defect",
        "resource",
        "spec_input",
        "unknown",
    }
)
_SEVERITIES = frozenset({"info", "warning", "error", "critical", "unknown"})
_RECOVERY_VALUES = frozenset(
    {"none", "pending", "retry", "recovered", "unrecovered", "fixed", "unknown"}
)
_ISSUE_STATUSES = frozenset(
    {"open", "triaged", "fixed", "verified", "closed", "regression_possible", "pending", "unknown"}
)
_REGRESSION_STATUSES = frozenset({"not_run", "pass", "fail", "pending", "unknown"})
_POSTDEPLOY_STATUSES = frozenset({"not_run", "pass", "fail", "pending", "unknown"})
_NON_FAILURE_OUTCOMES = frozenset({"accepted", "observed", "pending", "success", "waiting"})

class _InvalidEvent(ValueError):
    """Internal marker whose public message is intentionally stable."""

    def __init__(self, code: str = "INVALID_EVENT") -> None:
        super().__init__(code)
        self.code = code


def _invalid(code: str = "INVALID_EVENT") -> None:
    raise _InvalidEvent(code)


def _warn(code: str) -> None:
    # Warning codes are deliberately the only diagnostics emitted by this module.
    print(code, file=sys.stderr, flush=True)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _is_mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


def _check_fields(value: Mapping[str, Any], allowed: Iterable[str]) -> None:
    if any(not isinstance(key, str) for key in value):
        _invalid()
    if set(value) - set(allowed):
        _invalid()


def _normalize_id(value: Any, *, allow_none: bool = True, max_length: int = MAX_ID_LENGTH) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value or len(value) > max_length:
        _invalid()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        _invalid()
    if "\\" in value or ".." in value or not _ID_RE.fullmatch(value):
        _invalid()
    return value


def _normalize_event_id(value: Any) -> str | None:
    """Normalize the event identifier used as a single event filename."""
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > MAX_ID_LENGTH:
        _invalid()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        _invalid()
    if not _EVENT_ID_RE.fullmatch(value):
        _invalid()
    return value


def _normalize_code(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _CODE_RE.fullmatch(value):
        _invalid()
    return value.upper()


def _normalize_hash(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _HEX_RE.fullmatch(value):
        _invalid()
    return value.lower()


def _normalize_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        _invalid()
    return value


def _normalize_int(value: Any, *, maximum: int = MAX_COUNT) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        _invalid()
    return value


def _normalize_number(value: Any, *, maximum: float = 1_000_000_000_000.0) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _invalid()
    if not math.isfinite(float(value)) or value < 0 or value > maximum:
        _invalid()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _normalize_enum(value: Any, allowed: Iterable[str], *, default: str | None = None) -> str | None:
    if value is None:
        return default
    if not isinstance(value, str) or value.lower() not in set(allowed):
        _invalid()
    return value.lower()


def _normalize_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        _invalid()
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)) or value < 0 or value > 4_102_444_800:
            _invalid()
        parsed = _datetime.datetime.fromtimestamp(float(value), tz=_datetime.timezone.utc)
    elif isinstance(value, str):
        if not value or len(value) > 40 or any(ord(char) < 32 for char in value):
            _invalid()
        try:
            parsed = _datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            _invalid()
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_datetime.timezone.utc)
        parsed = parsed.astimezone(_datetime.timezone.utc)
    else:
        _invalid()
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _same_value(left: Any, right: Any) -> bool:
    return left == right


def _coalesce(raw: Mapping[str, Any], *keys: str) -> Any:
    present = [(key, raw[key]) for key in keys if key in raw and raw[key] is not None]
    if not present:
        return None
    first = present[0][1]
    if any(not _same_value(first, value) for _, value in present[1:]):
        _invalid()
    return first


def _normalize_id_list(value: Any) -> list[str]:
    if value is None:
        return []
    values: Sequence[Any]
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = value
    else:
        _invalid()
    if len(values) > MAX_LIST_ITEMS:
        _invalid("EVENT_LIST_TOO_LARGE")
    normalized = [_normalize_id(item, allow_none=False) for item in values]
    if len(set(normalized)) != len(normalized):
        _invalid()
    return sorted(normalized)


def _normalize_tokens(raw: Mapping[str, Any]) -> dict[str, int | None]:
    supplied = _coalesce(raw, "tokens", "usage", "token_usage")
    token_values: dict[str, Any] = {}
    if supplied is not None:
        if isinstance(supplied, Mapping):
            _check_fields(supplied, _TOKEN_FIELDS)
            token_values.update(supplied)
        elif isinstance(supplied, int) and not isinstance(supplied, bool):
            token_values["total"] = supplied
        else:
            _invalid()
    aliases = {
        "input": "input_tokens",
        "output": "output_tokens",
        "total": "total_tokens",
    }
    for target, alias in aliases.items():
        nested_alias = f"{target}_tokens"
        if nested_alias in token_values:
            if target in token_values and token_values[target] != token_values[nested_alias]:
                _invalid()
            token_values[target] = token_values[nested_alias]
    for target, alias in aliases.items():
        value = raw.get(alias)
        if value is not None:
            if target in token_values and token_values[target] != value:
                _invalid()
            token_values[target] = value
    if "token_count" in raw and raw["token_count"] is not None:
        if "total" in token_values and token_values["total"] != raw["token_count"]:
            _invalid()
        token_values["total"] = raw["token_count"]
    return {
        "input": _normalize_int(token_values.get("input"), maximum=MAX_COUNT),
        "output": _normalize_int(token_values.get("output"), maximum=MAX_COUNT),
        "total": _normalize_int(token_values.get("total"), maximum=MAX_COUNT),
    }


def _normalize_postdeploy(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return {"verification": _normalize_id(value)}
    if not isinstance(value, Mapping):
        _invalid()
    _check_fields(value, _POSTDEPLOY_FIELDS)
    return {
        "status": _normalize_enum(value.get("status"), _POSTDEPLOY_STATUSES),
        "verification": _normalize_id(value.get("verification")),
        "release": _normalize_id(value.get("release")),
        "release_hash": _normalize_hash(value.get("release_hash")),
        "verified_at": _normalize_timestamp(value.get("verified_at")),
        "evidence_ref": _normalize_id(value.get("evidence_ref")),
    }


def _normalize_issue(raw: Mapping[str, Any]) -> dict[str, Any]:
    nested = raw.get("issue")
    if nested is None:
        issue: Mapping[str, Any] = {}
    elif isinstance(nested, Mapping):
        _check_fields(nested, _ISSUE_FIELDS)
        issue = nested
    else:
        _invalid()

    def value(*keys: str) -> Any:
        nested_value = _coalesce(issue, *keys)
        top_value = _coalesce(raw, *keys)
        if nested_value is not None and top_value is not None and nested_value != top_value:
            _invalid()
        return nested_value if nested_value is not None else top_value

    evidence_values: list[str] = []
    for source in (
        value("evidence_refs"),
        value("evidence", "evidence_id", "evidence_ref"),
    ):
        if source is not None:
            evidence_values.extend(_normalize_id_list(source))
    if len(set(evidence_values)) != len(evidence_values):
        _invalid()
    postdeploy = value("postdeploy", "postdeploy_verification")
    normalized = {
        "issue_id": _normalize_id(value("issue_id")),
        "status": _normalize_enum(value("status", "issue_status"), _ISSUE_STATUSES),
        "evidence_refs": sorted(evidence_values),
        "fix_release": _normalize_id(value("fix_release")),
        "fix_release_hash": _normalize_hash(value("fix_release_hash")),
        "regression_test": _normalize_id(value("regression_test", "regression_test_id")),
        "regression_status": _normalize_enum(value("regression_status"), _REGRESSION_STATUSES),
        "regression": _normalize_bool(value("regression")),
        "postdeploy": _normalize_postdeploy(postdeploy),
        "postdeploy_status": _normalize_enum(value("postdeploy_status"), _POSTDEPLOY_STATUSES),
        "postdeploy_verified_at": _normalize_timestamp(value("postdeploy_verified_at")),
    }
    if normalized["postdeploy"] is not None and normalized["postdeploy_status"] is None:
        normalized["postdeploy_status"] = normalized["postdeploy"].get("status")
    return normalized


def _merge_metadata(event: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        _invalid()
    raw = dict(event)
    metadata = raw.pop("metadata", None)
    if metadata is not None:
        if not isinstance(metadata, Mapping):
            _invalid()
        _check_fields(metadata, _ALLOWED_FIELDS - {"metadata"})
        for key, value in metadata.items():
            if key in raw and raw[key] is not None and raw[key] != value:
                _invalid()
            raw[key] = value
    _check_fields(raw, _ALLOWED_FIELDS - {"metadata"})
    return raw


def _normalize_event(event: Mapping[str, Any]) -> dict[str, Any]:
    raw = _merge_metadata(event)
    version = raw.get("schema_version")
    if version is not None and version != SCHEMA_VERSION:
        _invalid("UNSUPPORTED_EVENT_SCHEMA")

    source = _normalize_id(_coalesce(raw, "source", "event_source"))
    event_type = _normalize_id(_coalesce(raw, "event_type", "kind")) or "incident"
    occurred_at = _normalize_timestamp(_coalesce(raw, "occurred_at", "timestamp")) or _utc_now()
    started_at = _normalize_timestamp(raw.get("started_at"))
    finished_at = _normalize_timestamp(raw.get("finished_at"))

    worker_ids = _normalize_id_list(_coalesce(raw, "worker_ids"))
    worker_id = _normalize_id(_coalesce(raw, "worker_id"))
    if worker_id is not None and worker_id not in worker_ids:
        worker_ids.append(worker_id)
        worker_ids.sort()
    if worker_id is None and len(worker_ids) == 1:
        worker_id = worker_ids[0]

    outcome = _normalize_enum(raw.get("outcome"), _OUTCOMES, default="unknown")
    classification = _normalize_enum(
        _coalesce(raw, "classification", "failure_class"),
        _CLASSIFICATIONS,
        default="not_failure" if outcome in _NON_FAILURE_OUTCOMES else "unknown",
    )
    issue = _normalize_issue(raw)

    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event_id": None,
        "source": source,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "task_id": _normalize_id(raw.get("task_id")),
        "owner_task_id": _normalize_id(raw.get("owner_task_id")),
        "business_task_id": _normalize_id(raw.get("business_task_id")),
        "root_task_id": _normalize_id(raw.get("root_task_id")),
        "root_id": _normalize_id(raw.get("root_id")),
        "run_id": _normalize_id(raw.get("run_id")),
        "graph_id": _normalize_id(raw.get("graph_id")),
        "node_id": _normalize_id(raw.get("node_id")),
        "gate_id": _normalize_id(raw.get("gate_id")),
        "gate_group_id": _normalize_id(_coalesce(raw, "gate_group_id", "group_id")),
        "attempt_id": _normalize_id(raw.get("attempt_id")),
        "attempt_index": _normalize_int(_coalesce(raw, "attempt_index", "attempt")),
        "worker_id": worker_id,
        "worker_ids": worker_ids,
        "owner_task_known": _normalize_bool(raw.get("owner_task_known")),
        "profile": _normalize_id(_coalesce(raw, "profile", "execution_profile")),
        "model": _normalize_id(raw.get("model")),
        "provider": _normalize_id(raw.get("provider")),
        "model_provider_id": _normalize_id(raw.get("model_provider_id")),
        "runtime": _normalize_id(_coalesce(raw, "runtime", "runtime_id")),
        "runtime_hash": _normalize_hash(raw.get("runtime_hash")),
        "release": _normalize_id(_coalesce(raw, "release", "release_id")),
        "release_hash": _normalize_hash(raw.get("release_hash")),
        "stage": _normalize_id(raw.get("stage")) or "unknown",
        "outcome": outcome,
        "status": _normalize_id(raw.get("status")),
        "error_code": _normalize_code(raw.get("error_code")),
        "classification": classification,
        "severity": _normalize_enum(raw.get("severity"), _SEVERITIES, default="unknown"),
        "retry": _normalize_bool(raw.get("retry")),
        "retry_index": _normalize_int(raw.get("retry_index")),
        "retry_count": _normalize_int(raw.get("retry_count")),
        "recovery": _normalize_enum(raw.get("recovery"), _RECOVERY_VALUES),
        "recovery_outcome": _normalize_enum(raw.get("recovery_outcome"), _RECOVERY_VALUES),
        "duration_ms": _normalize_number(raw.get("duration_ms"), maximum=86_400_000.0),
        "delivery_impact": _normalize_number(raw.get("delivery_impact"), maximum=86_400_000.0)
        if raw.get("delivery_impact") is None or isinstance(raw.get("delivery_impact"), (int, float))
        else _normalize_id(raw.get("delivery_impact")),
        "denominator": _normalize_int(_coalesce(raw, "denominator", "frequency_denominator")),
        "task_denominator": _normalize_int(raw.get("task_denominator")),
        "attempt_denominator": _normalize_int(raw.get("attempt_denominator")),
        "tokens": _normalize_tokens(raw),
        "issue": issue,
        "issue_id": issue["issue_id"],
        "issue_status": issue["status"],
        "evidence_refs": issue["evidence_refs"],
        "fix_release": issue["fix_release"],
        "fix_release_hash": issue["fix_release_hash"],
        "regression_test": issue["regression_test"],
        "regression_status": issue["regression_status"],
        "regression": issue["regression"],
        "postdeploy": issue["postdeploy"],
        "postdeploy_status": issue["postdeploy_status"],
        "postdeploy_verified_at": issue["postdeploy_verified_at"],
    }
    provided_event_id = _normalize_event_id(raw.get("event_id"))
    identity = {
        key: record[key]
        for key in (
            "source",
            "event_type",
            "task_id",
            "owner_task_id",
            "business_task_id",
            "root_task_id",
            "run_id",
            "graph_id",
            "node_id",
            "gate_id",
            "gate_group_id",
            "attempt_id",
            "attempt_index",
            "worker_ids",
            "stage",
            "outcome",
            "error_code",
            "classification",
            "retry_index",
        )
    }
    event_id = provided_event_id or "evt-" + hashlib.sha256(_json_bytes(identity)).hexdigest()[:32]
    record["event_id"] = event_id
    encoded = _json_bytes(record)
    if len(encoded) > MAX_EVENT_BYTES:
        _invalid("EVENT_TOO_LARGE")
    return record


def _state_paths(state_root: str | os.PathLike[str]) -> tuple[Path, Path, Path]:
    root = Path(state_root)
    if not root.name and not root.anchor:
        _invalid("INVALID_STATE_ROOT")
    incidents_dir = root / INCIDENTS_DIRNAME
    return incidents_dir / EVENTS_DIRNAME, incidents_dir / INDEX_FILENAME, incidents_dir / LOCK_FILENAME


class _StateLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None
        self.locked = False

    def __enter__(self) -> "_StateLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        if os.name == "nt":
            import msvcrt

            for _ in range(500):
                try:
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                    self.locked = True
                    break
                except OSError:
                    time.sleep(0.01)
            if not self.locked:
                self.handle.close()
                self.handle = None
                raise OSError("INCIDENT_LOCK_TIMEOUT")
        else:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
            self.locked = True
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        if self.handle is None:
            return
        try:
            if self.locked and os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            elif self.locked:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None
            self.locked = False


def _atomic_write_json(path: Path, value: Any) -> None:
    adaptive.atomic_write_bytes(path, _json_bytes(value) + b"\n", mode=0o600)


def _atomic_create_json(path: Path, value: Any) -> bool:
    """Create one immutable event without replacing an existing event id."""
    encoded = _json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _load_events(events_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    if not events_dir.exists():
        return records, warnings
    try:
        paths = sorted(events_dir.glob("*.json"), key=lambda item: item.name)
    except OSError:
        return records, ["INCIDENT_EVENT_READ_FAILED"]
    for path in paths:
        try:
            if path.stat().st_size > MAX_EVENT_BYTES:
                warnings.append("INCIDENT_EVENT_TOO_LARGE")
                continue
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
                warnings.append("INCIDENT_EVENT_INVALID")
                continue
            records.append(value)
        except (OSError, UnicodeError, ValueError, TypeError):
            warnings.append("INCIDENT_EVENT_INVALID")
    return records, sorted(set(warnings))


def _signature_parts(event: Mapping[str, Any]) -> tuple[str, str, str, str, str | None]:
    return (
        str(event.get("stage") or "unknown"),
        str(event.get("classification") or "unknown"),
        str(event.get("error_code") or "UNKNOWN"),
        str(event.get("outcome") or "unknown"),
        event.get("runtime") if isinstance(event.get("runtime"), str) else None,
    )


def _signature(event: Mapping[str, Any]) -> tuple[str, str]:
    parts = _signature_parts(event)
    key = "|".join(item if item is not None else "unknown" for item in parts)
    digest = hashlib.sha256(_json_bytes(parts)).hexdigest()[:20]
    return "sig-" + digest, key


def _is_problem(event: Mapping[str, Any]) -> bool:
    outcome = event.get("outcome")
    classification = event.get("classification")
    if outcome in _NON_FAILURE_OUTCOMES or classification == "not_failure":
        return False
    if outcome == "unknown" and not event.get("error_code") and classification == "unknown":
        return False
    return True


def _task_key(event: Mapping[str, Any]) -> tuple[str, str] | None:
    for field in ("owner_task_id", "task_id", "business_task_id"):
        value = event.get(field)
        if isinstance(value, str) and value:
            return "owner", value
    workers = event.get("worker_ids")
    if isinstance(workers, list) and workers:
        digest = hashlib.sha256(_json_bytes(sorted(workers))).hexdigest()[:20]
        return "workers", digest
    for field in ("root_task_id", "root_id"):
        value = event.get(field)
        if isinstance(value, str) and value:
            return "root", value
    return None


def _event_timestamp(event: Mapping[str, Any]) -> str | None:
    value = event.get("occurred_at")
    return value if isinstance(value, str) else None


def _new_lifecycle() -> dict[str, Any]:
    return {
        "issue_id": None,
        "status": None,
        "evidence_refs": [],
        "fix_releases": [],
        "fix_release_hashes": [],
        "regression_tests": [],
        "regression_statuses": [],
        "postdeploy_verifications": [],
        "postdeploy_statuses": [],
        "possible_regression": False,
    }


def _append_unique(values: list[Any], value: Any, limit: int = MAX_LIST_ITEMS) -> None:
    if value is not None and value not in values and len(values) < limit:
        values.append(value)


def _group_events(events: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups: dict[str, dict[str, Any]] = {}
    ordered_events = sorted(
        events,
        key=lambda event: (_event_timestamp(event) or "", str(event.get("event_id") or "")),
    )
    for event in ordered_events:
        signature, signature_key = _signature(event)
        problem = _is_problem(event)
        group_key = ("problem:" if problem else "observation:") + signature
        group = groups.get(group_key)
        if group is None:
            group = {
                "signature": signature,
                "signature_key": signature_key,
                "stage": event.get("stage"),
                "classification": event.get("classification"),
                "error_code": event.get("error_code"),
                "outcome": event.get("outcome"),
                "occurrences": 0,
                "affected_tasks": 0,
                "affected_tasks_known": True,
                "distinct_affected_tasks": 0,
                "owner_task_unknown": False,
                "unknown_task_events": 0,
                "retry_count": 0,
                "recovery_count": 0,
                "first_seen": None,
                "last_seen": None,
                "duration_ms": None,
                "duration_ms_known": 0,
                "delivery_impact": None,
                "delivery_impact_known": 0,
                "denominator": None,
                "frequency": None,
                "versions": {"runtime": [], "runtime_hash": [], "release": [], "release_hash": []},
                "providers": [],
                "models": [],
                "profiles": [],
                "evidence_refs": [],
                "issue": _new_lifecycle(),
                "quality_penalty": None,
                "_task_keys": set(),
                "_retry_marked": 0,
                "_denominators": set(),
                "_denominator_unknown": False,
                "_fix_timestamps": [],
                "_events": [],
            }
            groups[group_key] = group
        group["occurrences"] += 1
        group["_events"].append(event)
        timestamp = _event_timestamp(event)
        if timestamp is not None:
            if group["first_seen"] is None or timestamp < group["first_seen"]:
                group["first_seen"] = timestamp
            if group["last_seen"] is None or timestamp > group["last_seen"]:
                group["last_seen"] = timestamp

        task_key = _task_key(event)
        if task_key is None:
            group["unknown_task_events"] += 1
            group["affected_tasks_known"] = False
            group["owner_task_unknown"] = True
        else:
            group["_task_keys"].add(task_key)
            if task_key[0] != "owner":
                group["owner_task_unknown"] = True
                group["affected_tasks_known"] = False

        retry_index = event.get("retry_index")
        if (isinstance(retry_index, int) and retry_index > 0) or event.get("retry") is True:
            group["_retry_marked"] += 1
        for field in ("retry_count",):
            value = event.get(field)
            if isinstance(value, int) and value >= 0:
                group["retry_count"] = max(group["retry_count"], value)

        if event.get("recovery") in {"recovered", "fixed"} or event.get("recovery_outcome") in {
            "recovered",
            "fixed",
        }:
            group["recovery_count"] += 1
        duration = event.get("duration_ms")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            group["duration_ms"] = (group["duration_ms"] or 0) + duration
            group["duration_ms_known"] += 1
        impact = event.get("delivery_impact")
        if impact is not None:
            group["delivery_impact_known"] += 1
            if isinstance(impact, (int, float)) and not isinstance(impact, bool):
                group["delivery_impact"] = (group["delivery_impact"] or 0) + impact
            elif group["delivery_impact"] is None:
                group["delivery_impact"] = impact

        denominator = event.get("denominator")
        if isinstance(denominator, int) and not isinstance(denominator, bool):
            group["_denominators"].add(denominator)
        else:
            group["_denominator_unknown"] = True
        for version_field in ("runtime", "runtime_hash", "release", "release_hash"):
            _append_unique(group["versions"][version_field], event.get(version_field))
        _append_unique(group["providers"], event.get("provider"))
        _append_unique(group["models"], event.get("model"))
        _append_unique(group["profiles"], event.get("profile"))

        issue = event.get("issue") if isinstance(event.get("issue"), Mapping) else {}
        lifecycle = group["issue"]
        _append_unique(lifecycle["evidence_refs"], None)
        for evidence in issue.get("evidence_refs", []) if isinstance(issue.get("evidence_refs"), list) else []:
            _append_unique(lifecycle["evidence_refs"], evidence)
            _append_unique(group["evidence_refs"], evidence)
        if issue.get("issue_id") is not None:
            if lifecycle["issue_id"] is None:
                lifecycle["issue_id"] = issue["issue_id"]
            elif lifecycle["issue_id"] != issue["issue_id"]:
                _append_unique(lifecycle.setdefault("issue_ids", []), issue["issue_id"])
        if issue.get("status") is not None:
            lifecycle["status"] = issue["status"]
        _append_unique(lifecycle["fix_releases"], issue.get("fix_release"))
        _append_unique(lifecycle["fix_release_hashes"], issue.get("fix_release_hash"))
        _append_unique(lifecycle["regression_tests"], issue.get("regression_test"))
        _append_unique(lifecycle["regression_statuses"], issue.get("regression_status"))
        _append_unique(lifecycle["postdeploy_statuses"], issue.get("postdeploy_status"))
        postdeploy = issue.get("postdeploy")
        if isinstance(postdeploy, Mapping):
            for field in ("verification", "release", "release_hash", "evidence_ref", "verified_at"):
                _append_unique(lifecycle["postdeploy_verifications"], postdeploy.get(field))
            _append_unique(lifecycle["postdeploy_statuses"], postdeploy.get("status"))
        if issue.get("regression") is True or issue.get("regression_status") == "fail" or event.get("outcome") == "regression":
            lifecycle["possible_regression"] = True
        if issue.get("fix_release") is not None and timestamp is not None:
            group["_fix_timestamps"].append(timestamp)

    problems: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    for group in groups.values():
        task_keys = group.pop("_task_keys")
        group["distinct_affected_tasks"] = len(task_keys) if task_keys else None
        group["affected_tasks"] = len(task_keys) if task_keys else None
        group["retry_count"] = max(
            group["retry_count"],
            group["_retry_marked"],
            max(0, group["occurrences"] - max(1, len(task_keys))) if task_keys else group["_retry_marked"],
        )
        if group["_denominators"] and len(group["_denominators"]) == 1 and not group["_denominator_unknown"]:
            group["denominator"] = next(iter(group["_denominators"]))
            group["frequency"] = (
                group["occurrences"] / group["denominator"] if group["denominator"] > 0 else None
            )
        else:
            group["denominator"] = None
            group["frequency"] = None
        if group["_fix_timestamps"]:
            first_fix = min(group["_fix_timestamps"])
            group["issue"]["possible_regression"] = group["issue"]["possible_regression"] or any(
                isinstance(event.get("occurred_at"), str)
                and event.get("occurred_at") > first_fix
                and event.get("issue", {}).get("fix_release") is None
                for event in group["_events"]
            )
        if group["issue"]["issue_id"] is None:
            group["issue"]["issue_id"] = "issue-" + group["signature"][4:]
        group["issue"]["possible_regression"] = bool(group["issue"]["possible_regression"])
        group["issue_id"] = group["issue"]["issue_id"]
        group["possible_regression"] = group["issue"]["possible_regression"]
        group["_events"] = None
        group.pop("_retry_marked", None)
        group.pop("_denominators", None)
        group.pop("_denominator_unknown", None)
        group.pop("_fix_timestamps", None)
        if group["issue"].get("issue_ids"):
            group["issue_ids"] = sorted(group["issue"]["issue_ids"])
        (problems if group["outcome"] not in _NON_FAILURE_OUTCOMES and group["classification"] != "not_failure" else observations).append(group)

    key = lambda item: (-int(item.get("occurrences", 0)), item.get("first_seen") or "", item.get("signature") or "")
    problems.sort(key=key)
    observations.sort(key=key)
    return problems, observations


def _index_limits() -> tuple[int, int]:
    return max(1, min(int(MAX_INDEX_BYTES), int(INDEX_MAX_BYTES))), max(
        1, min(int(MAX_INDEX_ENTRIES), int(INDEX_MAX_ENTRIES))
    )


def _coverage(total_events: int, problem_groups: list[dict[str, Any]], selected: list[dict[str, Any]], *, truncated: bool) -> dict[str, Any]:
    total_problem_events = sum(int(group.get("occurrences", 0)) for group in problem_groups)
    indexed_events = sum(int(group.get("occurrences", 0)) for group in selected)
    total_signatures = len(problem_groups)
    indexed_signatures = len(selected)
    return {
        "complete": not truncated,
        "truncated": truncated,
        "overflow": truncated,
        "total_events": total_events,
        "problem_events": total_problem_events,
        "indexed_events": indexed_events,
        "omitted_events": max(0, total_problem_events - indexed_events),
        "total_signatures": total_signatures,
        "indexed_signatures": indexed_signatures,
        "omitted_signatures": max(0, total_signatures - indexed_signatures),
        "coverage_ratio": indexed_events / total_problem_events if total_problem_events else None,
        "authoritative_events_retained": True,
        "age_deletion": False,
    }


def _build_index(events: list[dict[str, Any]], load_warnings: Iterable[str]) -> dict[str, Any]:
    problems, observations = _group_events(events)
    max_bytes, max_entries = _index_limits()
    selected = list(problems[:max_entries])
    truncated = len(selected) != len(problems)

    def build(current: list[dict[str, Any]], is_truncated: bool) -> dict[str, Any]:
        warnings = sorted(set(load_warnings))
        coverage = _coverage(len(events), problems, current, truncated=is_truncated)
        if is_truncated:
            warnings.extend(["INCIDENT_INDEX_OVERFLOW", "INCIDENT_INDEX_COVERAGE_TRUNCATED"])
        return {
            "schema_version": SCHEMA_VERSION,
            "index_type": "derived_incident_index",
            "coverage": coverage,
            "warnings": sorted(set(warnings)),
            "events": {
                "total": len(events),
                "problems": sum(int(group.get("occurrences", 0)) for group in problems),
                "observations": sum(int(group.get("occurrences", 0)) for group in observations),
            },
            "incidents": current,
            "signatures": current,
            "observations": observations[:max_entries],
            "issues": [group["issue"] for group in current],
        }

    value = build(selected, truncated)
    while len(_json_bytes(value)) + 1 > max_bytes and selected:
        selected.pop()
        value = build(selected, True)
    if len(_json_bytes(value)) + 1 > max_bytes:
        # Keep the coverage and overflow signal even under an unusually tiny test bound.
        value = build([], True)
        value["observations"] = []
        value["issues"] = []
        value["incidents"] = []
        value["signatures"] = []
    return value


def _rebuild_index(events_dir: Path, index_path: Path) -> dict[str, Any]:
    events, load_warnings = _load_events(events_dir)
    value = _build_index(events, load_warnings)
    try:
        encoded_size = len(_json_bytes(value))
        if encoded_size + 1 > _index_limits()[0]:
            _warn("INCIDENT_INDEX_BOUND_UNSATISFIED")
        _atomic_write_json(index_path, value)
    except (OSError, ValueError, TypeError):
        _warn("INCIDENT_INDEX_WRITE_FAILED")
    return value


def record_event(state_root: str | os.PathLike[str], event: Mapping[str, Any]) -> dict[str, Any]:
    """Record one allowlisted metadata event without rebuilding the index.

    The returned dictionary is the normalized event plus non-persisted status
    fields (``stored``, ``duplicate`` and ``index_updated``).  Invalid metadata
    raises ``ValueError`` with a stable code; filesystem failures are reported
    visibly and do not prevent the caller from continuing its work.  The
    derived index is rebuilt by ``report`` on demand.
    """

    normalized = _normalize_event(event)
    events_dir, _, _ = _state_paths(state_root)
    result = dict(normalized)
    result.update({"stored": False, "duplicate": False, "index_updated": False})
    try:
        events_dir.mkdir(parents=True, exist_ok=True)
        event_path = events_dir / f"{normalized['event_id']}.json"
        if _atomic_create_json(event_path, normalized):
            result["stored"] = True
        else:
            result["duplicate"] = True
    except (OSError, ValueError, TypeError):
        _warn("INCIDENT_LOG_WRITE_FAILED")
    return result


def report(state_root: str | os.PathLike[str]) -> dict[str, Any]:
    """Return a deterministic offline report from local incident metadata only."""

    events_dir, index_path, lock_path = _state_paths(state_root)
    try:
        events_dir.mkdir(parents=True, exist_ok=True)
        with _StateLock(lock_path):
            return _rebuild_index(events_dir, index_path)
    except (OSError, ValueError, TypeError):
        _warn("INCIDENT_REPORT_READ_FAILED")
        return _build_index([], ["INCIDENT_REPORT_READ_FAILED"])


def _read_import_records(input_path: str | os.PathLike[str]) -> list[Mapping[str, Any]]:
    path = str(input_path)
    if path == "-":
        data = sys.stdin.buffer.read(MAX_IMPORT_BYTES + 1)
    else:
        candidate = Path(path)
        if candidate.suffix.lower() not in {".json", ".jsonl"}:
            _invalid("IMPORT_METADATA_ONLY")
        data = candidate.read_bytes()
    if len(data) > MAX_IMPORT_BYTES:
        _invalid("IMPORT_TOO_LARGE")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        _invalid("IMPORT_INVALID")
    try:
        parsed = json.loads(text)
        if isinstance(parsed, Mapping):
            if "events" in parsed:
                parsed = parsed["events"]
            elif "records" in parsed:
                parsed = parsed["records"]
            elif "event" in parsed:
                parsed = [parsed["event"]]
            else:
                parsed = [parsed]
        if isinstance(parsed, list):
            records = parsed
        else:
            _invalid("IMPORT_INVALID")
    except (ValueError, TypeError):
        records = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except (ValueError, TypeError):
                _invalid("IMPORT_INVALID")
            if not isinstance(value, Mapping):
                _invalid("IMPORT_INVALID")
            records.append(value.get("event", value))
    if len(records) > MAX_COUNT or any(not isinstance(record, Mapping) for record in records):
        _invalid("IMPORT_INVALID")
    return records


def import_records(state_root: str | os.PathLike[str], input_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Import only explicit JSON/JSONL metadata records, never raw logs."""

    records = _read_import_records(input_path)
    imported = 0
    duplicates = 0
    rejected = 0
    for record in records:
        try:
            result = record_event(state_root, record)
        except ValueError:
            rejected += 1
            continue
        if result.get("duplicate"):
            duplicates += 1
        elif result.get("stored"):
            imported += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "imported": imported,
        "duplicates": duplicates,
        "rejected": rejected,
        "report": report(state_root),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline local incident metadata report")
    parser.add_argument("--state-root", dest="global_state_root")
    commands = parser.add_subparsers(dest="command", required=True)

    report_parser = commands.add_parser("report", help="rebuild and print the local report")
    report_parser.add_argument("state_root", nargs="?")
    report_parser.add_argument("--state-root", dest="command_state_root")

    import_parser = commands.add_parser("import", help="import JSON/JSONL metadata records")
    import_parser.add_argument("state_root", nargs="?")
    import_parser.add_argument("input_path", nargs="?")
    import_parser.add_argument("--state-root", dest="command_state_root")
    import_parser.add_argument("--input", dest="command_input_path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    state_root = getattr(args, "command_state_root", None) or getattr(args, "global_state_root", None)
    if state_root is None:
        state_root = getattr(args, "state_root", None)
    if not state_root:
        print(json.dumps({"status": "error", "code": "STATE_ROOT_REQUIRED"}, sort_keys=True))
        return 2
    try:
        if args.command == "report":
            value = report(state_root)
        else:
            input_path = getattr(args, "command_input_path", None) or getattr(args, "input_path", None)
            if not input_path:
                print(json.dumps({"status": "error", "code": "INPUT_REQUIRED"}, sort_keys=True))
                return 2
            value = import_records(state_root, input_path)
        print(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return 0
    except _InvalidEvent as error:
        print(json.dumps({"status": "error", "code": error.code}, sort_keys=True))
        return 2
    except (OSError, ValueError, TypeError):
        print(json.dumps({"status": "error", "code": "INCIDENT_CLI_FAILED"}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
