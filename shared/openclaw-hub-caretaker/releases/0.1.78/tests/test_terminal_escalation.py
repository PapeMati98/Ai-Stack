"""Repeated transient failures must escalate to a terminal batch state."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from caretaker.audit import ensure_tables, policy_from
from caretaker.pubstate import (
    MAX_TRANSIENT_ATTEMPTS,
    batch_key,
    due_to_run,
    ensure_pub_tables,
    fingerprint,
    read_row,
    record_failure,
    record_start,
)


POLICY = policy_from({})


def memory_db():
    conn = sqlite3.connect(":memory:")
    ensure_tables(conn)
    ensure_pub_tables(conn)
    return conn


class TransientTerminalEscalationTests(unittest.TestCase):
    def test_repeated_native_timeout_becomes_terminal_not_backoff(self):
        db = memory_db()
        paths = ["proposals/codex/a.md"]
        key = batch_key("one", paths)
        fp = fingerprint("one", paths, ["hash-a"], POLICY, "code1")
        now = dt.datetime(2026, 9, 18, tzinfo=dt.timezone.utc)
        record_start(db, key, now.isoformat(), fp, {"proposal_ids": ["prop-a"]})
        last = None
        for i in range(MAX_TRANSIENT_ATTEMPTS):
            last = record_failure(
                db, key, "native_timeout", {"status": "BLOCKED"}, fp, now=now
            )
            if i < MAX_TRANSIENT_ATTEMPTS - 1:
                self.assertEqual(last["status"], "BACKOFF_DEFERRED")
                self.assertEqual(last["category"], "transient")
                self.assertTrue(last["next_due"])
        self.assertEqual(last["status"], "QUARANTINED")
        self.assertEqual(last["category"], "transient")
        self.assertEqual(last["next_due"], "")
        row = read_row(db, key)
        self.assertEqual(row["status"], "QUARANTINED")
        stored_reason = json.loads(row["result"])["reason"]
        self.assertIn("native_timeout", stored_reason)
        self.assertIn("transient_attempts_exhausted", stored_reason)
        due, why = due_to_run(row, now.isoformat(), fp)
        self.assertFalse(due)
        self.assertEqual(why, "quarantined_same_fingerprint")
        fp2 = fingerprint("one", paths, ["hash-b"], POLICY, "code1")
        due2, why2 = due_to_run(row, now.isoformat(), fp2)
        self.assertTrue(due2)
        self.assertEqual(why2, "fingerprint_changed")
        db.close()

    def test_custom_transient_limit_is_honored(self):
        db = memory_db()
        paths = ["proposals/codex/b.md"]
        key = batch_key("one", paths)
        fp = fingerprint("one", paths, ["hash-a"], POLICY, "code1")
        now = dt.datetime(2026, 9, 18, tzinfo=dt.timezone.utc)
        record_start(db, key, now.isoformat(), fp)
        first = record_failure(
            db, key, "native_timeout", {}, fp, now=now, max_transient_attempts=2
        )
        self.assertEqual(first["status"], "BACKOFF_DEFERRED")
        second = record_failure(
            db, key, "native_timeout", {}, fp, now=now, max_transient_attempts=2
        )
        self.assertEqual(second["status"], "QUARANTINED")
        db.close()

    def test_deterministic_failures_keep_existing_attempt_limit(self):
        db = memory_db()
        paths = ["proposals/codex/c.md"]
        key = batch_key("one", paths)
        fp = fingerprint("one", paths, ["hash-a"], POLICY, "code1")
        now = dt.datetime(2026, 9, 18, tzinfo=dt.timezone.utc)
        record_start(db, key, now.isoformat(), fp)
        first = record_failure(db, key, "independent_review_not_accepted", {"status": "BLOCKED"}, fp, now=now)
        self.assertEqual(first["status"], "BLOCKED")
        second = record_failure(db, key, "independent_review_not_accepted", {"status": "BLOCKED"}, fp, now=now)
        self.assertEqual(second["status"], "QUARANTINED")
        db.close()


if __name__ == "__main__":
    unittest.main()
