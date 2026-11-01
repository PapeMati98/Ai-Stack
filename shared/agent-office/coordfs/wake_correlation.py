"""Office Wake v2: task-scoped correlations for automatic continuation.

A requester session opens an expectation for replies to specific messages it
sent (``wake-expect``). A later message wakes the requester only when it answers
one of those messages (``reply_to``), comes from the named counterpart, and
belongs to the current epoch of the task. Limits are counted over the whole
correlation and are never reset by a new message. A reply is data for the woken
session, never an authorisation; ``received``/ACK does not mark it consumed --
only the requester itself records ``consumed``. Cancelling a task bumps its
epoch, which invalidates every older expectation and pending reply.

Every Office address (caller, counterpart, requester, task owner) is stored and
compared in its canonical form. A task id lives inside its owner's boundary: only
the first requester may add expectations to it or cancel it, and a cancellation
only ever touches that requester's correlations. A request message belongs to at
most one correlation, and never to an epoch that started after it was sent.

Identity scope: callers are Office addresses of cooperating local agents running as
the same OS user (the same trust model as Office itself). Requester-only expect,
consume and owner-only task cancellation stop mistakes and confused agents; they
are not authentication against a hostile process of the same user.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from .atomic import exclusive_create, exclusive_mutex, replace_with_backoff
from .constants import MUTEX_WAIT_S
from .errors import CoordError
from .identity import AGENT_PREFIX, canonicalize_agent
from .leases import format_ts, parse_ts
from .office import ID_RE, office_root

CORRELATION_SCHEMA = "office.wake-correlation/v2"
RECEIPT_SCHEMA = "office.wake-receipt/v2"
DEFAULT_MAX_WAKES = 6
DEFAULT_MAX_HOPS = 12
DEFAULT_TTL_S = 3 * 60 * 60
MAX_TTL_S = 24 * 60 * 60
MAX_REQUESTS = 64
MAX_PENDING = 32
ACTIVE_TICK_S = 60
RELAY_OBSERVATION_TICK_S = 300
# No ':' (an NTFS alternate data stream in a file name) and no '=' (the lock-kind
# separator), so identifiers can never collide with each other's lock files.
_ID_TEXT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}\Z")
OPEN = "open"
TERMINAL_STATES = frozenset({"closed", "cancelled", "limit_reached", "expired"})


def _text_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID_TEXT_RE.fullmatch(value) is None:
        raise CoordError(f"{label} must be a short lowercase identifier", code="invalid_wake_correlation")
    return value


def _address(value: Any, label: str) -> str:
    """Canonical Office address without the ``agent:`` prefix (as Office stores ``from``)."""
    if not isinstance(value, str) or not value.strip():
        raise CoordError(f"{label} must be an Office address", code="invalid_wake_correlation")
    try:
        return canonicalize_agent(value)[len(AGENT_PREFIX):]
    except CoordError as exc:
        raise CoordError(f"{label} must be an Office address", code="invalid_wake_correlation") from exc


def _same_address(stored: Any, wanted: str) -> bool:
    try:
        return _address(stored, "address") == wanted
    except CoordError:
        return False


def _message_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or ID_RE.fullmatch(value) is None:
        raise CoordError(f"{label} must be an Office message id", code="invalid_wake_correlation")
    return value.lower()


def _bounded_int(value: Any, label: str, *, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise CoordError(f"{label} must be an integer in {low}..{high}", code="invalid_wake_correlation")
    return value


def _canonical(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


class Correlations:
    """File-backed correlation registry under ``office/wake`` (one JSON per id)."""

    def __init__(self, home: Path, now: Callable[[], float]) -> None:
        self.home = Path(home)
        self._now = now

    # -- storage -----------------------------------------------------------
    @property
    def root(self) -> Path:
        return office_root(self.home) / "wake"

    def _dir(self) -> Path:
        return self.root / "correlations"

    def _tasks_dir(self) -> Path:
        return self.root / "tasks"

    def _path(self, correlation_id: str) -> Path:
        return self._dir() / f"{correlation_id}.json"

    def _task_path(self, task_id: str) -> Path:
        return self._tasks_dir() / f"{task_id}.json"

    def _lock(self, kind: str, name: str | None = None) -> Path:
        # One namespace per lock kind: a correlation id never shares a lock with a
        # task, a request or the receipts log ('=' is outside the id charset).
        stem = kind if name is None else f"{kind}={name}"
        return self.root / "locks" / f"correlation-{stem}.lock"

    def _corr_lock(self, correlation_id: str) -> Path:
        return self._lock("corr", correlation_id)

    def _iter_correlations(self) -> list[dict[str, Any]]:
        """Every readable correlation; a corrupt file is skipped, a transient read error raises."""
        rows: list[dict[str, Any]] = []
        if not self._dir().is_dir():
            return rows
        for path in sorted(self._dir().glob("*.json")):
            try:
                correlation = self._read(path)
            except CoordError as exc:
                if exc.code == "wake_state_busy":
                    raise
                continue
            if correlation:
                rows.append(correlation)
        return rows

    def _write(self, path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
        exclusive_create(temporary, _canonical(payload))
        if not replace_with_backoff(temporary, path):
            raise CoordError("correlation state replacement remained busy", code="wake_state_busy")

    def _read(self, path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except OSError as exc:
            # Transient (sharing violation, AV scan): never read as "no correlation".
            raise CoordError("correlation state is temporarily unreadable", code="wake_state_busy") from exc
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise CoordError("correlation state cannot be read", code="invalid_wake_correlation") from exc
        if not isinstance(value, dict) or value.get("schema") != CORRELATION_SCHEMA:
            raise CoordError("correlation state has an unknown schema", code="invalid_wake_correlation")
        return value

    def _append_receipt(self, correlation: Mapping[str, Any], state: str, **fields: Any) -> dict[str, Any]:
        """Append-only lifecycle receipt; the ledger of Wake stays the per-trigger record."""
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "receipt_id": uuid.uuid4().hex,
            "binding_id": correlation["binding_id"],
            "task_id": correlation["task_id"],
            "epoch": correlation["epoch"],
            "correlation_id": correlation["correlation_id"],
            "state": state,
            "at": format_ts(self._now()),
        }
        receipt.update({key: value for key, value in fields.items() if value is not None})
        path = self.root / "correlation-receipts.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_mutex(self._lock("receipts"), timeout_s=MUTEX_WAIT_S):
            with path.open("ab") as stream:
                stream.write(_canonical(receipt))
        return receipt

    def receipts(self, correlation_id: str | None = None, *, limit: int = 200) -> list[dict[str, Any]]:
        path = self.root / "correlation-receipts.jsonl"
        rows: list[dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return rows
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and (correlation_id is None or row.get("correlation_id") == correlation_id):
                rows.append(row)
        return rows[-limit:]

    # -- task epoch --------------------------------------------------------
    def _task(self, task_id: str) -> dict[str, Any] | None:
        try:
            value = json.loads(self._task_path(task_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise CoordError("task state cannot be read", code="invalid_wake_correlation") from exc
        if not isinstance(value, dict):
            raise CoordError("task state is invalid", code="invalid_wake_correlation")
        return value

    def _task_lock(self, task_id: str) -> Path:
        # Lock order everywhere: task lock first, then a correlation lock.
        return self._lock("task", task_id)

    def _claim_task_locked(self, task_id: str, *, caller: str, binding_id: str) -> None:
        """First expectation of a task records its owner. Caller holds the task lock."""
        if self._task(task_id) is None:
            self._write(self._task_path(task_id), {"task_id": task_id, "epoch": 1, "owner": caller,
                                                   "owner_binding_id": binding_id,
                                                   "at": format_ts(self._now())})

    def task_epoch(self, task_id: str) -> int:
        task_id = _text_id(task_id, "task_id")
        try:
            value = json.loads(self._task_path(task_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return 1
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise CoordError("task epoch cannot be read", code="invalid_wake_correlation") from exc
        epoch = value.get("epoch") if isinstance(value, dict) else None
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise CoordError("task epoch is invalid", code="invalid_wake_correlation")
        return epoch

    def cancel_task(self, task_id: str, *, caller: str) -> dict[str, Any]:
        """Bump the task epoch: older expectations and pending replies stop waking."""
        task_id = _text_id(task_id, "task_id")
        caller = _address(caller, "caller")
        cancelled = []
        # The epoch bump and the invalidation of every OLDER correlation are one
        # critical section under the task lock, which expect() and reserve() also take:
        # nothing can open or reserve in between, and a correlation of the new epoch is
        # never touched.
        with exclusive_mutex(self._task_lock(task_id), timeout_s=MUTEX_WAIT_S):
            task = self._task(task_id)
            if task is None:
                raise CoordError("task has no correlations to cancel", code="wake_task_not_found")
            if not _same_address(task.get("owner"), caller):
                raise CoordError("only the task owner may cancel it", code="wake_correlation_forbidden")
            epoch = self.task_epoch(task_id) + 1
            now = format_ts(self._now())
            self._write(self._task_path(task_id), {**task, "epoch": epoch, "cancelled_by": caller,
                                                   "epoch_started_at": now, "at": now})
            for correlation in self._iter_correlations():
                if (
                    correlation["task_id"] == task_id
                    # Never across the requester boundary (legacy state may mix requesters).
                    and _same_address(correlation["requester"], caller)
                    and correlation["status"] == OPEN
                    and correlation["epoch"] < epoch
                ):
                    cancelled.append(self._set_terminal(correlation["correlation_id"], "cancelled",
                                                        reason="task_epoch_bumped")["correlation_id"])
        return {"status": "ok", "code": "wake_task_cancelled", "task_id": task_id,
                "epoch": epoch, "cancelled_correlations": cancelled}

    # -- lifecycle ---------------------------------------------------------
    def expect(
        self,
        *,
        binding: Mapping[str, Any],
        caller: str,
        task_id: str,
        correlation_id: str,
        counterpart: str,
        request_message_id: str,
        max_wakes: int = DEFAULT_MAX_WAKES,
        max_hops: int = DEFAULT_MAX_HOPS,
        ttl_s: int = DEFAULT_TTL_S,
        request_ts: str | None = None,
    ) -> dict[str, Any]:
        """Open or extend an expectation; only the binding's own session may call it.

        ``request_ts`` is the Office timestamp of the request message (the caller
        verified it exists); a request sent before the current task epoch started is
        refused, so a cancelled exchange cannot be re-armed from old context.
        """
        caller = _address(caller, "caller")
        if not _same_address(binding["office_agent"], caller):
            raise CoordError("only the bound session may open its expectations", code="wake_correlation_forbidden")
        if binding.get("correlation_required") is not True:
            raise CoordError("binding does not use task correlations", code="wake_correlation_not_enabled")
        task_id = _text_id(task_id, "task_id")
        correlation_id = _text_id(correlation_id, "correlation_id")
        request_message_id = _message_id(request_message_id, "request_message_id")
        counterpart = _address(counterpart, "counterpart")
        if counterpart == caller:
            raise CoordError("counterpart must be another Office address", code="invalid_wake_correlation")
        max_wakes = _bounded_int(max_wakes, "max_wakes", low=1, high=50)
        max_hops = _bounded_int(max_hops, "max_hops", low=1, high=100)
        ttl_s = _bounded_int(ttl_s, "ttl_s", low=60, high=MAX_TTL_S)
        # Lock order: request -> task -> correlation.  The request lock makes the
        # "one request, one correlation" check atomic across tasks.
        with exclusive_mutex(self._lock("request", request_message_id), timeout_s=MUTEX_WAIT_S), \
                exclusive_mutex(self._task_lock(task_id), timeout_s=MUTEX_WAIT_S):
            task = self._task(task_id)
            if task is not None and not _same_address(task.get("owner"), caller):
                raise CoordError("task id belongs to another requester; choose your own task id",
                                 code="wake_task_foreign")
            self._check_request_epoch(task, request_ts)
            for other in self._iter_correlations():
                if other["correlation_id"] != correlation_id and request_message_id in other["requests"]:
                    raise CoordError("request already belongs to another correlation",
                                     code="wake_request_already_correlated")
            self._claim_task_locked(task_id, caller=caller, binding_id=str(binding["binding_id"]))
            return self._expect_locked(
                binding=binding, caller=caller, task_id=task_id, correlation_id=correlation_id,
                counterpart=counterpart, request_message_id=request_message_id,
                max_wakes=max_wakes, max_hops=max_hops, ttl_s=ttl_s,
            )

    @staticmethod
    def _check_request_epoch(task: Mapping[str, Any] | None, request_ts: str | None) -> None:
        started = (task or {}).get("epoch_started_at")
        if request_ts is None or started is None:
            return
        try:
            stale = parse_ts(str(request_ts)) < parse_ts(str(started))
        except (ValueError, OverflowError, OSError):
            stale = True
        if stale:
            raise CoordError("request predates the current task epoch", code="wake_correlation_stale")

    def _expect_locked(
        self, *, binding: Mapping[str, Any], caller: str, task_id: str, correlation_id: str,
        counterpart: str, request_message_id: str, max_wakes: int, max_hops: int, ttl_s: int,
    ) -> dict[str, Any]:
        epoch = self.task_epoch(task_id)
        now = self._now()
        expired: dict[str, Any] | None = None
        with exclusive_mutex(self._corr_lock(correlation_id), timeout_s=MUTEX_WAIT_S):
            current = self._read(self._path(correlation_id))
            if current is None:
                current = {
                    "schema": CORRELATION_SCHEMA,
                    "correlation_id": correlation_id,
                    "binding_id": binding["binding_id"],
                    "requester": caller,
                    "counterpart": counterpart,
                    "task_id": task_id,
                    "epoch": epoch,
                    "status": OPEN,
                    "requests": [],
                    "pending": [],
                    "woken": [],
                    "reserved": {},
                    "answered": [],
                    "consumed": [],
                    "wakes": 0,
                    "hops": 0,
                    "max_wakes": max_wakes,
                    "max_hops": max_hops,
                    "created_at": format_ts(now),
                }
            else:
                # Identity of an existing correlation never changes; limits only shrink.
                if (
                    current["binding_id"] != binding["binding_id"]
                    or not _same_address(current["requester"], caller)
                    or not _same_address(current["counterpart"], counterpart)
                    or current["task_id"] != task_id
                ):
                    raise CoordError("correlation id already belongs to another exchange",
                                     code="wake_correlation_conflict")
                if current["status"] == OPEN and self._past_ttl(current, now):
                    current["status"] = "expired"
                    current["pending"] = []
                    self._write(self._path(correlation_id), current)
                    expired = current
                elif current["status"] != OPEN:
                    raise CoordError(f"correlation is {current['status']}", code="wake_correlation_closed")
                elif current["epoch"] != epoch:
                    raise CoordError("task epoch changed; open a new correlation", code="wake_correlation_stale")
                else:
                    current["max_wakes"] = min(current["max_wakes"], max_wakes)
                    current["max_hops"] = min(current["max_hops"], max_hops)
            if expired is None:
                if current.get("expires_at"):
                    # The owner may keep an open exchange alive; the TTL never shrinks here.
                    current["expires_at"] = format_ts(max(now + ttl_s, parse_ts(str(current["expires_at"]))))
                else:
                    current["expires_at"] = format_ts(now + ttl_s)
                if request_message_id in current["requests"]:
                    # Idempotent retry: a lower limit and a longer TTL persist (never silently lost).
                    self._write(self._path(correlation_id), current)
                    return {"status": "ok", "code": "wake_expect_exists", "correlation": current}
                if len(current["requests"]) >= MAX_REQUESTS:
                    raise CoordError("too many open requests in one correlation", code="wake_correlation_limit")
                current["requests"].append(request_message_id)
                current["hops"] += 1
                if current["hops"] > current["max_hops"]:
                    current["status"] = "limit_reached"
                self._write(self._path(correlation_id), current)
        if expired is not None:
            self._append_receipt(expired, "expired", reason="ttl")
            raise CoordError("correlation is expired", code="wake_correlation_closed")
        self._append_receipt(current, "expected", request_message_id=request_message_id)
        if current["status"] == "limit_reached":
            self._append_receipt(current, "limit_reached", reason="max_hops")
        return {"status": "ok", "code": "wake_expected", "correlation": current}

    @staticmethod
    def _past_ttl(correlation: Mapping[str, Any], now: float) -> bool:
        try:
            return now >= parse_ts(str(correlation["expires_at"]))
        except (KeyError, ValueError, OverflowError, OSError):
            return True

    def load(self, correlation_id: str) -> dict[str, Any]:
        correlation = self._read(self._path(_text_id(correlation_id, "correlation_id")))
        if correlation is None:
            raise CoordError("correlation does not exist", code="wake_correlation_not_found")
        return correlation

    def _set_terminal(self, correlation_id: str, status: str, **fields: Any) -> dict[str, Any]:
        with exclusive_mutex(self._corr_lock(correlation_id), timeout_s=MUTEX_WAIT_S):
            correlation = self.load(correlation_id)
            if correlation["status"] == OPEN:
                correlation["status"] = status
                correlation["pending"] = []
                self._write(self._path(correlation_id), correlation)
                self._append_receipt(correlation, status, **fields)
            return correlation

    def close(self, correlation_id: str, *, caller: str) -> dict[str, Any]:
        caller = _address(caller, "caller")
        correlation = self.load(correlation_id)
        if not _same_address(correlation["requester"], caller):
            raise CoordError("only the requester may close its correlation", code="wake_correlation_forbidden")
        return {"status": "ok", "code": "wake_correlation_closed",
                "correlation": self._set_terminal(correlation_id, "closed", reason="requester_closed")}

    def consume(self, correlation_id: str, *, caller: str, trigger_message_id: str) -> dict[str, Any]:
        """The requester states it processed one reply; ACK/read never implies this.

        A reply counts as delivered from its reservation on: a CLI driver runs the whole
        resumed turn (including this call) before Wake can record ``woken``.
        """
        caller = _address(caller, "caller")
        trigger_message_id = _message_id(trigger_message_id, "trigger_message_id")
        with exclusive_mutex(self._corr_lock(_text_id(correlation_id, "correlation_id")), timeout_s=MUTEX_WAIT_S):
            correlation = self.load(correlation_id)
            if not _same_address(correlation["requester"], caller):
                raise CoordError("only the requester may mark a reply consumed", code="wake_correlation_forbidden")
            if (
                trigger_message_id not in correlation["woken"]
                and trigger_message_id not in correlation.get("reserved", {})
            ):
                raise CoordError("reply was not delivered through this correlation", code="wake_correlation_unknown_reply")
            if trigger_message_id not in correlation["consumed"]:
                correlation["consumed"].append(trigger_message_id)
                self._write(self._path(correlation_id), correlation)
                self._append_receipt(correlation, "consumed", trigger_message_id=trigger_message_id)
        return {"status": "ok", "code": "wake_reply_consumed", "correlation": correlation}

    # -- used by Wake.tick -------------------------------------------------
    def match(
        self, binding: Mapping[str, Any], metadata: Mapping[str, Any], *, persist: bool = True
    ) -> dict[str, Any] | None:
        """Open, current-epoch correlation this reply answers, else None.

        An OPEN correlation always wins over a terminal one that also lists the request
        (legacy state); a terminal one is returned only when no open one exists, so the
        caller can report why nothing wakes.  ``persist=False`` (dry-run) reports TTL and
        epoch transitions without writing them.
        """
        reply_to = metadata.get("reply_to")
        if not isinstance(reply_to, str):
            return None
        now = self._now()
        requester = _address(binding["office_agent"], "office_agent")
        terminal: dict[str, Any] | None = None
        candidate: dict[str, Any] | None = None
        for correlation in self._iter_correlations():
            if (
                correlation["binding_id"] != binding["binding_id"]
                or not _same_address(correlation["requester"], requester)
                or reply_to not in correlation["requests"]
            ):
                continue
            if correlation["status"] != OPEN:
                terminal = terminal or correlation
                continue
            candidate = correlation
            break
        if candidate is None:
            return terminal
        correlation = candidate
        if not _same_address(correlation["counterpart"], str(metadata.get("from", ""))):
            return None
        try:
            if now >= parse_ts(str(correlation["expires_at"])):
                return self._terminal_view(correlation, "expired", persist=persist, reason="ttl")
            if parse_ts(str(metadata["ts"])) < parse_ts(str(correlation["created_at"])):
                return None
        except (ValueError, OverflowError, OSError):
            return None
        if correlation["epoch"] != self.task_epoch(correlation["task_id"]):
            return self._terminal_view(correlation, "cancelled", persist=persist, reason="task_epoch_bumped")
        return correlation

    def _terminal_view(self, correlation: Mapping[str, Any], status: str, *, persist: bool, reason: str) -> dict[str, Any]:
        if persist:
            return self._set_terminal(str(correlation["correlation_id"]), status, reason=reason)
        return {**correlation, "status": status, "pending": []}

    def limit_reason(self, correlation: Mapping[str, Any]) -> str | None:
        if correlation["wakes"] >= correlation["max_wakes"]:
            return "max_wakes"
        if correlation["hops"] >= correlation["max_hops"]:
            return "max_hops"
        return None

    def mark_limit(self, correlation_id: str, reason: str) -> dict[str, Any]:
        return self._set_terminal(correlation_id, "limit_reached", reason=reason)

    def reserve(
        self, correlation_id: str, trigger_message_id: str, dispatch_id: str, *, reply_to: str | None = None
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Durably reserve one wake BEFORE any external effect, under the lock cancel uses.

        A cancellation (epoch bump / terminal status) that wins the lock prevents the
        reservation; a reservation that wins is recorded before the dispatch exists, so a
        restart finds it and never creates a second, different dispatch.  A queued reply
        stays in ``pending`` until ``record_wake`` settles it, so an interruption between
        the two is replayed onto the ``delivery_unknown_reserved`` path, never lost.
        """
        task_id = self.load(correlation_id)["task_id"]  # immutable for a correlation
        with exclusive_mutex(self._task_lock(task_id), timeout_s=MUTEX_WAIT_S), \
                exclusive_mutex(self._corr_lock(correlation_id), timeout_s=MUTEX_WAIT_S):
            correlation = self.load(correlation_id)
            if correlation["status"] != OPEN:
                return None, f"correlation_{correlation['status']}"
            if correlation["epoch"] != self.task_epoch(correlation["task_id"]):
                return None, "correlation_stale_epoch"
            reserved = correlation.setdefault("reserved", {})
            if trigger_message_id in reserved or trigger_message_id in correlation["woken"]:
                return correlation, None
            limit = self.limit_reason(correlation)
            if limit is not None:
                return None, "correlation_limit_reached"
            reserved[trigger_message_id] = dispatch_id
            correlation["wakes"] += 1
            correlation["hops"] += 1
            answered = correlation.setdefault("answered", [])
            if isinstance(reply_to, str) and reply_to not in answered:
                answered.append(reply_to)
            self._write(self._path(correlation_id), correlation)
        self._append_receipt(correlation, "reserved", trigger_message_id=trigger_message_id, dispatch_id=dispatch_id)
        return correlation, None

    def defer(self, correlation_id: str, metadata: Mapping[str, Any]) -> bool:
        """Queue one reply behind the requester's current turn (no interruption)."""
        with exclusive_mutex(self._corr_lock(correlation_id), timeout_s=MUTEX_WAIT_S):
            correlation = self.load(correlation_id)
            if correlation["status"] != OPEN:
                return False
            if any(item["id"] == metadata["id"] for item in correlation["pending"]):
                return False
            if (
                metadata["id"] in correlation["woken"]
                or metadata["id"] in correlation.get("reserved", {})
                or len(correlation["pending"]) >= MAX_PENDING
            ):
                return False
            correlation["pending"].append({key: metadata.get(key) for key in ("id", "from", "kind", "reply_to", "ts")})
            self._write(self._path(correlation_id), correlation)
        self._append_receipt(correlation, "queued_behind_turn", trigger_message_id=metadata["id"])
        return True

    def pending_for(self, binding: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        rows: list[tuple[str, dict[str, Any]]] = []
        for correlation in self._iter_correlations():
            if correlation["binding_id"] == binding["binding_id"] and correlation["status"] == OPEN:
                rows.extend((correlation["correlation_id"], dict(item)) for item in correlation["pending"])
        return rows

    def drop_pending(self, correlation_id: str, trigger_message_id: str) -> None:
        with exclusive_mutex(self._corr_lock(correlation_id), timeout_s=MUTEX_WAIT_S):
            correlation = self.load(correlation_id)
            kept = [item for item in correlation["pending"] if item["id"] != trigger_message_id]
            if kept != correlation["pending"]:
                correlation["pending"] = kept
                self._write(self._path(correlation_id), correlation)

    def record_wake(self, correlation_id: str, trigger_message_id: str, *, state: str, dispatch_id: str | None) -> dict[str, Any]:
        """Settle a reserved wake (counted at reservation) after its external effect."""
        with exclusive_mutex(self._corr_lock(correlation_id), timeout_s=MUTEX_WAIT_S):
            correlation = self.load(correlation_id)
            if trigger_message_id not in correlation["woken"]:
                correlation["woken"].append(trigger_message_id)
                if trigger_message_id not in correlation.get("reserved", {}):
                    correlation["wakes"] += 1
                    correlation["hops"] += 1
            correlation["pending"] = [item for item in correlation["pending"] if item["id"] != trigger_message_id]
            self._write(self._path(correlation_id), correlation)
        self._append_receipt(correlation, state, trigger_message_id=trigger_message_id, dispatch_id=dispatch_id)
        return correlation

    def for_dispatch(self, dispatch_id: str) -> dict[str, Any] | None:
        """The correlation that reserved this relay dispatch, if any."""
        for correlation in self._iter_correlations():
            if dispatch_id in correlation.get("reserved", {}).values():
                return correlation
        return None

    def awaiting_reply(self, correlation: Mapping[str, Any], now: float) -> bool:
        """Open, unexpired, and a reply is still expected or queued."""
        if correlation["status"] != OPEN or self._past_ttl(correlation, now):
            return False
        if correlation["pending"]:
            return True
        answered = correlation.get("answered")
        if isinstance(answered, list):
            return any(request not in answered for request in correlation["requests"])
        # Legacy state without "answered": one delivered reply per request.
        delivered = set(correlation["woken"]) | set(correlation.get("reserved", {}))
        return len(delivered) < len(correlation["requests"])

    def summary(self, binding_ids: set[str] | frozenset[str] | None = None) -> dict[str, int]:
        """Open and reply-awaiting counts, limited to ``binding_ids`` when given."""
        now = self._now()
        open_count = awaiting = 0
        try:
            rows = self._iter_correlations()
        except CoordError:
            return {"open": 0, "awaiting": 0}
        for correlation in rows:
            if correlation["status"] != OPEN or self._past_ttl(correlation, now):
                continue
            open_count += 1
            if (binding_ids is None or correlation["binding_id"] in binding_ids) and self.awaiting_reply(correlation, now):
                awaiting += 1
        return {"open": open_count, "awaiting": awaiting}

    def outstanding_dispatch_ids(self, binding_ids: set[str]) -> list[str]:
        """Reserved relay deliveries still awaiting the requester's consumption."""
        now = self._now()
        try:
            rows = self._iter_correlations()
        except CoordError:
            return []
        ids: list[str] = []
        for correlation in rows:
            if (correlation["status"] != OPEN or self._past_ttl(correlation, now)
                    or correlation["binding_id"] not in binding_ids):
                continue
            consumed = set(correlation["consumed"])
            ids.extend(dispatch_id for message_id, dispatch_id in correlation.get("reserved", {}).items()
                       if message_id not in consumed)
        return ids

    def open_count(self) -> int:
        return self.summary()["open"]

    def list(self) -> dict[str, Any]:
        rows = []
        if self._dir().is_dir():
            for path in sorted(self._dir().glob("*.json")):
                try:
                    correlation = self._read(path)
                except CoordError:
                    continue
                if correlation:
                    rows.append(correlation)
        return {"status": "ok", "code": "wake_correlations", "correlations": rows,
                "open": sum(1 for row in rows if row["status"] == OPEN)}
