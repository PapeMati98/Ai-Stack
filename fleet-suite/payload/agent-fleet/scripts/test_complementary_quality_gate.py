"""Functional reviews cannot substitute for implementation security acceptance."""
import json
from pathlib import Path
import tempfile
import unittest

import fleet


class ComplementaryQualityGateTests(unittest.TestCase):
    def test_latest_attempt_is_scoped_to_review_purpose(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = [
                ("1111111111", "security", False, 0),
                ("2222222222", "quality", True, 1),
                ("3333333333", "quality", False, 2),
            ]
            for suffix, kind, complementary, attempt in rows:
                fleet.persist_quality_gate_record(root, {
                    "gate_id": "qgate-20260913T020000Z-" + suffix,
                    "group_id": "group-test-purpose", "gate_kind": kind,
                    "complementary_review": complementary, "attempt_index": attempt,
                    "created_at": "2026-09-13T02:00:00Z",
                })
            for suffix, kind, complementary, _ in rows:
                found = fleet.latest_quality_gate_for_group(root, "group-test-purpose",
                    gate_kind=kind, complementary_review=complementary)
                self.assertTrue(found["gate_id"].endswith(suffix))
            fleet.persist_quality_gate_record(root, {
                "gate_id": "qgate-20260913T030000Z-4444444444",
                "group_id": "group-test-purpose", "gate_kind": "security",
                "attempt_index": 1, "created_at": "2026-09-13T03:00:00Z",
            })
            found = fleet.latest_quality_gate_for_group(root, "group-test-purpose",
                gate_kind="security", complementary_review=False)
            self.assertTrue(found["gate_id"].endswith("4444444444"))
            with self.assertRaisesRegex(fleet.FleetError, "latest quality gate"):
                fleet.accept_quality_group(root,
                    gate_id="qgate-20260913T020000Z-1111111111",
                    decision="accept", quality_score=93, note="Stale security must fail.")

    def test_additional_quality_review_is_explicit(self):
        workers = [{"run_id": "worker", "required_gate_kind": "security"}]
        with self.assertRaisesRegex(fleet.FleetError, "security-gate"):
            fleet.validate_gate_review_purpose(workers, gate_kind="quality")
        fleet.validate_gate_review_purpose(
            workers, gate_kind="quality", complementary_review=True)
        fleet.validate_gate_review_purpose(workers, gate_kind="security")

    def test_wrong_kind_and_truthy_non_boolean_rejected(self):
        for kind in ("security", "typo"):
            with self.subTest(kind=kind), self.assertRaises(fleet.FleetError):
                fleet.validate_gate_review_purpose([], gate_kind=kind, complementary_review=True)
        for flag in (1, "true", None):
            with self.subTest(flag=flag), self.assertRaises(fleet.FleetError):
                fleet.validate_gate_review_purpose([], gate_kind="quality", complementary_review=flag)

    def test_complementary_pass_cannot_accept_security_required_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_id = "run-20260913T010000Z-aaaaaaaaaa"
            gate_id = "qgate-20260913T020000Z-bbbbbbbbbb"
            fleet.persist_run_record(root, {
                "schema_version": 4, "run_id": run_id, "root_task_id": "task-worker-0001",
                "agent": "grok", "model": "grok-4.7", "quality_gate_required": True,
                "required_gate_kind": "security", "evaluation": None,
            })
            fleet.persist_quality_gate_record(root, {
                "schema_version": 1, "gate_kind": "quality", "complementary_review": True,
                "gate_id": gate_id, "group_id": "group-quality-0001",
                "created_at": "2026-09-13T02:00:00Z", "attempt_index": 0,
                "workers": [{"root_task_id": "task-worker-0001", "run_id": run_id}],
                "verdict": "ACCEPT", "validation_error": None,
                "independence": {"passed": True}, "parent_acceptance": None,
            })
            with self.assertRaisesRegex(fleet.FleetError, "security-gate"):
                fleet.accept_quality_group(root, gate_id=gate_id, decision="accept",
                    quality_score=95, note="Functional acceptance is not security acceptance.")
            self.assertIsNone(fleet.load_run_record(root, run_id)[0]["evaluation"])

    def test_opencode_preserves_semantic_rejection_without_accepting(self):
        handoff = {"status": "completed", "summary": "Actual independent product review.",
            "files_changed": [], "verification": ["Checked product"], "risks": [],
            "next_steps": [], "quality_gate": {"verdict": "REPAIR"}}
        stream = json.dumps({"type": "text", "part": {"text": json.dumps(handoff)}})
        stream += "\n" + json.dumps({"type": "step_finish", "part": {"reason": "stop"}})
        projected = fleet.project_provider_handoff("opencode", stream,
            handoff_validator=lambda _: "A non-accepted quality item must name failed criteria")
        self.assertIsNone(projected["handoff"])
        self.assertEqual(projected["terminal_reason"], "stop")
        self.assertEqual(projected["validation_error"],
            "A non-accepted quality item must name failed criteria")

    def test_opencode_valid_handoff_unchanged(self):
        handoff = {"status": "completed", "summary": "Actual independent product review.",
            "files_changed": [], "verification": [], "risks": [], "next_steps": []}
        projected = fleet.project_provider_handoff("opencode",
            json.dumps({"type": "text", "part": {"text": json.dumps(handoff)}}),
            handoff_validator=lambda _: None)
        self.assertEqual(projected["handoff"], handoff)
        self.assertIsNone(projected["validation_error"])


if __name__ == "__main__":
    unittest.main()
