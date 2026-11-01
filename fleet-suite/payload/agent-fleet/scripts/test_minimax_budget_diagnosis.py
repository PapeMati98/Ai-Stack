"""Native MiniMax step stops are unfinished work with a precise cause."""
import contextlib
import json
import os
from pathlib import Path
import unittest
from unittest import mock
import uuid

import fleet


def envelope():
    return {"schemaVersion": 1, "type": "exec.result", "status": "limit_exceeded",
            "runId": "synthetic-run", "sessionId": "synthetic-session",
            "turnId": "synthetic-turn", "durationMs": 1,
            "model": {"providerId": "minimax", "modelId": "MiniMax-M3",
                      "providerSource": "provider", "providerKind": "minimax-managed"}}


class MiniMaxBudgetDiagnosisTests(unittest.TestCase):
    def test_native_step_stop_has_actionable_diagnostic(self):
        for suffix in ("", "\nINFO MCP shutdown"):
            reason = fleet.provider_failure_reason(adapter_id="minimax", exit_code=7,
                                                  stdout=json.dumps(envelope()) + suffix)
            self.assertIn("MINIMAX STEP BUDGET EXHAUSTED", reason)
            self.assertNotIn("PROVIDER FAILURE", reason)
            self.assertIn("narrow", reason.lower())

    def test_malformed_foreign_and_textual_evidence_stay_generic(self):
        texts = [None, "", "limit_exceeded", json.dumps(envelope())[:-3],
                 "INFO " + json.dumps(envelope()), '{"output":' + json.dumps(envelope()) + '}',
                 json.dumps(envelope()).replace('"schemaVersion": 1', '"schemaVersion": 1, "schemaVersion": 1'),
                 json.dumps(envelope()).replace('"status": "limit_exceeded"',
                                               '"status": "failed", "status": "limit_exceeded"')]
        for key, value in (("schemaVersion", True), ("schemaVersion", 2),
                           ("type", "exec.progress"), ("status", "failed"),
                           ("status", "succeeded"), ("model", {})):
            item = envelope(); item[key] = value; texts.append(json.dumps(item))
        for key, value in (("providerId", "foreign"), ("modelId", "foreign"),
                           ("providerSource", "output"), ("providerKind", "foreign")):
            item = envelope(); item["model"][key] = value; texts.append(json.dumps(item))
        for text in texts:
            with self.subTest(text=text):
                reason = fleet.provider_failure_reason(adapter_id="minimax", exit_code=7,
                    stdout=text, stderr="mcode exec limit_exceeded: The run reached its step limit.")
                self.assertIn("PROVIDER FAILURE", reason)

    def test_exact_exit_and_adapter_are_required(self):
        for adapter, code in (("minimax", 1), ("minimax", 0), ("minimax", True),
                              ("minimax", "7"), ("grok", 7), ("mistral", 7)):
            with self.subTest(adapter=adapter, code=code):
                reason = fleet.provider_failure_reason(adapter_id=adapter, exit_code=code,
                                                      stdout=json.dumps(envelope()))
                self.assertNotIn("MINIMAX STEP BUDGET", reason or "")

    def test_timeout_wins_over_native_stop(self):
        reason = fleet.provider_failure_reason(adapter_id="minimax", exit_code=7,
                                              timed_out=True, stdout=json.dumps(envelope()))
        self.assertIn("parent timeout", reason)
        self.assertNotIn("MINIMAX STEP BUDGET", reason)

    def test_transport_label_requires_native_evidence_and_never_means_complete(self):
        for valid in (False, True):
            result = fleet.provider_transport_outcome("minimax", exit_code=7,
                terminal_reason="limit_exceeded", timed_out=False, handoff_valid=valid,
                stdout=json.dumps(envelope()))
            self.assertEqual(result, {"status": "partial" if valid else "failed",
                                      "reason": "minimax-step-budget-exhausted"})
        for text in (None, "limit_exceeded", json.dumps(envelope())[:-3]):
            result = fleet.provider_transport_outcome("minimax", exit_code=7,
                terminal_reason="limit_exceeded", timed_out=False, stdout=text)
            self.assertEqual(result, {"status": "failed", "reason": "exit-code-7"})
        result = fleet.provider_transport_outcome("minimax", exit_code=7,
            terminal_reason="limit_exceeded", timed_out=True, stdout=json.dumps(envelope()))
        self.assertEqual(result, {"status": "partial", "reason": "parent-timeout"})

    def test_legacy_call_and_acceptance_are_unchanged(self):
        self.assertIn("PROVIDER FAILURE", fleet.provider_failure_reason(exit_code=7))
        self.assertEqual(fleet.completed_run_status(7, None, adapter_id="minimax"), "FAILED")
        self.assertEqual(fleet.completed_run_status(7, {"status": "completed"}, adapter_id="minimax"), "FAILED")
        projection = fleet.project_provider_handoff("minimax", json.dumps(envelope()))
        self.assertIsNone(projection["handoff"])
        self.assertEqual(projection["terminal_reason"], "limit_exceeded")

    def test_execute_single_persists_cause_without_retry_or_identity_promotion(self):
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        for identity_status in ("verified", "unavailable", "mismatch"):
            with self.subTest(identity=identity_status):
                # Retained fixture, normal inherited permissions; no tempfile cleanup or ACL repair.
                root = Path(os.environ["TEAM_TEST_ROOT"]) / ("minimax-" + uuid.uuid4().hex)
                root.mkdir()
                child = {"exit_code": 7, "timed_out": False, "stdout": json.dumps(envelope()),
                         "stderr": "", "model_tool_calls": None, "tool_budget_exceeded": False}
                identity = {"status": identity_status, "error": None if identity_status == "verified" else "synthetic identity missing"}
                values = (("resolve_executable", "synthetic-mcode.exe"),
                    ("probe_command", {"launchable": True, "exit_code": 0, "output": "ok"}),
                    ("profile_runtime_environment", {}), ("profile_credential_status", {"ready": True}),
                    ("merge_credential_readiness", {"ready": True, "reasons": []}),
                    ("fleet_runtime_attestation", {"status": "VERIFIED"}), ("read_usage", []),
                    ("build_command", (["synthetic-mcode"], "", None)), ("minimax_model_identity", identity))
                with contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.dict(os.environ, {"AGENT_FLEET_DEPTH": "0",
                        "AGENT_FLEET_OFFICE": "0", "AGENT_FLEET_BILLING_MODE_PATH": str(root / "missing.json")}))
                    for name, value in values:
                        stack.enter_context(mock.patch.object(fleet, name, return_value=value))
                    stack.enter_context(mock.patch.object(fleet.routing_v2, "readiness_from_probes",
                                                         return_value={"ready": True, "reasons": []}))
                    stack.enter_context(mock.patch.object(fleet.routing_v2, "runtime_model_visible", return_value=(True, "visible")))
                    stack.enter_context(mock.patch.object(fleet.adaptive, "latest_quotas", return_value={}))
                    stack.enter_context(mock.patch.object(fleet.quota_collectors, "collect_profile_quotas", return_value=({}, {})))
                    provider = stack.enter_context(mock.patch.object(fleet, "run_provider_child",
                        return_value={"child": child, "provider_execution_seconds": 0}))
                    result = fleet.execute_single("Synthetic step stop.", profiles=profiles,
                        agent_id="minimax", mode="review", kind="review", complexity="low",
                        model_override=None, workspace=root, timeout=5, allow_write=False,
                        native_subagent_limit=0, enable_web=False, inherit_api_keys=False,
                        dry_run=False, output_root=root / "evidence", root=root, knowledge_mode="none")
                self.assertEqual(provider.call_count, 1)
                self.assertEqual(result["status"], "FAILED")
                self.assertIsNone(result["handoff"])
                self.assertEqual(result["model_identity"]["status"], identity_status)
                self.assertEqual(result["completion_reason"], "minimax-step-budget-exhausted")
                expected = {"status": "failed", "reason": "minimax-step-budget-exhausted"}
                self.assertEqual(result["transport_outcome"], expected)
                self.assertIn("MINIMAX STEP BUDGET EXHAUSTED", result["handoff_parse_error"])
                self.assertEqual(len(result["provider_attempts"]), 1)
                attempt = result["provider_attempts"][0]
                self.assertEqual(attempt["transport_outcome"], expected)
                self.assertIn("MINIMAX STEP BUDGET EXHAUSTED", attempt["handoff_error"])
                record = json.loads(Path(result["usage_record"]).read_text(encoding="utf-8"))
                self.assertEqual(record["completion_reason"], "minimax-step-budget-exhausted")
                self.assertEqual(record["run_status"], "FAILED")


if __name__ == "__main__":
    unittest.main()
