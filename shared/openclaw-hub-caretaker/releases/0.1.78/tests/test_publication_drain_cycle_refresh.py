"""Cycle refresh behavior for the bounded publication drain."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import maintenance
from caretaker.audit import (
    _open_cycle, active_cycle, ensure_tables, group_key, mark_members,
)
from caretaker.pubstate import ensure_pub_tables


ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ["alpha", "bravo", "charlie", "delta", "z-app"]
NOW = dt.datetime(2026, 10, 24, 17, 0, tzinfo=dt.timezone.utc)


class PublicationDrainCycleRefreshTests(unittest.TestCase):
    def _seed(self):
        temp = tempfile.TemporaryDirectory(prefix="drain-cycle-refresh-",
                                           dir=str(ROOT / ".test-tmp"))
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        vault = root / "vault"
        state = root / "state"
        vault.mkdir()
        state.mkdir()
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        ensure_tables(db)
        ensure_pub_tables(db)

        members = [{
            "proposal_id": "proposal-" + project,
            "path": "proposals/" + project + ".md",
            "sha256": (project.encode().hex() + "0" * 64)[:64],
            "project": project,
            "group_key": project,
            "status": "pending",
            "reason": "",
        } for project in PROJECTS]
        summary = {
            "eligible": members,
            "raw_eligible": len(members),
            "group_count": len(members),
            "ready_group_count": len(members),
            "oldest_age_hours": 0,
            "deferred": 0,
            "unsupported": 0,
            "trigger": "threshold",
            "policy": {"max_batch": 8},
        }
        initial = _open_cycle(
            db, summary, members, NOW.isoformat(), "legacy", 8,
        )
        self.assertEqual([item["project"] for item in initial["members"]], PROJECTS)
        return root, vault, state, db, summary, initial

    def _run_case(self, first_status):
        root, vault, state, db, summary, initial = self._seed()
        app_member = summary["eligible"][-1]
        calls = []
        app_cycle_ids = []

        def tick(_config_path, cfg, _control, database, **_kwargs):
            project = cfg["project_slug"]
            calls.append(project)
            if len(calls) == 1:
                if first_status == "PUBLISHED":
                    mark_members(database, initial["id"],
                                 [(initial["members"][0]["proposal_id"],
                                   initial["members"][0]["path"])],
                                 "published", "")
                exact_summary = {**summary, "eligible": [app_member], "raw_eligible": 1,
                                 "group_count": 1, "ready_group_count": 1}
                exact = _open_cycle(
                    database, exact_summary, [dict(app_member)], NOW.isoformat(),
                    "legacy", 8, predecessor_id=initial["id"],
                )
                app_cycle_ids.append(exact["id"])
                return {"status": first_status, "model_calls": 0,
                        "cycle_id": exact["id"]}
            if project == "z-app":
                current = active_cycle(database, "legacy")
                self.assertIsNotNone(current)
                self.assertEqual(current["id"], app_cycle_ids[0])
                mark_members(database, current["id"],
                             [(app_member["proposal_id"], app_member["path"])],
                             "published", "")
                return {"status": "PUBLISHED", "model_calls": 0,
                        "cycle_id": current["id"]}
            return {"status": "NO_PENDING", "model_calls": 0}

        control = {
            "max_projects_per_tick": 4,
            "publication": {
                "enabled": True,
                "include_existing_hub_backlog": True,
                "projects": list(PROJECTS),
                "held_projects": [],
                "min_ready_groups": 1,
                "max_wait_hours": 24,
                "max_batch": 8,
            },
        }
        with patch.object(maintenance, "_audit_summary", return_value=summary), \
                patch.object(maintenance, "publication_tick", side_effect=tick), \
                patch.object(maintenance, "_prepared_priority_candidate", return_value=None):
            result = maintenance.publication_drain(
                control, {"hub": {"vault": str(vault)}}, db, state,
                now=NOW, lane="legacy", recovery={"status": "NO_LOCK"},
            )

        app_cycle_status = db.execute(
            "SELECT status FROM audit_cycles WHERE id=?", (app_cycle_ids[0],),
        ).fetchone()[0]
        self.assertEqual(app_cycle_status, "DRAINED" if "z-app" in calls else "ACTIVE")
        self.assertEqual(db.execute(
            "SELECT status FROM audit_cycles WHERE id=?", (initial["id"],),
        ).fetchone()[0], "SCOPED_DRAINED")
        old_pending = db.execute(
            "SELECT project,status FROM audit_members WHERE cycle_id=? ORDER BY project",
            (initial["id"],),
        ).fetchall()
        expected_old = [(project, "pending") for project in PROJECTS]
        if first_status == "PUBLISHED":
            expected_old[0] = ("alpha", "published")
        self.assertEqual(old_pending, expected_old)
        self.assertLessEqual(len(calls), 4)
        self.assertEqual(len(calls), len(set(calls)))
        return calls, result

    def test_no_pending_cycle_switch_refreshes_same_drain_to_exact_app_cycle(self):
        calls, result = self._run_case("NO_PENDING")
        self.assertEqual(calls, ["alpha", "z-app"], result)
        self.assertEqual(result["batches"][-1]["status"], "PUBLISHED")

    def test_published_result_then_cycle_switch_refreshes_without_duplicate_project(self):
        calls, result = self._run_case("PUBLISHED")
        self.assertEqual(calls, ["alpha", "z-app"], result)
        self.assertEqual(result["batches"][-1]["status"], "PUBLISHED")


if __name__ == "__main__":
    unittest.main()
