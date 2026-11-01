"""Offline regressions for an attested Codex supervisor tool-budget stop."""
import contextlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import fleet


class CodexBudgetDiagnosisTests(unittest.TestCase):
    def test_attested_stop_names_budget(self):
        reason = fleet.provider_failure_reason(
            adapter_id="codex", exit_code=125, tool_budget_exceeded=True,
        )
        self.assertIn("CODEX TOOL BUDGET EXHAUSTED", reason)
        self.assertNotIn("PROVIDER FAILURE", reason)
        self.assertIn("identity rejected", fleet.identity_gate_error(reason, "identity rejected"))

    def test_attestation_exit_and_adapter_are_required(self):
        for adapter, code, evidence in (
            ("codex", 125, False), ("codex", 125, 1),
            ("codex", 125, "true"), ("codex", 1, True),
            ("claude", 125, True), ("qwen", 125, True),
        ):
            with self.subTest(adapter=adapter, code=code, evidence=evidence):
                reason = fleet.provider_failure_reason(
                    adapter_id=adapter, exit_code=code, tool_budget_exceeded=evidence,
                )
                self.assertIn("PROVIDER FAILURE", reason)
                outcome = fleet.provider_transport_outcome(
                    adapter, exit_code=code, timed_out=False, terminal_reason=None,
                    tool_budget_exceeded=evidence,
                )
                self.assertEqual(outcome, {"status": "failed", "reason": f"exit-code-{code}"})

    def test_timeout_wins(self):
        reason = fleet.provider_failure_reason(
            adapter_id="codex", exit_code=125, timed_out=True,
            tool_budget_exceeded=True,
        )
        self.assertIn("parent timeout", reason)
        self.assertNotIn("CODEX TOOL BUDGET", reason)
        self.assertEqual(fleet.provider_transport_outcome(
            "codex", exit_code=125, timed_out=True, terminal_reason=None,
            tool_budget_exceeded=True,
        ), {"status": "partial", "reason": "parent-timeout"})

    def test_valid_handoff_is_unfinished_and_rejected_handoff_fails(self):
        for valid in (False, True):
            with self.subTest(valid=valid):
                self.assertEqual(fleet.provider_transport_outcome(
                    "codex", exit_code=125, timed_out=False, terminal_reason=None,
                    handoff_valid=valid, tool_budget_exceeded=True,
                ), {"status": "partial" if valid else "failed",
                    "reason": "codex-tool-budget-exhausted"})
                self.assertEqual(fleet.completed_run_status(
                    125, {"status": "completed"} if valid else None,
                    adapter_id="codex", tool_budget_exceeded=True,
                ), "PARTIAL" if valid else "FAILED")
        self.assertEqual(fleet.completed_run_status(
            125, {"status": "completed"}, adapter_id="codex",
            tool_budget_exceeded=True, semantic_error="identity rejected",
        ), "FAILED")

    def test_execute_single_propagates_stop_and_keeps_parser_gates(self):
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        valid = {"status": "completed", "summary": "Synthetic Codex result.",
                 "files_changed": [], "verification": ["Synthetic fixture."],
                 "risks": [], "next_steps": []}
        cases = (
            (None, 125, True, False, "FAILED", "codex-tool-budget-exhausted"),
            (valid, 125, True, False, "PARTIAL", "codex-tool-budget-exhausted"),
            ({"summary": "Invalid handoff."}, 125, True, False,
             "FAILED", "codex-tool-budget-exhausted"),
            (valid, 125, False, False, "FAILED", "exit-code-125"),
            (valid, 1, True, False, "FAILED", "exit-code-1"),
            (valid, 125, True, True, "PARTIAL", "parent-timeout"),
        )
        for handoff, code, evidence, timed_out, expected_status, expected_reason in cases:
            with self.subTest(handoff=handoff, code=code, evidence=evidence,
                              timed_out=timed_out), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                stdout = json.dumps({"type": "thread.started", "thread_id": "synthetic"})
                stdout += "\n" + json.dumps({"type": "turn.started"})
                if handoff is not None:
                    stdout += "\n" + json.dumps({"type": "item.completed", "item": {
                        "type": "agent_message", "text": json.dumps(handoff)}})
                    stdout += "\n" + json.dumps({"type": "turn.completed"})
                child = {"exit_code": code, "timed_out": timed_out,
                         "stdout": stdout, "stderr": "Synthetic supervisor stop.",
                         "model_tool_calls": 49, "tool_budget_exceeded": evidence}
                patches = [mock.patch.dict(os.environ, {
                    "AGENT_FLEET_DEPTH": "0", "AGENT_FLEET_OFFICE": "0",
                    "AGENT_FLEET_BILLING_MODE_PATH": str(root / "missing-billing.json"),
                })]
                patches.extend(mock.patch.object(obj, name, return_value=value)
                    for obj, name, value in (
                        (fleet, "resolve_executable", "synthetic-codex.exe"),
                        (fleet, "probe_command", {"launchable": True, "exit_code": 0, "output": "ok"}),
                        (fleet, "codex_mcp_servers", {"status": "ok", "servers": [], "enabled": []}),
                        (fleet, "profile_runtime_environment", {}),
                        (fleet, "profile_credential_status", {"ready": True}),
                        (fleet, "merge_credential_readiness", {"ready": True, "reasons": []}),
                        (fleet.routing_v2, "readiness_from_probes", {"ready": True, "reasons": []}),
                        (fleet.routing_v2, "runtime_model_visible", (True, "visible")),
                        (fleet, "fleet_runtime_attestation", {"status": "VERIFIED"}),
                        (fleet, "read_usage", []),
                        (fleet.adaptive, "latest_quotas", {}),
                        (fleet.quota_collectors, "collect_profile_quotas", ({}, {})),
                        (fleet, "build_command", (["synthetic-codex"], "", None)),
                    ))
                with contextlib.ExitStack() as stack:
                    for patcher in patches:
                        stack.enter_context(patcher)
                    provider = stack.enter_context(mock.patch.object(
                        fleet, "run_provider_child", return_value={"child": child}))
                    result = fleet.execute_single(
                        "Synthetic Codex budget fixture.", profiles=profiles,
                        agent_id="codex", mode="advisory", kind="implementation",
                        complexity="low", model_override="gpt-6-luna", workspace=root,
                        timeout=5, allow_write=False, native_subagent_limit=0,
                        enable_web=False, inherit_api_keys=False, dry_run=False,
                        output_root=root / "evidence", root=root, knowledge_mode="none",
                        max_model_tool_calls=48,
                    )
                self.assertEqual(provider.call_count, 1)
                self.assertEqual(provider.call_args.kwargs["max_model_tool_calls"], 48)
                self.assertEqual(result["status"], expected_status)
                self.assertEqual(result["transport_outcome"]["reason"], expected_reason)
                self.assertEqual(len(result["provider_attempts"]), 1)
                attempt = result["provider_attempts"][0]
                self.assertEqual(attempt["transport_outcome"]["reason"], expected_reason)
                if expected_reason == "codex-tool-budget-exhausted":
                    self.assertEqual(result["completion_reason"], expected_reason)
                    if expected_status == "FAILED":
                        self.assertIsNone(result["handoff"])
                        self.assertTrue(result["handoff_parse_error"])
                        self.assertTrue(attempt["handoff_error"])
                        if handoff is None:
                            self.assertIn("CODEX TOOL BUDGET EXHAUSTED", result["handoff_parse_error"])
                            self.assertIn("CODEX TOOL BUDGET EXHAUSTED", attempt["handoff_error"])
                    else:
                        self.assertEqual(result["handoff"], valid)
                if timed_out:
                    self.assertEqual(result["completion_reason"], "parent-timeout")


if __name__ == "__main__":
    unittest.main()
