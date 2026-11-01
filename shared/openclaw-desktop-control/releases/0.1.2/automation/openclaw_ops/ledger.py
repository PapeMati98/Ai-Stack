"""One-authority-host SQLite ledger.

All mutations that decide dispatch rights run inside ``BEGIN IMMEDIATE``
transactions with compare-and-swap on (task_id, generation). The events table
is append-only: this module exposes no delete or update for it.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from openclaw_ops import models

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
  task_id TEXT PRIMARY KEY,
  generation INTEGER NOT NULL DEFAULT 1,
  state TEXT NOT NULL DEFAULT 'running',
  provider TEXT NOT NULL,
  host_session TEXT NOT NULL DEFAULT '',
  owner_grant_ref TEXT NOT NULL DEFAULT '',
  grant_expires REAL NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  attempts INTEGER NOT NULL DEFAULT 0,
  checkpoint_hash TEXT NOT NULL DEFAULT '',
  checkpoint_first REAL NOT NULL DEFAULT 0,
  checkpoint_last REAL NOT NULL DEFAULT 0,
  lease_owner TEXT NOT NULL DEFAULT '',
  lease_token INTEGER NOT NULL DEFAULT 0,
  lease_expires REAL NOT NULL DEFAULT 0,
  last_cause TEXT NOT NULL DEFAULT '',
  cause_transient INTEGER NOT NULL DEFAULT 0,
  cause_resolved INTEGER NOT NULL DEFAULT 0,
  next_retry_at REAL NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS intents(
  key TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  generation INTEGER NOT NULL,
  attempt INTEGER NOT NULL,
  native_run_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'dispatched',
  created_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,
  task_id TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT ''
);
"""


class Ledger:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.isolation_level = None  # autocommit; we manage transactions
        self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # -- events (append-only) -------------------------------------------
    def append_event(self, kind: str, task_id: str, detail: str, now: float) -> None:
        self.conn.execute(
            "INSERT INTO events(ts, kind, task_id, detail) VALUES(?,?,?,?)",
            (now, kind, task_id, detail),
        )

    def list_events(self, task_id: Optional[str] = None) -> list[dict]:
        if task_id is None:
            rows = self.conn.execute("SELECT * FROM events ORDER BY id").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE task_id=? ORDER BY id", (task_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # -- tasks -----------------------------------------------------------
    def register_task(
        self,
        task_id: str,
        provider: str,
        host_session: str = "",
        owner_grant_ref: str = "",
        grant_expires: float = 0.0,
        max_attempts: int = models.MAX_ATTEMPTS,
        now: float = 0.0,
    ) -> dict:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "INSERT OR IGNORE INTO tasks(task_id, provider, host_session,"
                " owner_grant_ref, grant_expires, max_attempts, updated_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    task_id,
                    provider,
                    host_session,
                    owner_grant_ref,
                    grant_expires,
                    max_attempts,
                    now,
                ),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        row = self.get_task(task_id)
        assert row is not None
        return row

    def get_task(self, task_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_tasks(self) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute("SELECT * FROM tasks ORDER BY task_id")
        ]

    def transfer_authority(
        self,
        task_id: str,
        expected_generation: int,
        new_host_session: str,
        owner_grant_ref: str,
        grant_expires: float,
        now: float,
    ) -> bool:
        """Explicit owner-approved authority move: CAS generation, new gen.

        The former host loses dispatch rights (leases are cleared, old
        fencing tokens are stale). Returns False when the CAS misses.
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self.conn.execute(
                "UPDATE tasks SET generation=generation+1,"
                " host_session=?, owner_grant_ref=?, grant_expires=?,"
                " attempts=0, lease_owner='', lease_expires=0,"
                " lease_token=lease_token+1, state=?, updated_at=?"
                " WHERE task_id=? AND generation=?",
                (
                    new_host_session,
                    owner_grant_ref,
                    grant_expires,
                    models.TASK_RUNNING,
                    now,
                    task_id,
                    expected_generation,
                ),
            )
            self.conn.execute(
                "INSERT INTO events(ts, kind, task_id, detail) VALUES(?,?,?,?)",
                (now, "authority-transfer", task_id, new_host_session),
            )
            self.conn.execute("COMMIT")
            return cur.rowcount == 1
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def acquire_lease(
        self, task_id: str, generation: int, owner: str, now: float,
        ttl: float = models.LEASE_TTL,
    ) -> Optional[int]:
        """CAS lease acquire/renew-takeover. Returns new fencing token."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None or int(row["generation"]) != int(generation):
                self.conn.execute("ROLLBACK")
                return None
            held = (
                row["lease_owner"]
                and float(row["lease_expires"]) > now
                and row["lease_owner"] != owner
            )
            if held:
                self.conn.execute("ROLLBACK")
                return None
            token = int(row["lease_token"]) + 1
            self.conn.execute(
                "UPDATE tasks SET lease_owner=?, lease_token=?,"
                " lease_expires=?, updated_at=? WHERE task_id=?",
                (owner, token, now + ttl, now, task_id),
            )
            self.conn.execute(
                "INSERT INTO events(ts, kind, task_id, detail) VALUES(?,?,?,?)",
                (now, "lease-acquire", task_id, f"{owner}#{token}"),
            )
            self.conn.execute("COMMIT")
            return token
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def renew_lease(
        self, task_id: str, generation: int, owner: str, token: int,
        now: float, ttl: float = models.LEASE_TTL,
    ) -> Optional[int]:
        """Renew an owned live lease; fencing token increases monotonically."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if (
                row is None
                or int(row["generation"]) != int(generation)
                or row["lease_owner"] != owner
                or int(row["lease_token"]) != int(token)
                or float(row["lease_expires"]) <= now
            ):
                self.conn.execute("ROLLBACK")
                return None
            new_token = int(token) + 1
            self.conn.execute(
                "UPDATE tasks SET lease_token=?, lease_expires=?,"
                " updated_at=? WHERE task_id=?",
                (new_token, now + ttl, now, task_id),
            )
            self.conn.execute(
                "INSERT INTO events(ts, kind, task_id, detail) VALUES(?,?,?,?)",
                (now, "lease-renew", task_id, f"{owner}#{new_token}"),
            )
            self.conn.execute("COMMIT")
            return new_token
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def release_lease(self, task_id: str, owner: str, now: float) -> None:
        self.conn.execute(
            "UPDATE tasks SET lease_owner='', lease_expires=0, updated_at=?"
            " WHERE task_id=? AND lease_owner=?",
            (now, task_id, owner),
        )

    # -- dispatch intents (durable, idempotent key) -----------------------
    @staticmethod
    def intent_key(task_id: str, generation: int, attempt: int) -> str:
        return f"{task_id}:{generation}:{attempt}"

    def record_intent(
        self, key: str, task_id: str, generation: int, attempt: int, now: float
    ) -> tuple[dict, bool]:
        """Persist dispatch intent BEFORE sending. Returns (row, created)."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO intents(key, task_id, generation, attempt,"
            " created_at) VALUES(?,?,?,?,?)",
            (key, task_id, generation, attempt, now),
        )
        created = cur.rowcount == 1
        row = self.conn.execute(
            "SELECT * FROM intents WHERE key=?", (key,)
        ).fetchone()
        assert row is not None
        return dict(row), created

    def set_intent(
        self, key: str, native_run_id: str, status: str
    ) -> None:
        self.conn.execute(
            "UPDATE intents SET native_run_id=?, status=? WHERE key=?",
            (native_run_id, status, key),
        )

    def get_intent(self, key: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM intents WHERE key=?", (key,)
        ).fetchone()
        return dict(row) if row else None

    # -- task progress ----------------------------------------------------
    def observe_checkpoint(
        self, task_id: str, digest: str, now: float,
        quiet: float = models.CHECKPOINT_QUIET,
    ) -> tuple[float, float, bool]:
        """Record a checkpoint observation; stable when unchanged >= quiet."""
        row = self.get_task(task_id)
        assert row is not None
        if row["checkpoint_hash"] != digest:
            self.conn.execute(
                "UPDATE tasks SET checkpoint_hash=?, checkpoint_first=?,"
                " checkpoint_last=?, updated_at=? WHERE task_id=?",
                (digest, now, now, now, task_id),
            )
            return now, now, False
        first = float(row["checkpoint_first"] or now)
        self.conn.execute(
            "UPDATE tasks SET checkpoint_last=?, updated_at=? WHERE task_id=?",
            (now, now, task_id),
        )
        return first, now, (now - first) >= quiet

    def mark_state(self, task_id: str, state: str, now: float) -> None:
        self.conn.execute(
            "UPDATE tasks SET state=?, updated_at=? WHERE task_id=?",
            (state, now, task_id),
        )
        self.append_event("state", task_id, state, now)

    def note_cause(
        self, task_id: str, cause: str, transient: bool, resolved: bool,
        now: float,
    ) -> None:
        self.conn.execute(
            "UPDATE tasks SET last_cause=?, cause_transient=?,"
            " cause_resolved=?, updated_at=? WHERE task_id=?",
            (cause, int(transient), int(resolved), now, task_id),
        )

    def note_attempt(self, task_id: str, attempts: int, next_retry_at: float,
                     now: float) -> None:
        self.conn.execute(
            "UPDATE tasks SET attempts=?, next_retry_at=?, updated_at=?"
            " WHERE task_id=?",
            (attempts, next_retry_at, now, task_id),
        )

    def update(self, task_id: str, now: float, **fields: Any) -> None:
        allowed = {
            "state", "owner_grant_ref", "grant_expires", "max_attempts",
            "attempts", "next_retry_at", "last_cause", "cause_transient",
            "cause_resolved",
        }
        sets = [f"{k}=?" for k in fields if k in allowed]
        vals = [fields[k] for k in fields if k in allowed]
        if not sets:
            return
        self.conn.execute(
            f"UPDATE tasks SET {', '.join(sets)}, updated_at=? WHERE task_id=?",
            (*vals, now, task_id),
        )
