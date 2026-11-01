"""Durable transport state inside the shared authority SQLite file.

This module adds ONLY ``cc_*`` tables. It never creates, migrates, or
writes the ``openclaw_ops`` authority tables (tasks/intents/events); the
owner grant, generations, and goal progress stay authoritative in
``openclaw_ops.Ledger``. Raw owner text is stored verbatim (exact Unicode,
no trim, no normalisation) and is only ever returned via the explicit
receiver-readback path -- never via status/diagnostic outputs.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from typing import Any, Optional

from openclaw_control import models

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cc_dispatches(
  dispatch_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL DEFAULT '',
  generation INTEGER NOT NULL DEFAULT 0,
  target_binding TEXT NOT NULL DEFAULT '',
  provider TEXT NOT NULL DEFAULT '',
  native_thread TEXT NOT NULL DEFAULT '',
  raw_text TEXT NOT NULL DEFAULT '',
  entities_json TEXT NOT NULL DEFAULT '[]',
  text_sha256 TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'queued',
  ack TEXT NOT NULL DEFAULT '',
  result_detail TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL DEFAULT 0,
  claim_owner TEXT NOT NULL DEFAULT '',
  claim_token TEXT NOT NULL DEFAULT '',
  claim_started_at REAL NOT NULL DEFAULT 0,
  alarm_sent INTEGER NOT NULL DEFAULT 0,
  correction_of TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS cc_updates(
  update_id INTEGER PRIMARY KEY,
  received_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cc_pairing(
  nonce TEXT PRIMARY KEY,
  created_at REAL NOT NULL DEFAULT 0,
  expires_at REAL NOT NULL DEFAULT 0,
  consumed INTEGER NOT NULL DEFAULT 0,
  sender_id INTEGER NOT NULL DEFAULT 0,
  chat_id INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cc_callbacks(
  action_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT '',
  task_id TEXT NOT NULL DEFAULT '',
  target_binding TEXT NOT NULL DEFAULT '',
  provider TEXT NOT NULL DEFAULT '',
  native_thread TEXT NOT NULL DEFAULT '',
  generation INTEGER NOT NULL DEFAULT 0,
  scope TEXT NOT NULL DEFAULT '',
  expires_at REAL NOT NULL DEFAULT 0,
  consumed INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cc_stop(
  scope TEXT PRIMARY KEY,
  stopped_at REAL NOT NULL DEFAULT 0,
  reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS cc_status(
  dispatch_id TEXT PRIMARY KEY,
  chat_id INTEGER NOT NULL DEFAULT 0,
  message_id INTEGER NOT NULL DEFAULT 0,
  last_hash TEXT NOT NULL DEFAULT '',
  uncertain INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cc_takeover(
  task_id TEXT PRIMARY KEY,
  host_session TEXT NOT NULL DEFAULT '',
  taken_at REAL NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS cc_receipts(
  receipt_id TEXT PRIMARY KEY,
  chat_id INTEGER NOT NULL DEFAULT 0,
  message_id INTEGER NOT NULL DEFAULT 0,
  reason TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cc_messages(
  chat_id INTEGER NOT NULL,
  message_id INTEGER NOT NULL,
  dispatch_id TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS cc_selection(
  chat_id INTEGER PRIMARY KEY,
  task_id TEXT NOT NULL DEFAULT '',
  generation INTEGER NOT NULL DEFAULT 0,
  target_binding TEXT NOT NULL DEFAULT '',
  provider TEXT NOT NULL DEFAULT '',
  native_thread TEXT NOT NULL DEFAULT '',
  action_id TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cc_inbox(
  update_id INTEGER PRIMARY KEY,
  payload_json TEXT NOT NULL DEFAULT '',
  received_at REAL NOT NULL DEFAULT 0,
  processed INTEGER NOT NULL DEFAULT 0,
  processed_at REAL NOT NULL DEFAULT 0,
  result_kind TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS cc_poll_cursor(
  key TEXT PRIMARY KEY,
  offset INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cc_runtime_kv(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT '',
  updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cc_supervision(
  target_id TEXT PRIMARY KEY,
  grant_ref TEXT NOT NULL DEFAULT '',
  grant_expires REAL NOT NULL DEFAULT 0,
  no_progress_attempts INTEGER NOT NULL DEFAULT 0,
  last_turn_id TEXT NOT NULL DEFAULT '',
  last_reason TEXT NOT NULL DEFAULT '',
  updated_at REAL NOT NULL DEFAULT 0
);
"""

_STATUS_EXTRA = (
    ("outbound_state", "TEXT NOT NULL DEFAULT ''"),
    ("intent_token", "TEXT NOT NULL DEFAULT ''"),
    ("intent_hash", "TEXT NOT NULL DEFAULT ''"),
)
_DISPATCH_EXTRA = (
    ("source_update_id", "TEXT NOT NULL DEFAULT ''"),
    ("delivery_attempt_at", "REAL NOT NULL DEFAULT 0"),
)


class ControlError(Exception):
    """Base error for transport-store contract violations."""


class StoppedError(ControlError):
    """Raised when a persisted STOP blocks a new dispatch or turn start."""


class UnknownDispatchError(ControlError):
    """No such dispatch id."""


class ClaimMismatchError(ControlError):
    """ACK/finish presented for a state or claim token that does not hold."""


class ReconcileError(ControlError):
    """Illegal delivery-state transition."""


class CallbackError(ControlError):
    """Base error for owner-callback rejection."""


class UnknownCallbackError(CallbackError):
    """Callback action id is forged/unknown."""


class ExpiredCallbackError(CallbackError):
    """Callback arrived after its expiry."""


class ReplayedCallbackError(CallbackError):
    """Callback was already consumed (single-use)."""


class GenerationMismatchError(CallbackError):
    """Callback generation does not match the expected generation."""


class PairingError(ControlError):
    """Pairing nonce rejected (unknown/expired/reused/non-private)."""


def sha256_text(text: str) -> str:
    """SHA256 over the exact UTF-8 bytes. No normalisation, no trim."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def target_binding_id(provider: str, native_thread: str) -> str:
    """Stable binding id for one native target (provider + native ref)."""
    return hashlib.sha256(
        ("%s\x00%s" % (provider, native_thread)).encode("utf-8")
    ).hexdigest()


class ControlStore:
    """All cc_* state. Shares the SQLite FILE with Ledger, never its tables."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.isolation_level = None  # autocommit; explicit transactions
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.executescript(_SCHEMA)
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        """Add columns introduced after the original cc_* schema."""
        status_cols = {
            row[1] for row in self.conn.execute("PRAGMA table_info(cc_status)")
        }
        for name, definition in _STATUS_EXTRA:
            if name not in status_cols:
                self.conn.execute(
                    "ALTER TABLE cc_status ADD COLUMN %s %s" % (name, definition)
                )
        dispatch_cols = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(cc_dispatches)")
        }
        for name, definition in _DISPATCH_EXTRA:
            if name not in dispatch_cols:
                self.conn.execute(
                    "ALTER TABLE cc_dispatches ADD COLUMN %s %s"
                    % (name, definition)
                )

    def in_transaction(self) -> bool:
        return bool(self.conn.in_transaction)

    # -- transaction helpers ------------------------------------------
    def _begin(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self.conn.execute("COMMIT")

    def _rollback(self) -> None:
        try:
            self.conn.execute("ROLLBACK")
        except Exception:
            pass

    # -- STOP ----------------------------------------------------------
    def set_stop(self, scope: str, now: float, reason: str = "") -> None:
        owns = not self.conn.in_transaction
        if owns:
            self._begin()
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO cc_stop(scope, stopped_at, reason)"
                " VALUES(?,?,?)",
                (scope, now, reason),
            )
            if owns:
                self._commit()
        except BaseException:
            if owns:
                self._rollback()
            raise

    def clear_stop(self, scope: str) -> None:
        self.conn.execute("DELETE FROM cc_stop WHERE scope=?", (scope,))

    def list_stops(self) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM cc_stop ORDER BY scope"
            ).fetchall()
        ]

    def is_stopped(self, task_id: str = "") -> bool:
        scopes = {models.STOP_SCOPE_ALL}
        if task_id:
            scopes.add(models.task_scope(task_id))
        rows = self.conn.execute("SELECT scope FROM cc_stop").fetchall()
        return any(r["scope"] in scopes for r in rows)

    # -- dispatches ----------------------------------------------------
    def enqueue_dispatch(
        self,
        task_id: str,
        generation: int,
        provider: str,
        native_thread: str,
        raw_text: str,
        entities: Any,
        now: float,
        target_binding: str = "",
        correction_of: str = "",
        dispatch_id: Optional[str] = None,
        source_update_id: Any = None,
    ) -> dict:
        """Persist one immutable prompt envelope. STOP blocks new entries."""
        if not isinstance(raw_text, str):
            raise ControlError("raw_text must be exact str")
        owns = not self.conn.in_transaction
        if owns:
            self._begin()
        try:
            if self.is_stopped(task_id):
                raise StoppedError("STOP active; new dispatch refused")
            if not target_binding and (provider or native_thread):
                target_binding = target_binding_id(provider, native_thread)
            if dispatch_id is None and source_update_id:
                dispatch_id = "tgupd:%s" % source_update_id
            dispatch_id = dispatch_id or uuid.uuid4().hex
            entities_json = json.dumps(entities or [], ensure_ascii=False)
            digest = sha256_text(raw_text)
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO cc_dispatches(dispatch_id, task_id,"
                " generation, target_binding, provider, native_thread,"
                " raw_text, entities_json, text_sha256, state, created_at,"
                " updated_at, correction_of, source_update_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    dispatch_id,
                    task_id,
                    int(generation),
                    target_binding,
                    provider,
                    native_thread,
                    raw_text,
                    entities_json,
                    digest,
                    models.QUEUED,
                    now,
                    now,
                    correction_of,
                    str(source_update_id or ""),
                ),
            )
            if cur.rowcount == 0 and source_update_id:
                row = self.get_dispatch(dispatch_id)
                if row is None:
                    raise ControlError("dispatch id collision")
                if owns:
                    self._commit()
                return row
            if cur.rowcount == 0:
                raise ControlError("dispatch id collision")
            if owns:
                self._commit()
            row = self.get_dispatch(dispatch_id)
            assert row is not None
            return row
        except BaseException:
            if owns:
                self._rollback()
            raise

    def get_dispatch(self, dispatch_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM cc_dispatches WHERE dispatch_id=?",
            (dispatch_id,),
        ).fetchone()
        return dict(row) if row else None

    def require_dispatch(self, dispatch_id: str) -> dict:
        row = self.get_dispatch(dispatch_id)
        if row is None:
            raise UnknownDispatchError(
                "unknown dispatch %r" % (dispatch_id,)
            )
        return row

    def list_dispatches(self, state: Optional[str] = None) -> list[dict]:
        if state is None:
            rows = self.conn.execute(
                "SELECT * FROM cc_dispatches ORDER BY created_at, rowid"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM cc_dispatches WHERE state=?"
                " ORDER BY created_at, rowid",
                (state,),
            ).fetchall()
        return [dict(r) for r in rows]

    def claim_one(
        self,
        worker_id: str,
        now: float,
        skip_targets: Optional[set] = None,
        expected_generation: Optional[int] = None,
    ) -> Optional[dict]:
        """Atomically claim one queued dispatch. Concurrent-safe.

        Rechecks persisted STOP inside the same IMMEDIATE transaction so a
        STOP that landed after enqueue cannot be claimed for send.
        """
        skip_targets = skip_targets or set()
        self._begin()
        try:
            rows = self.conn.execute(
                "SELECT * FROM cc_dispatches WHERE state=?"
                " ORDER BY created_at, rowid",
                (models.QUEUED,),
            ).fetchall()
            entry = None
            for row in rows:
                candidate = dict(row)
                if candidate.get("dispatch_id") in skip_targets:
                    continue
                if candidate.get("native_thread") in skip_targets:
                    continue
                if candidate.get("target_binding") in skip_targets:
                    continue
                if self.is_stopped(candidate.get("task_id") or ""):
                    continue
                if candidate.get('target_binding') and self.conn.execute(
                    'SELECT 1 FROM cc_dispatches WHERE target_binding=? '
                    'AND state IN (?,?,?,?,?,?) LIMIT 1',
                    (candidate['target_binding'], models.DISPATCHING, models.DELIVERED,
                     models.RUNNING, models.DELIVERY_UNKNOWN, models.CANCEL_PENDING,
                     models.USER_PAUSED)).fetchone():
                    continue
                if expected_generation is not None and int(
                    candidate.get("generation") or 0
                ) != int(expected_generation):
                    continue
                entry = candidate
                break
            if entry is None:
                self._commit()
                return None
            # Recheck STOP immediately before the claim write.
            if self.is_stopped(entry.get("task_id") or ""):
                self._commit()
                return None
            token = uuid.uuid4().hex
            cur = self.conn.execute(
                "UPDATE cc_dispatches SET state=?, claim_owner=?,"
                " claim_token=?, claim_started_at=?, updated_at=?, delivery_attempt_at=?"
                " WHERE dispatch_id=? AND state=?",
                (
                    models.DISPATCHING,
                    worker_id,
                    token,
                    now,
                    now,
                    now,
                    entry["dispatch_id"],
                    models.QUEUED,
                ),
            )
            if cur.rowcount != 1:
                self._rollback()
                return None
            self._commit()
            entry["state"] = models.DISPATCHING
            entry["claim_owner"] = worker_id
            entry["claim_token"] = token
            entry["claim_started_at"] = now
            entry['delivery_attempt_at'] = now
            return entry
        except BaseException:
            self._rollback()
            raise

    def unclaim(self, dispatch_id: str, now: float) -> dict:
        """Return a dispatching claim to queued (worker abort, no send)."""
        self._begin()
        try:
            row = self.require_dispatch(dispatch_id)
            if row["state"] != models.DISPATCHING:
                self._rollback()
                raise ClaimMismatchError(
                    "unclaim needs dispatching, has %r" % (row["state"],)
                )
            self.conn.execute(
                "UPDATE cc_dispatches SET state=?, claim_owner='',"
                " claim_token='', claim_started_at=0, updated_at=?"
                " WHERE dispatch_id=?",
                (models.QUEUED, now, dispatch_id),
            )
            self._commit()
            return self.require_dispatch(dispatch_id)
        except BaseException:
            if self.conn.in_transaction:
                self._rollback()
            raise

    def record_ack(
        self,
        dispatch_id: str,
        ack: str,
        now: float,
        claim_token: Optional[str] = None,
    ) -> dict:
        """Record the ACK fact (distinct from turn start and result)."""
        if ack not in (models.ACKED, models.ACK_UNKNOWN):
            raise ControlError("bad ack %r" % (ack,))
        self._begin()
        try:
            row = self.require_dispatch(dispatch_id)
            if row["state"] != models.DISPATCHING:
                self._rollback()
                raise ClaimMismatchError(
                    "ack needs dispatching, has %r" % (row["state"],)
                )
            if claim_token is not None and (
                claim_token != row["claim_token"]
            ):
                self._rollback()
                raise ClaimMismatchError("claim token mismatch")
            state = (
                models.DELIVERED
                if ack == models.ACKED
                else models.DELIVERY_UNKNOWN
            )
            self.conn.execute(
                "UPDATE cc_dispatches SET state=?, ack=?, claim_owner='',"
                " claim_token='', claim_started_at=0, updated_at=?"
                " WHERE dispatch_id=?",
                (state, ack, now, dispatch_id),
            )
            self._commit()
            return self.require_dispatch(dispatch_id)
        except BaseException:
            if self.conn.in_transaction:
                self._rollback()
            raise

    def mark_unknown(self, dispatch_id: str, now: float) -> dict:
        """Crash/exception path: uncertain delivery, never auto-resent."""
        self._begin()
        try:
            row = self.require_dispatch(dispatch_id)
            if row["state"] != models.DISPATCHING:
                self._rollback()
                raise ClaimMismatchError(
                    "mark_unknown needs dispatching, has %r"
                    % (row["state"],)
                )
            self.conn.execute(
                "UPDATE cc_dispatches SET state=?, ack=?, claim_owner='',"
                " claim_token='', claim_started_at=0, updated_at=?"
                " WHERE dispatch_id=?",
                (models.DELIVERY_UNKNOWN, models.ACK_UNKNOWN, now,
                 dispatch_id),
            )
            self._commit()
            return self.require_dispatch(dispatch_id)
        except BaseException:
            if self.conn.in_transaction:
                self._rollback()
            raise

    def record_start(self, dispatch_id: str, now: float) -> dict:
        """Record an already-observed native turn start (distinct from ACK).

        STOP blocks *new sends*, not recording observed truth. A STOP that
        becomes active after send must not erase a native start that already
        happened.
        """
        self._begin()
        try:
            row = self.require_dispatch(dispatch_id)
            if row["state"] != models.DELIVERED:
                self._rollback()
                raise ClaimMismatchError(
                    "start needs delivered, has %r" % (row["state"],)
                )
            self.conn.execute(
                "UPDATE cc_dispatches SET state=?, updated_at=?"
                " WHERE dispatch_id=?",
                (models.RUNNING, now, dispatch_id),
            )
            self._commit()
            return self.require_dispatch(dispatch_id)
        except BaseException:
            if self.conn.in_transaction:
                self._rollback()
            raise

    def record_result(
        self, dispatch_id: str, result: str, now: float, detail: str = ""
    ) -> dict:
        """Record the result fact (distinct from ACK and start)."""
        if result not in (models.COMPLETED, models.BLOCKED, models.FAILED):
            raise ControlError("bad result %r" % (result,))
        self._begin()
        try:
            row = self.require_dispatch(dispatch_id)
            if row["state"] not in (models.RUNNING, models.CANCEL_PENDING):
                self._rollback()
                raise ClaimMismatchError(
                    "result needs running, has %r" % (row["state"],)
                )
            self.conn.execute(
                "UPDATE cc_dispatches SET state=?, result_detail=?,"
                " updated_at=? WHERE dispatch_id=?",
                (result, detail, now, dispatch_id),
            )
            self._commit()
            return self.require_dispatch(dispatch_id)
        except BaseException:
            if self.conn.in_transaction:
                self._rollback()
            raise

    def mark_blocked(
        self, dispatch_id: str, now: float, detail: str = ""
    ) -> dict:
        """Block a dispatch that cannot be delivered (e.g. refusal)."""
        self._begin()
        try:
            row = self.require_dispatch(dispatch_id)
            if row["state"] not in (
                models.QUEUED,
                models.DISPATCHING,
                models.DELIVERED,
            ):
                self._rollback()
                raise ReconcileError(
                    "cannot block from %r" % (row["state"],)
                )
            self.conn.execute(
                "UPDATE cc_dispatches SET state=?, result_detail=?,"
                " claim_owner='', claim_token='', claim_started_at=0,"
                " updated_at=? WHERE dispatch_id=?",
                (models.BLOCKED, detail, now, dispatch_id),
            )
            self._commit()
            return self.require_dispatch(dispatch_id)
        except BaseException:
            if self.conn.in_transaction:
                self._rollback()
            raise

    def bind_target(
        self,
        dispatch_id: str,
        task_id: str,
        generation: int,
        provider: str,
        native_thread: str,
        now: float,
    ) -> dict:
        """Explicit target selection made OUTSIDE the prompt text."""
        binding = target_binding_id(provider, native_thread)
        self._begin()
        try:
            row = self.require_dispatch(dispatch_id)
            if row["state"] != models.QUEUED:
                self._rollback()
                raise ReconcileError(
                    "bind needs queued, has %r" % (row["state"],)
                )
            self.conn.execute(
                "UPDATE cc_dispatches SET task_id=?, generation=?,"
                " target_binding=?, provider=?, native_thread=?,"
                " updated_at=? WHERE dispatch_id=?",
                (
                    task_id,
                    int(generation),
                    binding,
                    provider,
                    native_thread,
                    now,
                    dispatch_id,
                ),
            )
            self._commit()
            return self.require_dispatch(dispatch_id)
        except BaseException:
            if self.conn.in_transaction:
                self._rollback()
            raise

    # -- crash recovery / reconciliation / alarm -----------------------
    def recover_crashed(self, now: float) -> list[dict]:
        """Restart recovery: every orphaned dispatching claim is UNKNOWN.

        Never resends. The owner reconciles each entry explicitly.
        """
        self._begin()
        try:
            rows = self.conn.execute(
                "SELECT * FROM cc_dispatches WHERE state=?",
                (models.DISPATCHING,),
            ).fetchall()
            out = [dict(r) for r in rows]
            self.conn.execute(
                "UPDATE cc_dispatches SET state=?, ack=?, claim_owner='',"
                " claim_token='', claim_started_at=0, updated_at=?"
                " WHERE state=?",
                (
                    models.DELIVERY_UNKNOWN,
                    models.ACK_UNKNOWN,
                    now,
                    models.DISPATCHING,
                ),
            )
            self._commit()
            for row in out:
                row["state"] = models.DELIVERY_UNKNOWN
                row["ack"] = models.ACK_UNKNOWN
            return out
        except BaseException:
            self._rollback()
            raise

    def pop_unknown_alarm(self, now: float) -> Optional[dict]:
        """Return at most ONE uncertain dispatch due for its single alarm.

        Due when it has been unknown for >= UNKNOWN_ALARM_AFTER and no
        alarm was sent yet. Marks alarm_sent so it never repeats.
        """
        self._begin()
        try:
            row = self.conn.execute(
                "SELECT * FROM cc_dispatches WHERE state=? AND alarm_sent=0"
                " AND updated_at<=? ORDER BY updated_at, rowid LIMIT 1",
                (models.DELIVERY_UNKNOWN, now - models.UNKNOWN_ALARM_AFTER),
            ).fetchone()
            if row is None:
                self._commit()
                return None
            entry = dict(row)
            self.conn.execute(
                "UPDATE cc_dispatches SET alarm_sent=1, updated_at=?"
                " WHERE dispatch_id=?",
                (now, entry["dispatch_id"]),
            )
            self._commit()
            entry["alarm_sent"] = 1
            return entry
        except BaseException:
            self._rollback()
            raise

    def reconcile(
        self, dispatch_id: str, delivered: bool, now: float
    ) -> dict:
        """Explicit owner reconciliation of an UNKNOWN dispatch.

        delivered=True confirms delivery; False requeues exactly once by
        owner decision -- never an automatic duplicate send.
        """
        self._begin()
        try:
            row = self.require_dispatch(dispatch_id)
            if row["state"] != models.DELIVERY_UNKNOWN:
                self._rollback()
                raise ReconcileError(
                    "reconcile needs delivery_unknown, has %r"
                    % (row["state"],)
                )
            state = models.DELIVERED if delivered else models.QUEUED
            ack = models.ACKED if delivered else ""
            self.conn.execute(
                "UPDATE cc_dispatches SET state=?, ack=?, updated_at=?"
                " WHERE dispatch_id=?",
                (state, ack, now, dispatch_id),
            )
            self._commit()
            return self.require_dispatch(dispatch_id)
        except BaseException:
            if self.conn.in_transaction:
                self._rollback()
            raise

    # -- cancellation: pending vs confirmed -----------------------------
    def request_cancel(self, dispatch_id: str, now: float) -> dict:
        self._begin()
        try:
            row = self.require_dispatch(dispatch_id)
            if row["state"] not in models.CANCELLABLE_STATES:
                self._rollback()
                raise ReconcileError(
                    "cannot cancel from %r" % (row["state"],)
                )
            self.conn.execute(
                "UPDATE cc_dispatches SET state=?, updated_at=?"
                " WHERE dispatch_id=?",
                (models.CANCEL_PENDING, now, dispatch_id),
            )
            self._commit()
            return self.require_dispatch(dispatch_id)
        except BaseException:
            if self.conn.in_transaction:
                self._rollback()
            raise

    def confirm_cancel(
        self, dispatch_id: str, confirmed: bool, now: float
    ) -> dict:
        """Adapter confirmation of a stop.

        confirmed=True ends the turn (cancelled). confirmed=False leaves
        cancel_pending -- absence of confirmation is NOT a stop.
        """
        self._begin()
        try:
            row = self.require_dispatch(dispatch_id)
            if row["state"] != models.CANCEL_PENDING:
                self._rollback()
                raise ReconcileError(
                    "confirm needs cancel_pending, has %r"
                    % (row["state"],)
                )
            state = models.CANCELLED if confirmed else models.CANCEL_PENDING
            self.conn.execute(
                "UPDATE cc_dispatches SET state=?, updated_at=?"
                " WHERE dispatch_id=?",
                (state, now, dispatch_id),
            )
            self._commit()
            return self.require_dispatch(dispatch_id)
        except BaseException:
            if self.conn.in_transaction:
                self._rollback()
            raise

    # -- owner callbacks (single-use, expiring, bound) -------------------
    def issue_callback(
        self,
        kind: str,
        task_id: str,
        provider: str,
        native_thread: str,
        generation: int,
        scope: str,
        now: float,
        ttl: float = models.CALLBACK_TTL,
    ) -> str:
        action_id = secrets.token_urlsafe(18)
        binding = (
            target_binding_id(provider, native_thread)
            if (provider or native_thread)
            else ""
        )
        self.conn.execute(
            "INSERT INTO cc_callbacks(action_id, kind, task_id,"
            " target_binding, provider, native_thread, generation, scope,"
            " expires_at, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                action_id,
                kind,
                task_id,
                binding,
                provider,
                native_thread,
                int(generation),
                scope,
                now + float(ttl),
                now,
            ),
        )
        return action_id

    def peek_callback(self, action_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM cc_callbacks WHERE action_id=?", (action_id,)
        ).fetchone()
        return dict(row) if row else None

    def consume_callback(
        self,
        action_id: str,
        now: float,
        expected_task: str = "",
        expected_generation: Optional[int] = None,
    ) -> dict:
        """Consume one callback transactionally. Rejects forged/stale/replay."""
        owns = not self.conn.in_transaction
        if owns:
            self._begin()
        try:
            row = self.conn.execute(
                "SELECT * FROM cc_callbacks WHERE action_id=?",
                (action_id,),
            ).fetchone()
            if row is None:
                if owns:
                    self._commit()
                raise UnknownCallbackError("forged or unknown callback")
            entry = dict(row)
            if entry["consumed"]:
                if owns:
                    self._commit()
                raise ReplayedCallbackError("callback already consumed")
            if float(entry["expires_at"]) <= now:
                self.conn.execute(
                    "UPDATE cc_callbacks SET consumed=1 WHERE action_id=?",
                    (action_id,),
                )
                if owns:
                    self._commit()
                raise ExpiredCallbackError("callback expired")
            if expected_task and entry["task_id"] != expected_task:
                if owns:
                    self._rollback()
                raise CallbackError("callback binds another task")
            if expected_generation is not None and int(
                entry["generation"]
            ) != int(expected_generation):
                if owns:
                    self._rollback()
                raise GenerationMismatchError(
                    "callback binds another generation"
                )
            self.conn.execute(
                "UPDATE cc_callbacks SET consumed=1 WHERE action_id=?",
                (action_id,),
            )
            if owns:
                self._commit()
            entry["consumed"] = 1
            return entry
        except BaseException:
            if owns and self.conn.in_transaction:
                self._rollback()
            raise

    def consume_and_bind(
        self, action_id: str, dispatch_id: str, now: float
    ) -> dict:
        """Consume a target_select callback AND bind its target atomically."""
        owns = not self.conn.in_transaction
        if owns:
            self._begin()
        try:
            row = self.conn.execute(
                "SELECT * FROM cc_callbacks WHERE action_id=?",
                (action_id,),
            ).fetchone()
            if row is None:
                if owns:
                    self._commit()
                raise UnknownCallbackError("forged or unknown callback")
            cb = dict(row)
            if cb["consumed"]:
                if owns:
                    self._commit()
                raise ReplayedCallbackError("callback already consumed")
            if float(cb["expires_at"]) <= now:
                self.conn.execute(
                    "UPDATE cc_callbacks SET consumed=1 WHERE action_id=?",
                    (action_id,),
                )
                if owns:
                    self._commit()
                raise ExpiredCallbackError("callback expired")
            disp = self.require_dispatch(dispatch_id)
            if disp["state"] != models.QUEUED:
                if owns:
                    self._rollback()
                raise ReconcileError(
                    "bind needs queued, has %r" % (disp["state"],)
                )
            self.conn.execute(
                "UPDATE cc_callbacks SET consumed=1 WHERE action_id=?",
                (action_id,),
            )
            self.conn.execute(
                "UPDATE cc_dispatches SET task_id=?, generation=?,"
                " target_binding=?, provider=?, native_thread=?,"
                " updated_at=? WHERE dispatch_id=?",
                (
                    cb["task_id"],
                    int(cb["generation"]),
                    cb["target_binding"],
                    cb["provider"],
                    cb["native_thread"],
                    now,
                    dispatch_id,
                ),
            )
            if owns:
                self._commit()
            return {
                "callback": cb,
                "dispatch": self.require_dispatch(dispatch_id),
            }
        except BaseException:
            if owns and self.conn.in_transaction:
                self._rollback()
            raise

    # -- pairing nonces -------------------------------------------------
    def issue_nonce(self, now: float, ttl: float = models.PAIRING_TTL) -> str:
        nonce = secrets.token_urlsafe(16)
        self.conn.execute(
            "INSERT INTO cc_pairing(nonce, created_at, expires_at)"
            " VALUES(?,?,?)",
            (nonce, now, now + float(ttl)),
        )
        return nonce

    def redeem_nonce(
        self,
        nonce: str,
        sender_id: int,
        chat_id: int,
        chat_type: str,
        now: float,
    ) -> dict:
        """Redeem one pairing nonce. Sender must be a verified private chat."""
        self._begin()
        try:
            row = self.conn.execute(
                "SELECT * FROM cc_pairing WHERE nonce=?", (nonce,)
            ).fetchone()
            if row is None:
                self._commit()
                raise PairingError("unknown pairing nonce")
            entry = dict(row)
            if entry["consumed"]:
                self._commit()
                raise PairingError("pairing nonce already used")
            if float(entry["expires_at"]) <= now:
                self.conn.execute(
                    "UPDATE cc_pairing SET consumed=1 WHERE nonce=?",
                    (nonce,),
                )
                self._commit()
                raise PairingError("pairing nonce expired")
            if chat_type != "private":
                self._rollback()
                raise PairingError("pairing needs a private chat")
            self.conn.execute(
                "UPDATE cc_pairing SET consumed=1, sender_id=?, chat_id=?"
                " WHERE nonce=?",
                (int(sender_id), int(chat_id), nonce),
            )
            self._commit()
            entry["consumed"] = 1
            entry["sender_id"] = int(sender_id)
            entry["chat_id"] = int(chat_id)
            return entry
        except BaseException:
            if self.conn.in_transaction:
                self._rollback()
            raise

    # -- Telegram intake bookkeeping ------------------------------------
    def seen_update(self, update_id: int, now: float) -> bool:
        """Record an update id. True when new, False on durable duplicate.

        Prefer stash_inbox + mark_inbox_processed for crash-safe intake.
        This helper remains for callers that only need durable dedupe.
        """
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO cc_updates(update_id, received_at)"
            " VALUES(?,?)",
            (int(update_id), now),
        )
        return cur.rowcount == 1

    def get_inbox(self, update_id: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM cc_inbox WHERE update_id=?",
            (int(update_id),),
        ).fetchone()
        return dict(row) if row else None

    def stash_inbox(
        self, update_id: int, payload_json: str, now: float
    ) -> str:
        """Durably stash one update. Returns new/pending/processed.

        Committed independently of enqueue so a crash after receipt still
        leaves the payload recoverable.
        """
        owns = not self.conn.in_transaction
        if owns:
            self._begin()
        try:
            row = self.conn.execute(
                "SELECT processed FROM cc_inbox WHERE update_id=?",
                (int(update_id),),
            ).fetchone()
            if row is not None:
                if owns:
                    self._commit()
                return "processed" if int(row["processed"] or 0) else "pending"
            self.conn.execute(
                "INSERT INTO cc_inbox(update_id, payload_json, received_at)"
                " VALUES(?,?,?)",
                (int(update_id), payload_json, now),
            )
            if owns:
                self._commit()
            return "new"
        except BaseException:
            if owns:
                self._rollback()
            raise

    def mark_inbox_processed(
        self, update_id: int, kind: str, now: float
    ) -> None:
        self.conn.execute(
            "UPDATE cc_inbox SET processed=1, processed_at=?, result_kind=?"
            " WHERE update_id=?",
            (now, kind or "", int(update_id)),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO cc_updates(update_id, received_at)"
            " VALUES(?,?)",
            (int(update_id), now),
        )

    def pending_inbox(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM cc_inbox WHERE processed=0"
            " ORDER BY update_id, rowid"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_poll_cursor(self, key: str) -> int:
        row = self.conn.execute(
            "SELECT offset FROM cc_poll_cursor WHERE key=?", (key,)
        ).fetchone()
        return int(row["offset"]) if row else 0

    def set_poll_cursor(self, key: str, offset: int, now: float) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO cc_poll_cursor(key, offset, updated_at)"
            " VALUES(?,?,?)",
            (key, int(offset), now),
        )

    def get_kv(self, key: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value FROM cc_runtime_kv WHERE key=?", (key,)
        ).fetchone()
        return str(row["value"]) if row else None

    def set_kv(self, key: str, value: str, now: float) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO cc_runtime_kv(key, value, updated_at)"
            " VALUES(?,?,?)",
            (key, value, now),
        )

    def get_supervision(self, target_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM cc_supervision WHERE target_id=?",
            (target_id,),
        ).fetchone()
        return dict(row) if row else None

    def put_supervision(
        self,
        target_id: str,
        now: float,
        grant_ref: str = "",
        grant_expires: float = 0.0,
        no_progress_attempts: int = 0,
        last_turn_id: str = "",
        last_reason: str = "",
    ) -> dict:
        self.conn.execute(
            "INSERT OR REPLACE INTO cc_supervision(target_id, grant_ref,"
            " grant_expires, no_progress_attempts, last_turn_id,"
            " last_reason, updated_at) VALUES(?,?,?,?,?,?,?)",
            (
                target_id,
                grant_ref,
                float(grant_expires),
                int(no_progress_attempts),
                last_turn_id,
                last_reason,
                now,
            ),
        )
        row = self.get_supervision(target_id)
        assert row is not None
        return row

    def mark_user_paused(
        self, dispatch_id: str, now: float, detail: str = ""
    ) -> dict:
        self._begin()
        try:
            row = self.require_dispatch(dispatch_id)
            if row["state"] not in (models.QUEUED, models.DISPATCHING):
                self._rollback()
                raise ReconcileError(
                    "cannot pause from %r" % (row["state"],)
                )
            self.conn.execute(
                "UPDATE cc_dispatches SET state=?, result_detail=?,"
                " claim_owner='', claim_token='', claim_started_at=0,"
                " updated_at=? WHERE dispatch_id=?",
                (models.USER_PAUSED, detail, now, dispatch_id),
            )
            self._commit()
            return self.require_dispatch(dispatch_id)
        except BaseException:
            if self.conn.in_transaction:
                self._rollback()
            raise

    def note_message(
        self, chat_id: int, message_id: int, dispatch_id: str, now: float
    ) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO cc_messages(chat_id, message_id,"
            " dispatch_id, created_at) VALUES(?,?,?,?)",
            (int(chat_id), int(message_id), dispatch_id, now),
        )

    def find_dispatch_by_message(
        self, chat_id: int, message_id: int
    ) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM cc_messages WHERE chat_id=? AND message_id=?",
            (int(chat_id), int(message_id)),
        ).fetchone()
        if row is None:
            return None
        return self.get_dispatch(dict(row)["dispatch_id"])

    def add_receipt(
        self, chat_id: int, message_id: int, reason: str, now: float
    ) -> str:
        receipt_id = uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO cc_receipts(receipt_id, chat_id, message_id,"
            " reason, created_at) VALUES(?,?,?,?,?)",
            (
                receipt_id,
                int(chat_id),
                int(message_id or 0),
                reason,
                now,
            ),
        )
        return receipt_id

    # -- target pre-selection (callback made BEFORE the prompt) ---------
    def set_selection(
        self,
        chat_id: int,
        task_id: str,
        generation: int,
        provider: str,
        native_thread: str,
        action_id: str,
        now: float,
    ) -> dict:
        binding = target_binding_id(provider, native_thread)
        self.conn.execute(
            "INSERT OR REPLACE INTO cc_selection(chat_id, task_id,"
            " generation, target_binding, provider, native_thread,"
            " action_id, created_at, updated_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?,"
            " COALESCE((SELECT created_at FROM cc_selection WHERE chat_id=?), ?), ?)",
            (
                int(chat_id),
                task_id,
                int(generation),
                binding,
                provider,
                native_thread,
                action_id,
                int(chat_id),
                now,
                now,
            ),
        )
        row = self.get_selection(int(chat_id))
        assert row is not None
        return row

    def get_selection(self, chat_id: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM cc_selection WHERE chat_id=?", (int(chat_id),)
        ).fetchone()
        return dict(row) if row else None

    def clear_selection(self, chat_id: int) -> None:
        self.conn.execute(
            "DELETE FROM cc_selection WHERE chat_id=?", (int(chat_id),)
        )

    # -- status message bookkeeping --------------------------------------
    def note_status(
        self,
        dispatch_id: str,
        chat_id: int,
        message_id: int,
        text_hash: str,
        now: float,
    ) -> None:
        existing = self.get_status(dispatch_id)
        if existing is None:
            self.conn.execute(
                "INSERT INTO cc_status(dispatch_id, chat_id, message_id,"
                " last_hash, uncertain, updated_at, outbound_state,"
                " intent_token, intent_hash)"
                " VALUES(?,?,?,?,0,?,'', '', ?)",
                (
                    dispatch_id,
                    int(chat_id),
                    int(message_id),
                    text_hash,
                    now,
                    text_hash,
                ),
            )
            return
        self.conn.execute(
            "UPDATE cc_status SET chat_id=?, message_id=?, last_hash=?,"
            " uncertain=0, updated_at=?, outbound_state='', intent_token='',"
            " intent_hash=? WHERE dispatch_id=?",
            (
                int(chat_id),
                int(message_id),
                text_hash,
                now,
                text_hash,
                dispatch_id,
            ),
        )

    def get_status(self, dispatch_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM cc_status WHERE dispatch_id=?", (dispatch_id,)
        ).fetchone()
        return dict(row) if row else None

    def set_status_uncertain(self, dispatch_id: str, now: float) -> None:
        self.conn.execute(
            "UPDATE cc_status SET uncertain=1, outbound_state='',"
            " intent_token='', updated_at=? WHERE dispatch_id=?",
            (now, dispatch_id),
        )

    def begin_status_intent(
        self,
        dispatch_id: str,
        chat_id: int,
        text_hash: str,
        now: float,
        token: Optional[str] = None,
    ) -> dict:
        """Persist outbound claim BEFORE the Telegram send.

        Survives a crash before ACK: recover_status_intents marks uncertain
        so the same notification is never duplicated.
        """
        token = token or uuid.uuid4().hex
        owns = not self.conn.in_transaction
        if owns:
            self._begin()
        try:
            row = self.get_status(dispatch_id)
            if row is None:
                self.conn.execute(
                    "INSERT INTO cc_status(dispatch_id, chat_id, message_id,"
                    " last_hash, uncertain, updated_at, outbound_state,"
                    " intent_token, intent_hash)"
                    " VALUES(?,?,0,'',0,?,?,?,?)",
                    (
                        dispatch_id,
                        int(chat_id),
                        now,
                        "intent",
                        token,
                        text_hash,
                    ),
                )
            else:
                if str(row.get("outbound_state") or "") == "intent":
                    if owns:
                        self._commit()
                    return dict(row)
                self.conn.execute(
                    "UPDATE cc_status SET chat_id=?, outbound_state=?,"
                    " intent_token=?, intent_hash=?, updated_at=?"
                    " WHERE dispatch_id=?",
                    (
                        int(chat_id) or int(row.get("chat_id") or 0),
                        "intent",
                        token,
                        text_hash,
                        now,
                        dispatch_id,
                    ),
                )
            if owns:
                self._commit()
            out = self.get_status(dispatch_id)
            assert out is not None
            return out
        except BaseException:
            if owns:
                self._rollback()
            raise

    def recover_status_intents(self, now: float) -> list[dict]:
        """Crash recovery: un-ACKed outbound intents become uncertain.

        Never resent automatically.
        """
        owns = not self.conn.in_transaction
        if owns:
            self._begin()
        try:
            rows = self.conn.execute(
                "SELECT * FROM cc_status WHERE outbound_state=?",
                ("intent",),
            ).fetchall()
            out = [dict(r) for r in rows]
            self.conn.execute(
                "UPDATE cc_status SET uncertain=1, outbound_state='',"
                " intent_token='', updated_at=? WHERE outbound_state=?",
                (now, "intent"),
            )
            if owns:
                self._commit()
            for row in out:
                row["uncertain"] = 1
                row["outbound_state"] = ""
            return out
        except BaseException:
            if owns:
                self._rollback()
            raise

    # -- takeover ---------------------------------------------------------
    def record_takeover(
        self, task_id: str, host_session: str, now: float, note: str = ""
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO cc_takeover(task_id, host_session,"
            " taken_at, note) VALUES(?,?,?,?)",
            (task_id, host_session, now, note),
        )

    def get_takeover(self, task_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM cc_takeover WHERE task_id=?", (task_id,)
        ).fetchone()
        return dict(row) if row else None
