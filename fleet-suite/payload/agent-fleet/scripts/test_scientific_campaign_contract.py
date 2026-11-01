import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

import fleet


ROOT = Path(__file__).resolve().parents[1]
REFERENCES = ROOT / "references"


def load_schema(name: str) -> dict:
    return json.loads((REFERENCES / name).read_text(encoding="utf-8"))


def valid_campaign() -> dict:
    return {
        "schema_version": 1,
        "campaign_id": "campaign-20260820-baseline",
        "mode": "scientific-campaign",
        "objective": "Compare bounded research candidates against the locked baseline.",
        "owner": "parent",
        "gate_0": {"required": True, "owner": "parent", "status": "pending", "evidence_ref": "parent://gate-0"},
        "experiment_cards": ["experiment-20260820-a"],
        "hardware_budget": {"max_concurrent_runs": 2, "max_total_runs": 8, "max_gpu_hours": 12},
        "stop_rules": {"no_improvement": True, "no_convergence": True, "budget_exceeded": True, "data_quality_failure": True},
        "review_policy": {"reviewer_scope": "group", "repair_scope": "rejected-only", "parent_after_gate": True},
        "promotion_policy": {"merge": False, "deploy": False, "promotion": False},
    }


def valid_card() -> dict:
    return {
        "schema_version": 1,
        "card_id": "experiment-20260820-a",
        "campaign_id": "campaign-20260820-baseline",
        "hypothesis": "Candidate A improves the primary metric without breaking guardrails.",
        "run_ids": [],
        "independence": {"independent": True, "depends_on_cards": []},
        "hardware_request": {"gpu_hours": 2, "concurrency_slot": 1},
        "data_quality": {"status": "passed", "evidence_ref": "data://quality"},
        "repair_policy": {"allowed": True, "scope": "rejected-only"},
    }


class ScientificCampaignContractTests(unittest.TestCase):
    def assert_valid(self, schema_name: str, instance: dict) -> None:
        errors = sorted(
            Draft202012Validator(load_schema(schema_name)).iter_errors(instance),
            key=lambda error: list(error.path),
        )
        self.assertEqual(errors, [], "schema errors: " + "; ".join(error.message for error in errors))

    def test_schemas_are_json_schema_documents(self) -> None:
        campaign = load_schema("scientific-campaign-contract.schema.json")
        card = load_schema("experiment-card.schema.json")
        self.assertEqual(campaign["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(card["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertIn("gate_0", campaign["required"])
        self.assertIn("independence", card["required"])

    def test_valid_campaign_and_card_validate_against_schema(self) -> None:
        self.assert_valid("scientific-campaign-contract.schema.json", valid_campaign())
        self.assert_valid("experiment-card.schema.json", valid_card())

    def test_campaign_contract_encodes_parent_gate_and_no_promotion(self) -> None:
        contract = valid_campaign()
        self.assertEqual(contract["gate_0"]["owner"], "parent")
        self.assertTrue(contract["gate_0"]["required"])
        self.assertEqual(contract["review_policy"]["repair_scope"], "rejected-only")
        self.assertEqual(contract["promotion_policy"], {"merge": False, "deploy": False, "promotion": False})

    def test_campaign_contract_rejects_executor_owned_gate_zero(self) -> None:
        contract = valid_campaign()
        contract["gate_0"]["owner"] = "executor"
        with self.assertRaises(AssertionError):
            self.assert_valid("scientific-campaign-contract.schema.json", contract)

    def test_experiment_card_requires_independent_run_and_repair_scope(self) -> None:
        card = valid_card()
        self.assertTrue(card["independence"]["independent"])
        self.assertEqual(card["independence"]["depends_on_cards"], [])
        self.assertEqual(card["repair_policy"]["scope"], "rejected-only")

    def test_experiment_card_rejects_dependency_or_unbounded_repair_semantics(self) -> None:
        card = valid_card()
        card["independence"]["depends_on_cards"] = ["experiment-other"]
        with self.assertRaises(AssertionError):
            self.assert_valid("experiment-card.schema.json", card)
        card = valid_card()
        card["repair_policy"]["scope"] = "all"
        with self.assertRaises(AssertionError):
            self.assert_valid("experiment-card.schema.json", card)

    def write_contracts(self, root: Path, *, gate_status: str = "passed") -> tuple[Path, Path]:
        campaign = valid_campaign()
        campaign["gate_0"]["status"] = gate_status
        campaign_path = root / "campaign.json"
        card_path = root / "card.json"
        campaign_path.write_text(json.dumps(campaign), encoding="utf-8")
        card_path.write_text(json.dumps(valid_card()), encoding="utf-8")
        return campaign_path, card_path

    def test_runtime_refuses_pending_gate_zero_before_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            campaign_path, card_path = self.write_contracts(Path(temp), gate_status="pending")
            with self.assertRaisesRegex(fleet.FleetError, "Gate 0 must be passed"):
                fleet.load_scientific_campaign_context(campaign_path, card_path)

    def test_runtime_accepts_only_frozen_no_promotion_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            campaign_path, card_path = self.write_contracts(root)
            context = fleet.load_scientific_campaign_context(campaign_path, card_path)
            self.assertEqual(context["gate_0"]["owner"], "parent")
            self.assertEqual(
                context["promotion_policy"],
                {"merge": False, "deploy": False, "promotion": False},
            )
            campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
            campaign["promotion_policy"]["deploy"] = True
            campaign_path.write_text(json.dumps(campaign), encoding="utf-8")
            with self.assertRaisesRegex(fleet.FleetError, "cannot authorize"):
                fleet.load_scientific_campaign_context(campaign_path, card_path)

    def test_campaign_worker_acceptance_fails_closed_on_pending_gate(self) -> None:
        context = {
            "campaign_id": "campaign-20260820-baseline",
            "campaign_sha256": "a" * 64,
            "experiment_card_id": "experiment-20260820-a",
            "gate_0": {
                "required": True,
                "owner": "parent",
                "status": "pending",
                "evidence_ref": "parent://gate-0",
            },
            "promotion_policy": {"merge": False, "deploy": False, "promotion": False},
        }
        with self.assertRaisesRegex(fleet.FleetError, "Gate 0"):
            fleet.validate_scientific_campaign_workers(
                [{"run_id": "run-20260820T120000Z-aaaaaaaaaa", "scientific_campaign": context, "quality_gate_required": True}]
            )

    def test_campaign_budget_reservation_is_atomic_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            campaign_path, card_path = self.write_contracts(root)
            campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
            campaign["hardware_budget"] = {
                "max_concurrent_runs": 1,
                "max_total_runs": 1,
                "max_gpu_hours": 2,
            }
            campaign_path.write_text(json.dumps(campaign), encoding="utf-8")
            context = fleet.load_scientific_campaign_context(campaign_path, card_path)
            fleet.reserve_scientific_campaign_run(
                root,
                context,
                run_id="run-20260820T120000Z-aaaaaaaaaa",
                timeout=60,
                dry_run=False,
            )
            with self.assertRaisesRegex(fleet.FleetError, "max_total_runs"):
                fleet.reserve_scientific_campaign_run(
                    root,
                    context,
                    run_id="run-20260820T120001Z-bbbbbbbbbb",
                    timeout=60,
                    dry_run=False,
                )

    def test_parser_exposes_campaign_runtime_gate(self) -> None:
        parser = fleet.build_parser()
        args = parser.parse_args(
            [
                "run",
                "--task",
                "bounded experiment",
                "--assessment-file",
                "assessment.json",
                "--campaign-contract",
                "campaign.json",
                "--experiment-card",
                "card.json",
            ]
        )
        self.assertEqual(args.campaign_contract, "campaign.json")
        self.assertEqual(args.experiment_card, "card.json")


if __name__ == "__main__":
    unittest.main(verbosity=2)
