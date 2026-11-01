"""Durable SQLite store.

Tables:
- ``events``        — every raw record plus filter outcome.
- ``cursors``       — per (tool, native_session_id, cursor) highest seen.
- ``outbox``        — Hub publish queue (pending|published|deduplicated|offline|rejected).
- ``proposals``     — synthesized drafts awaiting curator activation.
- ``incidents``     — durable outage markers (local outage, dirty tree, ...).
- ``checkpoints``   — named run checkpoints (crash recovery).
- ``runs``          — one row per run with start/end/dry-run flag.

The store is intentionally minimal: it never holds secrets or raw reasoning
text. It is durable across crashes because every state change is committed
before the in-memory pipeline returns.
"""

from __future__ import annotations

import json
import calendar
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    dry_run       INTEGER NOT NULL,
    config_hash   TEXT NOT NULL,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS cursors (
    tool              TEXT NOT NULL,
    native_session_id TEXT NOT NULL,
    cursor            TEXT NOT NULL,
    observed_at       TEXT NOT NULL,
    content_hash      TEXT,
    PRIMARY KEY (tool, native_session_id, cursor)
);

CREATE TABLE IF NOT EXISTS events (
    event_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT NOT NULL,
    identity_tool     TEXT NOT NULL,
    source            TEXT NOT NULL,
    native_session_id TEXT NOT NULL,
    cursor            TEXT NOT NULL,
    observed_at       TEXT NOT NULL,
    title             TEXT NOT NULL,
    excerpt           TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    filter_reason     TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    speaker           TEXT NOT NULL DEFAULT '',
    event_type        TEXT NOT NULL DEFAULT '',
    UNIQUE (identity_tool, native_session_id, cursor, content_hash)
);

CREATE TABLE IF NOT EXISTS outbox (
    dedup_id     TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    status       TEXT NOT NULL,
    receipt_id   TEXT,
    reason       TEXT,
    recorded_at  TEXT NOT NULL,
    request_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proposals (
    proposal_id    TEXT PRIMARY KEY,
    content_hash   TEXT NOT NULL,
    source_refs    TEXT NOT NULL,
    synthesis      TEXT NOT NULL,
    model_identity TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    decision       TEXT NOT NULL,
    merge_target   TEXT,
    rationale      TEXT
);

-- ``proposals.content_hash`` is the deterministic hash of the whole
-- synthesis group.  Keep the per-record hashes in a separate relation so a
-- proposal can be deduplicated after a restart without pretending that the
-- group hash is any individual source hash.  This table is additive and is
-- safe for stores created by earlier candidate versions.
CREATE TABLE IF NOT EXISTS proposal_source_hashes (
    proposal_id  TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    PRIMARY KEY (proposal_id, content_hash)
);

CREATE TABLE IF NOT EXISTS incidents (
    incident_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    kind         TEXT NOT NULL,
    message      TEXT NOT NULL,
    payload      TEXT,
    raised_at    TEXT NOT NULL,
    cooldown_until TEXT
);

CREATE TABLE IF NOT EXISTS checkpoints (
    name       TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL,
    state      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS published_hashes (
    content_hash TEXT PRIMARY KEY,
    proposal_id  TEXT NOT NULL,
    published_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_hash ON events(content_hash);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status);
CREATE INDEX IF NOT EXISTS idx_incidents_kind ON incidents(kind);
CREATE INDEX IF NOT EXISTS idx_proposal_source_hashes
    ON proposal_source_hashes(content_hash);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass(frozen=True)
class EventRow:
    event_id: int
    identity_tool: str
    native_session_id: str
    cursor: str
    content_hash: str
    filter_reason: str


@dataclass(frozen=True)
class AdmittedEventRow:
    """Sanitized event provenance that can be replayed after a crash."""

    event_id: int
    identity_tool: str
    source: str
    native_session_id: str
    cursor: str
    observed_at: str
    title: str
    excerpt: str
    content_hash: str
    speaker: str = ""
    event_type: str = ""


@dataclass(frozen=True)
class OutboxRow:
    dedup_id: str
    payload_hash: str
    status: str
    receipt_id: Optional[str]
    reason: Optional[str]
    recorded_at: str


@dataclass(frozen=True)
class IncidentRow:
    incident_id: int
    kind: str
    message: str
    raised_at: str


class Store:
    """Thin SQLite wrapper.

    All writes are committed eagerly; the pipeline commits after each
    logical step so a crash never loses unpublished work.
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.executescript(SCHEMA)
        for _column_ddl in (
            "ALTER TABLE events ADD COLUMN speaker TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE events ADD COLUMN event_type TEXT NOT NULL DEFAULT ''",
        ):
            try:
                self._conn.execute(_column_ddl)
            except sqlite3.OperationalError:
                pass

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # ---------- run bookkeeping ----------

    def begin_run(self, run_id: str, config_hash: str,
                  dry_run: bool, notes: str = "") -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, started_at, dry_run, "
            "config_hash, notes) VALUES (?, ?, ?, ?, ?)",
            (run_id, _now(), 1 if dry_run else 0, config_hash, notes),
        )

    def finish_run(self, run_id: str) -> None:
        self._conn.execute(
            "UPDATE runs SET finished_at = ? WHERE run_id = ?",
            (_now(), run_id),
        )

    # ---------- cursors ----------

    def upsert_cursor(self, *, tool: str, native_session_id: str,
                      cursor: str, observed_at: str,
                      content_hash: Optional[str]) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO cursors (tool, native_session_id, "
            "cursor, observed_at, content_hash) VALUES (?, ?, ?, ?, ?)",
            (tool, native_session_id, cursor, observed_at, content_hash),
        )

    def cursor_seen(self, *, tool: str, native_session_id: str,
                    cursor: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM cursors AS c JOIN events AS e ON "
            "e.identity_tool = c.tool AND "
            "e.native_session_id = c.native_session_id AND "
            "e.cursor = c.cursor WHERE c.tool = ? AND "
            "c.native_session_id = ? AND c.cursor = ? LIMIT 1",
            (tool, native_session_id, cursor),
        ).fetchone()
        return row is not None

    # ---------- events ----------

    def insert_event(self, *, run_id: str, identity_tool: str, source: str,
                     native_session_id: str, cursor: str, observed_at: str,
                     title: str, excerpt: str, content_hash: str,
                     filter_reason: str, speaker: str = "",
                     event_type: str = "") -> int:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO events (run_id, identity_tool, source, "
            "native_session_id, cursor, observed_at, title, excerpt, "
            "content_hash, filter_reason, created_at, speaker, event_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, identity_tool, source, native_session_id, cursor,
             observed_at, title, excerpt, content_hash, filter_reason,
             _now(), speaker, event_type),
        )
        if cur.lastrowid == 0:
            # UNIQUE conflict means the same triple already lives in the
            # store; recover the existing event_id for downstream stages.
            row = self._conn.execute(
                "SELECT event_id FROM events WHERE identity_tool = ? "
                "AND native_session_id = ? AND cursor = ? "
                "AND content_hash = ?",
                (identity_tool, native_session_id, cursor, content_hash),
            ).fetchone()
            return int(row["event_id"]) if row else 0
        return int(cur.lastrowid)

    def event_seen(self, content_hash: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM events WHERE content_hash = ? LIMIT 1",
            (content_hash,),
        ).fetchone()
        return row is not None

    def admitted_events(self) -> List[AdmittedEventRow]:
        """Return sanitized accepted events in durable insertion order.

        The event row is committed together with its cursor before model or
        Hub work begins.  Replaying this projection closes the crash window
        after that commit and before the synthesis-pending outbox is queued;
        it contains only the already-redacted title/excerpt and provenance.
        """
        rows = self._conn.execute(
            "SELECT event_id, identity_tool, source, native_session_id, "
            "cursor, observed_at, title, excerpt, content_hash, speaker, event_type "
            "FROM events WHERE filter_reason = 'accepted' "
            "ORDER BY event_id",
        ).fetchall()
        return [AdmittedEventRow(
            event_id=int(row["event_id"]),
            identity_tool=str(row["identity_tool"]),
            source=str(row["source"]),
            native_session_id=str(row["native_session_id"]),
            cursor=str(row["cursor"]),
            observed_at=str(row["observed_at"]),
            title=str(row["title"]),
            excerpt=str(row["excerpt"]),
            content_hash=str(row["content_hash"]),
            speaker=str(row["speaker"] or ""),
            event_type=str(row["event_type"] or ""),
        ) for row in rows]

    # ---------- outbox ----------

    def outbox_get(self, dedup_id: str) -> Optional[OutboxRow]:
        row = self._conn.execute(
            "SELECT dedup_id, payload_hash, status, receipt_id, reason, "
            "recorded_at FROM outbox WHERE dedup_id = ?",
            (dedup_id,),
        ).fetchone()
        if row is None:
            return None
        return OutboxRow(row["dedup_id"], row["payload_hash"], row["status"],
                         row["receipt_id"], row["reason"], row["recorded_at"])

    def outbox_put(self, *, dedup_id: str, payload_hash: str, status: str,
                   recorded_at: str, request: Dict[str, Any],
                   receipt_id: Optional[str] = None,
                   reason: Optional[str] = None) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO outbox (dedup_id, payload_hash, "
            "status, receipt_id, reason, recorded_at, request_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (dedup_id, payload_hash, status, receipt_id, reason,
             recorded_at, json.dumps(request, sort_keys=True)),
        )

    def outbox_pending(self) -> List[OutboxRow]:
        rows = self._conn.execute(
            "SELECT dedup_id, payload_hash, status, receipt_id, reason, "
            "recorded_at FROM outbox WHERE status IN ('pending', 'offline') "
            "ORDER BY recorded_at",
        ).fetchall()
        return [OutboxRow(r["dedup_id"], r["payload_hash"], r["status"],
                          r["receipt_id"], r["reason"], r["recorded_at"])
                for r in rows]

    def outbox_recorded_hash(self, payload_hash: str) -> bool:
        """Return True if any outbox row already recorded this hash."""
        row = self._conn.execute(
            "SELECT 1 FROM outbox WHERE payload_hash = ? "
            "AND status IN ('recorded', 'deduplicated') LIMIT 1",
            (payload_hash,),
        ).fetchone()
        return row is not None

    def outbox_request(self, dedup_id: str) -> Dict[str, Any]:
        """Return the redacted durable request/receipt metadata for a row."""
        row = self._conn.execute(
            "SELECT request_json FROM outbox WHERE dedup_id = ?",
            (dedup_id,),
        ).fetchone()
        if row is None:
            return {}
        try:
            value = json.loads(row["request_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def outbox_pending_requests(self, *, kind: Optional[str] = None
                                ) -> List[Tuple[OutboxRow, Dict[str, Any]]]:
        """Return pending/offline rows with their redacted request metadata.

        Requests are stored separately from the row projection so callers
        cannot accidentally retry arbitrary outbox entries as Hub writes.
        Malformed metadata is returned as an empty dict and is therefore
        ignored by the pipeline retry validator.
        """
        result: List[Tuple[OutboxRow, Dict[str, Any]]] = []
        for row in self.outbox_pending():
            request = self.outbox_request(row.dedup_id)
            if kind is not None and request.get("kind") != kind:
                continue
            result.append((row, request))
        return result

    def outbox_synthesis_pending(self) -> List[Tuple[OutboxRow, Dict[str, Any]]]:
        """Return every ``synthesis_pending`` row with its sanitized payload.

        Used by the pipeline to rebuild ``SynthesisRecord`` instances on
        restart without re-reading the original input files.  Rows whose
        request metadata is missing or malformed are skipped because the
        rebuild contract requires the original content hash and source ref.
        """
        result: List[Tuple[OutboxRow, Dict[str, Any]]] = []
        for row, request in self.outbox_pending_requests(kind="synthesis_pending"):
            content_hash = request.get("content_hash")
            source_ref = request.get("source_ref")
            title = request.get("title")
            excerpt = request.get("excerpt")
            project_slug = request.get("project_slug")
            if not (isinstance(content_hash, str) and content_hash
                    and isinstance(source_ref, str) and source_ref
                    and isinstance(title, str)
                    and isinstance(excerpt, str)
                    and isinstance(project_slug, str) and project_slug):
                continue
            result.append((row, request))
        return result

    def outbox_drain(self, dedup_ids: Iterable[str], *,
                     status: str = "drained") -> int:
        """Mark ``dedup_ids`` rows as drained after a successful handoff.

        Rows whose current status is ``recorded`` / ``deduplicated`` are
        left untouched: those rows are owned by the canonical gateway and
        must never be re-marked.  Any other pending/offline row is updated
        to ``status`` so the queue no longer returns it from
        ``outbox_pending``.
        """
        if status not in {"drained", "completed"}:
            raise ValueError("drain status must be drained or completed")
        n = 0
        for dedup_id in dedup_ids:
            if not isinstance(dedup_id, str) or not dedup_id.strip():
                continue
            row = self.outbox_get(dedup_id)
            if row is None or row.status in {"recorded", "deduplicated"}:
                continue
            self._conn.execute(
                "UPDATE outbox SET status = ?, reason = ? WHERE dedup_id = ?",
                (status, f"synthesis_drained:{status}", dedup_id),
            )
            n += 1
        return n

    def reconcile_outbox(self, *, content_hashes: Iterable[str],
                         proposal_id: Optional[str], status: str,
                         receipt_id: str, receipt: Dict[str, Any],
                         reason: Optional[str] = None) -> List[str]:
        """Acknowledge durable rows covered by a verified Hub receipt.

        Offline admission rows use one id per source record while a Hub
        handoff uses one id per proposal.  Matching by explicit source hashes
        (or the stable proposal key) joins those ledgers after a successful
        retry.  No title/page heuristic or group hash is used.
        """
        if status not in {"recorded", "deduplicated"}:
            raise ValueError("outbox reconciliation requires a recorded status")
        if not isinstance(receipt_id, str) or not receipt_id.strip():
            raise ValueError("outbox reconciliation requires a receipt id")
        if not isinstance(receipt, dict):
            raise ValueError("outbox reconciliation requires receipt metadata")
        hashes = {str(value) for value in content_hashes
                  if isinstance(value, str) and value.strip()}
        acknowledged: List[str] = []
        for row in self.outbox_pending():
            request = self.outbox_request(row.dedup_id)
            request_hashes: set[str] = set()
            single_hash = request.get("content_hash")
            if isinstance(single_hash, str) and single_hash.strip():
                request_hashes.add(single_hash)
            many_hashes = request.get("content_hashes")
            if isinstance(many_hashes, list):
                request_hashes.update(
                    value for value in many_hashes
                    if isinstance(value, str) and value.strip()
                )
            same_proposal = bool(
                proposal_id and request.get("proposal_id") == proposal_id
            )
            if not same_proposal and not (hashes & request_hashes):
                continue
            updated = dict(request)
            updated["receipt"] = dict(receipt)
            self.outbox_put(
                dedup_id=row.dedup_id,
                payload_hash=row.payload_hash,
                status=status,
                receipt_id=receipt_id,
                reason=reason,
                recorded_at=row.recorded_at,
                request=updated,
            )
            acknowledged.append(row.dedup_id)
        return acknowledged

    # ---------- published hashes ----------

    def publish_hash(self, content_hash: str, proposal_id: str) -> None:
        """Record an individual content hash as published.

        Allows the dedup logic to recognize previously published records by
        their per-record hash even when the proposal itself was stored with
        a group hash.
        """
        self._conn.execute(
            "INSERT OR IGNORE INTO published_hashes (content_hash, "
            "proposal_id, published_at) VALUES (?, ?, ?)",
            (content_hash, proposal_id, _now()),
        )

    def is_published(self, content_hash: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM published_hashes WHERE content_hash = ? LIMIT 1",
            (content_hash,),
        ).fetchone()
        return row is not None

    # ---------- proposals ----------

    def proposal_upsert(self, *, proposal_id: str, content_hash: str,
                        source_refs: List[str], synthesis: str,
                        model_identity: str, decision: str,
                        merge_target: Optional[str],
                        rationale: Optional[str],
                        source_hashes: Optional[Iterable[str]] = None) -> None:
        normalized_hashes = None
        if source_hashes is not None:
            normalized_hashes = sorted({str(value) for value in source_hashes
                                        if isinstance(value, str)
                                        and value.strip()})
        with self.transaction():
            self._conn.execute(
                "INSERT OR REPLACE INTO proposals (proposal_id, content_hash, "
                "source_refs, synthesis, model_identity, created_at, decision, "
                "merge_target, rationale) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (proposal_id, content_hash, json.dumps(source_refs),
                 synthesis, model_identity, _now(), decision, merge_target,
                 rationale),
            )
            if normalized_hashes is not None:
                self._conn.execute(
                    "DELETE FROM proposal_source_hashes WHERE proposal_id = ?",
                    (proposal_id,),
                )
                self._conn.executemany(
                    "INSERT OR IGNORE INTO proposal_source_hashes "
                    "(proposal_id, content_hash) VALUES (?, ?)",
                    [(proposal_id, value) for value in normalized_hashes],
                )

    def proposal_for_hash(self, content_hash: str
                          ) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT p.proposal_id, p.content_hash, p.source_refs, "
            "p.synthesis, p.model_identity, p.created_at, p.decision, "
            "p.merge_target, p.rationale "
            "FROM proposals AS p LEFT JOIN proposal_source_hashes AS h "
            "ON h.proposal_id = p.proposal_id "
            "WHERE p.content_hash = ? OR h.content_hash = ? "
            "ORDER BY CASE WHEN p.decision = 'duplicate' THEN 0 ELSE 1 END, "
            "p.created_at DESC LIMIT 1",
            (content_hash, content_hash),
        ).fetchone()
        if row is None:
            return None
        return {
            "proposal_id": row["proposal_id"],
            "content_hash": row["content_hash"],
            "source_refs": json.loads(row["source_refs"]),
            "synthesis": row["synthesis"],
            "model_identity": row["model_identity"],
            "created_at": row["created_at"],
            "decision": row["decision"],
            "merge_target": row["merge_target"],
            "rationale": row["rationale"],
        }

    # ---------- incidents ----------

    def raise_incident(self, *, run_id: str, kind: str, message: str,
                       payload: Optional[Dict[str, Any]] = None,
                       cooldown_seconds: int = 0) -> IncidentRow:
        payload_json = json.dumps(payload or {}, sort_keys=True)
        cooldown_until = ""
        if cooldown_seconds > 0:
            cooldown_until = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(time.time() + cooldown_seconds),
            )
        cur = self._conn.execute(
            "INSERT INTO incidents (run_id, kind, message, payload, "
            "raised_at, cooldown_until) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, kind, message, payload_json, _now(), cooldown_until),
        )
        return IncidentRow(int(cur.lastrowid), kind, message, _now())

    def recent_incident(self, kind: str, *, within_seconds: int
                        ) -> Optional[IncidentRow]:
        cutoff = time.time() - within_seconds
        row = self._conn.execute(
            "SELECT incident_id, kind, message, raised_at FROM incidents "
            "WHERE kind = ? ORDER BY incident_id DESC LIMIT 1",
            (kind,),
        ).fetchone()
        if row is None:
            return None
        try:
            # Incident timestamps are persisted in UTC. ``mktime`` interprets
            # its input in the local timezone, which makes cooldown checks
            # wrong on non-UTC hosts (the caretaker commonly runs in Warsaw).
            t = calendar.timegm(time.strptime(row["raised_at"],
                                              "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            return IncidentRow(row["incident_id"], row["kind"],
                               row["message"], row["raised_at"])
        if t < cutoff:
            return None
        return IncidentRow(row["incident_id"], row["kind"], row["message"],
                           row["raised_at"])

    # ---------- checkpoints ----------

    def checkpoint_set(self, name: str, run_id: str,
                       state: Dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO checkpoints (name, run_id, state, "
            "updated_at) VALUES (?, ?, ?, ?)",
            (name, run_id, json.dumps(state, sort_keys=True), _now()),
        )

    def checkpoint_get(self, name: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT state FROM checkpoints WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["state"])

    # ---------- introspection ----------

    def run_summary(self) -> Dict[str, Any]:
        events = self._conn.execute(
            "SELECT COUNT(*) AS c FROM events").fetchone()["c"]
        admitted = self._conn.execute(
            "SELECT COUNT(*) AS c FROM events WHERE filter_reason = "
            "'accepted'").fetchone()["c"]
        outbox = self._conn.execute(
            "SELECT status, COUNT(*) AS c FROM outbox GROUP BY status"
        ).fetchall()
        incidents = self._conn.execute(
            "SELECT kind, COUNT(*) AS c FROM incidents GROUP BY kind"
        ).fetchall()
        proposals = self._conn.execute(
            "SELECT decision, COUNT(*) AS c FROM proposals GROUP BY decision"
        ).fetchall()
        runs = self._conn.execute(
            "SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        cursors = self._conn.execute(
            "SELECT COUNT(*) AS c FROM cursors").fetchone()["c"]
        return {
            "runs": runs,
            "events_total": events,
            "events_admitted": admitted,
            "cursors": cursors,
            "outbox": {row["status"]: row["c"] for row in outbox},
            "incidents": {row["kind"]: row["c"] for row in incidents},
            "proposals": {row["decision"]: row["c"] for row in proposals},
        }

    def has_open_offline_outbox(self) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM outbox WHERE status = 'offline' LIMIT 1"
        ).fetchone()
        return row is not None


def config_hash(raw: Dict[str, Any]) -> str:
    import hashlib
    return hashlib.sha256(
        json.dumps(raw, sort_keys=True).encode("utf-8")
    ).hexdigest()
