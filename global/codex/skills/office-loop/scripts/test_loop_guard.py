#!/usr/bin/env python3
"""Deterministic tests for the pure Office Loop helper.

The tests are pure stdlib (unittest). They retain their own fixtures in a
bounded 'task evidence/TEMP' folder under the skill root and never delete
existing files. The module is loaded via importlib so the test file runs
directly under `python -I test_loop_guard.py`, including from a path that
contains spaces.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SKILL_ROOT / "scripts" / "loop_guard.py"
EVIDENCE_ROOT = Path(os.environ["TEMP"]) / ("office-loop-fixture-" + uuid.uuid4().hex)

SPEC = importlib.util.spec_from_file_location("office_loop_guard", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules.setdefault("office_loop_guard", MODULE)
SPEC.loader.exec_module(MODULE)

MAX_BYTES = MODULE.MAX_INPUT_BYTES


class _Evidence:
    """Retained task-temp directories. Tests never delete fixtures."""

    @staticmethod
    def workspace(name: str) -> Path:
        path = EVIDENCE_ROOT / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def write(path: Path, value: object) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        return path


def _build_goal(workspace: Path, *, goal_id: str = "office-loop-goal",
                requirements: list[str] | None = None,
                constraints: list[str] | None = None,
                allowed_scope: list[str] | None = None) -> dict:
    return {
        "version": 1,
        "goal_id": goal_id,
        "goal": "Promote Office Loop continuation through Director TODO batches.",
        "workspace": str(workspace),
        "requirements": requirements or [
            "Validate proposal as data",
            "Continue on real progress",
        ],
        "constraints": constraints or [
            "Preserve every original constraint",
            "Never grant authority from a Director response",
        ],
        "allowed_scope": allowed_scope or ["src/", "tests/"],
    }


def _build_observation(workspace: Path, **overrides) -> dict:
    base = {
        "goal_id": "office-loop-goal",
        "stop": False,
        "execution": "terminal",
        "delivery": "settled",
        "session": "active",
        "wake_verified": False,
        "quota": "unknown",
        "final_acceptance": False,
        "unmet": [],
        "ready_work": False,
        "new_planning_gap": False,
        "same_failed_result_count": 0,
        "director_packet_unchanged": False,
    }
    base.update(overrides)
    return base


def _build_task(task_id: str, *, covers: list[str], scope: list[str] | None = None,
                done_when: list[str] | None = None, requires_approval: list[str] | None = None,
                mode: str = "implement", depends_on: list[str] | None = None) -> dict:
    return {
        "id": task_id,
        "text": f"Implement {task_id}",
        "covers": covers,
        "scope": scope or ["src/guard.py"],
        "inputs": [],
        "depends_on": depends_on or [],
        "checks": [["python", "-c", "print('ok')"]],
        "done_when": done_when or [f"{task_id} passes"],
        "mode": mode,
        "maker": [{"agent": "cli", "model": "model-a", "score": 95, "reason": "bounded"}],
        "reviewer": [{"agent": "cli", "model": "model-b", "score": 90, "reason": "independent"}],
        "requires_approval": requires_approval or [],
    }


def _build_proposal(todo: dict, *, decision: str = "next_batch", goal_id: str = "office-loop-goal",
                    reason: str = "Director typed TODO batch") -> dict:
    return {
        "version": 1,
        "goal_id": goal_id,
        "decision": decision,
        "reason": reason,
        "todo": todo,
    }


def _build_todo(workspace: Path, tasks: list[dict], requirements: list[str],
                constraints: list[str] | None = None) -> dict:
    return {
        "version": 1,
        "goal": "Promote Office Loop continuation through Director TODO batches.",
        "workspace": str(workspace),
        "planner": {"model": "verified-director", "source": "run-20261001-stub"},
        "requirements": requirements,
        "constraints": constraints or [
            "Preserve every original constraint",
            "Never grant authority from a Director response",
        ],
        "max_parallel": 1,
        "tasks": tasks,
    }


class GoalAndObservationValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = _Evidence.workspace("validation")

    def test_version_and_tree_escape_are_not_coerced(self):
        for value in (True, "1", 1.0):
            goal = _build_goal(self.workspace)
            goal["version"] = value
            with self.assertRaises(MODULE.GuardError):
                MODULE.next_action(goal, _build_observation(self.workspace))
        for path in ("../", "C:/", "C:escape/", "src/../", "src\\..\\", "/absolute/"):
            goal = _build_goal(self.workspace, allowed_scope=[path])
            with self.assertRaises(MODULE.GuardError):
                MODULE.next_action(goal, _build_observation(self.workspace))

    def test_extra_constraints_and_actual_todo_fields_remain_compatible(self):
        goal = _build_goal(self.workspace)
        task = _build_task("batch-a", covers=[goal["requirements"][0]])
        task.update(complexity="medium", risk="low", timeout_seconds=60, min_tier_execute="W")
        todo = _build_todo(self.workspace, [task], requirements=[goal["requirements"][0]])
        todo["constraints"] = goal["constraints"] + ["Additional restriction"]
        result = MODULE.validate_proposal(goal, _build_proposal(todo))
        self.assertFalse(result["authority_granted"])

    def test_invalid_observation_unknown_field(self) -> None:
        goal = _build_goal(self.workspace)
        observation = _build_observation(self.workspace, garbage=True)
        with self.assertRaises(MODULE.GuardError):
            MODULE.next_action(goal, observation)

    def test_observation_must_be_bool_not_int(self) -> None:
        goal = _build_goal(self.workspace)
        observation = _build_observation(self.workspace, stop=1)
        with self.assertRaises(MODULE.GuardError):
            MODULE.next_action(goal, observation)
        observation = _build_observation(self.workspace, wake_verified="yes")
        with self.assertRaises(MODULE.GuardError):
            MODULE.next_action(goal, observation)

    def test_observation_goal_id_must_match(self) -> None:
        goal = _build_goal(self.workspace)
        observation = _build_observation(self.workspace, goal_id="different")
        with self.assertRaises(MODULE.GuardError):
            MODULE.next_action(goal, observation)

    def test_workspace_must_exist_and_be_absolute(self) -> None:
        bad = _build_goal(self.workspace)
        bad["workspace"] = "relative/path"
        observation = _build_observation(self.workspace)
        with self.assertRaises(MODULE.GuardError):
            MODULE.next_action(bad, observation)
        bad["workspace"] = str(self.workspace / "does-not-exist")
        with self.assertRaises(MODULE.GuardError):
            MODULE.next_action(bad, observation)

    def test_goal_allowed_scope_tree_boundary(self) -> None:
        goal = _build_goal(self.workspace, allowed_scope=["src/", "tests/"])
        bad = _build_goal(self.workspace, allowed_scope=[""])
        with self.assertRaises(MODULE.GuardError):
            MODULE.next_action(bad, _build_observation(self.workspace))


class NextActionBehavior(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = _Evidence.workspace("behavior")

    def test_cannot_accept_foreign_or_unfinished_goal(self):
        goal = _build_goal(self.workspace)
        for obs in (_build_observation(self.workspace, unmet=["invent extra work"]),
                    _build_observation(self.workspace, final_acceptance=True, unmet=goal["requirements"])):
            with self.assertRaises(MODULE.GuardError):
                MODULE.next_action(goal, obs)

    def test_unknown_delivery_never_requests_a_wake(self):
        goal = _build_goal(self.workspace)
        for field in ("execution", "delivery"):
            obs = _build_observation(self.workspace, session="ended", wake_verified=True, ready_work=True)
            obs[field] = "unknown"
            self.assertEqual(MODULE.next_action(goal, obs)["decision"], "reconcile_execution")

    def test_ready_work_precedes_expensive_planning(self):
        goal = _build_goal(self.workspace)
        obs = _build_observation(self.workspace, unmet=goal["requirements"], ready_work=True, new_planning_gap=True)
        self.assertEqual(MODULE.next_action(goal, obs)["decision"], "execute_current_batch")

    def test_stop_short_circuits(self) -> None:
        goal = _build_goal(self.workspace)
        action = MODULE.next_action(goal, _build_observation(self.workspace, stop=True))
        self.assertEqual(action["decision"], "stop")

    def test_quota_exhausted_short_circuits(self) -> None:
        goal = _build_goal(self.workspace)
        action = MODULE.next_action(goal, _build_observation(self.workspace, quota="exhausted"))
        self.assertEqual(action["decision"], "stop")

    def test_quota_unknown_does_not_block_ready_work(self) -> None:
        goal = _build_goal(self.workspace)
        action = MODULE.next_action(goal, _build_observation(
            self.workspace,
            quota="unknown",
            ready_work=True,
            unmet=["Continue on real progress"],
        ))
        self.assertEqual(action["decision"], "execute_current_batch")

    def test_unknown_execution_forces_reconcile(self) -> None:
        goal = _build_goal(self.workspace)
        action = MODULE.next_action(goal, _build_observation(self.workspace, execution="unknown"))
        self.assertEqual(action["decision"], "reconcile_execution")

    def test_inflight_delivery_forces_reconcile(self) -> None:
        goal = _build_goal(self.workspace)
        action = MODULE.next_action(goal, _build_observation(self.workspace, delivery="inflight"))
        self.assertEqual(action["decision"], "reconcile_execution")

    def test_active_session_with_active_execution_waits_on_handle(self) -> None:
        goal = _build_goal(self.workspace)
        action = MODULE.next_action(goal, _build_observation(self.workspace, execution="active"))
        self.assertEqual(action["decision"], "wait_on_existing_handle")

    def test_two_failures_diagnose(self) -> None:
        goal = _build_goal(self.workspace)
        action = MODULE.next_action(goal, _build_observation(
            self.workspace,
            unmet=["Continue on real progress"],
            same_failed_result_count=2,
        ))
        self.assertEqual(action["decision"], "diagnose")

    def test_unchanged_packet_reuses_advice(self) -> None:
        goal = _build_goal(self.workspace)
        action = MODULE.next_action(goal, _build_observation(
            self.workspace,
            unmet=["Validate proposal as data"],
            new_planning_gap=True,
            director_packet_unchanged=True,
        ))
        self.assertEqual(action["decision"], "reuse_advice")

    def test_changed_packet_consults_director(self) -> None:
        goal = _build_goal(self.workspace)
        action = MODULE.next_action(goal, _build_observation(
            self.workspace,
            unmet=["Continue on real progress"],
            new_planning_gap=True,
            director_packet_unchanged=False,
        ))
        self.assertEqual(action["decision"], "consult_director")

    def test_ready_work_executes_current_batch(self) -> None:
        goal = _build_goal(self.workspace)
        action = MODULE.next_action(goal, _build_observation(
            self.workspace,
            unmet=["Continue on real progress"],
            ready_work=True,
        ))
        self.assertEqual(action["decision"], "execute_current_batch")

    def test_partial_progress_without_invented_budget(self) -> None:
        goal = _build_goal(self.workspace)
        action = MODULE.next_action(goal, _build_observation(
            self.workspace,
            unmet=["Validate proposal as data", "Continue on real progress"],
            ready_work=False,
            new_planning_gap=False,
        ))
        self.assertEqual(action["decision"], "wait")

    def test_no_unmet_without_acceptance_requests_check(self) -> None:
        goal = _build_goal(self.workspace)
        action = MODULE.next_action(goal, _build_observation(
            self.workspace, unmet=[], final_acceptance=False,
        ))
        self.assertEqual(action["decision"], "final_acceptance_check")

    def test_complete_after_attested_acceptance(self) -> None:
        goal = _build_goal(self.workspace)
        action = MODULE.next_action(goal, _build_observation(
            self.workspace, unmet=[], final_acceptance=True,
        ))
        self.assertEqual(action["decision"], "complete")

    def test_session_ended_without_wake_waits(self) -> None:
        goal = _build_goal(self.workspace)
        action = MODULE.next_action(goal, _build_observation(
            self.workspace,
            session="ended", wake_verified=False, ready_work=True,
        ))
        self.assertEqual(action["decision"], "WAITING_NATIVE_WAKE")

    def test_session_ended_with_wake_and_work_requests_verified(self) -> None:
        goal = _build_goal(self.workspace)
        action = MODULE.next_action(goal, _build_observation(
            self.workspace,
            session="ended", wake_verified=True, ready_work=True,
        ))
        self.assertEqual(action["decision"], "request_verified_wake")

    def test_session_ended_with_wake_but_no_work_waits(self) -> None:
        goal = _build_goal(self.workspace)
        action = MODULE.next_action(goal, _build_observation(
            self.workspace,
            session="ended", wake_verified=True, ready_work=False,
        ))
        self.assertEqual(action["decision"], "WAITING_NATIVE_WAKE")

    def test_session_unknown_is_wake_wait(self) -> None:
        goal = _build_goal(self.workspace)
        action = MODULE.next_action(goal, _build_observation(
            self.workspace, session="unknown", ready_work=True,
        ))
        self.assertEqual(action["decision"], "WAITING_NATIVE_WAKE")


class FullContinuationTwoBatches(unittest.TestCase):
    """Useful A/B then decoy: no third Director call after caller-attested acceptance."""

    def setUp(self) -> None:
        self.workspace = _Evidence.workspace("full-continuation")
        self.goal = _build_goal(self.workspace)

    def test_two_batches_then_decoy_without_third_call(self) -> None:
        director_calls = 0

        def stub_director(unmet: list[str]) -> dict:
            nonlocal director_calls
            director_calls += 1
            if unmet == ["Validate proposal as data"]:
                tasks = [_build_task("validate-proposal", covers=["Validate proposal as data"],
                                     scope=["src/guard.py"], done_when=["validate passes"])]
                return _build_proposal(_build_todo(self.workspace, tasks,
                                                   requirements=["Validate proposal as data"]))
            if unmet == ["Continue on real progress"]:
                tasks = [_build_task("continue-progress", covers=["Continue on real progress"],
                                     scope=["src/loop.py"], done_when=["loop advances"])]
                return _build_proposal(_build_todo(self.workspace, tasks,
                                                   requirements=["Continue on real progress"]))
            raise AssertionError(f"stub Director must not be called for unmet={unmet}")

        # 1) First planning gap -> consult_director
        action = MODULE.next_action(self.goal, _build_observation(
            self.workspace,
            unmet=["Validate proposal as data"],
            new_planning_gap=True, director_packet_unchanged=False,
        ))
        self.assertEqual(action["decision"], "consult_director")
        proposal_a = stub_director(["Validate proposal as data"])
        validated = MODULE.validate_proposal(self.goal, proposal_a)
        self.assertEqual(validated["decision"], "next_batch")
        self.assertFalse(validated["authority_granted"])
        self.assertFalse(validated["dispatch"])

        # 2) After batch A: unmet is the other requirement -> consult_director
        action = MODULE.next_action(self.goal, _build_observation(
            self.workspace,
            unmet=["Continue on real progress"],
            new_planning_gap=True, director_packet_unchanged=False,
        ))
        self.assertEqual(action["decision"], "consult_director")
        proposal_b = stub_director(["Continue on real progress"])
        MODULE.validate_proposal(self.goal, proposal_b)

        # 3) Caller attests completion: helper returns complete without consulting
        action = MODULE.next_action(self.goal, _build_observation(
            self.workspace, unmet=[], final_acceptance=True,
        ))
        self.assertEqual(action["decision"], "complete")

        # Even if a decoy slipped in, validate_proposal rejects foreign requirements.
        decoy = {
            "version": 1, "goal_id": self.goal["goal_id"], "decision": "next_batch",
            "reason": "decoy unrelated work",
            "todo": _build_todo(
                self.workspace,
                [_build_task("decoy", covers=["Unrelated foreign requirement"],
                             scope=["src/x.py"], done_when=["x passes"])],
                requirements=["Unrelated foreign requirement"],
            ),
        }
        with self.assertRaises(MODULE.GuardError) as ctx:
            MODULE.validate_proposal(self.goal, decoy)
        self.assertIn("foreign requirements", str(ctx.exception))
        # Director should still have been called only twice for the useful batches.
        self.assertEqual(director_calls, 2)


class ProposalValidationGuards(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = _Evidence.workspace("proposal-guards")
        self.goal = _build_goal(self.workspace,
                                requirements=["Reject malformed input"],
                                allowed_scope=["src/parser.py"])

    def _minimal_proposal(self, workspace: Path) -> dict:
        tasks = [_build_task("malformed-input",
                             covers=["Reject malformed input"],
                             scope=["src/parser.py"],
                             done_when=["rejects malformed input"])]
        todo = _build_todo(workspace, tasks, requirements=["Reject malformed input"])
        return _build_proposal(todo)

    def test_minimal_proposal_passes(self) -> None:
        result = MODULE.validate_proposal(self.goal, self._minimal_proposal(self.workspace))
        self.assertEqual(result["decision"], "next_batch")
        self.assertTrue(result["advisory_only"] is False)
        self.assertFalse(result["authority_granted"])
        self.assertFalse(result["dispatch"])

    def test_changed_goal_text_rejected(self) -> None:
        proposal = self._minimal_proposal(self.workspace)
        proposal["todo"]["goal"] = "Modified goal text"
        with self.assertRaises(MODULE.GuardError) as ctx:
            MODULE.validate_proposal(self.goal, proposal)
        self.assertIn("goal", str(ctx.exception))

    def test_foreign_workspace_rejected(self) -> None:
        proposal = self._minimal_proposal(self.workspace)
        other = _Evidence.workspace("proposal-guards-other")
        proposal["todo"]["workspace"] = str(other)
        with self.assertRaises(MODULE.GuardError):
            MODULE.validate_proposal(self.goal, proposal)

    def test_foreign_requirement_rejected(self) -> None:
        proposal = self._minimal_proposal(self.workspace)
        proposal["todo"]["requirements"] = ["Reject malformed input", "Invented requirement"]
        proposal["todo"]["tasks"][0]["covers"] = ["Reject malformed input", "Invented requirement"]
        with self.assertRaises(MODULE.GuardError) as ctx:
            MODULE.validate_proposal(self.goal, proposal)
        self.assertIn("foreign", str(ctx.exception))

    def test_traversal_in_scope_rejected(self) -> None:
        proposal = self._minimal_proposal(self.workspace)
        proposal["todo"]["tasks"][0]["scope"] = ["../escape.py"]
        with self.assertRaises(MODULE.GuardError) as ctx:
            MODULE.validate_proposal(self.goal, proposal)
        self.assertIn("traversal", str(ctx.exception).lower())

    def test_absolute_scope_rejected_on_windows_and_posix(self) -> None:
        proposal = self._minimal_proposal(self.workspace)
        proposal["todo"]["tasks"][0]["scope"] = ["/etc/passwd"]
        with self.assertRaises(MODULE.GuardError):
            MODULE.validate_proposal(self.goal, proposal)
        proposal["todo"]["tasks"][0]["scope"] = ["C:\\Windows\\evil.py"]
        with self.assertRaises(MODULE.GuardError):
            MODULE.validate_proposal(self.goal, proposal)

    def test_outside_tree_scope_rejected(self) -> None:
        proposal = self._minimal_proposal(self.workspace)
        proposal["todo"]["tasks"][0]["scope"] = ["src/parser_extra.py"]
        with self.assertRaises(MODULE.GuardError) as ctx:
            MODULE.validate_proposal(self.goal, proposal)
        self.assertIn("allowed_scope", str(ctx.exception))

    def test_tree_scope_allows_inner_files(self) -> None:
        self.goal = _build_goal(self.workspace,
                                requirements=["Reject malformed input"],
                                allowed_scope=["src/", "tests/"])
        proposal = self._minimal_proposal(self.workspace)
        proposal["todo"]["tasks"][0]["scope"] = ["src/parser.py"]
        result = MODULE.validate_proposal(self.goal, proposal)
        self.assertEqual(result["decision"], "next_batch")

    def test_removed_constraint_rejected(self) -> None:
        proposal = self._minimal_proposal(self.workspace)
        proposal["todo"]["constraints"] = ["Preserve every original constraint"]
        with self.assertRaises(MODULE.GuardError) as ctx:
            MODULE.validate_proposal(self.goal, proposal)
        self.assertIn("constraint", str(ctx.exception).lower())

    def test_missing_planner_rejected(self) -> None:
        proposal = self._minimal_proposal(self.workspace)
        proposal["todo"]["planner"] = {}
        with self.assertRaises(MODULE.GuardError):
            MODULE.validate_proposal(self.goal, proposal)

    def test_max_parallel_must_be_positive_int(self) -> None:
        proposal = self._minimal_proposal(self.workspace)
        proposal["todo"]["max_parallel"] = True
        with self.assertRaises(MODULE.GuardError):
            MODULE.validate_proposal(self.goal, proposal)
        proposal["todo"]["max_parallel"] = 0
        with self.assertRaises(MODULE.GuardError):
            MODULE.validate_proposal(self.goal, proposal)
        proposal["todo"]["max_parallel"] = 1.5
        with self.assertRaises(MODULE.GuardError):
            MODULE.validate_proposal(self.goal, proposal)

    def test_empty_implement_checks_rejected(self) -> None:
        proposal = self._minimal_proposal(self.workspace)
        proposal["todo"]["tasks"][0]["checks"] = []
        with self.assertRaises(MODULE.GuardError) as ctx:
            MODULE.validate_proposal(self.goal, proposal)
        self.assertIn("checks", str(ctx.exception))

    def test_missing_candidate_score_rejected(self) -> None:
        proposal = self._minimal_proposal(self.workspace)
        proposal["todo"]["tasks"][0]["maker"][0].pop("score")
        with self.assertRaises(MODULE.GuardError):
            MODULE.validate_proposal(self.goal, proposal)

    def test_candidate_score_out_of_range_rejected(self) -> None:
        proposal = self._minimal_proposal(self.workspace)
        proposal["todo"]["tasks"][0]["maker"][0]["score"] = 87
        with self.assertRaises(MODULE.GuardError):
            MODULE.validate_proposal(self.goal, proposal)
        proposal["todo"]["tasks"][0]["maker"][0]["score"] = 101
        with self.assertRaises(MODULE.GuardError):
            MODULE.validate_proposal(self.goal, proposal)

    def test_requires_approval_rejected_with_owner_decision(self) -> None:
        proposal = self._minimal_proposal(self.workspace)
        proposal["todo"]["tasks"][0]["requires_approval"] = ["owner sign-off"]
        with self.assertRaises(MODULE.GuardError) as ctx:
            MODULE.validate_proposal(self.goal, proposal)
        self.assertIn("OWNER_DECISION_REQUIRED", str(ctx.exception))

    def test_cycle_rejected(self) -> None:
        proposal = self._minimal_proposal(self.workspace)
        second = _build_task("second", covers=["Reject malformed input"],
                             scope=["src/parser.py"], done_when=["second ok"],
                             depends_on=["malformed-input"])
        first = _build_task("malformed-input", covers=["Reject malformed input"],
                            scope=["src/parser.py"], done_when=["first ok"],
                            depends_on=["second"])
        proposal["todo"]["tasks"] = [first, second]
        with self.assertRaises(MODULE.GuardError) as ctx:
            MODULE.validate_proposal(self.goal, proposal)
        self.assertIn("cycle", str(ctx.exception).lower())

    def test_unknown_dependency_rejected(self) -> None:
        proposal = self._minimal_proposal(self.workspace)
        proposal["todo"]["tasks"][0]["depends_on"] = ["nope"]
        with self.assertRaises(MODULE.GuardError):
            MODULE.validate_proposal(self.goal, proposal)

    def test_coverage_must_equal_proposal_requirements(self) -> None:
        self.goal = _build_goal(self.workspace,
                                requirements=["Reject malformed input", "Plus one"],
                                allowed_scope=["src/parser.py"])
        proposal = self._minimal_proposal(self.workspace)
        proposal["todo"]["requirements"] = ["Reject malformed input", "Plus one"]
        proposal["todo"]["tasks"][0]["covers"] = ["Reject malformed input"]
        with self.assertRaises(MODULE.GuardError) as ctx:
            MODULE.validate_proposal(self.goal, proposal)
        self.assertIn("coverage", str(ctx.exception).lower())

    def test_blocked_decision_is_advisory_only(self) -> None:
        proposal = {
            "version": 1, "goal_id": self.goal["goal_id"], "decision": "blocked",
            "reason": "Owner has not approved the migration target yet.",
        }
        result = MODULE.validate_proposal(self.goal, proposal)
        self.assertEqual(result["decision"], "blocked")
        self.assertTrue(result["advisory_only"])
        self.assertFalse(result["authority_granted"])
        self.assertFalse(result["dispatch"])

    def test_complete_decision_is_advisory_only(self) -> None:
        proposal = {
            "version": 1, "goal_id": self.goal["goal_id"], "decision": "complete",
            "reason": "Run the real final acceptance test; the helper grants no authority.",
        }
        result = MODULE.validate_proposal(self.goal, proposal)
        self.assertEqual(result["decision"], "complete")
        self.assertTrue(result["advisory_only"])
        self.assertFalse(result["authority_granted"])


class _Junction:
    """Create a directory junction on Windows to simulate a symlink escape.

    On non-Windows hosts the test falls back to creating a real symlink via
    os.symlink when possible; if neither is available the test is skipped.
    """

    @staticmethod
    def create(link: Path, target: Path) -> bool:
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            if hasattr(os, "symlink"):
                os.symlink(str(target), str(link), target_is_directory=True)
                return True
        except (OSError, NotImplementedError):
            pass
        if sys.platform.startswith("win"):
            try:
                import subprocess as sp
                completed = sp.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                    capture_output=True, text=True, check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if completed.returncode == 0 and link.exists():
                    return True
            except OSError:
                return False
        return False


class JunctionEscapeRejection(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = _Evidence.workspace("junction-escape")
        # Create a separate target directory; a junction will point to it.
        self.outside = _Evidence.workspace("junction-escape-outside")

    def test_scope_resolved_via_junction_is_rejected(self) -> None:
        link = self.workspace / "inside" / "junction"
        if not _Junction.create(link, self.outside):
            self.skipTest("cannot create symlink/junction in this environment")
        goal = _build_goal(self.workspace,
                           requirements=["Reject malformed input"],
                           allowed_scope=["inside/"])
        proposal_proposal = {
            "version": 1,
            "goal_id": goal["goal_id"],
            "decision": "next_batch",
            "reason": "test junction escape",
            "todo": _build_todo(
                self.workspace,
                [_build_task("malformed-input",
                             covers=["Reject malformed input"],
                             scope=["inside/junction/evil.py"],
                             done_when=["rejects"])],
                requirements=["Reject malformed input"],
            ),
        }
        with self.assertRaises(MODULE.GuardError) as ctx:
            MODULE.validate_proposal(goal, proposal_proposal)
        self.assertIn("escapes", str(ctx.exception).lower())


class IOAndCLIBehavior(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = _Evidence.workspace("io-cli")
        self.goal = _build_goal(self.workspace)
        self.evidence_dir = _Evidence.workspace("io-cli/io")

    def _write_input(self, name: str, payload: dict, *, inflate: bool = False) -> Path:
        path = self.evidence_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if inflate:
            text = text + " " * (MAX_BYTES + 1024)
        path.write_text(text, encoding="utf-8")
        return path

    def _run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = MODULE.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_decide_returns_decision(self) -> None:
        goal_path = self._write_input("goal.json", self.goal)
        observation_path = self._write_input("obs.json", _build_observation(
            self.workspace, unmet=["Continue on real progress"], ready_work=True,
        ))
        code, stdout, stderr = self._run_cli(
            "decide", "--goal", str(goal_path), "--observation", str(observation_path),
        )
        self.assertEqual(code, 0, msg=stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["decision"], "execute_current_batch")

    def test_cli_validate_returns_decision(self) -> None:
        goal_path = self._write_input("goal.json", self.goal)
        proposal = _build_proposal(_build_todo(
            self.workspace,
            [_build_task("a", covers=["Validate proposal as data"], scope=["src/guard.py"],
                         done_when=["ok"])],
            requirements=["Validate proposal as data"],
        ))
        proposal_path = self._write_input("proposal.json", proposal)
        code, stdout, stderr = self._run_cli(
            "validate", "--goal", str(goal_path), "--proposal", str(proposal_path),
        )
        self.assertEqual(code, 0, msg=stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["decision"], "next_batch")
        self.assertFalse(payload["dispatch"])

    def test_cli_rejects_oversized_input(self) -> None:
        goal_path = self._write_input("goal.json", self.goal, inflate=True)
        observation_path = self._write_input("obs.json", _build_observation(self.workspace))
        code, stdout, stderr = self._run_cli(
            "decide", "--goal", str(goal_path), "--observation", str(observation_path),
        )
        self.assertNotEqual(code, 0)
        payload = json.loads(stderr)
        self.assertEqual(payload["code"], "GUARD_REJECTED")
        self.assertIn("limit", payload["error"].lower())

    def test_cli_rejects_invalid_observation(self) -> None:
        goal_path = self._write_input("goal.json", self.goal)
        observation = _build_observation(self.workspace, stop=1)
        observation_path = self._write_input("obs.json", observation)
        code, _, stderr = self._run_cli(
            "decide", "--goal", str(goal_path), "--observation", str(observation_path),
        )
        self.assertNotEqual(code, 0)
        payload = json.loads(stderr)
        self.assertEqual(payload["code"], "GUARD_REJECTED")

    def test_subprocess_run_with_path_with_spaces(self) -> None:
        spaced_dir = _Evidence.workspace("path with spaces")
        goal_path = spaced_dir / "goal.json"
        observation_path = spaced_dir / "observation.json"
        workspace = _Evidence.workspace("path with spaces" + os.sep + "ws")
        goal = _build_goal(workspace, goal_id="office-loop-goal")
        _Evidence.write(goal_path, goal)
        _Evidence.write(observation_path, _build_observation(
            workspace, unmet=[], final_acceptance=True,
        ))
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [sys.executable, "-I", str(SCRIPT_PATH),
             "decide", "--goal", str(goal_path), "--observation", str(observation_path)],
            capture_output=True, text=True, env=env, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["decision"], "complete")


class LeaseIsNotNativeState(unittest.TestCase):
    """Pure helpers must not infer native activity from a lease alone."""

    def setUp(self) -> None:
        self.workspace = _Evidence.workspace("lease")

    def test_lease_only_session_ended_yields_wait(self) -> None:
        goal = _build_goal(self.workspace)
        action = MODULE.next_action(goal, _build_observation(
            self.workspace,
            session="ended",
            wake_verified=False,
            ready_work=True,
        ))
        self.assertEqual(action["decision"], "WAITING_NATIVE_WAKE")

    def test_lease_only_session_unknown_yields_wait(self) -> None:
        goal = _build_goal(self.workspace)
        action = MODULE.next_action(goal, _build_observation(
            self.workspace,
            session="unknown",
            wake_verified=True,
            ready_work=True,
        ))
        self.assertEqual(action["decision"], "WAITING_NATIVE_WAKE")


if __name__ == "__main__":
    unittest.main()
