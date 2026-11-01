"""Synthetic native token stops must remain failures with an accurate cause."""
import contextlib
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import fleet
import mistral_runtime


def export():
    return {"schema_version": 1, "vibe_version": "2.26.1", "outcome": "token_limit",
            "exit_code": 3, "stop_reason": "interrupted", "error": None}


class MistralBudgetDiagnosisTests(unittest.TestCase):
    def test_native_token_stop_has_budget_diagnostic(self):
        reason = fleet.provider_failure_reason(adapter_id="mistral", exit_code=3,
                                              native_export=export())
        self.assertIn("MISTRAL TOKEN BUDGET EXHAUSTED", reason)
        self.assertNotIn("PROVIDER FAILURE", reason)
        self.assertIn("MISTRAL TOKEN BUDGET", fleet.identity_gate_error(reason, "identity missing"))

    def test_transport_never_promotes_budget_stop_to_success(self):
        for valid in (False, True):
            with self.subTest(valid=valid):
                outcome = fleet.provider_transport_outcome("mistral", exit_code=3,
                    terminal_reason="failed", timed_out=False, handoff_valid=valid,
                    native_export=export())
                self.assertEqual(outcome, {"status": "partial" if valid else "failed",
                                           "reason": "mistral-token-budget-exhausted"})
        self.assertEqual(fleet.completed_run_status(3, None, adapter_id="mistral"), "FAILED")
        self.assertEqual(fleet.completed_run_status(3, {"status": "completed"}, adapter_id="mistral"), "FAILED")
        self.assertEqual(mistral_runtime.mistral_terminal_status(export())[0], "failed")

    def test_timeout_wins(self):
        self.assertIn("PROVIDER FAILURE", fleet.provider_failure_reason(adapter_id="mistral",
            exit_code=3, timed_out=True, native_export=export()))
        self.assertEqual(fleet.provider_transport_outcome("mistral", exit_code=3,
            terminal_reason="failed", timed_out=True, native_export=export()),
            {"status": "partial", "reason": "parent-timeout"})

    def test_missing_inconsistent_and_forged_evidence_stays_generic(self):
        values = [None, {}, "token_limit"]
        for key, value in (("schema_version", True), ("schema_version", 2),
                ("vibe_version", "foreign"), ("outcome", "failed"),
                ("outcome", "time_limit"), ("exit_code", True), ("exit_code", "3"),
                ("exit_code", 0), ("error", {"code": "provider"})):
            modified = copy.deepcopy(export())
            modified[key] = value
            values.append(modified)
        for value in values:
            with self.subTest(value=value):
                self.assertIn("PROVIDER FAILURE", fleet.provider_failure_reason(adapter_id="mistral",
                    exit_code=3, stdout='{"outcome":"token_limit"}', stderr="token_limit",
                    native_export=value))
                self.assertEqual(fleet.provider_transport_outcome("mistral", exit_code=3,
                    terminal_reason="failed", timed_out=False, native_export=value),
                    {"status": "failed", "reason": "exit-code-3"})

    def test_foreign_adapter_and_exit_cannot_gain_budget_label(self):
        for adapter, code in (("mistral", 1), ("mistral", 0), ("qwen", 3), ("codex", 3)):
            with self.subTest(adapter=adapter, code=code):
                reason = fleet.provider_failure_reason(adapter_id=adapter, exit_code=code,
                    native_export=export())
                self.assertNotIn("MISTRAL TOKEN BUDGET", reason or "")
                outcome = fleet.provider_transport_outcome(adapter, exit_code=code,
                    terminal_reason="failed", timed_out=False, native_export=export())
                self.assertNotEqual(outcome["reason"], "mistral-token-budget-exhausted")

    def test_legacy_call_and_native_limits_are_unchanged(self):
        self.assertIn("PROVIDER FAILURE", fleet.provider_failure_reason(exit_code=3))
        self.assertEqual(mistral_runtime.MISTRAL_MIN_SESSION_TOKENS, 262144)
        self.assertEqual(mistral_runtime.MISTRAL_MAX_TOKENS, 1048576)

    def test_execute_single_records_native_cause_without_retry_or_identity_promotion(self):
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        for identity_status in ("verified", "unavailable", "mismatch"):
            with self.subTest(identity=identity_status), tempfile.TemporaryDirectory() as temp_root:
                root = Path(temp_root)
                child = {"exit_code": 3, "timed_out": False, "stdout": "[]", "stderr": "",
                         "model_tool_calls": 0, "tool_budget_exceeded": False}
                identity = {"status": identity_status, "configured_status": "verified",
                            "error": "synthetic identity missing" if identity_status != "verified" else None}
                patches = [mock.patch.dict(os.environ, {"AGENT_FLEET_DEPTH": "0",
                    "AGENT_FLEET_OFFICE": "0", "AGENT_FLEET_BILLING_MODE_PATH": str(root / "missing.json")})]
                patches.extend(mock.patch.object(obj, name, return_value=value) for obj, name, value in (
                    (fleet, "resolve_executable", "synthetic-vibe.exe"),
                    (fleet, "probe_command", {"launchable": True, "exit_code": 0, "output": "ok"}),
                    (fleet, "profile_runtime_environment", {}),
                    (fleet, "profile_credential_status", {"ready": True}),
                    (fleet, "merge_credential_readiness", {"ready": True, "reasons": []}),
                    (fleet.routing_v2, "readiness_from_probes", {"ready": True, "reasons": []}),
                    (fleet.routing_v2, "runtime_model_visible", (True, "visible")),
                    (fleet, "fleet_runtime_attestation", {"status": "VERIFIED"}),
                    (fleet, "read_usage", []), (fleet.adaptive, "latest_quotas", {}),
                    (fleet.quota_collectors, "collect_profile_quotas", ({}, {})),
                    (fleet, "build_command", (["synthetic-vibe"], "", None)),
                    (mistral_runtime, "bind_tool_policy", None),
                    (mistral_runtime, "require_native_login", {"ready": True, "reason": "synthetic"}),
                    (mistral_runtime, "prepare_mistral_run", root),
                    (mistral_runtime, "harden_mistral_environment", {}),
                    (mistral_runtime, "environment_attestation", {}),
                    (mistral_runtime, "read_native_export", export()),
                    (mistral_runtime, "read_model_witness", {}),
                    (mistral_runtime, "mistral_model_identity", identity),
                ))
                with contextlib.ExitStack() as stack:
                    for patcher in patches:
                        stack.enter_context(patcher)
                    provider = stack.enter_context(mock.patch.object(fleet, "run_provider_child",
                        return_value={"child": child, "provider_execution_seconds": 0}))
                    result = fleet.execute_single("Synthetic native token stop.", profiles=profiles,
                        agent_id="mistral", mode="review", kind="review", complexity="low",
                        model_override=None, workspace=root, timeout=5, allow_write=False,
                        native_subagent_limit=0, enable_web=False, inherit_api_keys=False, dry_run=False,
                        output_root=root / "evidence", root=root, knowledge_mode="none")
                self.assertEqual(provider.call_count, 1)
                self.assertEqual(result["status"], "FAILED")
                self.assertIsNone(result["handoff"])
                self.assertEqual(result["model_identity"]["status"], identity_status)
                self.assertEqual(result["completion_reason"], "mistral-token-budget-exhausted")
                expected = {"status": "failed", "reason": "mistral-token-budget-exhausted"}
                self.assertEqual(result["transport_outcome"], expected)
                self.assertIn("MISTRAL TOKEN BUDGET EXHAUSTED", result["handoff_parse_error"])
                self.assertEqual(len(result["provider_attempts"]), 1)
                attempt = result["provider_attempts"][0]
                self.assertEqual(attempt["transport_outcome"], expected)
                self.assertIn("MISTRAL TOKEN BUDGET EXHAUSTED", attempt["handoff_error"])
                record = json.loads(Path(result["usage_record"]).read_text(encoding="utf-8"))
                self.assertEqual(record["completion_reason"], "mistral-token-budget-exhausted")
                self.assertEqual(record["run_status"], "FAILED")


if __name__ == "__main__":
    unittest.main()
