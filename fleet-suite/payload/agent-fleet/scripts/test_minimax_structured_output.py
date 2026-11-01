"""MiniMax final answers are bound to the handoff schema natively (HD-20261013-61b381).

Measured 2026-10-14 with mcode 0.2.4 in the stack's synthetic directory:
- without --output-schema the model may answer with prose plus the echoed JSON
  Schema from the preamble; mcode prints status succeeded and that text as
  `output`, which Fleet 0.9.69-0.9.83 rejected as SCHEMA RETURNED INSTEAD OF DATA;
- with --output-schema a valid final answer arrives as a parsed object in
  `output` (structuredOutputMode native_strict), and an invalid final answer ends
  the run with status failed, output null and error.code STRUCTURED_OUTPUT_INVALID.
Every provider child in these tests is synthetic; no model CLI is called.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import fleet


def handoff() -> dict[str, object]:
    return {
        "status": "completed",
        "summary": "Synthetic MiniMax structured output.",
        "files_changed": [],
        "verification": ["Synthetic verification only."],
        "risks": [],
        "next_steps": [],
    }


def model() -> dict[str, str]:
    return {"providerId": "minimax", "modelId": "MiniMax-M3",
            "providerSource": "provider", "providerKind": "minimax-managed",
            "structuredOutputMode": "native_strict"}


def succeeded(output: object) -> dict[str, object]:
    return {"schemaVersion": 1, "type": "exec.result", "status": "succeeded",
            "runId": "synthetic-run", "sessionId": "synthetic-session", "turnId": "synthetic-turn",
            "output": output, "model": model(),
            "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}, "durationMs": 1}


def rejected(message: str = "Structured output was not valid JSON.") -> dict[str, object]:
    return {"schemaVersion": 1, "type": "exec.result", "status": "failed",
            "runId": "synthetic-run", "sessionId": "synthetic-session", "turnId": "synthetic-turn",
            "output": None,
            "error": {"category": "runtime", "code": "STRUCTURED_OUTPUT_INVALID",
                      "message": message, "retryable": True},
            "model": model(),
            "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}, "durationMs": 1}


def build(run_dir: Path, **overrides):
    kwargs = dict(prompt="Synthetic bounded task.", workspace=Path.cwd(), mode="review",
                  model="minimax/MiniMax-M3", complexity="medium", timeout=300, run_dir=run_dir,
                  native_subagent_limit=0, enable_web=False, materialize=True)
    kwargs.update(overrides)
    return fleet.build_command("minimax", "mcode.cmd", **kwargs)


# -1 on a base without the fix keeps the module importable so RED shows per test.
EXIT = getattr(fleet, "MINIMAX_STRUCTURED_OUTPUT_REJECTED_EXIT_CODE", -1)


class MiniMaxStructuredOutputCommandTests(unittest.TestCase):
    def test_default_handoff_schema_is_bound_to_the_native_output_schema_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            command, stdin_text, _ = build(Path(tmp))
        self.assertEqual(stdin_text, "Synthetic bounded task.")
        schema_path = Path(command[command.index("--output-schema") + 1])
        self.assertEqual(schema_path, fleet.HANDOFF_SCHEMA_PATH)
        self.assertTrue(schema_path.is_file())
        self.assertEqual(command.count("--output-schema"), 1)
        # the flag sits after the existing arguments; since 0.9.92 the output format is
        # stream-json (HD-20261015-374701, test_minimax_stream_salvage.py), nothing else changed
        self.assertEqual(command[command.index("--output-format") + 1], "stream-json")
        self.assertEqual(command[command.index("--permission") + 1], "smart")
        self.assertIn("--max-steps", command)

    def test_explicit_handoff_schema_is_materialized_and_bound(self):
        schema = json.loads(json.dumps(fleet.load_json(fleet.HANDOFF_SCHEMA_PATH, "handoff schema")))
        schema["properties"]["quality_gate"] = {"type": "object"}
        with tempfile.TemporaryDirectory() as tmp:
            command, _, _ = build(Path(tmp), handoff_schema=schema)
            schema_path = Path(command[command.index("--output-schema") + 1])
            self.assertEqual(schema_path, Path(tmp) / "expected-handoff.schema.json")
            self.assertEqual(json.loads(schema_path.read_text(encoding="utf-8")), schema)

    def test_headless_rule_names_instance_not_schema(self):
        schema = fleet.load_json(fleet.HANDOFF_SCHEMA_PATH, "handoff schema")
        prompt = fleet.build_worker_prompt(
            "Synthetic bounded task.", agent_id="minimax", adapter_id="minimax", mode="review",
            workspace=Path.cwd(), kind="review", complexity="medium", native_subagent_limit=0,
            enable_web=False, knowledge_snapshot=None, schema=schema)
        self.assertIn("MiniMax Code headless rules", prompt)
        self.assertIn("never the schema definition itself", prompt)
        self.assertIn("no properties/required/type keys", prompt)
        other = fleet.build_worker_prompt(
            "Synthetic bounded task.", agent_id="codebuddy", adapter_id="codebuddy", mode="review",
            workspace=Path.cwd(), kind="review", complexity="medium", native_subagent_limit=0,
            enable_web=False, knowledge_snapshot=None, schema=schema)
        self.assertNotIn("never the schema definition itself", other)


class MiniMaxStructuredOutputProjectionTests(unittest.TestCase):
    def test_parsed_object_output_is_the_handoff(self):
        projection = fleet.project_provider_handoff("minimax", json.dumps(succeeded(handoff())))
        self.assertIsNone(projection["error"])
        self.assertEqual(projection["handoff"], handoff())
        self.assertEqual(projection["source"], "minimax.exec.result.output")
        self.assertEqual(projection["terminal_reason"], "succeeded")

    def test_string_output_still_projects_and_schema_echo_still_rejects(self):
        projection = fleet.project_provider_handoff("minimax", json.dumps(succeeded(json.dumps(handoff()))))
        self.assertEqual(projection["handoff"], handoff())
        schema = fleet.load_json(fleet.HANDOFF_SCHEMA_PATH, "handoff schema")
        echo = "All files are in place. Now I'll deliver the final handoff as a single JSON object.\n\n" + json.dumps(schema)
        projection = fleet.project_provider_handoff("minimax", json.dumps(succeeded(echo)))
        self.assertIsNone(projection["handoff"])
        category, message = fleet.classify_handoff_rejection(projection["candidate_text"])
        self.assertEqual(category, "schema-not-data")
        self.assertIn("SCHEMA RETURNED INSTEAD OF DATA", message)

    def test_native_rejection_names_the_error_in_the_projection(self):
        projection = fleet.project_provider_handoff("minimax", json.dumps(rejected()))
        self.assertIsNone(projection["handoff"])
        self.assertEqual(projection["terminal_reason"], "failed")
        self.assertEqual(projection["error"],
                         "MiniMax exec result did not succeed: STRUCTURED_OUTPUT_INVALID "
                         "Structured output was not valid JSON.")
        bare = rejected(); del bare["error"]
        projection = fleet.project_provider_handoff("minimax", json.dumps(bare))
        self.assertEqual(projection["error"], "MiniMax exec result did not succeed")

    def test_terminal_completion_accepts_object_output_only_with_a_valid_handoff(self):
        self.assertTrue(fleet.minimax_terminal_completion(json.dumps(succeeded(handoff()))))
        self.assertTrue(fleet.minimax_terminal_completion(json.dumps(succeeded(json.dumps(handoff())))))
        broken = handoff(); broken["verification"] = []
        self.assertFalse(fleet.minimax_terminal_completion(json.dumps(succeeded(broken))))
        schema = fleet.load_json(fleet.HANDOFF_SCHEMA_PATH, "handoff schema")
        self.assertFalse(fleet.minimax_terminal_completion(json.dumps(succeeded(schema))))
        self.assertFalse(fleet.minimax_terminal_completion(json.dumps(succeeded(None))))
        self.assertFalse(fleet.minimax_terminal_completion(json.dumps(rejected())))


class MiniMaxStructuredOutputDiagnosisTests(unittest.TestCase):
    def test_native_rejection_has_actionable_diagnostic(self):
        for suffix in ("", "\nINFO MCP shutdown"):
            reason = fleet.provider_failure_reason(adapter_id="minimax", exit_code=EXIT,
                                                  stdout=json.dumps(rejected()) + suffix)
            self.assertIn("MINIMAX STRUCTURED OUTPUT REJECTED", reason)
            self.assertIn("Structured output was not valid JSON.", reason)
            self.assertNotIn("PROVIDER FAILURE", reason)
            self.assertNotIn("PROVIDER ERROR", reason)
            self.assertIn("re-receive", reason)

    def test_message_is_bounded_redacted_and_defaults_to_the_code(self):
        long = rejected("x" * 2000)
        reason = fleet.minimax_structured_output_rejected(json.dumps(long), EXIT)
        self.assertEqual(len(reason), fleet.PROVIDER_ERROR_DETAIL_CHARS)
        empty = rejected("   ")
        self.assertEqual(fleet.minimax_structured_output_rejected(json.dumps(empty), EXIT),
                         "STRUCTURED_OUTPUT_INVALID")
        # The synthetic key is assembled at runtime so the file itself never carries a key-shaped literal.
        fake_key = "sk-" + "synthetic-" + "ABCDEFGHIJKLMNOP" + "QRSTUVWXYZ0123456789"
        secret = rejected(f"token {fake_key} leaked")
        reason = fleet.minimax_structured_output_rejected(json.dumps(secret), EXIT)
        self.assertNotIn(fake_key, reason)
        self.assertIn("[REDACTED]", reason)

    def test_malformed_foreign_and_textual_evidence_stay_generic(self):
        texts = [None, "", "STRUCTURED_OUTPUT_INVALID", json.dumps(rejected())[:-3],
                 "INFO " + json.dumps(rejected()), '{"output":' + json.dumps(rejected()) + '}',
                 json.dumps(rejected()).replace('"schemaVersion": 1', '"schemaVersion": 1, "schemaVersion": 1'),
                 json.dumps(rejected()).replace('"status": "failed"', '"status": "succeeded", "status": "failed"')]
        for key, value in (("schemaVersion", True), ("schemaVersion", 2), ("type", "exec.progress"),
                           ("status", "succeeded"), ("status", "limit_exceeded"), ("output", "text"),
                           ("output", {}), ("model", {}), ("error", None), ("error", "STRUCTURED_OUTPUT_INVALID")):
            item = rejected(); item[key] = value; texts.append(json.dumps(item))
        for key, value in (("code", "RUNTIME_ERROR"), ("code", None), ("code", "structured_output_invalid")):
            item = rejected(); item["error"][key] = value; texts.append(json.dumps(item))
        texts.append(json.dumps(rejected()).replace('"durationMs": 1', '"durationMs": NaN'))
        texts.append(json.dumps(rejected()).replace('"durationMs": 1', '"durationMs": Infinity'))
        for key, value in (("providerId", "foreign"), ("modelId", "foreign"),
                           ("providerSource", "output"), ("providerKind", "foreign")):
            item = rejected(); item["model"][key] = value; texts.append(json.dumps(item))
        for text in texts:
            with self.subTest(text=text):
                self.assertIsNone(fleet.minimax_structured_output_rejected(text, EXIT))
                reason = fleet.provider_failure_reason(adapter_id="minimax", exit_code=EXIT, stdout=text)
                self.assertNotIn("MINIMAX STRUCTURED OUTPUT", reason or "")

    def test_missing_output_is_a_native_structured_rejection(self):
        omitted = rejected()
        del omitted["output"]
        self.assertEqual(
            fleet.minimax_structured_output_rejected(json.dumps(omitted), EXIT),
            "Structured output was not valid JSON.",
        )

    def test_exact_exit_and_adapter_are_required(self):
        for code in (EXIT + 1, 0, 7, True, str(EXIT), None):
            with self.subTest(code=code):
                self.assertIsNone(fleet.minimax_structured_output_rejected(json.dumps(rejected()), code))
        for adapter, code in (("minimax", EXIT + 1), ("minimax", 0), ("minimax", True),
                              ("minimax", str(EXIT)), ("grok", EXIT), ("mistral", EXIT), ("codex", EXIT)):
            with self.subTest(adapter=adapter, code=code):
                reason = fleet.provider_failure_reason(adapter_id=adapter, exit_code=code,
                                                      stdout=json.dumps(rejected()))
                self.assertNotIn("MINIMAX STRUCTURED OUTPUT", reason or "")
                outcome = fleet.provider_transport_outcome(adapter, exit_code=code if type(code) is int else 1,
                                                           terminal_reason="failed", timed_out=False,
                                                           stdout=json.dumps(rejected()))
                self.assertNotEqual(outcome.get("reason"), "minimax-structured-output-rejected")

    def test_step_budget_envelope_keeps_its_own_label(self):
        budget = rejected(); budget["status"] = "limit_exceeded"; del budget["error"]
        self.assertIsNone(fleet.minimax_structured_output_rejected(json.dumps(budget), 7))
        self.assertIn("MINIMAX STEP BUDGET EXHAUSTED",
                      fleet.provider_failure_reason(adapter_id="minimax", exit_code=7, stdout=json.dumps(budget)))
        self.assertEqual(fleet.provider_transport_outcome("minimax", exit_code=7, terminal_reason="limit_exceeded",
                                                          timed_out=False, stdout=json.dumps(budget)),
                         {"status": "failed", "reason": "minimax-step-budget-exhausted"})

    def test_timeout_wins_over_native_rejection(self):
        reason = fleet.provider_failure_reason(adapter_id="minimax", exit_code=EXIT,
                                              timed_out=True, stdout=json.dumps(rejected()))
        self.assertIn("parent timeout", reason)
        self.assertNotIn("MINIMAX STRUCTURED OUTPUT", reason)
        result = fleet.provider_transport_outcome("minimax", exit_code=EXIT, terminal_reason="failed",
                                                  timed_out=True, stdout=json.dumps(rejected()))
        self.assertEqual(result, {"status": "partial", "reason": "parent-timeout"})

    def test_transport_label_is_failed_and_run_status_stays_failed(self):
        result = fleet.provider_transport_outcome("minimax", exit_code=EXIT, terminal_reason="failed",
                                                  timed_out=False, stdout=json.dumps(rejected()))
        self.assertEqual(result, {"status": "failed", "reason": "minimax-structured-output-rejected"})
        # 0.9.92 (HD-20261015-374701): handoff_valid is True only for a handoff
        # re-received from the stream's final agent_message; the transport is then
        # complete and the handoff decides the run status (see
        # test_minimax_stream_salvage.py).
        result = fleet.provider_transport_outcome("minimax", exit_code=EXIT, terminal_reason="failed",
                                                  timed_out=False, handoff_valid=True, stdout=json.dumps(rejected()))
        self.assertEqual(result, {"status": "complete", "reason": "minimax-structured-output-recovered"})
        for text in (None, "failed", json.dumps(rejected())[:-3]):
            result = fleet.provider_transport_outcome("minimax", exit_code=EXIT, terminal_reason="failed",
                                                      timed_out=False, stdout=text)
            self.assertEqual(result, {"status": "failed", "reason": f"exit-code-{EXIT}"})
        self.assertEqual(fleet.completed_run_status(EXIT, None, adapter_id="minimax"), "FAILED")
        self.assertEqual(fleet.completed_run_status(EXIT, handoff(), adapter_id="minimax"), "FAILED")

    def test_execute_single_persists_cause_without_retry_or_identity_promotion(self):
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        for identity_status in ("verified", "unavailable"):
            with self.subTest(identity=identity_status):
                # Retained fixture, normal inherited permissions; no tempfile cleanup.
                root = Path(os.environ["TEAM_TEST_ROOT"]) / ("minimax-so-" + uuid.uuid4().hex)
                root.mkdir()
                child = {"exit_code": EXIT, "timed_out": False, "stdout": json.dumps(rejected()),
                         "stderr": "", "model_tool_calls": None, "tool_budget_exceeded": False}
                identity = {"status": identity_status,
                            "error": None if identity_status == "verified" else "synthetic identity missing"}
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
                    stack.enter_context(mock.patch.object(fleet.routing_v2, "runtime_model_visible",
                                                          return_value=(True, "visible")))
                    stack.enter_context(mock.patch.object(fleet.adaptive, "latest_quotas", return_value={}))
                    stack.enter_context(mock.patch.object(fleet.quota_collectors, "collect_profile_quotas",
                                                          return_value=({}, {})))
                    provider = stack.enter_context(mock.patch.object(fleet, "run_provider_child",
                        return_value={"child": child, "provider_execution_seconds": 0}))
                    result = fleet.execute_single("Synthetic structured output rejection.", profiles=profiles,
                        agent_id="minimax", mode="review", kind="review", complexity="low",
                        model_override=None, workspace=root, timeout=5, allow_write=False,
                        native_subagent_limit=0, enable_web=False, inherit_api_keys=False,
                        dry_run=False, output_root=root / "evidence", root=root, knowledge_mode="none")
                self.assertEqual(provider.call_count, 1)
                self.assertEqual(result["status"], "FAILED")
                self.assertIsNone(result["handoff"])
                self.assertEqual(result["model_identity"]["status"], identity_status)
                self.assertEqual(result["completion_reason"], "minimax-structured-output-rejected")
                expected = {"status": "failed", "reason": "minimax-structured-output-rejected"}
                self.assertEqual(result["transport_outcome"], expected)
                self.assertIn("MINIMAX STRUCTURED OUTPUT REJECTED", result["handoff_parse_error"])
                self.assertNotIn("SCHEMA RETURNED INSTEAD OF DATA", result["handoff_parse_error"])
                self.assertEqual(len(result["provider_attempts"]), 1)
                attempt = result["provider_attempts"][0]
                self.assertEqual(attempt["transport_outcome"], expected)
                self.assertIn("MINIMAX STRUCTURED OUTPUT REJECTED", attempt["handoff_error"])
                record = json.loads(Path(result["usage_record"]).read_text(encoding="utf-8"))
                self.assertEqual(record["completion_reason"], "minimax-structured-output-rejected")
                self.assertEqual(record["run_status"], "FAILED")

    def test_execute_single_accepts_parsed_object_output(self):
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        root = Path(os.environ["TEAM_TEST_ROOT"]) / ("minimax-so-ok-" + uuid.uuid4().hex)
        root.mkdir()
        child = {"exit_code": 0, "timed_out": False, "stdout": json.dumps(succeeded(handoff())),
                 "stderr": "", "model_tool_calls": None, "tool_budget_exceeded": False}
        values = (("resolve_executable", "synthetic-mcode.exe"),
                  ("probe_command", {"launchable": True, "exit_code": 0, "output": "ok"}),
                  ("profile_runtime_environment", {}), ("profile_credential_status", {"ready": True}),
                  ("merge_credential_readiness", {"ready": True, "reasons": []}),
                  ("fleet_runtime_attestation", {"status": "VERIFIED"}), ("read_usage", []),
                  ("build_command", (["synthetic-mcode"], "", None)),
                  ("minimax_model_identity", {"status": "verified", "error": None}))
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {"AGENT_FLEET_DEPTH": "0",
                "AGENT_FLEET_OFFICE": "0", "AGENT_FLEET_BILLING_MODE_PATH": str(root / "missing.json")}))
            for name, value in values:
                stack.enter_context(mock.patch.object(fleet, name, return_value=value))
            stack.enter_context(mock.patch.object(fleet.routing_v2, "readiness_from_probes",
                                                  return_value={"ready": True, "reasons": []}))
            stack.enter_context(mock.patch.object(fleet.routing_v2, "runtime_model_visible",
                                                  return_value=(True, "visible")))
            stack.enter_context(mock.patch.object(fleet.adaptive, "latest_quotas", return_value={}))
            stack.enter_context(mock.patch.object(fleet.quota_collectors, "collect_profile_quotas",
                                                  return_value=({}, {})))
            stack.enter_context(mock.patch.object(fleet, "run_provider_child",
                                                  return_value={"child": child, "provider_execution_seconds": 0}))
            result = fleet.execute_single("Synthetic structured output success.", profiles=profiles,
                agent_id="minimax", mode="review", kind="review", complexity="low",
                model_override=None, workspace=root, timeout=5, allow_write=False,
                native_subagent_limit=0, enable_web=False, inherit_api_keys=False,
                dry_run=False, output_root=root / "evidence", root=root, knowledge_mode="none")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["handoff"], handoff())
        self.assertEqual(result["completion_reason"], "completed")
        self.assertIsNone(result["handoff_parse_error"])
        self.assertEqual(result["provider_attempts"][0]["handoff_projection_source"], "minimax.exec.result.output")


if __name__ == "__main__":
    unittest.main(verbosity=2)
