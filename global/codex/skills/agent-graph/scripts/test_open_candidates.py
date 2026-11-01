"""Tests for open-candidate enrichment, explicit model pinning, and policy validation."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import model_tiers
import todo_fleet
import todo_graph
from test_model_tiers import Base, CATALOG, TodoFleetIntegrationTests
from test_todo_fleet import FakeFleet


class OpenCandidatesSelectionTests(TodoFleetIntegrationTests):
    def test_compare_fixed_generic_case_narrow_gains_family_when_open(self):
        """Fixed generic case: narrow input gains qualified family under candidate_policy: 'open'."""
        self.add_runs("busy", "k-busy", 40)
        self.add_runs("idle", "k-idle", 2)
        fleet = self.fleet(["busy", "idle"])
        cands = [{"agent": "busy", "model": "k-busy", "reason": "narrow", "score": 85}]

        # Before / legacy (policy absent): narrow input stays narrow, busy is picked
        res_legacy, out_legacy = self.run_node(cands, fleet, task_extra={"min_tier_execute": "K"})
        self.assertEqual(res_legacy["status"], "CANDIDATE")
        self.assertEqual(fleet.execute_calls[0]["agent_id"], "busy")

        # After / open: narrow input gains qualified same-class family (idle);
        # recency balancing prefers less-used idle over busy!
        fleet_open = self.fleet(["busy", "idle"])
        res_open, out_open = self.run_node(
            cands, fleet_open, task_extra={"min_tier_execute": "K", "candidate_policy": "open"}
        )
        self.assertEqual(res_open["status"], "CANDIDATE")
        self.assertEqual(fleet_open.execute_calls[0]["agent_id"], "idle")
        sel = json.loads((out_open / "tier-selection.json").read_text(encoding="utf-8"))
        ordered_agents = [r["agent"] for r in sel["ordered"]]
        self.assertIn("busy", ordered_agents)
        self.assertIn("idle", ordered_agents)
        self.assertEqual(sel["ordered"][0]["agent"], "idle")
        self.assertEqual(sel["candidate_policy"], "open")

    def test_compare_fixed_generic_case_pinned_exact_stays_exact(self):
        """Fixed generic case: pinned exact stays exact under candidate_policy: 'pin'."""
        self.add_runs("busy", "k-busy", 40)
        self.add_runs("idle", "k-idle", 2)
        fleet = self.fleet(["busy", "idle"])
        cands = [{"agent": "busy", "model": "k-busy", "reason": "exact pinned", "score": 85}]

        res_pin, out_pin = self.run_node(
            cands, fleet, task_extra={"min_tier_execute": "K", "candidate_policy": "pin"}
        )
        self.assertEqual(res_pin["status"], "CANDIDATE")
        # busy must stay exact despite higher recent usage and available idle peer!
        self.assertEqual(fleet.execute_calls[0]["agent_id"], "busy")
        sel = json.loads((out_pin / "tier-selection.json").read_text(encoding="utf-8"))
        ordered_agents = [r["agent"] for r in sel["ordered"]]
        self.assertEqual(ordered_agents, ["busy"])
        self.assertNotIn("idle", ordered_agents)
        self.assertEqual(sel["candidate_policy"], "pin")

    def test_failed_pinned_does_not_fallback(self):
        """When a pinned model fails or is disallowed, it never falls back to catalog substitution."""
        fleet = self.fleet(["claude", "idle", "busy", "worker"])
        # claude/old is DENY in CATALOG
        cands = [{"agent": "claude", "model": "old", "reason": "denied", "score": 90}]

        # Under legacy absent policy, it would substitute from the catalog
        res_legacy, out_legacy = self.run_node(cands, fleet, task_extra={"min_tier_execute": "K"})
        self.assertEqual(res_legacy["status"], "CANDIDATE")
        self.assertIn(fleet.execute_calls[0]["agent_id"], {"idle", "busy"})

        # Under candidate_policy: 'pin', it MUST NOT fallback; must return BLOCKED!
        fleet_pin = self.fleet(["claude", "idle", "busy", "worker"])
        res_pin, out_pin = self.run_node(
            cands, fleet_pin, task_extra={"min_tier_execute": "K", "candidate_policy": "pin"}
        )
        self.assertEqual(res_pin["status"], "BLOCKED")
        self.assertEqual(fleet_pin.execute_calls, [])
        self.assertIn("Pinned candidate not available or allowed", res_pin["reason"])

    def test_failed_pinned_exhausted_does_not_fallback(self):
        """Pinned model with exhausted pool does not substitute."""
        fleet = self.fleet(["busy", "idle"])
        cands = [{"agent": "busy", "model": "k-busy", "reason": "pinned", "score": 90}]
        # Mark busy as exhausted
        fleet.current_quota_views = lambda root, profiles: (
            {a: ({'availability': {'status': 'exhausted', 'source': 'native-provider-error'},
                  'availability_blocked': True} if a == "busy" else dict(fleet.quota))
             for a in profiles},
            {}
        )
        result, _ = self.run_node(
            cands, fleet, task_extra={"min_tier_execute": "K", "candidate_policy": "pin"}
        )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(fleet.execute_calls, [])

    def test_same_lineage_reviewer_is_excluded_from_open_enrichment(self):
        """Open enrichment for reviewer role excludes same-lineage candidates."""
        # CATALOG: k-busy is lineage 'xai', k-idle is lineage 'meta'
        fleet = self.fleet(["busy", "idle"])
        fleet.result["handoff"] = {
            "verdict": "ACCEPT", "findings": [], "checked": ["e"],
            "summary": "Independent review summary",
        }
        fleet.execution_lineage = "xai"
        # Maker has lineage xai (same as k-busy)
        cands = [{"agent": "idle", "model": "k-idle", "reason": "reviewer", "score": 85}]
        result, outdir = self.run_node(
            cands, fleet, role="reviewer",
            maker={"lineage": "xai/maker"},
            task_extra={"reviewer_min_tier": "K", "candidate_policy": "open"}
        )
        self.assertEqual(result["status"], "REVIEWED")
        self.assertEqual(fleet.execute_calls[0]["agent_id"], "idle")
        sel = json.loads((outdir / "tier-selection.json").read_text(encoding="utf-8"))
        # busy (xai) should not be in ordered candidates or should not be allowed
        allowed = [r["agent"] for r in sel["ordered"] if r["allowed"]]
        self.assertEqual(allowed, ["idle"])
        self.assertNotIn("busy", allowed)

    def test_denied_model_is_not_enriched_or_selected(self):
        """Denied model (tier DENY or deny pattern) is never enriched into open list."""
        # claude/old is tier DENY in CATALOG
        fleet = self.fleet(["claude", "worker"])
        cands = [{"agent": "worker", "model": "w1", "reason": "worker", "score": 90}]
        result, outdir = self.run_node(
            cands, fleet, task_extra={"min_tier_execute": "W", "candidate_policy": "open"}
        )
        self.assertEqual(result["status"], "CANDIDATE")
        sel = json.loads((outdir / "tier-selection.json").read_text(encoding="utf-8"))
        allowed = [r["agent"] for r in sel["ordered"] if r["allowed"]]
        self.assertNotIn("claude", allowed)

    def test_mode_mismatch_is_excluded_from_open_enrichment(self):
        """Profile lacking required mode is not enriched."""
        fleet = self.fleet(["busy", "idle"])
        # idle only supports review, not implement
        fleet.DEFAULT_PROFILES["idle"]["supported_modes"] = ["review", "advisory"]
        cands = [{"agent": "busy", "model": "k-busy", "reason": "maker", "score": 85}]
        result, outdir = self.run_node(
            cands, fleet, task_extra={"min_tier_execute": "K", "mode": "implement", "candidate_policy": "open"}
        )
        self.assertEqual(result["status"], "CANDIDATE")
        self.assertEqual(fleet.execute_calls[0]["agent_id"], "busy")
        sel = json.loads((outdir / "tier-selection.json").read_text(encoding="utf-8"))
        allowed = [r["agent"] for r in sel["ordered"] if r["allowed"]]
        self.assertNotIn("idle", allowed)

    def test_excluded_autopsy_candidate_is_not_selected(self):
        """Autopsy excluded candidates are omitted from open enrichment."""
        self.add_runs("busy", "k-busy", 40)
        self.add_runs("idle", "k-idle", 2)
        fleet = self.fleet(["busy", "idle"])
        cands = [{"agent": "busy", "model": "k-busy", "reason": "maker", "score": 85}]
        # Exclude idle via excluded_candidates
        excluded = {"maker": [{"agent": "idle", "model": "k-idle"}]}
        result, outdir = self.run_node(
            cands, fleet, task_extra={"min_tier_execute": "K", "candidate_policy": "open",
                                      "excluded_candidates": excluded}
        )
        self.assertEqual(result["status"], "CANDIDATE")
        self.assertEqual(fleet.execute_calls[0]["agent_id"], "busy")

    def test_unknown_availability_remains_neutral(self):
        """Unknown availability is not blocked and can be selected."""
        fleet = self.fleet(["busy", "idle"])
        # Set idle quota availability to unknown
        fresh = dict(fleet.quota)
        fleet.current_quota_views = lambda root, profiles: (
            {a: ({'status': 'unknown', 'current': False} if a == "idle" else fresh)
             for a in profiles},
            {}
        )
        cands = [{"agent": "busy", "model": "k-busy", "reason": "maker", "score": 85}]
        result, outdir = self.run_node(
            cands, fleet, task_extra={"min_tier_execute": "K", "candidate_policy": "open"}
        )
        self.assertEqual(result["status"], "CANDIDATE")
        sel = json.loads((outdir / "tier-selection.json").read_text(encoding="utf-8"))
        idle_entry = next((r for r in sel["ordered"] if r["agent"] == "idle"), None)
        self.assertIsNotNone(idle_entry)
        self.assertTrue(idle_entry["allowed"])
        self.assertEqual(idle_entry["availability"], "unknown")

    def test_disabled_profile_is_excluded(self):
        """Profile with routing_enabled: false is not enriched."""
        fleet = self.fleet(["busy", "idle"])
        fleet.DEFAULT_PROFILES["idle"]["routing_enabled"] = False
        cands = [{"agent": "busy", "model": "k-busy", "reason": "maker", "score": 85}]
        result, outdir = self.run_node(
            cands, fleet, task_extra={"min_tier_execute": "K", "candidate_policy": "open"}
        )
        self.assertEqual(result["status"], "CANDIDATE")
        self.assertEqual(fleet.execute_calls[0]["agent_id"], "busy")
        sel = json.loads((outdir / "tier-selection.json").read_text(encoding="utf-8"))
        allowed = [r["agent"] for r in sel["ordered"] if r["allowed"]]
        self.assertNotIn("idle", allowed)

    def test_trial_profile_without_admission_is_excluded(self):
        """Beam trial profile outside admitted workspace is not enriched."""
        fleet = self.beam_fleet(["worker", "opencode-beam"], admission="outside")
        cands = [{"agent": "worker", "model": "w1", "reason": "worker", "score": 90}]
        result, outdir = self.run_node(
            cands, fleet, task_extra={"min_tier_execute": "W", "candidate_policy": "open"}
        )
        self.assertEqual(result["status"], "CANDIDATE")
        self.assertEqual(fleet.execute_calls[0]["agent_id"], "worker")
        self.assertNotIn("opencode-beam", fleet.quota_profiles)

    def test_no_duplicate_readiness_checks(self):
        """Readiness check is called exactly once for all profiles."""
        fleet = self.fleet(["busy", "idle"])
        cands = [{"agent": "busy", "model": "k-busy", "reason": "maker", "score": 85}]
        readiness_calls = []
        original_live_readiness = fleet.live_readiness
        def spy_readiness(profiles, **kwargs):
            readiness_calls.append(list(profiles.keys()))
            return original_live_readiness(profiles, **kwargs)
        fleet.live_readiness = spy_readiness

        self.run_node(cands, fleet, task_extra={"min_tier_execute": "K", "candidate_policy": "open"})
        self.assertEqual(len(readiness_calls), 1)
        self.assertIn("busy", readiness_calls[0])
        self.assertIn("idle", readiness_calls[0])

    def test_malformed_candidate_policy_fails_before_native_calls(self):
        """Malformed policy fails validation before any native dispatch or probes."""
        fleet = self.fleet(["busy"])
        cands = [{"agent": "busy", "model": "k-busy", "reason": "maker", "score": 85}]
        for bad in ("unsupported", 123, True, ["open"], {"maker": "bad_lane"}, {"invalid_lane": "open"}):
            with self.subTest(policy=bad):
                with self.assertRaises(ValueError):
                    self.run_node(cands, fleet, task_extra={"candidate_policy": bad})
                self.assertEqual(fleet.execute_calls, [])

    def test_real_adapter_transports_plan_policy_and_preserves_task_override(self):
        from unittest.mock import patch
        self.add_runs("busy", "k-busy", 40)
        self.add_runs("idle", "k-idle", 2)
        candidates = [{"agent": "busy", "model": "k-busy", "score": 85, "reason": "fixture"}]
        cases = [(None, None, "busy"), ("open", None, "idle"),
                 ("pin", None, "busy"), ("open", "pin", "busy"),
                 ("pin", "open", "idle")]
        for index, (plan_policy, task_policy, expected) in enumerate(cases):
            with self.subTest(plan=plan_policy, task=task_policy):
                workspace = self.root / "ws"
                workspace.mkdir(exist_ok=True)
                folder = self.root / ("adapter-" + str(index))
                plan = {"goal": "synthetic transport"}
                if plan_policy is not None:
                    plan["candidate_policy"] = plan_policy
                task = {"id": "slice-1", "text": "t", "mode": "advisory", "scope": ["src"],
                        "done_when": ["e"], "maker": candidates, "reviewer": candidates,
                        "complexity": "low", "review_complexity": "low", "min_tier_execute": "K"}
                if task_policy is not None:
                    task["candidate_policy"] = task_policy
                # Exercise the real scheduler-to-bridge serialization. Only model dispatch is mocked.
                with patch.object(todo_graph, "command", return_value={"exit_code": 0}):
                    todo_graph.FleetAdapter().invoke(plan, task, "maker", workspace, folder, {})
                request = json.loads((folder / "request.json").read_text(encoding="utf8"))
                self.assertEqual(request["task"], task)
                self.assertEqual(request.get("candidate_policy"), plan_policy)
                fleet = self.fleet(["busy", "idle"])
                result, _ = self.run_node(candidates, fleet, request_extra=request)
                self.assertEqual(result["status"], "CANDIDATE")
                self.assertEqual(fleet.execute_calls[0]["agent_id"], expected)

    def test_plan_level_policy_and_task_override(self):
        """Plan-level candidate_policy is inherited, and task-level overrides it."""
        self.add_runs("busy", "k-busy", 40)
        self.add_runs("idle", "k-idle", 2)
        fleet = self.fleet(["busy", "idle"])
        cands = [{"agent": "busy", "model": "k-busy", "reason": "maker", "score": 85}]

        # Inherit open from plan
        res_open, _ = self.run_node(
            cands, fleet, task_extra={"min_tier_execute": "K"},
            request_extra={"plan": {"candidate_policy": "open"}}
        )
        self.assertEqual(fleet.execute_calls[0]["agent_id"], "idle")

        # Task overrides plan to pin
        fleet2 = self.fleet(["busy", "idle"])
        res_pin, _ = self.run_node(
            cands, fleet2, task_extra={"min_tier_execute": "K", "candidate_policy": "pin"},
            request_extra={"plan": {"candidate_policy": "open"}}
        )
        self.assertEqual(fleet2.execute_calls[0]["agent_id"], "busy")

    def test_dict_candidate_policy_per_role(self):
        """Dict candidate_policy configures maker and reviewer independently."""
        self.add_runs("busy", "k-busy", 40)
        self.add_runs("idle", "k-idle", 2)
        cands = [{"agent": "busy", "model": "k-busy", "reason": "test", "score": 85}]
        policy = {"maker": "open", "reviewer": "pin"}

        fleet_maker = self.fleet(["busy", "idle"])
        self.run_node(cands, fleet_maker, task_extra={"min_tier_execute": "K", "candidate_policy": policy})
        self.assertEqual(fleet_maker.execute_calls[0]["agent_id"], "idle")

        fleet_reviewer = self.fleet(["busy", "idle"])
        fleet_reviewer.result["handoff"] = {
            "verdict": "ACCEPT", "findings": [], "checked": ["e"],
            "summary": "Review summary",
        }
        self.run_node(cands, fleet_reviewer, role="reviewer",
                      maker={"lineage": "other"},
                      task_extra={"reviewer_min_tier": "K", "candidate_policy": policy})
        # Reviewer is pinned, so busy stays exact!
        self.assertEqual(fleet_reviewer.execute_calls[0]["agent_id"], "busy")

    def test_todo_graph_validate_validates_candidate_policy(self):
        """todo_graph.validate enforces valid candidate_policy on plan and tasks."""
        task = {
            "id": "t1", "text": "task text", "mode": "implement", "covers": ["req1"],
            "done_when": ["done"], "scope": ["src"], "checks": [["python", "-c", "0"]],
            "maker": [{"agent": "busy", "model": "k-busy", "score": 85, "reason": "r"}],
            "reviewer": [{"agent": "idle", "model": "k-idle", "score": 85, "reason": "r"}],
        }
        plan = {
            "version": 1, "goal": "test goal", "workspace": str(self.root), "max_parallel": 1,
            "requirements": ["req1"], "planner": {"model": "p", "source": "s"},
            "tasks": [task],
        }

        # Absent is valid
        todo_graph.validate(plan)

        # Valid policies
        for p in ("open", "pin", "pinned", {"maker": "open", "reviewer": "pin"}):
            plan["candidate_policy"] = p
            todo_graph.validate(plan)
            task["candidate_policy"] = p
            todo_graph.validate(plan)

        # Invalid policies
        for bad in ("bad", 123, True, ["open"], {"maker": "bad"}, {"unknown": "open"}):
            plan["candidate_policy"] = bad
            with self.assertRaises(ValueError):
                todo_graph.validate(plan)
            plan.pop("candidate_policy", None)
            task["candidate_policy"] = bad
            with self.assertRaises(ValueError):
                todo_graph.validate(plan)

    def test_open_enrichment_peers_are_candidates_own_tier_not_lower_floor(self):
        """Open enrichment peers must match candidate's own tier, not a lower required floor.

        Regression for finding: when task declares min_tier_execute 'W' (floor W), but reviewer
        only lists a Kierownik model (k-busy, tier K), open enrichment must enrich tier K peers
        (k-idle) and copy the score ONLY within tier K. Worker models (tier W) must not be
        enriched with the K reviewer's score or outrank the explicit K reviewer.
        """
        fleet = self.fleet(["busy", "idle", "worker"])
        fleet.result["handoff"] = {
            "verdict": "ACCEPT", "findings": [], "checked": ["e"],
            "summary": "Independent review summary",
        }
        # Explicit reviewer is tier K (k-busy) with score 85, task floor is W
        cands = [{"agent": "busy", "model": "k-busy", "reason": "reviewer K", "score": 85}]
        result, outdir = self.run_node(
            cands, fleet, role="reviewer",
            maker={"lineage": "other/maker"},
            task_extra={"min_tier_execute": "W", "candidate_policy": "open"}
        )
        self.assertEqual(result["status"], "REVIEWED")
        sel = json.loads((outdir / "tier-selection.json").read_text(encoding="utf-8"))
        ordered_agents = [r["agent"] for r in sel["ordered"]]
        # busy and idle (tier K) are in ordered list
        self.assertIn("busy", ordered_agents)
        self.assertIn("idle", ordered_agents)
        # worker (tier W) MUST NOT be enriched into reviewer!
        self.assertNotIn("worker", ordered_agents)
        # Enriched idle peer received score 85 (copied within tier K)
        idle_entry = next(r for r in sel["ordered"] if r["agent"] == "idle")
        self.assertEqual(idle_entry["base"], 85)
        self.assertEqual(idle_entry["tier"], "K")
        self.assertIn(fleet.execute_calls[0]["agent_id"], {"busy", "idle"})

    def test_open_enrichment_scores_copied_only_within_tier_for_multi_tier_candidates(self):
        """When candidates span multiple tiers, scores are copied only within the respective tier."""
        fleet = self.fleet(["worker", "busy", "idle"])
        # maker lists worker (tier W, score 90) and busy (tier K, score 75)
        cands = [
            {"agent": "worker", "model": "w1", "reason": "worker", "score": 90},
            {"agent": "busy", "model": "k-busy", "reason": "busy K fallback", "score": 75},
        ]
        result, outdir = self.run_node(
            cands, fleet, role="maker",
            task_extra={"min_tier_execute": "W", "candidate_policy": "open"}
        )
        self.assertEqual(result["status"], "CANDIDATE")
        sel = json.loads((outdir / "tier-selection.json").read_text(encoding="utf-8"))
        # idle is tier K peer of busy: it must inherit busy's score 75, NOT worker's score 90!
        idle_entry = next(r for r in sel["ordered"] if r["agent"] == "idle")
        self.assertEqual(idle_entry["base"], 75)
        self.assertEqual(idle_entry["tier"], "K")


if __name__ == "__main__":
    unittest.main()
