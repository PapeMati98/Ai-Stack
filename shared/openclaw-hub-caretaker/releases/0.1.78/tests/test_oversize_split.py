"""Oversized groups split into ordered max_batch chunks, never blocked."""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from caretaker.audit import (
    active_cycle, ensure_tables, mark_members, project_batches,
)
from caretaker.pubstate import ensure_pub_tables


def _member(i, project="one", group_key="huge", status="pending"):
    return {
        "proposal_id": "prop-%02d" % i,
        "path": "proposals/codex/p-%02d.md" % i,
        "sha256": "h",
        "project": project,
        "group_key": group_key,
        "status": status,
    }


def _cycle(n=19):
    return {"members": [_member(i) for i in range(n)]}


class OversizeSplitTests(unittest.TestCase):
    def test_nineteen_members_split_8_8_3_no_loss_or_dup(self):
        members = _cycle(19)["members"]
        batches, blocked = project_batches({"members": members}, 8)
        self.assertEqual(blocked, [])
        sizes = [len(batch) for batch in batches["one"]]
        self.assertEqual(sizes, [8, 8, 3])
        seen = [(m["proposal_id"], m["path"]) for batch in batches["one"] for m in batch]
        expected = [(m["proposal_id"], m["path"]) for m in members]
        self.assertEqual(seen, sorted(expected))
        self.assertEqual(len(seen), 19)
        self.assertEqual(len(set(seen)), 19)

    def test_split_order_is_stable_across_repeated_calls(self):
        cycle = _cycle(19)
        first, _ = project_batches(cycle, 8)
        second, _ = project_batches(cycle, 8)
        ids_first = [[m["proposal_id"] for m in batch] for batch in first["one"]]
        ids_second = [[m["proposal_id"] for m in batch] for batch in second["one"]]
        self.assertEqual(ids_first, ids_second)
        shuffled = list(reversed(cycle["members"]))
        third, _ = project_batches({"members": shuffled}, 8)
        ids_third = [[m["proposal_id"] for m in batch] for batch in third["one"]]
        self.assertEqual(ids_first, ids_third)

    def test_fitting_groups_are_not_packed_past_max_batch(self):
        members = []
        for i, size in enumerate((3, 7)):
            for j in range(size):
                members.append(_member(i * 10 + j, group_key="g%d" % i))
        batches, blocked = project_batches({"members": members}, 8)
        self.assertEqual(blocked, [])
        self.assertEqual([len(b) for b in batches["one"]], [3, 7])

    def test_oversized_only_cycle_drains_when_split_batches_are_marked(self):
        conn = sqlite3.connect(":memory:")
        ensure_tables(conn)
        ensure_pub_tables(conn)
        members = [_member(i) for i in range(19)]
        cycle_id = "audit-oversize-split"
        conn.execute(
            "INSERT INTO audit_cycles(id, trigger, created_at, status, counts) "
            "VALUES(?,?,?,?,?)",
            (cycle_id, "threshold", "2026-09-19T00:00:00+00:00", "ACTIVE", "{}"),
        )
        for m in members:
            conn.execute(
                "INSERT INTO audit_members("
                "cycle_id, proposal_id, path, sha256, project, group_key, status, reason) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (cycle_id, m["proposal_id"], m["path"], m["sha256"], m["project"],
                 m["group_key"], "pending", ""),
            )
        conn.commit()
        cycle = active_cycle(conn)
        self.assertIsNotNone(cycle)
        self.assertEqual(cycle["status"], "ACTIVE")
        statuses = {m["status"] for m in cycle["members"]}
        self.assertEqual(statuses, {"pending"})
        batches, blocked = project_batches(cycle, 8)
        self.assertEqual(blocked, [])
        self.assertEqual([len(b) for b in batches["one"]], [8, 8, 3])
        for batch in batches["one"]:
            mark_members(
                conn,
                cycle["id"],
                [(m["proposal_id"], m["path"]) for m in batch],
                "published",
            )
        self.assertIsNone(active_cycle(conn))
        row = conn.execute("SELECT status FROM audit_cycles WHERE id=?", (cycle["id"],)).fetchone()
        self.assertEqual(row[0], "DRAINED")
        pending = conn.execute(
            "SELECT COUNT(*) FROM audit_members WHERE cycle_id=? AND status='pending'",
            (cycle["id"],),
        ).fetchone()[0]
        blocked_n = conn.execute(
            "SELECT COUNT(*) FROM audit_members WHERE cycle_id=? AND status='blocked'",
            (cycle["id"],),
        ).fetchone()[0]
        published = conn.execute(
            "SELECT COUNT(*) FROM audit_members WHERE cycle_id=? AND status='published'",
            (cycle["id"],),
        ).fetchone()[0]
        self.assertEqual(pending, 0)
        self.assertEqual(blocked_n, 0)
        self.assertEqual(published, 19)
        conn.close()


if __name__ == "__main__":
    unittest.main()
