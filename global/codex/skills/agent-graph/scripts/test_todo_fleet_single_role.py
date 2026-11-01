"""HD-3af7ce: single-role TODO tasks must validate only their active lane.

A documented planning request carries a maker lane only (no reviewer field).
Before the fix, todo_fleet.execute validated both lanes unconditionally, so a
single-role task died with a KeyError on the missing lane before any Fleet
dispatch. The call site now validates the active role only for a single-role
task; a task that declares BOTH lanes still has every declared lane validated
before dispatch, and the whole-plan validator keeps checking both lanes.
"""
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


SCRIPTS = Path(__file__).parent
sys.path.insert(0, str(SCRIPTS))
os.environ["AGENT_GRAPH_WATCH_EVENTS"] = "0"

# Reuse the existing offline fleet double; do not subclass TodoFleetTests.
import test_todo_fleet

FakeFleet = test_todo_fleet.FakeFleet
todo_fleet = test_todo_fleet.todo_fleet

PLANNING_REQUEST = SCRIPTS.parent / "references" / "planning-request.json"


class SingleRoleTodoFleetTests(unittest.TestCase):
    def setUp(self):
        # Keep the legacy quota-headroom choice so the test stays offline.
        self._tier_env = os.environ.get("AGENT_GRAPH_TIER_ROUTING")
        os.environ["AGENT_GRAPH_TIER_ROUTING"] = "0"
        self.addCleanup(self._restore_tier_env)
        self.temp_context = TemporaryDirectory()
        self.addCleanup(self.temp_context.cleanup)
        self.root = Path(self.temp_context.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.home_patch = patch.object(Path, "home", return_value=self.root)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def _restore_tier_env(self):
        if self._tier_env is None:
            os.environ.pop("AGENT_GRAPH_TIER_ROUTING", None)
        else:
            os.environ["AGENT_GRAPH_TIER_ROUTING"] = self._tier_env

    def candidate(self, agent, model="m-model", score=80):
        return {"agent": agent, "model": model, "reason": "test fit", "score": score}

    def single_role_task(self, role, *, mode="advisory"):
        """A task that declares exactly one lane, like the planning request."""
        task = {
            "id": "slice-1",
            "text": "plan one slice",
            "mode": mode,
            "scope": ["src"],
            "done_when": ["evidence"],
            "complexity": "low",
            "review_complexity": "high",
        }
        task[role] = [self.candidate(role)]
        return task

    def paired_task(self, mode="advisory"):
        """A task that declares BOTH lanes, like a full plan."""
        task = {
            "id": "slice-1",
            "text": "plan one slice",
            "mode": mode,
            "scope": ["src"],
            "done_when": ["evidence"],
            "complexity": "low",
            "review_complexity": "high",
            "maker": [self.candidate("maker")],
            "reviewer": [self.candidate("reviewer")],
        }
        return task

    def request(self, role, task):
        return {"goal": "test bridge", "role": role, "workspace": str(self.workspace),
                "task": task}

    def execute(self, request, fleet, name="out"):
        outdir = self.root / name
        outdir.mkdir(exist_ok=True)
        return todo_fleet.execute(request, outdir, fleet=fleet)

    def test_documented_planning_request_has_no_reviewer_lane(self):
        document = json.loads(PLANNING_REQUEST.read_text(encoding="utf-8"))
        self.assertNotIn("reviewer", document["task"])
        self.assertIn("maker", document["task"])

    def test_maker_only_task_reaches_fleet_dispatch(self):
        fleet = FakeFleet()
        task = self.single_role_task("maker")
        self.execute(self.request("maker", task), fleet, name="maker-only")
        self.assertEqual([call["agent_id"] for call in fleet.execute_calls], ["maker"])

    def test_reviewer_only_task_reaches_fleet_dispatch(self):
        fleet = FakeFleet()
        task = self.single_role_task("reviewer")
        self.execute(self.request("reviewer", task), fleet, name="reviewer-only")
        self.assertEqual([call["agent_id"] for call in fleet.execute_calls], ["reviewer"])

    def test_unsupported_active_role_mode_is_rejected_before_dispatch(self):
        class ModeLimitedFleet(FakeFleet):
            def load_profiles(self, default):
                return {"maker": {"adapter": "claude", "supported_modes": ["review", "advisory"]},
                        "reviewer": {"adapter": "qwen", "supported_modes": ["review", "advisory"]}}

        fleet = ModeLimitedFleet()
        task = self.single_role_task("maker", mode="implement")
        with self.assertRaisesRegex(ValueError, "lacks supported_modes=implement"):
            self.execute(self.request("maker", task), fleet, name="mode-rejected")
        self.assertEqual(fleet.execute_calls, [])

    def test_unknown_active_candidate_is_rejected_before_dispatch(self):
        fleet = FakeFleet()
        task = self.single_role_task("maker")
        task["maker"] = [self.candidate("ghost-agent")]
        with self.assertRaisesRegex(ValueError, "Unknown candidate agent"):
            self.execute(self.request("maker", task), fleet, name="unknown-agent")
        self.assertEqual(fleet.execute_calls, [])

    def test_paired_request_rejects_invalid_inactive_maker_on_review(self):
        class MakerWithoutAdvisoryFleet(FakeFleet):
            def load_profiles(self, default):
                return {"maker": {"adapter": "claude", "supported_modes": ["implement", "review"]},
                        "reviewer": {"adapter": "qwen", "supported_modes": ["review", "advisory"]}}

        fleet = MakerWithoutAdvisoryFleet()
        task = self.paired_task(mode="advisory")
        with self.assertRaisesRegex(ValueError, "maker maker lacks supported_modes=advisory"):
            self.execute(self.request("reviewer", task), fleet, name="paired-bad-maker")
        self.assertEqual(fleet.execute_calls, [])

    def test_paired_request_rejects_invalid_inactive_reviewer_on_maker(self):
        class ReviewerWithoutReviewFleet(FakeFleet):
            def load_profiles(self, default):
                return {"maker": {"adapter": "claude", "supported_modes": ["implement", "review", "advisory"]},
                        "reviewer": {"adapter": "qwen", "supported_modes": ["advisory"]}}

        fleet = ReviewerWithoutReviewFleet()
        task = self.paired_task(mode="advisory")
        with self.assertRaisesRegex(ValueError, "reviewer reviewer lacks supported_modes=review"):
            self.execute(self.request("maker", task), fleet, name="paired-bad-reviewer")
        self.assertEqual(fleet.execute_calls, [])

    def test_whole_plan_validation_still_checks_both_lanes(self):
        profiles = FakeFleet().load_profiles(None)
        task = {
            "id": "slice-1", "text": "plan one slice", "mode": "advisory",
            "scope": ["src"], "done_when": ["evidence"],
            "maker": [self.candidate("maker")],
            "reviewer": [self.candidate("ghost-agent")],
        }
        with self.assertRaisesRegex(ValueError, "Unknown candidate agent: ghost-agent"):
            todo_fleet.validate_candidate_modes(task, profiles)
        task["reviewer"] = [self.candidate("reviewer")]
        todo_fleet.validate_candidate_modes(task, profiles)


if __name__ == "__main__":
    unittest.main()
