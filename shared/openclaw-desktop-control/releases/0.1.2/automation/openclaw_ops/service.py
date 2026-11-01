"""Service CLI for the TEST candidate. Bounded tick/status/pending.

Adapters are injected via register_adapter() (tests and the parent's real
wiring); providers without a registered capable adapter stay visibly
unconnected. All output is versioned JSON. Diagnostics carry references
and hashes only -- never raw helpdesk bodies or tokens.
"""

from __future__ import annotations

import argparse
import json
import sys

from openclaw_ops import models
from openclaw_ops.adapters import Adapter
from openclaw_ops.clock import SystemClock
from openclaw_ops.ledger import Ledger
from openclaw_ops.watchdog import tick

REGISTRY: dict[str, Adapter] = {}


def register_adapter(adapter: Adapter) -> None:
    REGISTRY[adapter.name] = adapter


def cmd_tick(db: str) -> dict:
    ledger = Ledger(db)
    try:
        decisions = tick(ledger, REGISTRY, SystemClock())
        return models.envelope("tick", {"decisions": decisions})
    finally:
        ledger.close()


def cmd_status(db: str) -> dict:
    ledger = Ledger(db)
    try:
        tasks = []
        for t in ledger.list_tasks():
            # Human-readable state only: lease owner/token/expiry, grant
            # presence (never the grant secret itself -- the ledger stores
            # only a reference), attempts and backoff. No bodies stored.
            tasks.append({
                "task_id": t["task_id"],
                "generation": t["generation"],
                "state": t["state"],
                "provider": t["provider"],
                "provider_connected": t["provider"] in REGISTRY,
                "grant_present": bool(t["owner_grant_ref"]),
                "grant_expired": False,  # evaluated per tick with host time
                "attempts": t["attempts"],
                "max_attempts": t["max_attempts"],
                "lease_held": bool(t["lease_owner"]),
                "next_retry_at": t["next_retry_at"],
                "last_cause": t["last_cause"],
            })
        intents = [
            {"key": r["key"], "task_id": r["task_id"],
             "attempt": r["attempt"], "status": r["status"],
             "native_run_id": r["native_run_id"]}
            for r in ledger.conn.execute(
                "SELECT key, task_id, attempt, status, native_run_id"
                " FROM intents ORDER BY key").fetchall()
        ]
        return models.envelope("status", {"tasks": tasks,
                                          "intents": intents})
    finally:
        ledger.close()


def cmd_pending(db: str) -> dict:
    # Pending = outbox entries + undecided repair requests across helpdesk
    # stores. The helpdesk store path is derived from the ledger path by
    # convention (<db>.helpdesk); absent file means nothing pending.
    import os

    from openclaw_ops.notify import Outbox
    import sqlite3

    pending: list[dict] = []
    helpdesk_path = db + ".helpdesk"
    if os.path.exists(helpdesk_path):
        conn = sqlite3.connect(helpdesk_path)
        conn.row_factory = sqlite3.Row
        try:
            outbox = Outbox(conn)
            pending = outbox.pending()
            reqs = conn.execute(
                "SELECT request_id, project, stable_id, revision, scope,"
                " status FROM repair_requests WHERE status='proposed'"
                " ORDER BY request_id").fetchall()
            proposed = [dict(r) for r in reqs]
        finally:
            conn.close()
    else:
        proposed = []
    return models.envelope("pending", {"outbox": pending,
                                       "proposed_requests": proposed})


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="openclaw-ops",
                                description="OpenClaw heartbeat/helpdesk TEST")
    sub = p.add_subparsers(dest="command", required=True)
    t = sub.add_parser("tick", help="bounded watchdog pass")
    t.add_argument("--db", required=True)
    s = sub.add_parser("status", help="ledger state (redacted)")
    s.add_argument("--db", required=True)
    pe = sub.add_parser("pending", help="outbox + proposed requests")
    pe.add_argument("--db", required=True)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "tick":
        doc = cmd_tick(args.db)
    elif args.command == "status":
        doc = cmd_status(args.db)
    elif args.command == "pending":
        doc = cmd_pending(args.db)
    else:  # pragma: no cover - argparse guards
        return 2
    json.dump(doc, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
