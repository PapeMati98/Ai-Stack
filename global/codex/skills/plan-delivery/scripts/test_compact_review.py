import argparse
import copy
import unittest
from pathlib import Path

import test_plan_contract as _fixture_module

from test_plan_contract import plan_contract, write_json


class CompactCombinedReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _fixture_module.PlanContractTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.plan_contract = plan_contract
        self.contract = copy.deepcopy(self.fixture.make_contract())
        self.contract["classification"]["tier"] = "compact"
        self.contract["planning"]["review_history"] = []
        for gate in self.contract["gates"].values():
            gate.update(
                {
                    "status": "pending",
                    "auditor": None,
                    "reviewed_prompt_sha256": None,
                    "reviewed_plan_sha256": None,
                    "reviewed_clause_ids": [],
                    "reviewed_requirement_ids": [],
                    "reviewed_task_ids": [],
                    "attested_hard_risk_flags": [],
                    "evidence": None,
                }
            )
        self.contract_path = self.fixture.root / "compact-draft.json"
        write_json(self.contract_path, self.contract)
        self.evidence = self.fixture.root / "compact-review.md"
        self.evidence.write_text("PASS: one combined review covers both compact gates.\n", encoding="utf-8")

    def combined_review(self, status: str = "pass", **overrides: object) -> Path:
        contract = self.plan_contract.load_json(self.contract_path, "contract")
        result = {
            "round": 0,
            "status": status,
            "auditor": {
                "identity": "compact-auditor",
                "lineage": "independent-lineage",
                "session_id": "compact-session",
                "independent_from": [contract["planning"]["planner_identity"]],
            },
            "reviewed_prompt_sha256": contract["prompt"]["sha256"],
            "reviewed_plan_sha256": self.plan_contract.planning_payload_hash(contract),
            "reviewed_clause_ids": [item["clause_id"] for item in contract["clauses"]],
            "reviewed_requirement_ids": [item["requirement_id"] for item in contract["requirements"]],
            "reviewed_task_ids": [item["task_id"] for item in contract["tasks"]],
            "attested_hard_risk_flags": [],
            "evidence": {
                "path": self.evidence.name,
                "sha256": self.plan_contract.file_hash(self.evidence),
            },
        }
        result.update(overrides)
        review_path = self.fixture.root / "combined-review.json"
        write_json(review_path, {"gate": "combined", "result": result})
        return review_path

    def record(self, review: Path, output_name: str = "reviewed.json") -> dict:
        output = self.fixture.root / output_name
        return self.plan_contract.command_record_review(
            argparse.Namespace(
                contract=str(self.contract_path),
                review=str(review),
                output=str(output),
            )
        )

    def test_pass_projects_once_with_shared_evidence(self) -> None:
        output = self.fixture.root / "reviewed.json"
        result = self.record(self.combined_review())
        reviewed = self.plan_contract.load_json(output, "reviewed")
        self.assertEqual(result["projections"], ["fidelity", "sufficiency"])
        self.assertEqual(reviewed["gates"]["fidelity"]["status"], "pass")
        self.assertEqual(reviewed["gates"]["fidelity"]["reviewed_task_ids"], [])
        self.assertEqual(
            reviewed["gates"]["sufficiency"]["reviewed_task_ids"],
            [item["task_id"] for item in self.contract["tasks"]],
        )
        self.assertEqual(reviewed["gates"]["fidelity"]["evidence"], reviewed["gates"]["sufficiency"]["evidence"])
        self.assertEqual(len(reviewed["planning"]["review_history"]), 2)

    def test_repair_projects_to_both_gates(self) -> None:
        self.record(self.combined_review("repair"))
        reviewed = self.plan_contract.load_json(self.fixture.root / "reviewed.json", "reviewed")
        self.assertEqual(reviewed["gates"]["fidelity"]["status"], "repair")
        self.assertEqual(reviewed["gates"]["sufficiency"]["status"], "repair")

    def test_mixed_round_is_refused(self) -> None:
        fidelity = self.combined_review()
        fidelity_payload = self.plan_contract.load_json(fidelity, "review")
        fidelity_payload["gate"] = "fidelity"
        fidelity_payload["result"]["reviewed_task_ids"] = []
        write_json(self.fixture.root / "fidelity.json", fidelity_payload)
        self.plan_contract.command_record_review(
            argparse.Namespace(
                contract=str(self.contract_path),
                review=str(self.fixture.root / "fidelity.json"),
                output=str(self.fixture.root / "fidelity-recorded.json"),
            )
        )
        self.contract_path = self.fixture.root / "fidelity-recorded.json"
        with self.assertRaisesRegex(self.plan_contract.ContractError, "both compact gates to be pending"):
            self.record(self.combined_review(), "mixed.json")

    def test_stale_prompt_and_plan_hashes_are_refused(self) -> None:
        with self.assertRaisesRegex(self.plan_contract.ContractError, "another prompt"):
            self.record(self.combined_review(reviewed_prompt_sha256="0" * 64), "stale-prompt.json")
        with self.assertRaisesRegex(self.plan_contract.ContractError, "another planning payload"):
            self.record(self.combined_review(reviewed_plan_sha256="0" * 64), "stale-plan.json")
        with self.assertRaisesRegex(self.plan_contract.ContractError, "Gate evidence hash mismatch"):
            self.record(
                self.combined_review(
                    evidence={"path": self.evidence.name, "sha256": "0" * 64}
                ),
                "stale-evidence.json",
            )

    def test_full_and_same_planner_are_refused(self) -> None:
        full = copy.deepcopy(self.contract)
        full["classification"]["tier"] = "full"
        full_path = self.fixture.root / "full.json"
        write_json(full_path, full)
        with self.assertRaisesRegex(self.plan_contract.ContractError, "only for compact"):
            self.plan_contract.command_record_review(
                argparse.Namespace(
                    contract=str(full_path),
                    review=str(self.combined_review()),
                    output=str(self.fixture.root / "full-out.json"),
                )
            )
        same_planner = self.combined_review()
        payload = self.plan_contract.load_json(same_planner, "review")
        payload["result"]["auditor"]["lineage"] = self.contract["planning"]["planner_lineage"]
        write_json(self.fixture.root / "same-planner.json", payload)
        with self.assertRaisesRegex(self.plan_contract.ContractError, "not independent"):
            self.record(self.fixture.root / "same-planner.json", "same-planner-out.json")

    def test_replay_is_immutable_and_materialized_result_cannot_be_recombined(self) -> None:
        review = self.combined_review()
        self.record(review)
        with self.assertRaisesRegex(self.plan_contract.ContractError, "overwrite immutable"):
            self.record(review)
        materialized = self.fixture.root / "reviewed.json"
        self.contract_path = materialized
        with self.assertRaisesRegex(self.plan_contract.ContractError, "both compact gates to be pending"):
            self.plan_contract.command_record_review(
                argparse.Namespace(
                    contract=str(materialized),
                    review=str(review),
                    output=str(self.fixture.root / "replay-materialized.json"),
                )
            )


if __name__ == "__main__":
    unittest.main()
