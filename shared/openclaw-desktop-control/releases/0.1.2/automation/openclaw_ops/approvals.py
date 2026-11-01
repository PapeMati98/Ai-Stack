"""Telegram decision contract for repair-request approval.

An approval authorises ONLY the exact bound request: authenticated trusted
transport (senderID/chatID allowlist) + pending request id + exact issue
revision, scope and scope hash + unexpired window + explicit action.
Replayed, old, forwarded, wrong-sender/group, or plain arbitrary text
messages never authorise. Message text is data, never shell.

An approval permits local isolated candidate work (code/tests/review)
ONLY. Deployment, IAM, deletion and customer-data mutation require a
separate explicit scope and stay forbidden by default.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from typing import Optional

from openclaw_ops import models

ACTION_APPROVE = "approve"
ACTION_REJECT = "reject"
MAX_MESSAGE_AGE = 300  # seconds; older messages are replay/forward suspects


@dataclass
class PendingRequest:
    request_id: str
    stable_id: str
    revision: str
    scope: str
    scope_hash: str
    action: str  # 'approve' authorises candidate-local work only
    expires_at: float
    created_at: float = 0.0
    project: str = ""
    candidate_hash: str = ""


@dataclass
class TelegramMessage:
    message_id: str
    sender_id: str
    chat_id: str
    text: str
    sent_at: float
    is_forwarded: bool = False


def canonical_text(req: PendingRequest) -> str:
    return (f"APPROVE {req.request_id} {req.revision}"
            f" {req.scope_hash} {req.action}")


class ApprovalStore:
    """Pending requests + seen-message replay cache (in-memory).

    Production wiring MUST persist this (parent owns durable capture);
    serialise() / restore() carry the replay cache across restarts so a
    replay after reboot is still rejected.
    """

    def __init__(self) -> None:
        self.pending: dict[str, PendingRequest] = {}
        self.seen_messages: set[str] = set()
        self.decided: dict[str, dict] = {}  # request_id -> verdict

    def add(self, req: PendingRequest) -> None:
        self.pending[req.request_id] = req

    def serialise(self) -> dict:
        return {
            "pending": [vars(r) for r in self.pending.values()],
            "seen_messages": sorted(self.seen_messages),
            "decided": self.decided,
        }

    @classmethod
    def restore(cls, data: dict) -> "ApprovalStore":
        store = cls()
        for item in data.get("pending", []):
            store.pending[item["request_id"]] = PendingRequest(**item)
        store.seen_messages = set(data.get("seen_messages", []))
        store.decided = dict(data.get("decided", {}))
        return store


def decide(store: ApprovalStore, allowlist: set[tuple[str, str]],
           msg: TelegramMessage, now: float) -> dict:
    """Verify one Telegram message. Returns a verdict dict (JSON-safe)."""

    def reject(reason: str) -> dict:
        return {"authorized": False, "reason": reason,
                "message_id": msg.message_id}

    if (msg.sender_id, msg.chat_id) not in allowlist:
        return reject("untrusted-sender-or-chat")
    if msg.is_forwarded:
        return reject("forwarded-message")
    # Preserve the distinct request-expiry verdict for a syntactically valid
    # approval that arrives after its grant ended.  A stale message for an
    # otherwise live request still fails closed as stale-message below.
    if now - msg.sent_at > MAX_MESSAGE_AGE:
        parts = msg.text.strip().split()
        if len(parts) == 5 and parts[0] == "APPROVE":
            req = store.pending.get(parts[1])
            if req is not None and now > req.expires_at:
                store.seen_messages.add(msg.message_id)
                return reject("request-expired")
        return reject("stale-message")
    if msg.sent_at - now > models.CLOCK_SKEW_LIMIT:
        return reject("future-message")
    if msg.message_id in store.seen_messages:
        return reject("replayed-message")

    parts = msg.text.strip().split()
    if len(parts) != 5 or parts[0] != "APPROVE":
        store.seen_messages.add(msg.message_id)
        return reject("not-a-decision")
    _, request_id, revision, scope_hash, action = parts
    req = store.pending.get(request_id)
    if req is None:
        store.seen_messages.add(msg.message_id)
        return reject("unknown-request")
    if now > req.expires_at:
        store.seen_messages.add(msg.message_id)
        return reject("request-expired")
    if (revision, scope_hash, action) != (
            req.revision, req.scope_hash, req.action):
        store.seen_messages.add(msg.message_id)
        return reject("revision-scope-hash-action-mismatch")
    if req.scope not in models.CANDIDATE_SCOPES:
        # Exact binding holds, but the scope itself is not candidate-local:
        # approval alone never unlocks deploy/IAM/delete/customer-mutation.
        store.seen_messages.add(msg.message_id)
        verdict = {"authorized": False, "request_id": request_id,
                   "reason": "scope-requires-separate-explicit-authorization",
                   "message_id": msg.message_id}
        store.decided[request_id] = verdict
        return verdict
    if msg.text.strip() != canonical_text(req):
        store.seen_messages.add(msg.message_id)
        return reject("non-canonical-text")
    store.seen_messages.add(msg.message_id)
    verdict = {"authorized": True, "request_id": request_id,
               "revision": revision, "scope": req.scope,
               "scope_hash": scope_hash, "action": action,
               "candidate_only": True, "message_id": msg.message_id}
    store.decided[request_id] = verdict
    return verdict

class DurableApprovalStore:
    """SQLite persistence for approval decisions and the replay cache.

    Every decision is bound to the exact tuple: project, issue (stable_id),
    source revision, scope, scope hash, candidate hash and expiry.  Wraps an
    existing sqlite3 connection (like Outbox) and commits only when it owns
    the transaction, so it can share the helpdesk database file.  The parent
    Codex owns live wiring; this class performs no network or model calls.
    """

    def __init__(self, conn) -> None:
        self.conn = conn
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS approval_pending(
              request_id TEXT PRIMARY KEY, project TEXT NOT NULL DEFAULT '',
              stable_id TEXT NOT NULL DEFAULT '',
              revision TEXT NOT NULL DEFAULT '',
              scope TEXT NOT NULL DEFAULT '',
              scope_hash TEXT NOT NULL DEFAULT '',
              candidate_hash TEXT NOT NULL DEFAULT '',
              action TEXT NOT NULL DEFAULT 'approve',
              expires_at REAL NOT NULL DEFAULT 0,
              created_at REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS approval_seen(
              message_id TEXT PRIMARY KEY, seen_at REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS approval_decisions(
              request_id TEXT PRIMARY KEY, project TEXT NOT NULL DEFAULT '',
              stable_id TEXT NOT NULL DEFAULT '',
              revision TEXT NOT NULL DEFAULT '',
              scope TEXT NOT NULL DEFAULT '',
              scope_hash TEXT NOT NULL DEFAULT '',
              candidate_hash TEXT NOT NULL DEFAULT '',
              action TEXT NOT NULL DEFAULT '',
              authorized INTEGER NOT NULL DEFAULT 0,
              reason TEXT NOT NULL DEFAULT '',
              message_id TEXT NOT NULL DEFAULT '',
              decided_at REAL NOT NULL DEFAULT 0);
            """
        )

    def save_pending(self, req: PendingRequest) -> None:
        owns_transaction = not self.conn.in_transaction
        self.conn.execute(
            "INSERT OR REPLACE INTO approval_pending(request_id, project,"
            " stable_id, revision, scope, scope_hash, candidate_hash,"
            " action, expires_at, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (req.request_id, req.project, req.stable_id, req.revision,
             req.scope, req.scope_hash, req.candidate_hash, req.action,
             req.expires_at, req.created_at),
        )
        if owns_transaction:
            self.conn.commit()

    def load_pending(self, request_id: str) -> Optional[PendingRequest]:
        row = self.conn.execute(
            "SELECT * FROM approval_pending WHERE request_id=?",
            (request_id,)).fetchone()
        if row is None:
            return None
        cols = [d[0] for d in self.conn.execute("SELECT * FROM approval_pending WHERE 0").description]
        data = dict(zip(cols, row)) if not isinstance(row, dict) else dict(row)
        return PendingRequest(
            request_id=data["request_id"], stable_id=data["stable_id"],
            revision=data["revision"], scope=data["scope"],
            scope_hash=data["scope_hash"], action=data["action"],
            expires_at=data["expires_at"], created_at=data["created_at"],
            project=data.get("project", ""),
            candidate_hash=data.get("candidate_hash", ""))

    def has_seen(self, message_id: str) -> bool:
        return (self.conn.execute(
            "SELECT 1 FROM approval_seen WHERE message_id=?",
            (message_id,)).fetchone() is not None)

    def mark_seen(self, message_id: str, now: float) -> bool:
        """Record a message id; False means it was already seen (replay)."""
        owns_transaction = not self.conn.in_transaction
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO approval_seen(message_id, seen_at)"
            " VALUES(?,?)",
            (message_id, now),
        )
        if owns_transaction:
            self.conn.commit()
        return cur.rowcount == 1

    def record_decision(self, req: PendingRequest, verdict: dict,
                        now: float) -> None:
        owns_transaction = not self.conn.in_transaction
        self.conn.execute(
            "INSERT OR REPLACE INTO approval_decisions(request_id, project,"
            " stable_id, revision, scope, scope_hash, candidate_hash,"
            " action, authorized, reason, message_id, decided_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (req.request_id, req.project, req.stable_id, req.revision,
             req.scope, req.scope_hash, req.candidate_hash,
             verdict.get("action", req.action),
             int(bool(verdict.get("authorized"))),
             str(verdict.get("reason", "")),
             str(verdict.get("message_id", "")), now),
        )
        if owns_transaction:
            self.conn.commit()

    def get_decision(self, request_id: str) -> Optional[dict]:
        cursor = self.conn.execute(
            "SELECT * FROM approval_decisions WHERE request_id=?",
            (request_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        if isinstance(row, dict):
            return dict(row)
        cols = [d[0] for d in cursor.description]
        return {c: v for c, v in zip(cols, row)}
