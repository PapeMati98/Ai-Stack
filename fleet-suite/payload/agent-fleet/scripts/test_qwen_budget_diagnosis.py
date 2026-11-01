"""Synthetic regression for Qwen's native exit 53/55 budget stops."""
import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fleet


class QwenBudgetDiagnosisTests(unittest.TestCase):
    def test_native_budget_stop_is_not_provider_failure(self):
        for code in (53, 55):
            with self.subTest(code=code):
                reason = fleet.provider_failure_reason(
                    adapter_id="qwen", stdout="native stream", stderr="aborted",
                    exit_code=code, timed_out=False,
                )
                self.assertIn("QWEN RUN BUDGET EXHAUSTED", reason)
                self.assertNotIn("PROVIDER FAILURE", reason)
                self.assertIn("handoff", reason.lower())

    def test_missing_handoff_stays_failed_with_budget_transport_reason(self):
        for code in (53, 55):
            with self.subTest(code=code):
                outcome = fleet.provider_transport_outcome("qwen", exit_code=code,
                    terminal_reason=None, timed_out=False, handoff_valid=False)
                self.assertEqual(outcome, {"status": "failed", "reason": "qwen-run-budget-exhausted"})
                self.assertEqual(fleet.completed_run_status(code, None, adapter_id="qwen",
                    timed_out=False), "FAILED")

    def test_valid_handoff_remains_partial(self):
        outcome = fleet.provider_transport_outcome("qwen", exit_code=55,
            terminal_reason=None, timed_out=False, handoff_valid=True)
        self.assertEqual(outcome, {"status": "partial", "reason": "qwen-run-budget-exhausted"})

    def test_parent_timeout_has_priority(self):
        reason = fleet.provider_failure_reason(adapter_id="qwen", stdout="",
            stderr="aborted", exit_code=55, timed_out=True)
        self.assertIn("PROVIDER FAILURE", reason)
        self.assertNotIn("QWEN RUN BUDGET EXHAUSTED", reason)
        self.assertEqual(fleet.provider_transport_outcome("qwen", exit_code=55,
            terminal_reason=None, timed_out=True), {"status": "partial", "reason": "parent-timeout"})

    def test_real_error_and_cancellation_are_not_budget_stops(self):
        for adapter, code in (("qwen", 1), ("qwen", 54), ("qwen", 130), ("grok", 55)):
            with self.subTest(adapter=adapter, code=code):
                reason = fleet.provider_failure_reason(adapter_id=adapter, stdout="x",
                    stderr="aborted", exit_code=code, timed_out=False)
                self.assertIn("PROVIDER FAILURE", reason)
                self.assertNotIn("QWEN RUN BUDGET EXHAUSTED", reason)

    def test_legacy_diagnostic_call_stays_compatible(self):
        self.assertIn("PROVIDER FAILURE", fleet.provider_failure_reason(
            stdout="x", stderr="aborted", exit_code=55))


    def test_execute_single_propagates_budget_diagnosis_to_run_and_attempt(self):
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        handoff = {"status": "completed", "summary": "Synthetic Qwen fixture.",
                   "files_changed": [], "verification": ["Mocked Fleet path reached telemetry."],
                   "risks": [], "next_steps": []}
        for code, candidate, status, transport in (
            (53, None, "FAILED", "failed"), (55, handoff, "PARTIAL", "partial")
        ):
            with self.subTest(exit_code=code), tempfile.TemporaryDirectory() as temp_root:
                root = Path(temp_root)
                child = {"exit_code": code, "timed_out": False,
                         "stdout": "synthetic Qwen stream",
                         "stderr": "Run aborted: native run budget exceeded",
                         "model_tool_calls": 0, "tool_budget_exceeded": False}
                projection = {"handoff": candidate,
                    "candidate_text": json.dumps(candidate) if candidate else None,
                    "terminal_reason": None, "source": "fixture" if candidate else None,
                    "error": None if candidate else "Qwen terminal result has no valid handoff"}
                patches = [mock.patch.dict(os.environ, {
                    "AGENT_FLEET_DEPTH": "0", "AGENT_FLEET_OFFICE": "0",
                    "AGENT_FLEET_BILLING_MODE_PATH": str(root / "missing-billing.json")})]
                patches.extend(mock.patch.object(obj, name, return_value=value)
                    for obj, name, value in (
                        (fleet, "resolve_executable", "synthetic-qwen.exe"),
                        (fleet, "probe_command", {"launchable": True, "exit_code": 0, "output": "ok"}),
                        (fleet, "profile_runtime_environment", {}),
                        (fleet, "profile_credential_status", {"ready": True}),
                        (fleet, "merge_credential_readiness", {"ready": True, "reasons": []}),
                        (fleet.routing_v2, "readiness_from_probes", {"ready": True, "reasons": []}),
                        (fleet.routing_v2, "runtime_model_visible", (True, "visible")),
                        (fleet, "fleet_runtime_attestation", {"status": "VERIFIED"}),
                        (fleet, "read_usage", []),
                        (fleet.adaptive, "latest_quotas", {}),
                        (fleet.quota_collectors, "collect_profile_quotas", ({}, {})),
                        (fleet, "qwen_model_registration", {"status": "registered", "registry_source": "fixture"}),
                        (fleet, "project_provider_handoff", projection),
                        (fleet, "qwen_model_identity", {"status": "verified", "expected_model": "qwen3.8-max",
                                                        "observed_model": "qwen3.8-max"}),
                        (fleet, "run_provider_child", {"child": child}),
                    ))
                with contextlib.ExitStack() as stack:
                    for patcher in patches:
                        stack.enter_context(patcher)
                    result = fleet.execute_single(
                        "Synthetic Qwen budget fixture.", profiles=profiles, agent_id="qwen",
                        mode="review", kind="review", complexity="low", model_override="qwen3.8-max",
                        workspace=root, timeout=5, allow_write=False, native_subagent_limit=0,
                        enable_web=False, inherit_api_keys=False, dry_run=False,
                        output_root=root / "evidence", root=root, knowledge_mode="none")
                self.assertEqual(result["status"], status)
                record = json.loads(Path(result["usage_record"]).read_text(encoding="utf-8"))
                self.assertEqual(record["run_status"], status)
                self.assertEqual(result["completion_reason"], "qwen-run-budget-exhausted")
                self.assertEqual(record["completion_reason"], "qwen-run-budget-exhausted")
                expected = {"status": transport, "reason": "qwen-run-budget-exhausted"}
                self.assertEqual(result["transport_outcome"], expected)
                attempt = result["provider_attempts"][0]
                self.assertEqual(attempt["transport_outcome"], expected)
                if candidate is None:
                    self.assertIn("QWEN RUN BUDGET EXHAUSTED", result["handoff_parse_error"])
                    self.assertNotIn("PROVIDER FAILURE", result["handoff_parse_error"])
                    self.assertIn("QWEN RUN BUDGET EXHAUSTED", attempt["handoff_error"])
                    self.assertNotIn("PROVIDER FAILURE", attempt["handoff_error"])

if __name__ == "__main__":
    unittest.main()
