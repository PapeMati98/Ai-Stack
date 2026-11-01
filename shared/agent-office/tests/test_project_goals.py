"""I6 goals retain state across callers while banners stay informational."""

import shutil
import unittest
import uuid
from pathlib import Path

from coordfs.errors import CoordError
from coordfs.project_goals import ProjectGoals


class ProjectGoalsTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(__file__).parent / f"_goals_test_{uuid.uuid4().hex}"
        self.home.mkdir()
        self.addCleanup(shutil.rmtree, self.home)
        self.goals = ProjectGoals(self.home)

    def test_blocked_and_owner_banners_are_scoped_and_clear_on_unblock(self):
        self.assertEqual(self.goals.banner(["agent-stack"])["project_banners"], [])
        self.goals.add({"goal_id": "g1", "project_slug": "agent-stack", "title": "Restore import", "status": "blocked", "blocked_reason": "Fixture review pending", "unblock_by": "reviewer"})
        self.goals.add({"goal_id": "g2", "project_slug": "northwind", "title": "Owner choice", "status": "needs_owner", "question_ref": "docs/decision-1.md"})
        self.goals.add({"goal_id": "g3", "project_slug": "agent-stack", "title": "Normal goal"})
        banner = self.goals.banner(["agent-stack"])["project_banners"]
        self.assertEqual(len(banner), 1)
        self.assertEqual(banner[0]["blocked_goals"][0]["goal_id"], "g1")
        self.assertEqual(banner[0]["pending_owner_decisions"], [])
        self.assertLessEqual(len(banner[0]["text"]), 300)
        self.assertEqual(self.goals.banner(["unlinked-project"])["project_banners"], [])
        self.assertEqual(self.goals.banner(["northwind"])["project_banners"][0]["pending_owner_decisions"], [{"id": "g2", "question_ref": "docs/decision-1.md"}])
        self.goals.update("g1", {"status": "in_progress"})
        self.assertEqual(self.goals.banner(["agent-stack"])["project_banners"], [])

    def test_attempts_budget_restart_and_deduplication(self):
        self.goals.add({"goal_id": "g1", "project_slug": "agent-stack", "title": "Test", "daily_budget": {"date": "2026-10-05", "limit": 4, "used": 1}})
        first = "a" * 64
        self.goals.update("g1", {"status": "in_progress", "input_hash": first, "progress_at": "2026-10-05T01:00:00Z", "daily_budget": {"date": "2026-10-05", "limit": 4, "used": 2}})
        restarted = ProjectGoals(self.home)
        restarted.update("g1", {"input_hash": first})
        self.assertEqual(restarted.get("g1")["goal"]["attempts"]["count"], 1)
        restarted.update("g1", {"input_hash": "b" * 64})
        goal = restarted.get("g1")["goal"]
        self.assertEqual(goal["attempts"], {"count": 2, "last_inputs_hash": "b" * 64, "last_progress_at": "2026-10-05T01:00:00Z"})
        self.assertEqual(goal["daily_budget"], {"date": "2026-10-05", "limit": 4, "used": 2})

    def test_reject_invalid_transitions_and_content_fields_without_writes(self):
        self.goals.add({"goal_id": "g1", "project_slug": "agent-stack", "title": "Test"})
        before = self.goals.path.read_bytes()
        for changes in ({"body": "private"}, {"prompt": "private"}, {"contacts": "private"}, {"style": "private"}, {"status": "done"}, {"status": "blocked", "blocked_reason": "x" * 301}, {"status": "needs_owner", "question_ref": "https://example.com"}):
            with self.subTest(changes=changes), self.assertRaises(CoordError):
                self.goals.update("g1", changes)
            self.assertEqual(self.goals.path.read_bytes(), before)
        self.goals.update("g1", {"status": "in_progress"})
        self.goals.update("g1", {"status": "verify"})
        self.goals.update("g1", {"status": "done"})
        with self.assertRaises(CoordError) as transition:
            self.goals.update("g1", {"status": "in_progress"})
        self.assertEqual(transition.exception.code, "invalid_transition")

    def test_frozen_store_and_corruption(self):
        self.goals.root.mkdir(parents=True)
        self.goals.path.write_text("{bad", encoding="utf-8")
        with self.assertRaises(CoordError) as corrupt:
            self.goals.list()
        self.assertEqual(corrupt.exception.code, "goals_corrupt")
        (self.home / "FROZEN").write_text("frozen", encoding="utf-8")
        with self.assertRaises(CoordError) as frozen:
            self.goals.banner(["agent-stack"])
        self.assertEqual(frozen.exception.code, "store_frozen")


if __name__ == "__main__":
    unittest.main()
