"""Tests for Qwen native --max-tool-calls stop attestation and telemetry."""
import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fleet


class QwenToolBudgetTelemetryTests(unittest.TestCase):
    def test_parser_exact_tool_call_line(self):
        line = "Run aborted: tool-call budget of 20 exceeded (--max-tool-calls); observed 21."
        result = fleet.qwen_native_budget_stop(line, exit_code=55)
        self.assertEqual(result, {"kind": "tool-calls", "limit": 20, "observed": 21})

    def test_parser_line_embedded_between_other_stderr_lines(self):
        stderr = (
            "2026-10-18 10:00:00 [info] starting process\n"
            "Run aborted: tool-call budget of 20 exceeded (--max-tool-calls); observed 21.\n"
            "2026-10-18 10:00:05 [info] process exiting with code 55\n"
        )
        result = fleet.qwen_native_budget_stop(stderr, exit_code=55)
        self.assertEqual(result, {"kind": "tool-calls", "limit": 20, "observed": 21})

    def test_parser_wall_clock_line(self):
        line = "Run aborted: wall-clock budget of 600s exceeded (--max-wall-time)."
        result = fleet.qwen_native_budget_stop(line, exit_code=55)
        self.assertEqual(result, {"kind": "wall-clock", "limit": 600, "observed": None})

    def test_parser_exit_code_filtering(self):
        line = "Run aborted: tool-call budget of 20 exceeded (--max-tool-calls); observed 21."
        for code in (53, 1, 0, None, True):
            with self.subTest(exit_code=code):
                self.assertIsNone(fleet.qwen_native_budget_stop(line, exit_code=code))

    def test_parser_invalid_stderr(self):
        for err in (None, "aborted"):
            with self.subTest(stderr=err):
                self.assertIsNone(fleet.qwen_native_budget_stop(err, exit_code=55))

    def test_parser_eleven_digits_not_matched(self):
        line = "Run aborted: tool-call budget of 20 exceeded (--max-tool-calls); observed 99999999999."
        self.assertIsNone(fleet.qwen_native_budget_stop(line, exit_code=55))

    def test_parser_prefixed_line_not_matched(self):
        line = "x Run aborted: tool-call budget of 20 exceeded (--max-tool-calls); observed 21."
        self.assertIsNone(fleet.qwen_native_budget_stop(line, exit_code=55))

    def test_provider_failure_reason(self):
        tool_call_line = "Run aborted: tool-call budget of 20 exceeded (--max-tool-calls); observed 21."
        reason = fleet.provider_failure_reason(
            adapter_id="qwen",
            exit_code=55,
            timed_out=False,
            stderr=tool_call_line,
            stdout="x",
            tool_budget_exceeded=True,
        )
        self.assertIn("QWEN TOOL-CALL BUDGET EXHAUSTED", reason)
        self.assertIn("--max-tool-calls 20, observed 21", reason)
        self.assertNotIn("PROVIDER FAILURE", reason)

        reason_false = fleet.provider_failure_reason(
            adapter_id="qwen",
            exit_code=55,
            timed_out=False,
            stderr=tool_call_line,
            stdout="x",
            tool_budget_exceeded=False,
        )
        self.assertIn("QWEN RUN BUDGET EXHAUSTED", reason_false)
        self.assertNotIn("TOOL-CALL", reason_false)

        reason_timeout = fleet.provider_failure_reason(
            adapter_id="qwen",
            exit_code=55,
            timed_out=True,
            stderr=tool_call_line,
            stdout="x",
            tool_budget_exceeded=True,
        )
        self.assertIn("PROVIDER FAILURE", reason_timeout)

    def test_provider_transport_outcome(self):
        for handoff_valid, expected_status in ((False, "failed"), (True, "partial")):
            with self.subTest(handoff_valid=handoff_valid):
                outcome = fleet.provider_transport_outcome(
                    "qwen",
                    exit_code=55,
                    terminal_reason=None,
                    timed_out=False,
                    handoff_valid=handoff_valid,
                    tool_budget_exceeded=True,
                )
                self.assertEqual(
                    outcome,
                    {"status": expected_status, "reason": "qwen-tool-budget-exhausted"},
                )

        for handoff_valid, expected_status in ((False, "failed"), (True, "partial")):
            with self.subTest(handoff_valid=handoff_valid, tool_budget_exceeded=False):
                outcome = fleet.provider_transport_outcome(
                    "qwen",
                    exit_code=55,
                    terminal_reason=None,
                    timed_out=False,
                    handoff_valid=handoff_valid,
                    tool_budget_exceeded=False,
                )
                self.assertEqual(
                    outcome,
                    {"status": expected_status, "reason": "qwen-run-budget-exhausted"},
                )

    def test_execute_single_propagation(self):
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        subcases = [
            (
                "Run aborted: tool-call budget of 20 exceeded (--max-tool-calls); observed 21.",
                True,
                21,
                "qwen-tool-budget-exhausted",
                ("QWEN TOOL-CALL BUDGET EXHAUSTED", "observed 21"),
                ("PROVIDER FAILURE",),
            ),
            (
                "Run aborted: tool-call budget of 7 exceeded (--max-tool-calls); observed 8.",
                False,
                None,
                "qwen-run-budget-exhausted",
                ("QWEN RUN BUDGET EXHAUSTED",),
                ("TOOL-CALL",),
            ),
            (
                "Run aborted: wall-clock budget of 600s exceeded (--max-wall-time).",
                False,
                None,
                "qwen-run-budget-exhausted",
                ("QWEN RUN BUDGET EXHAUSTED",),
                ("TOOL-CALL",),
            ),
        ]
        for (
            stderr_line,
            expected_budget_exceeded,
            expected_tool_calls,
            expected_reason,
            expected_error_substrs,
            forbidden_error_substrs,
        ) in subcases:
            with self.subTest(stderr=stderr_line), tempfile.TemporaryDirectory() as temp_root:
                root = Path(temp_root)
                child = {
                    "exit_code": 55,
                    "timed_out": False,
                    "stdout": "synthetic Qwen stream",
                    "stderr": stderr_line,
                    "model_tool_calls": None,
                    "tool_budget_exceeded": False,
                }
                projection = {
                    "handoff": None,
                    "candidate_text": None,
                    "terminal_reason": None,
                    "source": None,
                    "error": "Qwen terminal result has no valid handoff",
                }
                patches = [
                    mock.patch.dict(
                        os.environ,
                        {
                            "AGENT_FLEET_DEPTH": "0",
                            "AGENT_FLEET_OFFICE": "0",
                            "AGENT_FLEET_BILLING_MODE_PATH": str(root / "missing-billing.json"),
                        },
                    )
                ]
                patches.extend(
                    mock.patch.object(obj, name, return_value=value)
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
                    )
                )
                with contextlib.ExitStack() as stack:
                    for patcher in patches:
                        stack.enter_context(patcher)
                    result = fleet.execute_single(
                        "Synthetic Qwen budget fixture.",
                        profiles=profiles,
                        agent_id="qwen",
                        mode="review",
                        kind="review",
                        complexity="low",
                        model_override="qwen3.8-max",
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
                        max_model_tool_calls=20,
                    )
                self.assertEqual(result["status"], "FAILED")
                record = json.loads(Path(result["usage_record"]).read_text(encoding="utf-8"))
                self.assertEqual(record["run_status"], "FAILED")
                self.assertEqual(result["tool_budget_exceeded"], expected_budget_exceeded)
                self.assertEqual(record["tool_budget_exceeded"], expected_budget_exceeded)
                self.assertEqual(result["model_tool_calls"], expected_tool_calls)
                self.assertEqual(record["model_tool_calls"], expected_tool_calls)
                self.assertEqual(result["completion_reason"], expected_reason)
                self.assertEqual(record["completion_reason"], expected_reason)
                expected_outcome = {"status": "failed", "reason": expected_reason}
                self.assertEqual(result["transport_outcome"], expected_outcome)
                for expected_error_substr in expected_error_substrs:
                    self.assertIn(expected_error_substr, result["handoff_parse_error"])
                for forbidden_error_substr in forbidden_error_substrs:
                    self.assertNotIn(forbidden_error_substr, result["handoff_parse_error"])


if __name__ == "__main__":
    unittest.main()
