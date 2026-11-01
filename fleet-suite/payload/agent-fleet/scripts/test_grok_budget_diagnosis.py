"""Synthetic regressions for the adapter's attested Grok tool budget stop."""
import contextlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import fleet


class GrokBudgetDiagnosisTests(unittest.TestCase):
    def test_attested_budget_is_not_a_provider_failure(self):
        reason = fleet.provider_failure_reason(
            adapter_id="grok", stdout="synthetic envelope", stderr="",
            exit_code=125, tool_budget_exceeded=True,
        )
        self.assertIn("GROK TOOL BUDGET EXHAUSTED", reason)
        self.assertNotIn("PROVIDER FAILURE", reason)

    def test_budget_diagnostic_preserves_identity_rejection(self):
        reason = fleet.provider_failure_reason(
            adapter_id="grok", stdout="synthetic envelope", exit_code=125,
            tool_budget_exceeded=True,
        )
        error = fleet.identity_gate_error(reason, "No provider model identity")
        self.assertIn("GROK TOOL BUDGET EXHAUSTED", error)
        self.assertIn("No provider model identity", error)

    def test_transport_stays_incomplete(self):
        for valid in (False, True):
            with self.subTest(handoff_valid=valid):
                outcome = fleet.provider_transport_outcome(
                    "grok", exit_code=125, timed_out=False,
                    terminal_reason="end_turn", handoff_valid=valid,
                    tool_budget_exceeded=True,
                )
                self.assertEqual(outcome["status"], "partial" if valid else "failed")
                self.assertEqual(outcome["reason"], "grok-tool-budget-exhausted")
        handoff = {"status": "completed"}
        self.assertEqual(
            fleet.completed_run_status(
                125, handoff, adapter_id="grok", tool_budget_exceeded=True
            ),
            "PARTIAL",
        )
        self.assertEqual(
            fleet.completed_run_status(
                125, None, adapter_id="grok", tool_budget_exceeded=True
            ),
            "FAILED",
        )

    def test_timeout_has_priority(self):
        reason = fleet.provider_failure_reason(
            adapter_id="grok", exit_code=125, timed_out=True,
            tool_budget_exceeded=True,
        )
        self.assertIn("PROVIDER FAILURE", reason)
        self.assertNotIn("GROK TOOL BUDGET", reason)
        self.assertEqual(fleet.provider_transport_outcome(
            "grok", exit_code=125, timed_out=True, terminal_reason="error",
            tool_budget_exceeded=True,
        ), {"status": "partial", "reason": "parent-timeout"})

    def test_exit_code_alone_is_not_budget_evidence(self):
        reason = fleet.provider_failure_reason(adapter_id="grok", exit_code=125)
        self.assertIn("PROVIDER FAILURE", reason)
        self.assertEqual(fleet.provider_transport_outcome(
            "grok", exit_code=125, timed_out=False, terminal_reason="error",
        ), {"status": "failed", "reason": "exit-code-125"})
        for evidence in (False, 1, "true"):
            with self.subTest(tool_budget_evidence=evidence):
                reason = fleet.provider_failure_reason(
                    adapter_id="grok", exit_code=125,
                    tool_budget_exceeded=evidence,
                )
                self.assertIn("PROVIDER FAILURE", reason)

    def test_foreign_adapter_or_other_exit_does_not_gain_budget_label(self):
        for adapter, code in (("qwen", 125), ("claude", 125), ("grok", 1)):
            with self.subTest(adapter=adapter, code=code):
                reason = fleet.provider_failure_reason(
                    adapter_id=adapter, exit_code=code, tool_budget_exceeded=True,
                )
                self.assertIn("PROVIDER FAILURE", reason)
                self.assertNotIn("GROK TOOL BUDGET", reason)

    def test_execute_single_propagates_budget_diagnosis_and_rejects_unverified_handoff(self):
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        cases = (
            ("{}", None, "unavailable"),
            (json.dumps({"modelId": "foreign-model"}), None, "mismatch"),
        )
        for stdout, candidate, identity_status in cases:
            with self.subTest(identity_status=identity_status), tempfile.TemporaryDirectory() as temp_root:
                root = Path(temp_root)
                child = {
                    "exit_code": 125,
                    "timed_out": False,
                    "stdout": stdout,
                    "stderr": "",
                    "model_tool_calls": 41,
                    "tool_budget_exceeded": True,
                    "prompt_count": 1,
                    "attempts": [],
                }
                projection = {
                    "handoff": candidate,
                    "candidate_text": stdout,
                    "terminal_reason": None,
                    "source": "fixture" if candidate else None,
                    "error": None if candidate else "Grok terminal result has no valid handoff",
                }
                provider_calls = []

                def run_session(*args, **kwargs):
                    provider_calls.append(kwargs)
                    return dict(child)
                patches = [mock.patch.dict(os.environ, {
                    "AGENT_FLEET_DEPTH": "0",
                    "AGENT_FLEET_OFFICE": "0",
                    "AGENT_FLEET_BILLING_MODE_PATH": str(root / "missing-billing.json"),
                })]
                patches.extend(mock.patch.object(obj, name, return_value=value)
                    for obj, name, value in (
                        (fleet, "resolve_executable", "synthetic-grok.exe"),
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
                        (fleet, "project_provider_handoff", projection),
                        (fleet, "build_command", (["synthetic-grok"], "", None)),
                        (fleet.grok_acp, "build_environment", {}),
                    ))
                with contextlib.ExitStack() as stack:
                    for patcher in patches:
                        stack.enter_context(patcher)
                    stack.enter_context(
                        mock.patch.object(fleet.grok_acp, "run_session", run_session)
                    )
                    result = fleet.execute_single(
                        "Synthetic Grok budget fixture.",
                        profiles=profiles,
                        agent_id="grok",
                        mode="review",
                        kind="review",
                        complexity="low",
                        model_override=None,
                        workspace=root,
                        timeout=5,
                        allow_write=False,
                        native_subagent_limit=0,
                        enable_web=False,
                        inherit_api_keys=False,
                        dry_run=False,
                        output_root=root / "evidence",
                        root=root,
                        knowledge_mode="none",
                    )

                self.assertEqual(len(provider_calls), 1)
                self.assertEqual(
                    provider_calls[0]["max_model_tool_calls"],
                    fleet.grok_acp.GROK_TOOL_LIMITS["low"],
                )
                self.assertEqual(
                    provider_calls[0]["max_additional_prompts"],
                    fleet.GROK_MAX_SAME_SESSION_REPAIRS["low"],
                )
                self.assertEqual(result["status"], "FAILED")
                self.assertEqual(result["model_identity"]["status"], identity_status)
                self.assertIsNone(result["handoff"])
                expected_transport = {
                    "status": "failed",
                    "reason": "grok-tool-budget-exhausted",
                }
                self.assertEqual(result["transport_outcome"], expected_transport)
                self.assertEqual(
                    result["completion_reason"], "grok-tool-budget-exhausted"
                )
                self.assertEqual(len(result["provider_attempts"]), 1)
                attempt = result["provider_attempts"][0]
                self.assertEqual(attempt["transport_outcome"], expected_transport)
                self.assertIn(
                    "GROK TOOL BUDGET EXHAUSTED", result["handoff_parse_error"]
                )
                self.assertIn("GROK TOOL BUDGET EXHAUSTED", attempt["handoff_error"])
                self.assertIn("exact-model gate failed", result["handoff_parse_error"])


if __name__ == "__main__":
    unittest.main()
