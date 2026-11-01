from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("budget_pools.py")

import importlib.util

SPEC = importlib.util.spec_from_file_location("budget_pools", MODULE_PATH)
assert SPEC and SPEC.loader
budget_pools = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(budget_pools)

ROLLOUT_PATH = (
    Path(__file__).resolve().parents[1] / "references" / "budget-rollout-20260902.runs.json"
)


class BudgetPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ceiling = budget_pools.example_owner_ceiling(
            planning_and_review=1500000,
            execution=3500000,
        )
        self.ceiling["token_policy"] = "hard"
        self.profiles = [
            {
                "id": "expensive-quality",
                "cost_pln": 10,
                "premium": True,
                "capabilities": {
                    "instruction-following": 95,
                    "tool-use": 90,
                    "write": 100,
                    "independent-review": 97,
                },
                "efficiency_priors": {"first_pass": 90},
                "evidence_confidence": 0.8,
            },
            {
                "id": "cheap-capable",
                "cost_pln": 1,
                "premium": False,
                "capabilities": {
                    "instruction-following": 90,
                    "tool-use": 85,
                    "write": 90,
                    "independent-review": 88,
                },
                "efficiency_priors": {"first_pass": 85},
                "evidence_confidence": 0.7,
            },
            {
                "id": "local-qwen-analogue",
                "cost_pln": 0,
                "premium": False,
                "capabilities": {
                    "instruction-following": 84,
                    "tool-use": 86,
                    "write": 100,
                    "independent-review": 82,
                },
                "efficiency_priors": {"first_pass": 78},
                "evidence_confidence": 0.35,
            },
        ]

    def test_schema_rejects_missing_pools(self) -> None:
        broken = dict(self.ceiling)
        del broken["planning_and_review"]
        with self.assertRaisesRegex(budget_pools.BudgetPoolError, "keys mismatch"):
            budget_pools.validate_owner_ceiling(broken)

    def test_exhausted_planning_does_not_block_execution(self) -> None:
        ceiling = budget_pools.example_owner_ceiling(
            planning_and_review=0,
            execution=3500000,
        )
        ceiling["token_policy"] = "hard"
        ledger = budget_pools.empty_ledger()
        with self.assertRaisesRegex(budget_pools.BudgetPoolError, "planning_and_review"):
            budget_pools.authorize_pool_spend(ceiling, ledger, "planning_and_review", 1)
        allowed = budget_pools.authorize_pool_spend(
            ceiling, ledger, "execution", 20000
        )
        self.assertEqual(allowed["status"], "AUTHORIZED")
        self.assertFalse(allowed["borrowed_from_other_pool"])
        self.assertEqual(allowed["other_pool_remaining"], 0)

    def test_pools_cannot_borrow(self) -> None:
        ceiling = budget_pools.example_owner_ceiling(
            planning_and_review=10,
            execution=1000,
        )
        ceiling["token_policy"] = "hard"
        ledger = budget_pools.empty_ledger()
        budget_pools.apply_charge(
            ledger,
            {
                "run_id": "run-plan",
                "kind": "fidelity",
                "budget_pool": "planning_and_review",
            },
            budget_pools.charge_token_usage(
                total_tokens=10, token_status="known", max_tokens=10
            ),
        )
        with self.assertRaisesRegex(budget_pools.BudgetPoolError, "planning_and_review"):
            budget_pools.authorize_pool_spend(ceiling, ledger, "planning_and_review", 1)
        allowed = budget_pools.authorize_pool_spend(ceiling, ledger, "execution", 50)
        self.assertEqual(allowed["other_pool_remaining"], 0)

    def test_unknown_usage_is_preserved_without_fake_charge(self) -> None:
        charge = budget_pools.charge_token_usage(
            total_tokens=None,
            token_status="unknown",
            max_tokens=200000,
        )
        self.assertFalse(charge["estimated"])
        self.assertIsNone(charge["attested_tokens"])
        self.assertEqual(charge["status"], "unknown")
        no_substitute = budget_pools.charge_token_usage(
            total_tokens=None, token_status="unknown", max_tokens=None
        )
        self.assertIsNone(no_substitute["attested_tokens"])

    def test_rollout_nine_runs_all_charge_planning_pool(self) -> None:
        payload = json.loads(ROLLOUT_PATH.read_text(encoding="utf-8"))
        ceiling = budget_pools.example_owner_ceiling(
            planning_and_review=5000000,
            execution=3500000,
        )
        report = budget_pools.reconcile_runs(ceiling, payload["runs"])
        self.assertEqual(len(payload["runs"]), 9)
        self.assertEqual(len(report["assignments"]), 9)
        self.assertTrue(
            all(item["pool"] == "planning_and_review" for item in report["assignments"])
        )
        self.assertEqual(report["pools"]["execution"]["run_count"], 0)
        self.assertEqual(report["pools"]["execution"]["attested_tokens"], 0)
        self.assertEqual(report["pools"]["planning_and_review"]["known_tokens"], 4988626)
        self.assertEqual(report["pools"]["planning_and_review"]["estimated_tokens"], 0)
        self.assertEqual(report["pools"]["planning_and_review"]["attested_tokens"], 4988626)
        self.assertEqual(report["pools"]["planning_and_review"]["unknown_runs"], 2)
        unknown_ids = {
            "run-20260901T233213Z-8037b5ca2b",
            "run-20260902T001114Z-a52e3052ec",
        }
        unknown = [item for item in report["assignments"] if item["run_id"] in unknown_ids]
        self.assertEqual(len(unknown), 2)
        self.assertTrue(all(item["attested_tokens"] is None for item in unknown))
        markdown = budget_pools.render_reconciliation_markdown(report)
        self.assertIn("planning_and_review", markdown)
        self.assertIn("4988626", markdown)

    def test_quality_versus_cost_routing_respects_capability_floor(self) -> None:
        quality = budget_pools.select_profile(
            self.profiles, self.ceiling, "planning_and_review"
        )
        cheap = budget_pools.select_profile(self.profiles, self.ceiling, "execution")
        self.assertEqual(quality["id"], "expensive-quality")
        self.assertEqual(cheap["id"], "cheap-capable")
        self.assertFalse(
            budget_pools.profile_meets_capability_floor(
                self.profiles[2], self.ceiling["capability_floor"]
            )
        )

    def test_stale_quota_is_not_neutral(self) -> None:
        now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
        result = budget_pools.evaluate_quota_snapshot(
            {"observed_at": "2026-08-17T00:00:00Z", "status": "unknown", "age_hours": 387},
            now=now,
            max_age_hours=24,
            collector_mode="tui-assisted",
        )
        self.assertFalse(result["fresh"])
        self.assertTrue(result["stale"])
        self.assertEqual(result["routing_signal"], "stale-not-neutral")
        self.assertEqual(result["refresh_action"], "mark-stale-provider-console-required")
        self.assertTrue(result["should_refresh_before_routing"])

    def test_recommended_split_reserves_execution_majority(self) -> None:
        split = budget_pools.RECOMMENDED_OWNER_SPLIT
        self.assertEqual(split["planning_and_review"] + split["execution"], 5000000)
        self.assertGreater(split["execution"], split["planning_and_review"])
        self.assertEqual(split["raised_used_tokens"], 4988626)
        self.assertEqual(split["execution_runs"], 0)


if __name__ == "__main__":
    unittest.main()
