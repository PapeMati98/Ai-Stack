"""Notification outbox: durable claims, SENT / UNKNOWN, no blind resend.

Payloads carry references and hashes only -- never raw helpdesk bodies or
tokens (see service diagnostics). A transport reports 'sent', 'unknown',
or raises. After an uncertain ACK the entry stays UNKNOWN until the owner
explicitly reconciles it; a worker persists a claim before transport and the
outbox never resends on its own.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Callable, Optional

from openclaw_ops import models

Transport = Callable[[str, str], str]  # (ref, summary_hash) -> sent|unknown


class Outbox:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS outbox(
              key TEXT PRIMARY KEY, channel TEXT NOT NULL,
              ref TEXT NOT NULL, summary_hash TEXT NOT NULL,
              state TEXT NOT NULL DEFAULT 'pending',
              attempts INTEGER NOT NULL DEFAULT 0,
              updated_at REAL NOT NULL DEFAULT 0,
              claim_owner TEXT NOT NULL DEFAULT '',
              claim_token TEXT NOT NULL DEFAULT '',
              claim_started_at REAL NOT NULL DEFAULT 0);
            """
        )
        # Candidate databases may have been created by the first version of
        # the schema.  Add the claim columns without relying on a destructive
        # migration or on sqlite3.Row being configured by the caller.
        columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(outbox)")
        }
        for name, definition in (
            ("claim_owner", "TEXT NOT NULL DEFAULT ''"),
            ("claim_token", "TEXT NOT NULL DEFAULT ''"),
            ("claim_started_at", "REAL NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                self.conn.execute(
                    f"ALTER TABLE outbox ADD COLUMN {name} {definition}"
                )

    def enqueue(self, key: str, channel: str, ref: str,
                summary_hash: str, now: float = 0.0) -> bool:
        owns_transaction = not self.conn.in_transaction
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO outbox(key, channel, ref, summary_hash,"
            " updated_at) VALUES(?,?,?,?,?)",
            (key, channel, ref, summary_hash, now),
        )
        # HelpdeskStore wraps this call in its own transaction; preserve it.
        # A standalone sqlite connection may use Python's implicit
        # transaction mode, in which case make this enqueue durable so the
        # next explicit claim transaction can start cleanly.
        if owns_transaction:
            self.conn.commit()
        return cur.rowcount == 1

    @staticmethod
    def _as_dict(row, description) -> dict:
        """Convert both sqlite3.Row and ordinary tuple rows to a dict."""
        if isinstance(row, sqlite3.Row):
            return dict(row)
        return {column[0]: value for column, value in zip(description, row)}

    def _recover_orphaned_claims(self, now: float) -> int:
        """Fence claims past the bounded handoff window.

        A concurrent worker may observe a live claim, so only an old claim
        is converted to UNKNOWN.  The authority-host ``now`` supplied by the
        caller is the sole expiry clock; constructing an Outbox never guesses
        the current time.
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cutoff = now - models.NOTIFY_CLAIM_TTL
            cur = self.conn.execute(
                "UPDATE outbox SET state=?, claim_owner='',"
                "claim_token='', claim_started_at=0, updated_at=?"
                " WHERE state=? AND claim_started_at<=?",
                (models.NOTIFY_UNKNOWN, now, models.NOTIFY_CLAIMED, cutoff),
            )
            self.conn.execute("COMMIT")
            return cur.rowcount
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def pending(self) -> list[dict]:
        cursor = self.conn.execute(
            "SELECT key, channel, ref, summary_hash, state, attempts"
            " FROM outbox WHERE state=? ORDER BY key",
            (models.NOTIFY_PENDING,),
        )
        return [self._as_dict(row, cursor.description)
                for row in cursor.fetchall()]

    def get(self, key: str) -> Optional[dict]:
        cursor = self.conn.execute(
            "SELECT * FROM outbox WHERE key=?", (key,))
        row = cursor.fetchone()
        return self._as_dict(row, cursor.description) if row else None

    def _claim_one(self, worker_id: str, now: float,
                   max_attempts: int) -> Optional[dict]:
        """Atomically claim one pending entry before invoking transport."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.conn.execute(
                "SELECT * FROM outbox WHERE state=? AND attempts<?"
                " ORDER BY key LIMIT 1",
                (models.NOTIFY_PENDING, max_attempts),
            )
            row = cursor.fetchone()
            if row is None:
                self.conn.execute("COMMIT")
                return None
            entry = self._as_dict(row, cursor.description)
            token = uuid.uuid4().hex
            cur = self.conn.execute(
                "UPDATE outbox SET state=?, attempts=attempts+1,"
                " claim_owner=?, claim_token=?, claim_started_at=?,"
                " updated_at=? WHERE key=? AND state=?",
                (models.NOTIFY_CLAIMED, worker_id, token, now, now,
                 entry["key"], models.NOTIFY_PENDING),
            )
            if cur.rowcount != 1:
                self.conn.execute("ROLLBACK")
                return None
            self.conn.execute("COMMIT")
            entry["state"] = models.NOTIFY_CLAIMED
            entry["attempts"] = int(entry["attempts"]) + 1
            entry["claim_owner"] = worker_id
            entry["claim_token"] = token
            entry["claim_started_at"] = now
            return entry
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def _finish_claim(self, entry: dict, state: str, now: float) -> bool:
        """Finish only this worker's claim; a recovered claim stays unknown."""
        owns_transaction = not self.conn.in_transaction
        cur = self.conn.execute(
            "UPDATE outbox SET state=?, claim_owner='', claim_token='',"
            "claim_started_at=0, updated_at=? WHERE key=? AND state=?"
            " AND claim_token=?",
            (state, now, entry["key"], models.NOTIFY_CLAIMED,
             entry["claim_token"]),
        )
        if owns_transaction:
            self.conn.commit()
        return cur.rowcount == 1

    def send_all(self, transport: Transport, now: float,
                 max_attempts: int = 3, *,
                 worker_id: Optional[str] = None,
                 after_claim: Optional[Callable[[dict], None]] = None
                 ) -> list[dict]:
        """Send pending entries with a durable at-most-once claim.

        ``after_claim`` exists for deterministic failure-injection tests.  If
        it raises, the claim is intentionally left durable; the next Outbox
        instance converts it to UNKNOWN and does not resend it.
        """
        self._recover_orphaned_claims(now)
        worker_id = worker_id or uuid.uuid4().hex
        results = []
        while True:
            entry = self._claim_one(worker_id, now, max_attempts)
            if entry is None:
                break
            if after_claim is not None:
                after_claim(dict(entry))
            try:
                outcome = transport(entry["ref"], entry["summary_hash"])
            except Exception as exc:
                outcome = f"error:{type(exc).__name__}"
            if outcome == "sent":
                updated = self._finish_claim(entry, models.NOTIFY_SENT, now)
                results.append({"key": entry["key"],
                                "state": ("sent" if updated else
                                          models.NOTIFY_UNKNOWN)})
            else:
                # Unknown or failed ACK: stay visible, never auto-resend.
                self._finish_claim(entry, models.NOTIFY_UNKNOWN, now)
                results.append({"key": entry["key"], "state": "unknown",
                                "outcome": str(outcome)})
        return results

    def reconcile(self, key: str, delivered: bool, now: float) -> dict:
        """Explicit owner reconciliation of an UNKNOWN entry.

        delivered=True marks SENT; False requeues to PENDING exactly once
        by owner decision -- never an automatic duplicate send.
        """
        row = self.get(key)
        if row is None:
            raise KeyError(f"unknown outbox key {key!r}")
        if row["state"] != models.NOTIFY_UNKNOWN:
            raise ValueError(f"outbox key {key!r} is {row['state']},"
                             " only UNKNOWN can be reconciled")
        state = models.NOTIFY_SENT if delivered else models.NOTIFY_PENDING
        owns_transaction = not self.conn.in_transaction
        self.conn.execute(
            "UPDATE outbox SET state=?, claim_owner='', claim_token='',"
            "claim_started_at=0, updated_at=? WHERE key=? AND state=?",
            (state, now, key, models.NOTIFY_UNKNOWN),
        )
        if owns_transaction:
            self.conn.commit()
        return {"key": key, "state": state}
