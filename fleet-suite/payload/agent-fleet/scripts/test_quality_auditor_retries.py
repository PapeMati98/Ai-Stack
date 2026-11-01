"""An invalid auditor response must not spend a worker's only repair round."""
import copy
import unittest
from pathlib import Path

import fleet


class AuditorRetryTests(unittest.TestCase):
    def setUp(self):
        self.contract = {"group_id": "group-auditor-retry", "max_repair_rounds": 1}
        self.workers = [{"run_id": "worker-1", "root_task_id": "task-1"}]
        self.previous = {
            "group_id": self.contract["group_id"], "attempt_index": 0,
            "contract_sha256": fleet.quality_contract_sha256(self.contract),
            "workers": copy.deepcopy(self.workers), "parent_acceptance": None,
            "result": None, "verdict": "BLOCKED",
            "validation_error": "Quality auditor did not return a complete terminal handoff",
        }

    def test_one_failed_auditor_retry_preserves_worker_round(self):
        self.assertEqual(fleet.validate_quality_lineage(self.contract, self.workers, self.previous), 0)
        self.assertEqual(fleet.quality_auditor_retry_count(self.previous), 1)

    def test_second_failed_auditor_retry_is_refused(self):
        self.previous["auditor_retry_count"] = 1
        with self.assertRaisesRegex(fleet.FleetError, "auditor.*retry.*exhausted"):
            fleet.validate_quality_lineage(self.contract, self.workers, self.previous)

    def test_failed_auditor_retry_cannot_change_workers(self):
        changed = [{"run_id": "worker-2", "root_task_id": "task-1"}]
        with self.assertRaisesRegex(fleet.FleetError, "same worker run set"):
            fleet.validate_quality_lineage(self.contract, changed, self.previous)

    def test_real_worker_repair_still_spends_a_worker_round(self):
        self.previous.update(auditor_retry_count=1, verdict="REPAIR",
            result={"items": [{**self.workers[0], "verdict": "REPAIR"}]})
        changed = [{"run_id": "worker-2", "root_task_id": "task-1"}]
        self.assertEqual(fleet.validate_quality_lineage(self.contract, changed, self.previous), 1)
        self.assertEqual(fleet.quality_auditor_retry_count(self.previous), 1)
        self.previous["attempt_index"] = 1
        with self.assertRaisesRegex(fleet.FleetError, "repair-round budget is exhausted"):
            fleet.validate_quality_lineage(self.contract, changed, self.previous)

    def test_real_repair_cannot_be_treated_as_infra_retry(self):
        self.previous.update(verdict="REPAIR", result={"items": [
            {**self.workers[0], "verdict": "REPAIR"}]})
        with self.assertRaisesRegex(fleet.FleetError, "needs a new run"):
            fleet.validate_quality_lineage(self.contract, self.workers, self.previous)

    def test_invalid_counter_and_unexplained_null_result_are_refused(self):
        for value in (True, -1, 2, "0"):
            with self.subTest(value=value):
                self.previous["auditor_retry_count"] = value
                with self.assertRaises(fleet.FleetError):
                    fleet.validate_quality_lineage(self.contract, self.workers, self.previous)
        self.previous.pop("auditor_retry_count")
        self.previous["verdict"] = "REPAIR"
        with self.assertRaisesRegex(fleet.FleetError, "failed auditor"):
            fleet.validate_quality_lineage(self.contract, self.workers, self.previous)

    def test_malformed_non_null_result_cannot_use_infra_retry(self):
        for value in ([], "incomplete", 0, False):
            with self.subTest(value=value):
                self.previous["result"] = value
                with self.assertRaisesRegex(fleet.FleetError, "result is invalid"):
                    fleet.validate_quality_lineage(self.contract, self.workers, self.previous)

    def test_invalid_worker_round_counter_is_refused(self):
        for value in (None, True, -1, "0"):
            with self.subTest(value=value):
                self.previous["attempt_index"] = value
                with self.assertRaisesRegex(fleet.FleetError, "worker repair counter is invalid"):
                    fleet.validate_quality_lineage(self.contract, self.workers, self.previous)

    def test_concise_handoff_guidance_keeps_exact_criteria(self):
        task = fleet.build_quality_audit_task(gate_id="gate-test", contract=self.contract,
            bundle=Path("bundle"), manifest={"workers": self.workers}, previous=None)
        self.assertIn("one complete valid JSON handoff", task)
        self.assertIn("Preserve every required field and criterion", task)
        self.assertIn("copy strings byte-for-byte", task)


if __name__ == "__main__":
    unittest.main()
