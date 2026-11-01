"""Host-scoped server dispatch state machine (B5 Module 1 candidate).

Implements the frozen ``CLI-HOST-DISPATCH-CONTRACT.md`` server side only.
Internal server operation: ``enqueue`` runs AFTER current Wake gates and is
never exposed through remote RPC. ``pending``/``claim``/``start``/``receipt``
arguments (``peer_host``, ``executor_agent``) must come from the
forced-command authenticated context, never from RPC fields. No invocation is
ever authorized by an Office message body or by possession of a dispatch id.

State lives only below ``<home>/wake/cli-dispatches``. Records are retained;
nothing is unlinked or removed. Short operations run under one global
file mutex; the lock is never held while a native process runs (this module
never runs one). Corruption, symlink/reparse state, malformed index or
oversized records fail closed. Self-contained except existing safe IO
primitives (``atomic``), host spelling (``checkout_claims``) and ``errors``;
no Wake import.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
import time
import unicodedata
import uuid
from pathlib import PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any, Callable
from collections.abc import Mapping

from .atomic import exclusive_create, exclusive_mutex
from .checkout_claims import canonical_host
from .errors import CoordError

DISPATCH_DIR = ("wake", "cli-dispatches")
SUPPORTED_DRIVERS = ("codex-cli", "claude-code-cli")
TRIGGERS = ("message", "child-terminal", "schedule")
STATES = ("pending", "claimed", "started", "delivery_unknown", "accepted", "rejected")
FINAL_STATES = ("accepted", "rejected")
RECEIPT_STATUSES = ("accepted", "rejected", "delivery_unknown")

MAX_ACTIVE = 256
CLAIM_TTL_S = 60
STARTED_TTL_S = 300
MAX_RECORD_BYTES = 64 * 1024
MAX_TEXT = 256
MAX_BINDING_ID = 128
MAX_THREAD_ID = 256
MAX_TRIGGER_REF = 512
MAX_WORKSPACE = 1024
EXIT_MIN = -(2**31)
EXIT_MAX = 2**31 - 1

# Closed binding projection. Anything carrying a prompt, message text,
# argv, environment, binary or command is refused, as is any unknown key.
BINDING_FIELDS = (
    "binding_id",
    "version",
    "execution_host",
    "executor_agent",
    "driver",
    "native_thread_id",
    "workspace",
    "workspace_flavor",
)
FORBIDDEN_KEYS = frozenset({
    "message", "message_text", "body", "prompt", "argv", "args", "command",
    "script", "binary", "env", "environment", "stdin", "stdout", "stderr",
    "transcript", "code", "patch",
})

EligibilityFn = Callable[[Mapping[str, Any]], "str | None"]


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _safe_text(value: Any, *, label: str, max_len: int, code: str) -> str:
    if not isinstance(value, str):
        raise CoordError(f"{label} must be text", code=code)
    text = unicodedata.normalize("NFC", value)
    if not text or text != text.strip() or len(text) > max_len:
        raise CoordError(
            f"{label} must be 1..{max_len} characters without surrounding whitespace",
            code=code,
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise CoordError(f"{label} cannot contain control characters", code=code)
    return text


def _validate_workspace(workspace: Any, flavor: Any) -> tuple[str, str]:
    if flavor not in ("windows", "posix"):
        raise CoordError('workspace_flavor must be "windows" or "posix"', code="invalid_workspace")
    if not isinstance(workspace, str):
        raise CoordError("workspace must be text", code="invalid_workspace")
    if not workspace or len(workspace) > MAX_WORKSPACE:
        raise CoordError(
            f"workspace must be 1..{MAX_WORKSPACE} characters", code="invalid_workspace"
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in workspace):
        raise CoordError("workspace cannot contain control characters", code="invalid_workspace")
    if flavor == "windows":
        path = PureWindowsPath(workspace)
        if not path.is_absolute():
            raise CoordError("windows workspace must be absolute", code="invalid_workspace")
        parts = path.parts[1:]
        if any(part in (".", "..") or not part for part in parts) or not parts:
            raise CoordError(
                "windows workspace cannot contain traversal or be a bare anchor",
                code="invalid_workspace",
            )
        return str(path), flavor
    path = PurePosixPath(workspace)
    if not path.is_absolute():
        raise CoordError("posix workspace must be absolute", code="invalid_workspace")
    parts = path.parts[1:]
    if any(part in (".", "..") or not part for part in parts) or not parts:
        raise CoordError(
            "posix workspace cannot contain traversal or be bare root",
            code="invalid_workspace",
        )
    return str(path), flavor


def _validate_binding(binding: Any) -> dict[str, Any]:
    if not isinstance(binding, Mapping):
        raise CoordError("binding must be an object", code="invalid_binding")
    keys = set(binding)
    forbidden = keys & FORBIDDEN_KEYS
    if forbidden:
        raise CoordError(
            f"binding must not carry execution payload: {sorted(forbidden)[0]}",
            code="forbidden_payload",
        )
    unknown = keys - set(BINDING_FIELDS)
    if unknown:
        raise CoordError(
            f"binding has unknown field: {sorted(unknown)[0]}", code="invalid_binding"
        )
    missing = set(BINDING_FIELDS) - keys
    if missing:
        raise CoordError(
            f"binding is missing field: {sorted(missing)[0]}", code="invalid_binding"
        )
    version = binding["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise CoordError("binding version must be a positive integer", code="invalid_binding")
    driver = binding["driver"]
    if driver not in SUPPORTED_DRIVERS:
        raise CoordError(
            f"driver must be one of {', '.join(SUPPORTED_DRIVERS)}", code="invalid_binding"
        )
    workspace, flavor = _validate_workspace(binding["workspace"], binding["workspace_flavor"])
    return {
        "binding_id": _safe_text(
            binding["binding_id"], label="binding_id",
            max_len=MAX_BINDING_ID, code="invalid_binding",
        ),
        "version": version,
        "execution_host": canonical_host(binding["execution_host"]),
        "executor_agent": _safe_text(
            binding["executor_agent"], label="executor_agent",
            max_len=MAX_TEXT, code="invalid_binding",
        ),
        "driver": driver,
        "native_thread_id": _safe_text(
            binding["native_thread_id"], label="native_thread_id",
            max_len=MAX_THREAD_ID, code="invalid_binding",
        ),
        "workspace": workspace,
        "workspace_flavor": flavor,
    }


def _validate_trigger(trigger: Any) -> str:
    if trigger not in TRIGGERS:
        raise CoordError(
            f"trigger must be one of {', '.join(TRIGGERS)}", code="invalid_trigger"
        )
    return trigger


def _validate_ttl(ttl_s: Any) -> int:
    if isinstance(ttl_s, bool) or not isinstance(ttl_s, int) or not 1 <= ttl_s <= 3600:
        raise CoordError("ttl_s must be an integer in 1..3600", code="invalid_ttl")
    return ttl_s


def _validate_limit(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise CoordError("limit must be an integer in 1..20", code="invalid_limit")
    return limit


def _validate_request_id(request_id: Any) -> str:
    if not isinstance(request_id, str) or len(request_id) != 32:
        raise CoordError("request_id must be exactly 32 lowercase hex", code="invalid_request")
    try:
        raw = bytes.fromhex(request_id)
    except ValueError:
        raise CoordError("request_id must be exactly 32 lowercase hex", code="invalid_request")
    if request_id != raw.hex() or any(c not in "0123456789abcdef" for c in request_id):
        raise CoordError("request_id must be exactly 32 lowercase hex", code="invalid_request")
    return request_id


def _validate_dispatch_id(dispatch_id: Any) -> str:
    if not isinstance(dispatch_id, str):
        raise CoordError("dispatch_id must be text", code="invalid_dispatch")
    _validate_request_id(dispatch_id)
    return dispatch_id


def _validate_exit_code(exit_code: Any) -> int | None:
    if exit_code is None:
        return None
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise CoordError("exit_code must be an integer or null", code="invalid_receipt")
    if not EXIT_MIN <= exit_code <= EXIT_MAX:
        raise CoordError("exit_code is out of range", code="invalid_receipt")
    return exit_code


def dispatch_id_for(binding_id: str, version: int, trigger: str, trigger_ref: str) -> str:
    material = "\x00".join((binding_id, str(version), trigger, trigger_ref))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _target_projection(record: Mapping[str, Any]) -> dict[str, Any]:
    return {field: record[field] for field in BINDING_FIELDS}


def _metadata_row(record: Mapping[str, Any]) -> dict[str, Any]:
    row = {"dispatch_id": record["dispatch_id"], **_target_projection(record)}
    row.update({
        "trigger": record["trigger"],
        "trigger_ref": record["trigger_ref"],
        "correlation_id": record["correlation_id"],
        "state": record["state"],
        "enqueued_at": _iso(record["enqueued_at"]),
        "expires_at": _iso(record["expires_at"]),
    })
    return row


class CliDispatches:
    """Closed server dispatch store. See module docstring for the trust model."""

    def __init__(
        self,
        home: Any,
        *,
        now: Callable[[], float] | float | None = None,
        eligibility: EligibilityFn | None = None,
    ) -> None:
        if isinstance(now, (int, float)) and not isinstance(now, bool):
            self._now: Callable[[], float] = lambda v=float(now): v
        elif now is None:
            self._now = time.time
        elif callable(now):
            self._now = now
        else:
            raise CoordError("now must be a clock callable, epoch number or null", code="invalid_clock")
        self._eligibility = eligibility
        root = home if hasattr(home, "joinpath") else None
        if root is None:
            from pathlib import Path

            root = Path(os.fspath(home))
        self._root = root.joinpath(*DISPATCH_DIR)
        self._root.mkdir(parents=True, exist_ok=True)
        # Fail closed on symlink/reparse ancestry planted after creation.
        cursor = root
        for part in DISPATCH_DIR:
            cursor = cursor.joinpath(part)
            if cursor.is_symlink() or not cursor.is_dir():
                raise CoordError(
                    "dispatch state path is not a real directory", code="corrupt_state"
                )
        self._lock = self._root / "store.lock"
        self._index_path = self._root / "active.json"

    # -- internal IO ----------------------------------------------------

    def _record_path(self, dispatch_id: str) -> Any:
        return self._root / f"{dispatch_id}.json"

    def _read_bytes(self, path: Any) -> bytes:
        if path.is_symlink():
            raise CoordError("dispatch state is a symlink", code="corrupt_state")
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise CoordError("dispatch state is unreadable", code="corrupt_state") from exc
        if len(data) > MAX_RECORD_BYTES:
            raise CoordError("dispatch record is oversized", code="corrupt_state")
        return data

    def _load_record(self, dispatch_id: str) -> dict[str, Any]:
        raw = self._read_bytes(self._record_path(dispatch_id))
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CoordError("dispatch record is malformed", code="corrupt_state") from exc
        if not self._well_formed(record, dispatch_id):
            raise CoordError("dispatch record is malformed", code="corrupt_state")
        return record

    @staticmethod
    def _well_formed(record: Any, dispatch_id: str) -> bool:
        if not isinstance(record, dict):
            return False
        try:
            if record.get("dispatch_id") != dispatch_id:
                return False
            if record.get("state") not in STATES:
                return False
            for field in BINDING_FIELDS:
                if not isinstance(record.get(field), str if field != "version" else int):
                    return False
            if not isinstance(record.get("trigger"), str):
                return False
            if not isinstance(record.get("trigger_ref"), str):
                return False
            correlation = record.get("correlation_id")
            if correlation is not None and not isinstance(correlation, str):
                return False
            ttl = record.get("ttl_s")
            if ttl is not None and (
                isinstance(ttl, bool)
                or not isinstance(ttl, int)
                or not 1 <= ttl <= 3600
            ):
                return False
            for stamp in ("enqueued_at", "expires_at"):
                value = record.get(stamp)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return False
            claim = record.get("claim")
            if claim is not None:
                if not isinstance(claim, dict):
                    return False
                if set(claim) != {"lease_id", "request_id", "claimed_at", "deadline"}:
                    return False
            start = record.get("start")
            if start is not None and (
                not isinstance(start, dict) or set(start) != {"started_at"}
            ):
                return False
            receipt = record.get("receipt")
            if receipt is not None:
                if not isinstance(receipt, dict):
                    return False
                if set(receipt) != {"status", "exit_code", "received_at"}:
                    return False
                if receipt["status"] not in RECEIPT_STATUSES:
                    return False
        except (AttributeError, TypeError):
            return False
        return True

    def _store_record(self, record: dict[str, Any]) -> None:
        data = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(data) > MAX_RECORD_BYTES:
            raise CoordError("dispatch record is oversized", code="corrupt_state")
        final = self._record_path(record["dispatch_id"])
        if final.is_symlink():
            raise CoordError("dispatch state is a symlink", code="corrupt_state")
        tmp = self._root / f".tmp-{uuid.uuid4().hex}.json"
        exclusive_create(tmp, data)
        try:
            os.replace(tmp, final)
        except OSError as exc:
            raise CoordError("dispatch write failed", code="busy") from exc
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    def _load_index(self) -> list[str]:
        try:
            raw = self._read_bytes(self._index_path)
        except FileNotFoundError:
            return []
        try:
            index = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CoordError("dispatch index is malformed", code="corrupt_state") from exc
        if (
            not isinstance(index, dict)
            or set(index) != {"active"}
            or not isinstance(index["active"], list)
            or len(index["active"]) > MAX_ACTIVE
            or any(not isinstance(item, str) for item in index["active"])
        ):
            raise CoordError("dispatch index is malformed", code="corrupt_state")
        return list(index["active"])

    def _store_index(self, active: list[str]) -> None:
        if len(active) > MAX_ACTIVE:
            raise CoordError("dispatch queue is full", code="queue_full")
        data = json.dumps({"active": active}, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        tmp = self._root / f".tmp-{uuid.uuid4().hex}.json"
        exclusive_create(tmp, data)
        try:
            os.replace(tmp, self._index_path)
        except OSError as exc:
            raise CoordError("dispatch index write failed", code="busy") from exc
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    # -- eligibility ----------------------------------------------------

    def _require_eligibility(self) -> EligibilityFn:
        if self._eligibility is None:
            raise CoordError(
                "server eligibility callback is required; refusing without it",
                code="eligibility_required",
            )
        return self._eligibility

    def _check_eligibility(self, record: dict[str, Any]) -> None:
        callback = self._require_eligibility()
        try:
            reason = callback(MappingProxyType(copy.deepcopy(record)))
        except CoordError:
            raise
        except Exception as exc:
            raise CoordError("eligibility check failed", code="eligibility_failed") from exc
        if reason is None:
            return
        if (
            not isinstance(reason, str)
            or not reason
            or len(reason) > MAX_TEXT
            or any(ord(char) < 32 or ord(char) == 127 for char in reason)
        ):
            raise CoordError("eligibility refusal is malformed", code="eligibility_failed")
        raise CoordError(f"dispatch refused: {reason}", code="refused", payload={"reason": reason})

    def _check_tuple(
        self, record: Mapping[str, Any], peer_host: Any, executor_agent: Any
    ) -> tuple[str, str]:
        host = canonical_host(peer_host)
        caller = _safe_text(
            executor_agent, label="executor_agent",
            max_len=MAX_TEXT, code="invalid_caller",
        )
        if record["execution_host"] != host or record["executor_agent"] != caller:
            # Generic refusal: never confirm which half mismatched and never
            # expose native identifiers to a foreign host/agent.
            raise CoordError("dispatch is not authorized for this caller", code="forbidden")
        return host, caller

    def _prune(self, active: list[str], now_ts: float) -> tuple[list[str], bool]:
        """Drop terminal/expired ids from the active index; retain their files."""
        kept: list[str] = []
        changed = False
        seen: set[str] = set()
        for dispatch_id in active:
            if dispatch_id in seen:
                changed = True
                continue
            seen.add(dispatch_id)
            try:
                record = self._load_record(dispatch_id)
            except FileNotFoundError as exc:
                # Index entry without a retained record is corruption: fail
                # closed rather than silently forgetting a dispatch.
                raise CoordError(
                    "dispatch index references a missing record", code="corrupt_state"
                ) from exc
            if record["state"] in FINAL_STATES or now_ts >= record["expires_at"]:
                changed = True
            else:
                kept.append(dispatch_id)
        return kept, changed

    # -- public operations ----------------------------------------------

    def enqueue(
        self,
        binding: Any,
        *,
        trigger: Any,
        trigger_ref: Any,
        correlation_id: Any = None,
        ttl_s: Any = 3600,
    ) -> dict[str, Any]:
        self._require_eligibility()
        projection = _validate_binding(binding)
        trigger_name = _validate_trigger(trigger)
        ref = _safe_text(
            trigger_ref, label="trigger_ref",
            max_len=MAX_TRIGGER_REF, code="invalid_trigger_ref",
        )
        correlation: str | None = None
        if correlation_id is not None:
            correlation = _safe_text(
                correlation_id, label="correlation_id",
                max_len=MAX_TEXT, code="invalid_correlation",
            )
        ttl = _validate_ttl(ttl_s)
        now_ts = self._now()
        dispatch_id = dispatch_id_for(
            projection["binding_id"], projection["version"], trigger_name, ref
        )
        record = {
            "dispatch_id": dispatch_id,
            **projection,
            "trigger": trigger_name,
            "trigger_ref": ref,
            "correlation_id": correlation,
            "ttl_s": ttl,
            "state": "pending",
            "enqueued_at": now_ts,
            "expires_at": now_ts + ttl,
            "claim": None,
            "start": None,
            "receipt": None,
        }
        with exclusive_mutex(self._lock):
            try:
                existing = self._load_record(dispatch_id)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                same = all(existing.get(key) == record[key] for key in (
                    *BINDING_FIELDS, "trigger", "trigger_ref", "correlation_id",
                    "ttl_s",
                ))
                if not same:
                    raise CoordError(
                        "dispatch id already carries a different payload",
                        code="dispatch_conflict",
                    )
                # Crash recovery: the record write is durable before the
                # index write, so an interrupted enqueue can leave a retained
                # record with no index entry. Re-link exactly that id when it
                # is still runnable; this authorizes nothing new and a replay
                # can never confer a second launch.
                active = self._load_index()
                pruned, _ = self._prune(active, now_ts)
                if (
                    dispatch_id not in pruned
                    and existing["state"] not in FINAL_STATES
                    and now_ts < existing["expires_at"]
                ):
                    if len(pruned) >= MAX_ACTIVE:
                        raise CoordError("dispatch queue is full", code="queue_full")
                    self._store_index([*pruned, dispatch_id])
                return _metadata_row(existing)
            active = self._load_index()
            active, _ = self._prune(active, now_ts)
            if len(active) >= MAX_ACTIVE:
                raise CoordError("dispatch queue is full", code="queue_full")
            self._store_record(record)
            self._store_index([*active, dispatch_id])
        return _metadata_row(record)

    def pending(
        self, *, peer_host: Any, executor_agent: Any, limit: Any = 20
    ) -> list[dict[str, Any]]:
        self._require_eligibility()
        count = _validate_limit(limit)
        host = canonical_host(peer_host)
        caller = _safe_text(
            executor_agent, label="executor_agent",
            max_len=MAX_TEXT, code="invalid_caller",
        )
        now_ts = self._now()
        rows: list[dict[str, Any]] = []
        with exclusive_mutex(self._lock):
            active = self._load_index()
            pruned, changed = self._prune(active, now_ts)
            for dispatch_id in pruned:
                record = self._load_record(dispatch_id)
                if record["state"] != "pending":
                    continue
                if record["execution_host"] != host or record["executor_agent"] != caller:
                    continue
                try:
                    self._check_eligibility(record)
                except CoordError:
                    continue
                rows.append(_metadata_row(record))
            rows.sort(key=lambda row: (row["enqueued_at"], row["dispatch_id"]))
            rows = rows[:count]
            # Retire started records that timed out to delivery_unknown while
            # holding the lock so a lost start can never launch twice.
            retired = False
            for dispatch_id in pruned:
                record = self._load_record(dispatch_id)
                if (
                    record["state"] == "started"
                    and record["start"] is not None
                    and now_ts >= record["start"]["started_at"] + STARTED_TTL_S
                ):
                    record["state"] = "delivery_unknown"
                    self._store_record(record)
                    retired = True
            if changed or retired:
                fresh, _ = self._prune(self._load_index(), now_ts)
                self._store_index(fresh)
        return rows

    def claim(
        self, dispatch_id: Any, *, peer_host: Any, executor_agent: Any, request_id: Any
    ) -> dict[str, Any]:
        self._require_eligibility()
        wanted = _validate_dispatch_id(dispatch_id)
        request = _validate_request_id(request_id)
        now_ts = self._now()
        with exclusive_mutex(self._lock):
            try:
                record = self._load_record(wanted)
            except FileNotFoundError:
                raise CoordError("unknown dispatch", code="unknown_dispatch")
            self._check_tuple(record, peer_host, executor_agent)
            if now_ts >= record["expires_at"]:
                raise CoordError("dispatch has expired", code="dispatch_expired")
            state = record["state"]
            if state == "claimed" and record["claim"] is not None:
                claim = record["claim"]
                if now_ts >= claim["deadline"]:
                    record["state"] = "delivery_unknown"
                    self._store_record(record)
                    raise CoordError("claim lease has expired", code="claim_expired")
                if claim["request_id"] == request:
                    self._check_eligibility(record)
                    return {
                        "status": "claimed",
                        "dispatch_id": wanted,
                        "lease_id": claim["lease_id"],
                        "request_id": claim["request_id"],
                        "deadline": _iso(claim["deadline"]),
                        "deadline_ts": claim["deadline"],
                        "target": _target_projection(record),
                        "replayed": True,
                    }
                raise CoordError(
                    "dispatch is already claimed under a different request",
                    code="claim_conflict",
                )
            if state != "pending":
                raise CoordError(
                    f"dispatch is not claimable from state {state}", code="not_pending"
                )
            self._check_eligibility(record)
            lease_id = secrets.token_hex(16)
            record["claim"] = {
                "lease_id": lease_id,
                "request_id": request,
                "claimed_at": now_ts,
                "deadline": now_ts + CLAIM_TTL_S,
            }
            record["state"] = "claimed"
            self._store_record(record)
            claim = record["claim"]
            return {
                "status": "claimed",
                "dispatch_id": wanted,
                "lease_id": lease_id,
                "request_id": request,
                "deadline": _iso(claim["deadline"]),
                "deadline_ts": claim["deadline"],
                "target": _target_projection(record),
                "replayed": False,
            }

    def start(
        self,
        dispatch_id: Any,
        *,
        peer_host: Any,
        executor_agent: Any,
        lease_id: Any,
        request_id: Any,
    ) -> dict[str, Any]:
        self._require_eligibility()
        wanted = _validate_dispatch_id(dispatch_id)
        request = _validate_request_id(request_id)
        if not isinstance(lease_id, str):
            raise CoordError("lease_id must be text", code="invalid_lease")
        _validate_request_id(lease_id)
        now_ts = self._now()
        with exclusive_mutex(self._lock):
            try:
                record = self._load_record(wanted)
            except FileNotFoundError:
                raise CoordError("unknown dispatch", code="unknown_dispatch")
            self._check_tuple(record, peer_host, executor_agent)
            claim = record["claim"]
            if claim is None or claim["lease_id"] != lease_id:
                raise CoordError("claim lease does not match", code="lease_mismatch")
            if claim["request_id"] != request:
                raise CoordError("claim request does not match", code="claim_conflict")
            if record["state"] == "started":
                # Replay of a lost start response: never confer a second launch.
                return {
                    "status": "authorized",
                    "dispatch_id": wanted,
                    "lease_id": lease_id,
                    "first_start": False,
                }
            if record["state"] != "claimed":
                raise CoordError(
                    f"dispatch is not startable from state {record['state']}",
                    code="not_claimed",
                )
            if now_ts >= claim["deadline"] or now_ts >= record["expires_at"]:
                record["state"] = "delivery_unknown"
                self._store_record(record)
                return {
                    "status": "delivery_unknown",
                    "dispatch_id": wanted,
                    "lease_id": lease_id,
                    "first_start": False,
                }
            # STOP and every other eligibility refusal block the transition;
            # the record stays claimed and nothing is authorized to launch.
            self._check_eligibility(record)
            record["state"] = "started"
            record["start"] = {"started_at": now_ts}
            self._store_record(record)
            return {
                "status": "authorized",
                "dispatch_id": wanted,
                "lease_id": lease_id,
                "first_start": True,
            }

    def receipt(
        self,
        dispatch_id: Any,
        *,
        peer_host: Any,
        executor_agent: Any,
        lease_id: Any,
        status: Any,
        exit_code: Any = None,
    ) -> dict[str, Any]:
        self._require_eligibility()
        wanted = _validate_dispatch_id(dispatch_id)
        if not isinstance(lease_id, str):
            raise CoordError("lease_id must be text", code="invalid_lease")
        _validate_request_id(lease_id)
        if status not in RECEIPT_STATUSES:
            raise CoordError(
                f"receipt status must be one of {', '.join(RECEIPT_STATUSES)}",
                code="invalid_receipt",
            )
        code = _validate_exit_code(exit_code)
        now_ts = self._now()
        with exclusive_mutex(self._lock):
            try:
                record = self._load_record(wanted)
            except FileNotFoundError:
                raise CoordError("unknown dispatch", code="unknown_dispatch")
            self._check_tuple(record, peer_host, executor_agent)
            claim = record["claim"]
            if claim is None or claim["lease_id"] != lease_id:
                raise CoordError("claim lease does not match", code="lease_mismatch")
            state = record["state"]
            if state in FINAL_STATES:
                prior = record["receipt"] or {}
                if prior.get("status") == status and prior.get("exit_code") == code:
                    return {
                        "dispatch_id": wanted,
                        "lease_id": lease_id,
                        "status": status,
                        "exit_code": code,
                        "final": True,
                        "replayed": True,
                    }
                raise CoordError(
                    "final receipt is immutable and conflicts", code="receipt_conflict"
                )
            if state == "started":
                # A late receipt from the owning lease settles the run as
                # process delivery only; it never authorizes another launch.
                record["receipt"] = {
                    "status": status,
                    "exit_code": code,
                    "received_at": now_ts,
                }
                record["state"] = status
                self._store_record(record)
                if status == "delivery_unknown":
                    return {
                        "dispatch_id": wanted,
                        "lease_id": lease_id,
                        "status": status,
                        "exit_code": code,
                        "final": False,
                        "replayed": False,
                    }
                active = self._load_index()
                pruned, _ = self._prune(active, now_ts)
                self._store_index(pruned)
                return {
                    "dispatch_id": wanted,
                    "lease_id": lease_id,
                    "status": status,
                    "exit_code": code,
                    "final": True,
                    "replayed": False,
                }
            if state == "delivery_unknown":
                prior = record["receipt"]
                if (
                    prior is not None
                    and prior.get("status") == status
                    and prior.get("exit_code") == code
                ):
                    return {
                        "dispatch_id": wanted,
                        "lease_id": lease_id,
                        "status": status,
                        "exit_code": code,
                        "final": status != "delivery_unknown",
                        "replayed": True,
                    }
                # A valid late receipt settles delivery_unknown for the same
                # original lease only; it never authorizes another launch.
                record["receipt"] = {
                    "status": status,
                    "exit_code": code,
                    "received_at": now_ts,
                }
                record["state"] = status
                self._store_record(record)
                if status in FINAL_STATES:
                    active = self._load_index()
                    pruned, _ = self._prune(active, now_ts)
                    self._store_index(pruned)
                return {
                    "dispatch_id": wanted,
                    "lease_id": lease_id,
                    "status": status,
                    "exit_code": code,
                    "final": status != "delivery_unknown",
                    "replayed": False,
                }
            raise CoordError(
                f"receipt is not receivable from state {state}", code="not_receivable"
            )
