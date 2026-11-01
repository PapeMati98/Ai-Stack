from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import adaptive
import fleet


class GateAssessmentTests(unittest.TestCase):
    def setUp(self) -> None:
        billing = mock.patch.dict(
            os.environ,
            {"AGENT_FLEET_BILLING_MODE_PATH": str(Path(__file__).with_name("__missing_billing.json"))},
        )
        billing.start()
        self.addCleanup(billing.stop)
        temporary = tempfile.TemporaryDirectory(prefix="fleet-gate-assessment-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)

    def assessment(self, complexity: str = "medium") -> dict[str, object]:
        value = adaptive.fallback_assessment(
            kind="review", complexity=complexity, profiles=self.profiles
        )
        value.pop("assessment_source", None)
        value["data_handling"] = {
            "classification": "internal",
            "anthropic_zdr_authorization": None,
        }
        return value

    def write_assessment(self, name: str = "assessment.json", **changes: object) -> Path:
        value = self.assessment()
        value.update(changes)
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_real_parser_exposes_assessment_file_for_both_gates(self) -> None:
        parser = fleet.build_parser()
        for action in ("quality-gate", "security-gate"):
            with self.subTest(action=action):
                args = parser.parse_args([
                    action, "--contract", "contract.json", "--run-id", "run-1",
                    "--assessment-file", "assessment.json",
                ])
                self.assertEqual(args.assessment_file, "assessment.json")

    def test_real_main_dispatches_assessment_file_for_both_gates(self) -> None:
        assessment = self.write_assessment()
        for action in ("quality-gate", "security-gate"):
            with self.subTest(action=action), mock.patch.object(
                fleet, "run_quality_gate", return_value={"status": "PASS"}
            ) as gate:
                code = fleet.main([
                    action, "--contract", str(self.root / "contract.json"),
                    "--run-id", "run-1", "--assessment-file", str(assessment), "--dry-run",
                ])
                self.assertEqual(code, fleet.EXIT_SUCCESS)
                self.assertEqual(gate.call_args.kwargs["assessment_file"], str(assessment))

    def test_no_flag_preserves_legacy_none_through_real_main(self) -> None:
        with mock.patch.object(
            fleet, "run_quality_gate", return_value={"status": "PASS"}
        ) as gate:
            code = fleet.main([
                "quality-gate", "--contract", "contract.json", "--run-id", "run-1", "--dry-run"
            ])
        self.assertEqual(code, fleet.EXIT_SUCCESS)
        self.assertIsNone(gate.call_args.kwargs["assessment_file"])

    def test_invalid_assessments_refuse_before_runtime_discovery(self) -> None:
        invalid_json = self.root / "invalid-json.json"
        invalid_json.write_text("{", encoding="utf-8")
        invalid_schema = self.write_assessment("invalid-schema.json", schema_version=99)
        wrong_complexity = self.write_assessment("wrong-complexity.json", complexity="high")
        cases = (
            (self.root / "missing.json", adaptive.AdaptiveError),
            (invalid_json, adaptive.AdaptiveError),
            (invalid_schema, adaptive.AdaptiveError),
            (wrong_complexity, fleet.FleetError),
        )
        for path, error in cases:
            with self.subTest(path=path.name), mock.patch.object(
                fleet, "require_fresh_model_audit", return_value={}
            ), mock.patch.object(fleet, "live_readiness") as discovery:
                with self.assertRaises(error):
                    fleet.route_task(
                        "Review this candidate", profiles=self.profiles, kind="review",
                        complexity="medium", root=self.root, cwd=self.root,
                        assessment_file=str(path), mode="review",
                    )
                discovery.assert_not_called()

    def test_caller_relative_assessment_survives_gate_bundle_cwd_switch(self) -> None:
        caller = self.root / "caller"
        caller.mkdir()
        assessment = caller / "assessment.json"
        assessment.write_text(json.dumps(self.assessment()), encoding="utf-8")
        bundle = self.root / "bundle"
        bundle.mkdir()
        contract_path = self.root / "quality-contract.json"
        contract_path.write_text(json.dumps({
            "schema_version": 1, "group_id": "group-assessment-0001",
            "title": "Assessment forwarding", "acceptance_criteria": ["Forward assessment"],
            "integration_criteria": ["Routing remains compatible"], "risk": "medium",
            "max_repair_rounds": 1,
        }), encoding="utf-8")
        worker_run_id = "run-20260906T200000Z-aaaaaaaaaa"
        fleet.persist_run_record(self.root, {
            "schema_version": 4, "run_id": worker_run_id,
            "root_task_id": "task-worker-0001", "attempt_id": "attempt-worker-0001",
            "agent": "qwen", "adapter": "qwen", "model": "test-model",
            "execution_profile_id": "test-profile", "mode": "review", "kind": "review",
            "complexity": "medium", "workspace": str(self.root),
            "execution_workspace": str(self.root), "exit_code": 0,
            "run_status": "COMPLETED", "handoff_valid": True,
            "handoff_status": "completed", "handoff": {
                "status": "completed", "summary": "Reviewed.", "files_changed": [],
                "verification": ["Inspected."], "risks": [], "next_steps": [],
            }, "transport_outcome": {"status": "complete"},
            "token_usage": {"status": "known", "total_tokens": 10}, "evaluation": None,
        })
        ranked = [{
            "eligible": True, "launchable": True, "agent": "codex",
            "suggested_model": "gpt-5", "selected_execution_profile": {},
            "execution_profile_id": "test-profile", "assignment_score": 90,
            "score_version": 1, "gate_results": [], "routing_mode": "adaptive",
            "baseline_rank": 1,
        }]
        real_route_task = fleet.route_task

        old_cwd = Path.cwd()
        try:
            os.chdir(caller)
            with mock.patch.object(fleet, "validate_auditor_model", return_value=None), \
                 mock.patch.object(fleet, "prepare_quality_bundle", return_value=(bundle, {})), \
                 mock.patch.object(fleet, "build_quality_audit_task", return_value="Review"), \
                 mock.patch.object(fleet, "scan_sensitive_task", return_value=[]), \
                 mock.patch.object(fleet, "require_fresh_model_audit", return_value={}), \
                 mock.patch.object(fleet, "classify_task", return_value=("review", {})), \
                 mock.patch.object(fleet, "live_readiness", return_value=({}, {})), \
                 mock.patch.object(fleet, "read_usage", return_value=[]), \
                 mock.patch.object(fleet, "current_quota_views", return_value=({}, {})), \
                 mock.patch.object(fleet, "rank_profiles", return_value=ranked), \
                 mock.patch.object(fleet, "read_claude_billing_policy", return_value={}), \
                 mock.patch.object(fleet, "apply_billing_last_resort_order", side_effect=lambda rows, _policy: rows), \
                 mock.patch.object(fleet, "route_task", wraps=real_route_task) as routed, \
                 mock.patch.object(fleet, "quality_auditor_candidates", side_effect=fleet.FleetError("boundary")):
                with self.assertRaisesRegex(fleet.FleetError, "boundary"):
                    fleet.run_quality_gate(
                        self.root, profiles=self.profiles, contract_path=contract_path,
                        run_ids=[worker_run_id], previous_gate_id=None, auditor="auto",
                        auditor_model=None, complexity="medium", workspace=caller, timeout=300,
                        dry_run=False, output_root=None, assessment_file="assessment.json",
                    )
        finally:
            os.chdir(old_cwd)
        self.assertEqual(
            routed.call_args.kwargs["assessment_file"], str(assessment.resolve())
        )

    def test_actual_fable_retention_oracle_enforces_classification(self) -> None:
        profile = self.profiles["claude"]
        execution = profile["execution_profiles"]["claude-fable-5-1-medium-canary"]
        cases = ((None, False), ("unknown", False), ("confidential", False),
                 ("restricted", False), ("internal", True), ("public", True))
        for classification, expected in cases:
            assessment = {} if classification is None else {"data_handling": {
                "classification": classification, "anthropic_zdr_authorization": None}}
            with self.subTest(classification=classification):
                result = fleet.routing_v2.anthropic_retention_gate(
                    execution, profile=profile, assessment=assessment
                )
                self.assertEqual(result["passed"], expected)


if __name__ == "__main__":
    unittest.main()
