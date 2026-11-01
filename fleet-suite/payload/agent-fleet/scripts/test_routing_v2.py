from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import adaptive  # noqa: E402
import fleet  # noqa: E402
import routing_v2  # noqa: E402


class RouterV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        self.task_profile = json.loads(
            (
                fleet.SKILL_ROOT
                / "references"
                / "task-profile-v2-example.json"
            ).read_text(encoding="utf-8")
        )
        for profile_id in self.profiles:
            self.task_profile['model_scores'].setdefault(
                profile_id,
                {'score': 0, 'reason': 'Neutral fixture score for newly configured profile.'},
            )

    def test_quota_gate_denies_fresh_exhaustion_but_not_stale_or_unknown(self) -> None:
        profile = self.profiles["grok"]
        fresh_exhausted = routing_v2.quota_hard_gates(
            profile,
            {"status": "fresh", "headroom": 0.0, "confidence": 1.0},
        )
        self.assertFalse(
            next(gate for gate in fresh_exhausted if gate["code"] == "quota-not-exhausted")[
                "passed"
            ]
        )
        for quota in (
            {"status": "stale", "headroom": None, "hard_blocking": True},
            {"status": "unknown", "headroom": None, "hard_blocking": True},
        ):
            with self.subTest(status=quota["status"]):
                gates = routing_v2.quota_hard_gates(profile, quota)
                self.assertTrue(
                    next(
                        gate for gate in gates if gate["code"] == "quota-not-exhausted"
                    )["passed"]
                )

    def test_task_profile_v2_validates_and_v1_normalizes(self) -> None:
        validated = adaptive.validate_assessment(
            self.task_profile,
            profile_ids=self.profiles,
            valid_kinds=fleet.VALID_KINDS,
            valid_complexities=fleet.VALID_COMPLEXITIES,
        )
        self.assertEqual(validated["schema_version"], 2)
        legacy = adaptive.fallback_assessment(
            kind="implementation",
            complexity="medium",
            profiles=self.profiles,
        )
        normalized = adaptive.normalize_task_profile(legacy)
        self.assertEqual(normalized["source_schema_version"], 1)
        self.assertEqual(normalized["allocation_policy"], "balanced")
        self.assertEqual(normalized["required_tools"], [])
        self.assertIsNone(normalized["red_team_intent"])
        missing_intent = dict(self.task_profile)
        missing_intent.pop("red_team_intent")
        with self.assertRaisesRegex(adaptive.AdaptiveError, "fields do not match"):
            adaptive.validate_assessment(
                missing_intent,
                profile_ids=self.profiles,
                valid_kinds=fleet.VALID_KINDS,
                valid_complexities=fleet.VALID_COMPLEXITIES,
            )

    def test_authentication_denial_is_a_hard_runtime_failure(self) -> None:
        readiness = routing_v2.readiness_from_probes(
            self.profiles["grok"],
            executable="grok.exe",
            version_probe={"launchable": True, "output": "grok 1.0"},
            deep_probe={
                "launchable": True,
                "output": "You are not authenticated",
            },
        )
        self.assertFalse(readiness["ready"])
        self.assertIn(
            "authentication-or-entitlement-denied", readiness["reasons"]
        )

    def test_claude_logged_out_json_is_a_hard_runtime_failure(self) -> None:
        self.assertEqual(
            self.profiles["claude"]["deep_probe_args"], ["auth", "status"]
        )
        readiness = routing_v2.readiness_from_probes(
            self.profiles["claude"],
            executable="claude.exe",
            version_probe={"launchable": True, "output": "2.1.218"},
            deep_probe={
                "launchable": True,
                "output": '{"loggedIn": false, "authMethod": "none"}',
            },
        )
        self.assertFalse(readiness["ready"])
        self.assertIn(
            "authentication-or-entitlement-denied", readiness["reasons"]
        )

    def test_minimum_cli_version_and_fatal_probe_output_are_hard_failures(self) -> None:
        profile = copy.deepcopy(self.profiles["opencode"])
        profile["readiness"]["minimum_version"] = "1.18.18"
        profile["readiness"]["fatal_patterns"] = ["unexpected server error"]

        outdated = routing_v2.readiness_from_probes(
            profile,
            executable="opencode.exe",
            version_probe={"launchable": True, "output": "1.17.15"},
            deep_probe={
                "launchable": True,
                "output": "zai-coding-plan/glm-5.3",
            },
        )
        current = routing_v2.readiness_from_probes(
            profile,
            executable="opencode.exe",
            version_probe={"launchable": True, "output": "1.18.21"},
            deep_probe={
                "launchable": True,
                "output": "zai-coding-plan/glm-5.3",
            },
        )
        fatal = routing_v2.readiness_from_probes(
            profile,
            executable="opencode.exe",
            version_probe={"launchable": True, "output": "1.18.21"},
            deep_probe={
                "launchable": True,
                "output": "Unexpected server error. Check server logs for details.",
            },
        )

        self.assertFalse(outdated["ready"])
        self.assertIn("minimum-version-not-met", outdated["reasons"])
        self.assertEqual(outdated["observed_version"], "1.17.15")
        self.assertTrue(current["ready"])
        self.assertEqual(current["observed_version"], "1.18.21")
        self.assertFalse(fatal["ready"])
        self.assertIn("runtime-probe-reported-fatal-error", fatal["reasons"])

    def test_exact_runtime_model_inventory_is_a_hard_gate(self) -> None:
        execution = routing_v2.resolve_execution_profile(
            "grok",
            self.profiles["grok"],
            complexity="medium",
            mode="review",
            required_tools=[],
        )
        visible, _ = routing_v2.runtime_model_visible(
            execution,
            profile=self.profiles["grok"],
            readiness={"deep_probe": {"output": "Available: grok-4.7"}},
        )
        missing, evidence = routing_v2.runtime_model_visible(
            execution,
            profile=self.profiles["grok"],
            readiness={"deep_probe": {"output": "Available: older-model"}},
        )
        self.assertTrue(visible)
        self.assertEqual(execution["model"], "grok-4.7")
        self.assertFalse(missing)
        self.assertIn("not-listed", evidence)

    def test_unknown_and_advisory_quota_are_neutral_but_confirmed_exhaustion_blocks(self) -> None:
        execution = routing_v2.resolve_execution_profile(
            "codex",
            self.profiles["codex"],
            complexity="high",
            mode="implement",
            required_tools=["write"],
        )
        ready = {"ready": True, "reasons": []}
        unknown = routing_v2.hard_gates(
            execution,
            profile=self.profiles["codex"],
            assessment=self.task_profile,
            readiness=ready,
            quota={"status": "unknown", "headroom": None},
            mode="implement",
        )
        advisory = routing_v2.hard_gates(
            execution,
            profile=self.profiles["codex"],
            assessment=self.task_profile,
            readiness=ready,
            quota={
                "status": "fresh",
                "headroom": 0.0,
                "reported_headroom": 0.0,
                "authority": "advisory",
                "hard_blocking": False,
            },
            mode="implement",
        )
        exhausted = routing_v2.hard_gates(
            execution,
            profile=self.profiles["codex"],
            assessment=self.task_profile,
            readiness=ready,
            quota={"status": "fresh", "headroom": 0.0},
            mode="implement",
        )
        self.assertTrue(
            next(gate for gate in unknown if gate["code"] == "quota-not-exhausted")[
                "passed"
            ]
        )
        self.assertTrue(
            next(
                gate for gate in advisory if gate["code"] == "quota-not-exhausted"
            )["passed"]
        )
        self.assertFalse(
            next(
                gate for gate in exhausted if gate["code"] == "quota-not-exhausted"
            )["passed"]
        )

    def test_risk_and_capability_floors_are_hard_gates(self) -> None:
        execution = routing_v2.resolve_execution_profile(
            "kimi",
            self.profiles["kimi"],
            complexity="low",
            mode="implement",
            required_tools=["write"],
        )
        task = copy.deepcopy(self.task_profile)
        task["risk"] = "critical"
        task["minimum_capabilities"]["instruction-following"] = 99
        gates = routing_v2.hard_gates(
            execution,
            profile=self.profiles["kimi"],
            assessment=task,
            readiness={"ready": True, "reasons": []},
            quota={"status": "unknown", "headroom": None},
            mode="implement",
        )
        failures = {gate["code"] for gate in gates if not gate["passed"]}
        self.assertIn("risk-ceiling", failures)
        self.assertIn("capability-floors", failures)

    def test_arbitrary_model_override_is_refused(self) -> None:
        with self.assertRaisesRegex(
            routing_v2.RoutingV2Error, "not configured"
        ):
            routing_v2.resolve_execution_profile(
                "codex",
                self.profiles["codex"],
                complexity="medium",
                mode="review",
                required_tools=[],
                model_override="made-up-premium-model",
            )

    def test_qwen_mode_maps_use_each_shared_pool_model_for_its_strength(self) -> None:
        qwen = self.profiles["qwen"]
        review_low = routing_v2.resolve_execution_profile(
            "qwen",
            qwen,
            complexity="low",
            mode="review",
            required_tools=[],
        )
        implement_medium = routing_v2.resolve_execution_profile(
            "qwen",
            qwen,
            complexity="medium",
            mode="implement",
            required_tools=["write"],
        )
        advisory_high = routing_v2.resolve_execution_profile(
            "qwen",
            qwen,
            complexity="high",
            mode="advisory",
            required_tools=[],
        )
        implement_high = routing_v2.resolve_execution_profile(
            "qwen",
            qwen,
            complexity="high",
            mode="implement",
            required_tools=["write"],
        )
        self.assertEqual(review_low["model"], "deepseek-v4.1-flash")
        self.assertEqual(implement_medium["model"], "qwen3.8-flash")
        self.assertEqual(
            implement_medium["product_name"], "Qwen3.8-Flash-Next"
        )
        self.assertEqual(implement_medium["runtime_model_id"], "qwen3.8-flash")
        self.assertEqual(advisory_high["model"], "qwen3.8-max")
        self.assertEqual(implement_high["model"], "qwen3.8-max")
        self.assertEqual(review_low["lifecycle"], "ACTIVE")
        self.assertEqual(implement_medium["lifecycle"], "LIMITED")
        self.assertEqual(advisory_high["lifecycle"], "LIMITED")
        self.assertEqual(implement_high["lifecycle"], "LIMITED")

    def test_grok_46_is_exact_and_45_override_is_rejected(self) -> None:
        current = routing_v2.resolve_execution_profile(
            "grok",
            self.profiles["grok"],
            complexity="medium",
            mode="implement",
            required_tools=["write"],
            model_override="grok-4.7",
        )
        self.assertEqual(current["execution_profile_id"], "grok-4-7-balanced")
        self.assertEqual(current["runtime_model_id"], "grok-4.7")
        with self.assertRaisesRegex(
            routing_v2.RoutingV2Error,
            "Model grok-4.5 is not configured for grok",
        ):
            routing_v2.resolve_execution_profile(
                "grok",
                self.profiles["grok"],
                complexity="medium",
                mode="implement",
                required_tools=["write"],
                model_override="grok-4.5",
            )
        visible, evidence = routing_v2.runtime_model_visible(
            current,
            profile=self.profiles["grok"],
            readiness={
                "deep_probe": {
                    "output": "Default model: grok-4.7; Available: grok-4.7, grok-4.5"
                }
            },
        )
        self.assertTrue(visible, evidence)

    def test_local_learning_deduplicates_root_task_and_shrinks(self) -> None:
        records = []
        for attempt in range(2):
            records.append(
                {
                    "run_id": f"run-{attempt}",
                    "root_task_id": "task-one",
                    "agent": "codex",
                    "model": "gpt-5.6-terra",
                    "execution_profile_id": "codex-gpt-5-6-terra",
                    "kind": "implementation",
                    "duration_seconds": 120,
                    "token_usage": {"total_tokens": 1000 + attempt},
                    "evaluation": {
                        "criteria_pass": True,
                        "quality_score": 90,
                        "first_pass": attempt == 0,
                    },
                }
            )
        stats = routing_v2.local_execution_stats(
            records,
            execution_profile_id="codex-gpt-5-6-terra",
            agent="codex",
            model="gpt-5.6-terra",
            kind="implementation",
        )
        self.assertEqual(stats["sample_count"], 1)
        self.assertFalse(stats["active"])
        self.assertLess(stats["shrinkage_weight"], 0.05)

    def test_tokens_never_create_a_positive_routing_reward(self) -> None:
        execution = routing_v2.resolve_execution_profile(
            "codex",
            self.profiles["codex"],
            complexity="medium",
            mode="implement",
            required_tools=["write"],
        )
        common = {
            "capability_fit": 90.0,
            "execution": execution,
            "assessment": self.task_profile,
            "quota": {"status": "unknown", "headroom": None},
            "underuse_score": 50.0,
        }
        efficient, *_ = routing_v2.assignment_score(
            **common,
            local={
                "sample_count": 20,
                "active": True,
                "shrinkage_weight": 0.5,
                "quality_mean": 90,
                "first_pass_score": 90,
                "median_duration_seconds": 100,
                "median_accepted_tokens": 1000,
            },
        )
        wasteful, *_ = routing_v2.assignment_score(
            **common,
            local={
                "sample_count": 20,
                "active": True,
                "shrinkage_weight": 0.5,
                "quality_mean": 90,
                "first_pass_score": 90,
                "median_duration_seconds": 100,
                "median_accepted_tokens": 1000000,
            },
        )
        self.assertEqual(efficient, wasteful)

    def test_delayed_regression_downgrades_effective_local_outcome(self) -> None:
        records = [
            {
                "run_id": "run-one",
                "root_task_id": "task-one",
                "agent": "codex",
                "model": "gpt-5.6-terra",
                "execution_profile_id": "codex-gpt-5-6-terra",
                "kind": "implementation",
                "evaluation": {
                    "criteria_pass": True,
                    "quality_score": 95,
                    "first_pass": True,
                },
                "followups": [
                    {"reopened_7d": True, "regressions_found": 1}
                ],
            }
        ]
        stats = routing_v2.local_execution_stats(
            records,
            execution_profile_id="codex-gpt-5-6-terra",
            agent="codex",
            model="gpt-5.6-terra",
            kind="implementation",
        )
        self.assertEqual(stats["reliability_score"], 0.0)
        self.assertEqual(stats["quality_mean"], 60.0)

    def test_local_qwen_is_eligible_for_bounded_implementation_after_readiness(self) -> None:
        for agent_id in ("qwen-local", "qwen-local-5070ti"):
            execution = routing_v2.resolve_execution_profile(
                agent_id,
                self.profiles[agent_id],
                complexity="medium",
                mode="implement",
                required_tools=["write"],
            )
            gates = routing_v2.hard_gates(
                execution,
                profile=self.profiles[agent_id],
                assessment=self.task_profile,
                readiness={"ready": True, "reasons": []},
                quota={"status": "fresh", "headroom": 1.0, "confidence": 1.0},
                mode="implement",
            )
            failures = {gate["code"] for gate in gates if not gate["passed"]}
            self.assertNotIn("red-team-only", failures)
            self.assertNotIn("lifecycle", failures)
            self.assertNotIn("mode", failures)
            self.assertNotIn("tools", failures)
            unavailable = routing_v2.hard_gates(
                execution,
                profile=self.profiles[agent_id],
                assessment=self.task_profile,
                readiness={"ready": False, "reasons": ["listener-unavailable"]},
                quota={"status": "fresh", "headroom": 1.0, "confidence": 1.0},
                mode="implement",
            )
            unavailable_codes = {
                gate["code"] for gate in unavailable if not gate["passed"]
            }
            self.assertIn("runtime-readiness", unavailable_codes)

    def test_security_review_qualification_ignores_provider_names(self) -> None:
        grok = self.profiles["grok"]["execution_profiles"][
            "grok-4-7-security-review"
        ]
        self.assertTrue(routing_v2.execution_qualifies_for_security_review(grok))
        codex = self.profiles["codex"]["execution_profiles"][
            "codex-gpt-5-6-sol-security-review"
        ]
        self.assertFalse(routing_v2.execution_qualifies_for_security_review(codex))
        codex_active = copy.deepcopy(codex)
        codex_active["lifecycle"] = "ACTIVE"
        self.assertTrue(routing_v2.execution_qualifies_for_security_review(codex_active))
        weak = dict(grok)
        weak["capabilities"] = {**grok["capabilities"], "tool-use": 94}
        self.assertFalse(routing_v2.execution_qualifies_for_security_review(weak))
        glm = self.profiles["opencode"]["execution_profiles"][
            "opencode-glm-5-3-flash-high"
        ]
        self.assertTrue(routing_v2.execution_qualifies_for_security_review(glm))
        self.assertEqual(
            routing_v2.SECURITY_REVIEW_QUALIFICATION[
                "primary_execution_profile_id"
            ],
            "opencode-glm-5-3-flash-high",
        )

    def test_security_review_qualification_requires_the_exact_read_only_toolset(self) -> None:
        baseline = self.profiles["opencode"]["execution_profiles"][
            "opencode-glm-5-3-flash-high"
        ]
        allowed = set(
            routing_v2.SECURITY_REVIEW_QUALIFICATION["read_only_auditor_tools"]
        )
        self.assertEqual(set(baseline["tools"]), allowed)
        for forbidden in ("filesystem-write", "shell", "write", "network"):
            contaminated = dict(baseline)
            contaminated["tools"] = [*baseline["tools"], forbidden]
            reasons = routing_v2.security_review_qualification_reasons(contaminated)
            self.assertIn(
                "non-read-only-review-tools=" + forbidden,
                reasons,
            )
        missing = dict(baseline)
        missing["tools"] = ["filesystem-read", "independent-review"]
        reasons = routing_v2.security_review_qualification_reasons(missing)
        self.assertTrue(
            any(reason.startswith("inexact-read-only-review-toolset=") for reason in reasons)
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
