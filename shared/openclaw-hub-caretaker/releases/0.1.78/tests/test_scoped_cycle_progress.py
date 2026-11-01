"""Scoped publication progress when an ACTIVE cycle is exhausted for allowed projects.

Release 0.1.50 is the predicate-free contract preserved in this candidate.
This checkout has no sibling 0.1.50 tree; calling maybe_start_cycle without
project_allowed is that contract and must keep the starved ACTIVE cycle.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import maintenance
from caretaker.audit import (
    active_cycle, cycle_projects, ensure_tables, mark_members, maybe_start_cycle, policy_from,
)
from caretaker.pubstate import (
    batch_key, code_version, due_to_run, ensure_pub_tables, fingerprint,
)

ALLOWED = ["example-cs-app", "examplecrm", "example-crm", "example-robotics", "example-career-os"]
HELD = ["example-agent-stack", "example-knowledge-hub", "example-project"]
NOW = dt.datetime(2026, 10, 21, 12, 0, tzinfo=dt.timezone.utc)
TASK_TMP = ROOT / ".test-tmp"


def _policy(projects=None, held=None):
    return {
        "enabled": True,
        "include_existing_hub_backlog": True,
        "projects": list(ALLOWED if projects is None else projects),
        "held_projects": list(HELD if held is None else held),
        "min_ready_groups": 10,
        "max_wait_hours": 24,
        "max_batch": 8,
    }


def _predicate(projects=None, held=None):
    policy = _policy(projects, held)
    return lambda project: maintenance._publication_project_allowed(project, policy)


def _prop(pid, project, digest="hash", source=None):
    return {
        "proposal_id": pid,
        "path": "proposals/codex/" + pid + ".md",
        "sha256": digest,
        "project": project,
        "producer": "codex",
        "episode": "",
        "source_ids": [source or ("src-" + pid)],
    }


def _summary(eligible, trigger="threshold"):
    return {
        "trigger": trigger,
        "eligible": list(eligible),
        "raw_eligible": len(eligible),
        "group_count": len(eligible),
        "ready_group_count": len(eligible),
        "oldest_age_hours": 48,
        "deferred": 0,
        "unsupported": 0,
        "policy": policy_from(_policy()),
    }


def _db():
    conn = sqlite3.connect(":memory:")
    ensure_tables(conn)
    ensure_pub_tables(conn)
    return conn


def _insert_cycle(conn, cycle_id, members, lane="legacy", status="ACTIVE",
                  created="2026-10-04T00:00:00+00:00"):
    conn.execute(
        "INSERT INTO audit_cycles(id, trigger, created_at, status, counts, lane) "
        "VALUES(?,?,?,?,?,?)",
        (cycle_id, "threshold", created, status, "{}", lane),
    )
    for member in members:
        conn.execute(
            "INSERT INTO audit_members("
            "cycle_id, proposal_id, path, sha256, project, group_key, status, reason) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                cycle_id, member["proposal_id"], member["path"], member["sha256"],
                member["project"], member.get("group_key", "g"), member.get("status", "pending"),
                member.get("reason", ""),
            ),
        )
    conn.commit()


def _member(prop, status="pending", reason=""):
    row = dict(prop)
    row["status"] = status
    row["reason"] = reason
    return row


def _members(conn, cycle_id=None):
    sql = (
        "SELECT cycle_id, proposal_id, path, sha256, project, group_key, status, reason "
        "FROM audit_members"
    )
    if cycle_id is None:
        return conn.execute(sql + " ORDER BY cycle_id, project, proposal_id, path").fetchall()
    return conn.execute(
        sql + " WHERE cycle_id=? ORDER BY project, proposal_id, path", (cycle_id,)
    ).fetchall()


def _cycle_status(conn, cycle_id):
    return conn.execute("SELECT status FROM audit_cycles WHERE id=?", (cycle_id,)).fetchone()[0]


def _starved_members():
    return [
        _member(_prop("prop-held", "example-agent-stack"), "pending", "held"),
        _member(_prop("prop-held-kh", "example-knowledge-hub"), "pending", "held"),
        _member(_prop("prop-pub", "example-cs-app", "hash-pub"), "published", "done"),
        _member(_prop("prop-fail", "examplecrm", "hash-fail"), "failed", "BLOCKED"),
        _member(_prop("prop-acc", "example-crm", "hash-acc"), "accounted_elsewhere", "ledger"),
        _member(_prop("prop-quar", "example-robotics", "hash-quar"), "quarantined", "same"),
        _member(_prop("prop-unk", "example-career-os", "hash-unk"), "unknown", "needs review"),
    ]


def _tempting_summary():
    fresh = _prop("prop-fresh", "example-cs-app", "hash-fresh")
    eligible = [
        fresh,
        _prop("prop-held-new", "example-agent-stack", "hash-held-new"),
        _prop("prop-foreign", "foreign-project", "hash-foreign"),
        _prop("prop-fail", "examplecrm", "hash-fail"),
        _prop("prop-unk", "example-career-os", "hash-unk"),
        _prop("prop-quar", "example-robotics", "hash-quar"),
        _prop("prop-pub", "example-cs-app", "hash-pub"),
        _prop("prop-acc", "example-crm", "hash-acc"),
    ]
    return fresh, _summary(eligible)


def _accept_synthetic_batch(_vault, members, _project, _registry, **_kwargs):
    """Keep cadence tests independent from the real closure-validation contract."""
    return list(members), []


class ScopedCycleProgressTests(unittest.TestCase):
    def test_050_starvation_and_051_scoped_progress(self):
        fresh, summary = _tempting_summary()
        legacy = _db()
        self.addCleanup(legacy.close)
        _insert_cycle(legacy, "cycle-old", _starved_members())
        before = _members(legacy, "cycle-old")
        held_projects_before = cycle_projects(active_cycle(legacy))
        returned = maybe_start_cycle(legacy, summary, now_iso=NOW.isoformat())
        self.assertEqual(returned["id"], "cycle-old")
        self.assertEqual(_cycle_status(legacy, "cycle-old"), "ACTIVE")
        self.assertEqual(_members(legacy, "cycle-old"), before)
        self.assertIsNone(legacy.execute(
            "SELECT 1 FROM audit_members WHERE proposal_id='prop-fresh'"
        ).fetchone())
        self.assertEqual(held_projects_before, ["example-agent-stack", "example-knowledge-hub"])
        self.assertNotIn("example-cs-app", held_projects_before)

        current = _db()
        self.addCleanup(current.close)
        _insert_cycle(current, "cycle-old", _starved_members())
        before = _members(current, "cycle-old")
        cycle_before = current.execute(
            "SELECT trigger, created_at, counts, lane FROM audit_cycles WHERE id='cycle-old'"
        ).fetchone()
        opened = maybe_start_cycle(
            current, summary, now_iso=NOW.isoformat(), project_allowed=_predicate(),
        )
        self.assertEqual(_cycle_status(current, "cycle-old"), "SCOPED_DRAINED")
        self.assertNotEqual(_cycle_status(current, "cycle-old"), "DRAINED")
        self.assertEqual(_members(current, "cycle-old"), before)
        self.assertEqual(current.execute(
            "SELECT trigger, created_at, counts, lane FROM audit_cycles WHERE id='cycle-old'"
        ).fetchone(), cycle_before)
        self.assertNotEqual(opened["id"], "cycle-old")
        self.assertEqual(opened["status"], "ACTIVE")
        self.assertEqual(opened["lane"], "legacy")
        self.assertEqual(
            [(m["proposal_id"], m["project"], m["status"], m["sha256"]) for m in opened["members"]],
            [("prop-fresh", "example-cs-app", "pending", "hash-fresh")],
        )
        self.assertEqual(cycle_projects(opened), ["example-cs-app"])
        self.assertEqual(current.execute("SELECT COUNT(*) FROM audit_members").fetchone()[0], 8)
        self.assertIsNone(current.execute(
            "SELECT 1 FROM audit_members WHERE proposal_id IN ('prop-held-new','prop-foreign')"
        ).fetchone())
        self.assertIsNone(current.execute(
            "SELECT 1 FROM audit_members WHERE status='processed'"
        ).fetchone())

    def test_no_churn_without_fresh_authorized_work(self):
        db = _db()
        self.addCleanup(db.close)
        _insert_cycle(db, "cycle-old", _starved_members())
        before = _members(db)
        summary = _summary([
            _prop("prop-held-new", "example-agent-stack"),
            _prop("prop-fail", "examplecrm", "hash-fail"),
        ])
        opened = maybe_start_cycle(
            db, summary, now_iso=NOW.isoformat(), project_allowed=_predicate(),
        )
        self.assertEqual(opened["id"], "cycle-old")
        self.assertEqual(_cycle_status(db, "cycle-old"), "ACTIVE")
        self.assertEqual(_members(db), before)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_cycles").fetchone()[0], 1)

    def test_triggerless_active_cycle_still_admits_fresh_allowed_work(self):
        db = _db()
        self.addCleanup(db.close)
        _insert_cycle(db, "cycle-old", _starved_members())
        fresh = _prop("prop-fresh", "examplecrm", "hash-fresh")
        opened = maybe_start_cycle(
            db, _summary([fresh], trigger=None), now_iso=NOW.isoformat(),
            project_allowed=_predicate(),
        )
        self.assertEqual(opened["members"][0]["proposal_id"], "prop-fresh")
        self.assertEqual(_cycle_status(db, "cycle-old"), "SCOPED_DRAINED")

    def test_predicate_free_contract_still_freezes_every_eligible_project(self):
        db = _db()
        self.addCleanup(db.close)
        eligible = [
            _prop("prop-a", "example-cs-app"),
            _prop("prop-h", "example-agent-stack"),
        ]
        opened = maybe_start_cycle(db, _summary(eligible), now_iso=NOW.isoformat())
        self.assertEqual(
            sorted(m["project"] for m in opened["members"]),
            ["example-agent-stack", "example-cs-app"],
        )
        self.assertEqual(_cycle_status(db, opened["id"]), "ACTIVE")

    def test_allowlist_does_not_broaden_held_projects(self):
        db = _db()
        self.addCleanup(db.close)
        _insert_cycle(db, "cycle-old", _starved_members())
        fresh_allowed = _prop("prop-ok", "example-career-os", "hash-ok")
        fresh_held = _prop("prop-broadened", "example-cs-app", "hash-b")
        summary = _summary([fresh_allowed, fresh_held, _prop("prop-out", "other")])
        opened = maybe_start_cycle(
            db, summary, now_iso=NOW.isoformat(),
            project_allowed=_predicate(
                projects=["example-career-os", "example-cs-app"],
                held=["example-cs-app", "example-agent-stack", "example-knowledge-hub", "example-project"],
            ),
        )
        self.assertEqual([m["proposal_id"] for m in opened["members"]], ["prop-ok"])
        self.assertEqual(_members(db, "cycle-old"), _members_snapshot_held_and_terminal())

    def test_lanes_and_new_arrivals_stay_independent(self):
        db = _db()
        self.addCleanup(db.close)
        _insert_cycle(db, "cycle-old", _starved_members(), lane="legacy")
        new_members = [_member(_prop("prop-new-pending", "example-cs-app", "hash-np"), "pending")]
        _insert_cycle(db, "cycle-new", new_members, lane="new", created="2026-10-11T00:00:00+00:00")
        new_before = _members(db, "cycle-new")
        fresh = _prop("prop-fresh", "example-cs-app", "hash-fresh")
        later = _prop("prop-later", "examplecrm", "hash-later")
        opened = maybe_start_cycle(
            db, _summary([fresh, later]), now_iso=NOW.isoformat(), lane="legacy",
            project_allowed=_predicate(),
        )
        self.assertEqual(_members(db, "cycle-new"), new_before)
        self.assertEqual(_cycle_status(db, "cycle-new"), "ACTIVE")
        self.assertEqual(sorted(m["proposal_id"] for m in opened["members"]), ["prop-fresh", "prop-later"])
        again = maybe_start_cycle(
            db, _summary([fresh, later, _prop("prop-third", "example-robotics", "hash-third")]),
            now_iso=NOW.isoformat(), lane="legacy", project_allowed=_predicate(),
        )
        self.assertEqual(again["id"], opened["id"])
        self.assertIsNone(db.execute(
            "SELECT 1 FROM audit_members WHERE proposal_id='prop-third'"
        ).fetchone())
        self.assertEqual(cycle_projects(again), ["example-cs-app", "examplecrm"])
        new_opened = maybe_start_cycle(
            db, _summary([_prop("prop-new-fresh", "examplecrm", "hash-nf")]),
            now_iso=NOW.isoformat(), lane="new", project_allowed=_predicate(),
        )
        self.assertEqual(new_opened["id"], "cycle-new")
        self.assertEqual(_members(db, "cycle-new"), new_before)
        legacy_after = _members(db, opened["id"])
        self.assertEqual(new_opened["lane"], "new")
        self.assertEqual(_members(db, opened["id"]), legacy_after)

    def test_scope_restoration_keeps_pending_evidence_until_policy_authorizes(self):
        db = _db()
        self.addCleanup(db.close)
        _insert_cycle(db, "cycle-old", _starved_members())
        held_before = [
            row for row in _members(db, "cycle-old")
            if row[4] in {"example-agent-stack", "example-knowledge-hub"}
        ]
        fresh = _prop("prop-fresh", "example-cs-app", "hash-fresh")
        later = _prop("prop-later", "examplecrm", "hash-later")
        opened = maybe_start_cycle(
            db, _summary([fresh, later]), now_iso="2026-10-21T12:00:00+00:00",
            project_allowed=_predicate(),
        )
        self.assertEqual(_cycle_status(db, "cycle-old"), "SCOPED_DRAINED")
        self.assertCountEqual(
            [row for row in _members(db, "cycle-old") if row[4] in {"example-agent-stack", "example-knowledge-hub"}],
            held_before,
        )
        broadened = _predicate(
            projects=ALLOWED + ["example-agent-stack"],
            held=["example-knowledge-hub", "example-project"],
        )
        while_busy = maybe_start_cycle(
            db, _summary([fresh, later, _prop("prop-soon", "example-career-os", "hash-soon")]),
            now_iso="2026-10-21T13:00:00+00:00", project_allowed=broadened,
        )
        self.assertEqual(while_busy["id"], opened["id"])
        self.assertEqual(_cycle_status(db, "cycle-old"), "SCOPED_DRAINED")
        self.assertIsNone(db.execute(
            "SELECT 1 FROM audit_members WHERE proposal_id='prop-soon'"
        ).fetchone())
        mark_members(db, opened["id"], [(m["proposal_id"], m["path"]) for m in opened["members"]], "published", "")
        restored = maybe_start_cycle(
            db, _summary([later, _prop("prop-soon", "example-career-os", "hash-soon")]),
            now_iso="2026-10-21T14:00:00+00:00", project_allowed=broadened,
        )
        self.assertEqual(restored["id"], "cycle-old")
        self.assertEqual(_cycle_status(db, "cycle-old"), "ACTIVE")
        self.assertEqual(
            [row for row in _members(db, "cycle-old") if row[1] == "prop-held"],
            [row for row in held_before if row[1] == "prop-held"],
        )
        self.assertEqual(
            [row for row in _members(db, "cycle-old") if row[1] == "prop-held-kh"],
            [row for row in held_before if row[1] == "prop-held-kh"],
        )
        self.assertIsNone(db.execute(
            "SELECT 1 FROM audit_members WHERE proposal_id='prop-soon'"
        ).fetchone())
        kh_before = [row for row in _members(db, "cycle-old") if row[1] == "prop-held-kh"]
        mark_members(db, "cycle-old", [("prop-held", "proposals/codex/prop-held.md")], "published", "")
        successor = maybe_start_cycle(
            db, _summary([_prop("prop-soon", "example-career-os", "hash-soon")]),
            now_iso="2026-10-21T15:00:00+00:00", project_allowed=broadened,
        )
        self.assertEqual(successor["members"][0]["proposal_id"], "prop-soon")
        self.assertEqual(_cycle_status(db, "cycle-old"), "SCOPED_DRAINED")
        self.assertEqual(
            [row for row in _members(db, "cycle-old") if row[1] == "prop-held-kh"],
            kh_before,
        )
        self.assertIsNone(db.execute(
            "SELECT 1 FROM audit_members WHERE proposal_id='prop-held' AND cycle_id=?",
            (successor["id"],),
        ).fetchone())

    def test_changed_input_hash_is_a_new_admission_and_old_row_stays(self):
        db = _db()
        self.addCleanup(db.close)
        _insert_cycle(db, "cycle-old", [
            _member(_prop("prop-fail", "example-cs-app", "hash-old"), "failed", "BLOCKED"),
        ])
        before = _members(db, "cycle-old")
        opened = maybe_start_cycle(
            db, _summary([_prop("prop-fail", "example-cs-app", "hash-new")]),
            now_iso=NOW.isoformat(), project_allowed=_predicate(),
        )
        self.assertEqual(opened["members"][0]["sha256"], "hash-new")
        self.assertEqual(_members(db, "cycle-old"), before)
        self.assertEqual(_cycle_status(db, "cycle-old"), "SCOPED_DRAINED")

    def test_publication_tick_publishes_fresh_allowed_work_without_models_on_terminal_rows(self):
        db = _db()
        self.addCleanup(db.close)
        _insert_cycle(db, "cycle-old", _starved_members())
        before = _members(db, "cycle-old")
        fresh, summary = _tempting_summary()
        calls = []

        def invoke(config_path, paths):
            calls.append(list(paths))
            return {"status": "PUBLISHED"}

        with _workspace() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            state = Path(root) / "state"
            state.mkdir()
            cfg = {"hub": {"vault": str(vault)}, "project_slug": "example-cs-app", "state_dir": str(state)}
            with patch("maintenance._audit_summary", return_value=summary), \
                    patch("maintenance._invoke_curate", side_effect=invoke), \
                    patch("maintenance._live_member_hash", return_value="hash-fresh"), \
                    patch("caretaker.audit.partition_batch_members", side_effect=_accept_synthetic_batch):
                result = maintenance.publication_tick(
                    "config", cfg, {"publication": _policy()}, db, now=NOW,
                )
        self.assertEqual(calls, [["proposals/codex/prop-fresh.md"]])
        self.assertEqual(result["status"], "PUBLISHED")
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(_members(db, "cycle-old"), before)
        self.assertEqual(_cycle_status(db, "cycle-old"), "SCOPED_DRAINED")
        self.assertEqual(db.execute(
            "SELECT status FROM audit_members WHERE proposal_id='prop-fresh'"
        ).fetchone()[0], "published")

    def test_identical_failed_unknown_and_quarantine_do_not_republish(self):
        db = _db()
        self.addCleanup(db.close)
        members = _starved_members() + [
            _member(_prop("prop-qpend", "example-cs-app", "hash-q"), "pending", "quarantine-row"),
        ]
        _insert_cycle(db, "cycle-old", members)
        held_before = [row for row in _members(db, "cycle-old") if row[1] == "prop-held"]
        unk_before = [row for row in _members(db, "cycle-old") if row[1] == "prop-unk"]
        fail_before = [row for row in _members(db, "cycle-old") if row[1] == "prop-fail"]
        policy = policy_from(_policy())
        path = "proposals/codex/prop-qpend.md"
        current_fp = fingerprint(
            "example-cs-app", [path], ["hash-q", "missing_target"], policy, code_version(),
        )
        key = batch_key("example-cs-app", [path])
        db.execute(
            "INSERT INTO publications(batch, status, failures, result, attempt, next_due, category, fingerprint, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (key, "QUARANTINED", 2, "{}", 2, "", "deterministic", current_fp, NOW.isoformat()),
        )
        db.commit()
        self.assertEqual(due_to_run(
            {"status": "UNKNOWN_REQUIRES_RECONCILIATION", "fingerprint": "other", "next_due": ""},
            NOW.isoformat(), "other",
        ), (False, "unknown_needs_reconciliation"))
        self.assertEqual(due_to_run(
            {"status": "QUARANTINED", "fingerprint": current_fp, "next_due": ""},
            NOW.isoformat(), current_fp,
        )[0], False)
        summary = _summary([
            _prop("prop-fail", "examplecrm", "hash-fail"),
            _prop("prop-unk", "example-career-os", "hash-unk"),
            _prop("prop-quar", "example-robotics", "hash-quar"),
            _prop("prop-qpend", "example-cs-app", "hash-q"),
        ])
        with _workspace() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            cfg = {
                "hub": {"vault": str(vault)},
                "project_slug": "example-cs-app",
                "state_dir": str(Path(root) / "state"),
            }
            with patch("maintenance._audit_summary", return_value=summary), \
                    patch("maintenance._invoke_curate", side_effect=AssertionError("model")), \
                    patch("maintenance._live_member_hash", return_value="hash-q"):
                first = maintenance.publication_tick(
                    "config", cfg, {"publication": _policy()}, db, now=NOW,
                )
                second = maintenance.publication_tick(
                    "config", cfg, {"publication": _policy()}, db, now=NOW,
                )
        self.assertEqual(first["model_calls"], 0)
        self.assertEqual(second["model_calls"], 0)
        self.assertNotEqual(first["status"], "PUBLISHED")
        self.assertEqual(db.execute(
            "SELECT status, fingerprint FROM publications WHERE batch=?", (key,)
        ).fetchone(), ("QUARANTINED", current_fp))
        self.assertEqual([row for row in _members(db, "cycle-old") if row[1] == "prop-held"], held_before)
        self.assertEqual([row for row in _members(db) if row[1] == "prop-unk"], unk_before)
        self.assertEqual([row for row in _members(db) if row[1] == "prop-fail"], fail_before)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_cycles").fetchone()[0], 1)
        self.assertEqual(_cycle_status(db, "cycle-old"), "ACTIVE")

    def test_backoff_stays_pending_and_lock_does_not_mutate_cycles(self):
        db = _db()
        self.addCleanup(db.close)
        pending = _member(_prop("prop-back", "example-cs-app", "hash-back"), "pending", "backoff")
        _insert_cycle(db, "cycle-old", _starved_members() + [pending])
        before = _members(db)
        path = pending["path"]
        key = batch_key("example-cs-app", [path])
        next_due = "2999-01-01T00:00:00+00:00"
        db.execute(
            "INSERT INTO publications(batch, status, failures, result, attempt, next_due, category, fingerprint, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (key, "BACKOFF_DEFERRED", 1, "{}", 1, next_due, "transient", "fp", NOW.isoformat()),
        )
        db.commit()
        self.assertEqual(due_to_run(
            {"status": "BACKOFF_DEFERRED", "fingerprint": "fp", "next_due": next_due},
            NOW.isoformat(), "other-fp",
        ), (False, "backoff_not_due"))
        summary = _summary([_prop("prop-back", "example-cs-app", "hash-back"), _prop("prop-fresh", "examplecrm", "hash-fresh")])
        with _workspace() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            cfg = {
                "hub": {"vault": str(vault)},
                "project_slug": "example-cs-app",
                "state_dir": str(Path(root) / "state"),
            }
            with patch("maintenance._audit_summary", return_value=summary), \
                    patch("maintenance._invoke_curate", side_effect=AssertionError("model")), \
                    patch("maintenance._live_member_hash", return_value="hash-back"), \
                    patch("caretaker.audit.partition_batch_members", side_effect=_accept_synthetic_batch):
                result = maintenance.publication_tick(
                    "config", cfg, {"publication": _policy()}, db, now=NOW,
                )
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(result["status"], "SKIPPED_NO_DUE_BATCH")
        self.assertEqual(_members(db), before)
        self.assertEqual(_cycle_status(db, "cycle-old"), "ACTIVE")
        self.assertEqual(db.execute(
            "SELECT status, next_due FROM publications WHERE batch=?", (key,)
        ).fetchone(), ("BACKOFF_DEFERRED", next_due))
        locked = maintenance.publication_drain(
            {"publication": _policy()},
            {"hub": {"vault": "synthetic-vault"}, "project_slug": "example-cs-app"},
            db, Path("synthetic-state"), now=NOW, recovery={"status": "LOCK_FRESH"},
        )
        self.assertEqual(locked["status"], "BLOCKED")
        self.assertEqual(locked["reason"], "curator_lock_held")
        self.assertEqual(locked["model_calls"], 0)
        unverified = maintenance.publication_drain(
            {"publication": _policy()},
            {"hub": {"vault": "synthetic-vault"}, "project_slug": "example-cs-app"},
            db, Path("synthetic-state"), now=NOW, recovery={"status": "LOCK_TIME_UNVERIFIED"},
        )
        self.assertEqual(unverified["status"], "BLOCKED")
        self.assertEqual(_members(db), before)
        self.assertEqual(_cycle_status(db, "cycle-old"), "ACTIVE")

    def test_drain_fairness_publishes_each_fresh_allowed_project_once(self):
        db = _db()
        self.addCleanup(db.close)
        _insert_cycle(db, "cycle-old", _starved_members())
        held_before = [row for row in _members(db, "cycle-old") if row[4] in {"example-agent-stack", "example-knowledge-hub"}]
        terminal_before = [row for row in _members(db, "cycle-old") if row[4] not in {"example-agent-stack", "example-knowledge-hub"}]
        first = _prop("prop-a", "examplecrm", "hash-fresh")
        second = _prop("prop-b", "example-cs-app", "hash-fresh")
        summary = _summary([first, second])
        calls = []

        def invoke(config_path, paths):
            calls.append(list(paths))
            return {"status": "PUBLISHED"}

        with _workspace() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            state = Path(root) / "state"
            with patch("maintenance._audit_summary", return_value=summary), \
                    patch("maintenance._invoke_curate", side_effect=invoke), \
                    patch("maintenance._live_member_hash", return_value="hash-fresh"), \
                    patch("caretaker.audit.partition_batch_members", side_effect=_accept_synthetic_batch):
                result = maintenance.publication_drain(
                    {"publication": _policy(), "max_projects_per_tick": 2},
                    {"hub": {"vault": str(vault), "script": "knowledge.py"}, "project_slug": "example-cs-app",
                     "allowlist": {"tools": []}},
                    db, state, now=NOW, recovery={"status": "NO_LOCK"},
                )
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(sorted(path for call in calls for path in call), [
            "proposals/codex/prop-a.md", "proposals/codex/prop-b.md",
        ])
        self.assertEqual(
            [row for row in _members(db, "cycle-old") if row[4] in {"example-agent-stack", "example-knowledge-hub"}],
            held_before,
        )
        self.assertEqual(
            [row for row in _members(db, "cycle-old") if row[4] not in {"example-agent-stack", "example-knowledge-hub"}],
            terminal_before,
        )
        self.assertEqual(_cycle_status(db, "cycle-old"), "SCOPED_DRAINED")
        self.assertEqual(db.execute(
            "SELECT status FROM audit_cycles WHERE id!='cycle-old'"
        ).fetchone()[0], "DRAINED")
        self.assertIsNone(db.execute(
            "SELECT 1 FROM audit_members WHERE status='processed' OR cycle_id='cycle-old' AND status='pending' AND project IN ('example-cs-app','examplecrm')"
        ).fetchone())


def _members_snapshot_held_and_terminal():
    conn = _db()
    try:
        _insert_cycle(conn, "cycle-old", _starved_members())
        return _members(conn, "cycle-old")
    finally:
        conn.close()


def _workspace():
    TASK_TMP.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=str(TASK_TMP))


if __name__ == "__main__":
    unittest.main()
