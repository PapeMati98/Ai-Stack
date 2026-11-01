"""MiniMax final answers survive a native structured-output rejection (HD-20261015-374701).

Measured 2026-10-15 with mcode 0.2.4 in the stack's synthetic directory (evidence
3747019db3b9/synthetic): `exec --output-format json --output-schema` prints the
exec.result envelope with output null whenever the validator refuses the final
text (prose, a code fence, an extra key), so the model's answer was lost; with
`--output-format stream-json` the same run streams item.completed agent_message
events carrying that text and ends with exec.completed whose `result` is the
very envelope json mode printed. Fleet now asks for stream-json, folds the stream
back into the json-mode envelope right after the process ends and re-receives the
final agent_message through its own lenient parser when the native validator
refused it. mcode itself still lingers ~950 s after the envelope (HD-0344ec), so
the terminal predicate must recognise the exec.completed event.
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
import process_supervision

EXIT = fleet.MINIMAX_STRUCTURED_OUTPUT_REJECTED_EXIT_CODE


def handoff() -> dict[str, object]:
    return {
        "status": "completed",
        "summary": "Synthetic MiniMax stream salvage.",
        "files_changed": [],
        "verification": ["Synthetic verification only."],
        "risks": [],
        "next_steps": [],
    }


def model() -> dict[str, str]:
    return {"providerId": "minimax", "modelId": "MiniMax-M3", "variant": "thinking",
            "providerSource": "provider", "providerKind": "minimax-managed",
            "structuredOutputMode": "native_strict"}


def succeeded(output: object) -> dict[str, object]:
    return {"schemaVersion": 1, "type": "exec.result", "runId": "exec_turn_synthetic",
            "sessionId": "mvs_synthetic", "turnId": "turn_synthetic", "status": "succeeded",
            "output": output, "model": model(),
            "usage": {"inputTokens": 10, "outputTokens": 5, "reasoningTokens": 0,
                      "cacheReadTokens": 3, "cacheWriteTokens": 0, "totalTokens": 15},
            "durationMs": 1}


def rejected(message: str = "Structured output was not valid JSON.",
             code: str = "STRUCTURED_OUTPUT_INVALID") -> dict[str, object]:
    return {"schemaVersion": 1, "type": "exec.result", "runId": "exec_turn_synthetic",
            "sessionId": "mvs_synthetic", "turnId": "turn_synthetic", "status": "failed",
            "error": {"category": "runtime", "code": code, "message": message, "retryable": True},
            "model": model(),
            "usage": {"inputTokens": 10, "outputTokens": 5, "reasoningTokens": 0,
                      "cacheReadTokens": 3, "cacheWriteTokens": 0, "totalTokens": 15},
            "durationMs": 1}


def limit_exceeded() -> dict[str, object]:
    body = rejected(); body["status"] = "limit_exceeded"; del body["error"]
    return body


def event(sequence: int, kind: str, **fields: object) -> str:
    base = {"schemaVersion": 1, "sequence": sequence, "timestampMs": 1792044900000 + sequence,
            "runId": "exec_turn_synthetic", "sessionId": "mvs_synthetic",
            "turnId": "turn_synthetic", "type": kind}
    base.update(fields)
    return json.dumps(base, ensure_ascii=True, separators=(",", ":"))


def stream(result: dict[str, object] | None, message: str | None, *,
           trailer: str = "INFO MCP shutdown") -> str:
    lines = [event(1, "exec.started"), event(2, "session.started"), event(3, "turn.started"),
             event(4, "item.started", item={"id": "i1:reasoning", "type": "reasoning",
                                            "contentDelta": "Th"}),
             event(5, "item.completed", item={"id": "i1:reasoning", "type": "reasoning",
                                              "content": "Thinking only."})]
    if message is not None:
        lines.append(event(6, "item.started", item={"id": "i1:message", "type": "agent_message",
                                                    "contentDelta": message[:3]}))
        lines.append(event(7, "item.completed", item={"id": "i1:message", "type": "agent_message",
                                                      "content": message}))
    if result is not None:
        terminal = "turn.completed" if result.get("status") == "succeeded" else "turn.failed"
        lines.append(event(8, terminal, status=result.get("status"), durationMs=1))
        lines.append(event(9, "exec.completed", result=result))
    text = "\n".join(lines) + "\n"
    if trailer:
        text += trailer + "\n"
    return text


def fenced(value: dict[str, object]) -> str:
    return "All checks are done.\n\n```json\n" + json.dumps(value, indent=2) + "\n```\n"


def validator(value: dict[str, object]) -> str | None:
    return fleet.handoff_schema_validation_error(
        value, fleet.load_json(fleet.HANDOFF_SCHEMA_PATH, "handoff schema"))


class StreamCommandTests(unittest.TestCase):
    def test_minimax_argv_asks_for_stream_json_and_keeps_the_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            command, stdin_text, _ = fleet.build_command(
                "minimax", "mcode.cmd", prompt="Synthetic bounded task.", workspace=Path.cwd(),
                mode="review", model="minimax/MiniMax-M3", complexity="medium", timeout=300,
                run_dir=Path(tmp), native_subagent_limit=0, enable_web=False, materialize=True)
        self.assertEqual(stdin_text, "Synthetic bounded task.")
        self.assertEqual(command[command.index("--output-format") + 1], "stream-json")
        self.assertEqual(command.count("--output-format"), 1)
        self.assertEqual(Path(command[command.index("--output-schema") + 1]), fleet.HANDOFF_SCHEMA_PATH)
        self.assertEqual(command.count("--output-schema"), 1)
        self.assertIn("--max-steps", command)


class StreamFoldTests(unittest.TestCase):
    def test_json_mode_envelope_is_not_a_stream(self):
        text = json.dumps(succeeded(handoff())) + "\nINFO MCP shutdown\n"
        fold = fleet.minimax_stream_fold(text)
        self.assertFalse(fold["folded"])
        self.assertEqual(fold["stdout"], text)
        self.assertIsNone(fold["final_message"])
        for other in (None, "", "   ", "not json", '{"type":"exec.result"}', "[1,2]"):
            with self.subTest(other=other):
                fold = fleet.minimax_stream_fold(other)
                self.assertFalse(fold["folded"])
                self.assertEqual(fold["stdout"], other)

    def test_succeeded_stream_folds_to_the_json_mode_envelope(self):
        result = succeeded(handoff())
        fold = fleet.minimax_stream_fold(stream(result, json.dumps(handoff())))
        self.assertTrue(fold["folded"])
        self.assertTrue(fold["has_result"])
        self.assertEqual(fold["event_count"], 9)
        self.assertEqual(fold["final_message"], json.dumps(handoff()))
        envelope, error = fleet.parse_minimax_exec_result_envelope(fold["stdout"])
        self.assertIsNone(error)
        self.assertEqual(envelope, result)
        self.assertTrue(fold["stdout"].startswith("{"))
        self.assertTrue(fold["stdout"].endswith("INFO MCP shutdown\n"))
        projection = fleet.project_provider_handoff("minimax", fold["stdout"])
        self.assertEqual(projection["handoff"], handoff())
        self.assertEqual(projection["source"], "minimax.exec.result.output")
        identity = fleet.minimax_model_identity(fold["stdout"], expected_runtime_model="minimax/MiniMax-M3")
        self.assertEqual(identity["status"], "verified")
        usage = fleet.aggregate_token_usage([fold["stdout"]], adapter="minimax")
        self.assertEqual(usage["status"], "known")
        self.assertEqual(usage["output_tokens"], 5)
        self.assertTrue(fleet.minimax_terminal_completion(fold["stdout"].splitlines()[0]))

    def test_cut_stream_has_no_envelope_but_keeps_the_message(self):
        fold = fleet.minimax_stream_fold(stream(None, "Half way through.", trailer=""))
        self.assertTrue(fold["folded"])
        self.assertFalse(fold["has_result"])
        self.assertEqual(fold["stdout"], "")
        self.assertEqual(fold["final_message"], "Half way through.")
        projection = fleet.project_provider_handoff("minimax", fold["stdout"],
                                                    final_message_text=fold["final_message"])
        self.assertIsNone(projection["handoff"])
        self.assertIn("not an exec.result envelope", projection["error"])

    def test_later_turn_resets_the_message_and_duplicate_keys_are_not_events(self):
        first = stream(rejected(), fenced(handoff()), trailer="")
        second = stream(succeeded(json.dumps(handoff())), None, trailer="").replace('"sequence":', '"sequence":1')
        fold = fleet.minimax_stream_fold(first + second)
        self.assertTrue(fold["folded"])
        self.assertIsNone(fold["final_message"])
        envelope, _ = fleet.parse_minimax_exec_result_envelope(fold["stdout"])
        self.assertEqual(envelope["status"], "succeeded")
        dup = event(9, "exec.completed", result=succeeded(handoff())).replace(
            '"schemaVersion":1', '"schemaVersion":1,"schemaVersion":1')
        self.assertIsNone(fleet.minimax_stream_event(dup))
        self.assertIsNone(fleet.minimax_stream_event(event(9, "exec.completed").replace(
            '"sequence":9', '"sequence":NaN')))
        self.assertIsNone(fleet.minimax_stream_event(json.dumps(succeeded(handoff()))))
        self.assertIsNone(fleet.minimax_stream_event(event(1, "exec.started").replace('"sequence":1', '"sequence":true')))
        self.assertIsNotNone(fleet.minimax_stream_event(event(1, "exec.started")))

    def test_empty_final_agent_message_clears_the_earlier_handoff(self):
        lines = [event(1, "exec.started"), event(2, "session.started"), event(3, "turn.started"),
                 event(4, "item.completed", item={"id": "i1:message", "type": "agent_message",
                                                  "content": fenced(handoff())}),
                 event(5, "item.completed", item={"id": "i2:message", "type": "agent_message",
                                                  "content": "   \n"}),
                 event(6, "turn.failed", status="failed", durationMs=1),
                 event(7, "exec.completed", result=rejected())]
        fold = fleet.minimax_stream_fold("\n".join(lines) + "\n")
        self.assertTrue(fold["folded"])
        self.assertTrue(fold["has_result"])
        self.assertIsNone(fold["final_message"])
        projection = fleet.project_provider_handoff("minimax", fold["stdout"],
                                                    final_message_text=fold["final_message"])
        self.assertIsNone(projection["handoff"])
        self.assertIn("Structured output was not valid JSON.", projection["error"])
        for broken in (None, 7, {"text": fenced(handoff())}):
            with self.subTest(content=broken):
                lines[4] = event(5, "item.completed", item={"id": "i2:message", "type": "agent_message",
                                                            "content": broken})
                self.assertIsNone(fleet.minimax_stream_fold("\n".join(lines) + "\n")["final_message"])
        lines[4] = event(5, "item.completed", item={"id": "i2:message", "type": "agent_message",
                                                    "content": "Second answer."})
        self.assertEqual(fleet.minimax_stream_fold("\n".join(lines) + "\n")["final_message"],
                         "Second answer.")

    def test_stream_bound_is_measured_in_utf8_bytes(self):
        raw = stream(rejected(), "Final.", trailer="INFO MCP shutdown caf" + chr(233) * 4)
        with mock.patch.object(fleet, "MINIMAX_STREAM_MAX_BYTES", len(raw)):
            self.assertFalse(fleet.minimax_stream_fold(raw)["folded"])
        with mock.patch.object(fleet, "MINIMAX_STREAM_MAX_BYTES", len(raw.encode("utf-8"))):
            self.assertTrue(fleet.minimax_stream_fold(raw)["folded"])

    def test_fold_child_keeps_raw_stream_and_telemetry(self):
        raw = stream(rejected(), fenced(handoff()))
        child = fleet.minimax_fold_child_stdout({"stdout": raw, "stderr": "", "exit_code": EXIT})
        self.assertEqual(child["stdout_stream"], raw)
        self.assertEqual(child["minimax_final_message"], fenced(handoff()))
        self.assertEqual(child["minimax_stream"], {"folded": True, "event_count": 9, "has_result": True,
                                                  "final_message_chars": len(fenced(handoff()))})
        self.assertEqual(fleet.minimax_structured_output_rejected(child["stdout"], EXIT),
                         "Structured output was not valid JSON.")
        plain = fleet.minimax_fold_child_stdout({"stdout": json.dumps(rejected()), "stderr": ""})
        self.assertNotIn("stdout_stream", plain)
        self.assertNotIn("minimax_final_message", plain)
        self.assertEqual(plain["minimax_stream"]["folded"], False)
        self.assertEqual(plain["stdout"], json.dumps(rejected()))


class TerminalCompletionTests(unittest.TestCase):
    def test_exec_completed_event_is_terminal_like_the_envelope(self):
        ok = event(9, "exec.completed", result=succeeded(handoff()))
        self.assertTrue(fleet.minimax_terminal_completion(ok))
        self.assertTrue(fleet.minimax_terminal_completion(json.dumps(succeeded(handoff()))))
        failed = fleet.minimax_terminal_completion(event(9, "exec.completed", result=rejected()))
        self.assertIsInstance(failed, process_supervision.TerminalFailureOutcome)
        self.assertEqual(failed.exit_code, EXIT)
        limit = fleet.minimax_terminal_completion(event(9, "exec.completed", result=limit_exceeded()))
        self.assertIsInstance(limit, process_supervision.TerminalFailureOutcome)
        self.assertEqual(limit.exit_code, 7)

    def test_other_stream_events_and_broken_results_are_not_terminal(self):
        broken = handoff(); broken["verification"] = []
        cases = [event(7, "item.completed", item={"id": "i", "type": "agent_message",
                                                  "content": json.dumps(handoff())}),
                 event(8, "turn.completed", status="succeeded"),
                 event(9, "exec.completed"),
                 event(9, "exec.completed", result="text"),
                 event(9, "exec.completed", result={"type": "exec.progress"}),
                 event(9, "exec.completed", result=succeeded(broken)),
                 event(9, "exec.completed", result=succeeded(None)),
                 event(9, "exec.completed", result=rejected(code="RUNTIME_ERROR")),
                 event(9, "exec.completed", result=succeeded(handoff())).replace(
                     '"schemaVersion":1', '"schemaVersion":1,"schemaVersion":1')]
        foreign = succeeded(handoff()); foreign["model"]["providerId"] = "foreign"
        cases.append(event(9, "exec.completed", result=foreign))
        for line in cases:
            with self.subTest(line=line[:80]):
                self.assertFalse(fleet.minimax_terminal_completion(line))


class RecoveryProjectionTests(unittest.TestCase):
    def folded(self, result, message):
        child = fleet.minimax_fold_child_stdout({"stdout": stream(result, message), "stderr": ""})
        return child["stdout"], child.get("minimax_final_message")

    def test_fenced_answer_is_re_received_after_native_rejection(self):
        text, message = self.folded(rejected(), fenced(handoff()))
        projection = fleet.project_provider_handoff("minimax", text, handoff_validator=validator,
                                                    final_message_text=message)
        self.assertEqual(projection["handoff"], handoff())
        self.assertEqual(projection["terminal_reason"], "failed")
        self.assertTrue(projection["source"].startswith(
            "minimax.stream.agent_message.recovered-after-native-rejection"))
        self.assertIsNone(projection["error"])
        self.assertIn("STRUCTURED_OUTPUT_INVALID", projection["native_error"])
        transport = fleet.provider_transport_outcome("minimax", exit_code=EXIT, terminal_reason="failed",
                                                     timed_out=False, handoff_valid=True, stdout=text)
        self.assertEqual(transport, {"status": "complete", "reason": "minimax-structured-output-recovered"})
        self.assertEqual(fleet.completed_run_status(EXIT, handoff(), adapter_id="minimax",
                                                    terminal_reason="failed", stdout=text), "COMPLETED")
        # Without the envelope the exit code alone still means failed (0.9.84 pin).
        self.assertEqual(fleet.completed_run_status(EXIT, handoff(), adapter_id="minimax",
                                                    terminal_reason="failed"), "FAILED")
        partial = handoff(); partial["status"] = "partial"; partial["next_steps"] = ["Finish."]
        self.assertEqual(fleet.completed_run_status(EXIT, partial, adapter_id="minimax",
                                                    terminal_reason="failed", stdout=text), "PARTIAL")
        self.assertIn("MINIMAX STRUCTURED OUTPUT REJECTED",
                      fleet.provider_failure_reason(adapter_id="minimax", exit_code=EXIT, stdout=text))

    def test_prose_only_answer_stays_failed_with_the_answer_as_candidate_text(self):
        text, message = self.folded(rejected(), "Review finished. No findings. Verification: none.")
        projection = fleet.project_provider_handoff("minimax", text, handoff_validator=validator,
                                                    final_message_text=message)
        self.assertIsNone(projection["handoff"])
        self.assertEqual(projection["terminal_reason"], "failed")
        self.assertIn("Review finished.", projection["candidate_text"])
        self.assertIn("STRUCTURED_OUTPUT_INVALID", projection["error"])
        transport = fleet.provider_transport_outcome("minimax", exit_code=EXIT, terminal_reason="failed",
                                                     timed_out=False, handoff_valid=False, stdout=text)
        self.assertEqual(transport, {"status": "failed", "reason": "minimax-structured-output-rejected"})
        self.assertEqual(fleet.completed_run_status(EXIT, None, adapter_id="minimax",
                                                    terminal_reason="failed"), "FAILED")

    def test_schema_echo_and_run_validator_rejections_are_not_recovered(self):
        schema = fleet.load_json(fleet.HANDOFF_SCHEMA_PATH, "handoff schema")
        echo = "Now I will deliver the final handoff as a single JSON object.\n\n" + json.dumps(schema)
        text, message = self.folded(rejected("Structured output did not match --output-schema: x"), echo)
        projection = fleet.project_provider_handoff("minimax", text, handoff_validator=validator,
                                                    final_message_text=message)
        self.assertIsNone(projection["handoff"])
        category, _ = fleet.classify_handoff_rejection(projection["candidate_text"])
        self.assertEqual(category, "schema-not-data")
        extra = handoff(); extra["office_replies"] = []
        text, message = self.folded(rejected(), fenced(extra))
        projection = fleet.project_provider_handoff("minimax", text, handoff_validator=validator,
                                                    final_message_text=message)
        self.assertIsNone(projection["handoff"])
        projection = fleet.project_provider_handoff("minimax", text, final_message_text=message)
        self.assertEqual(projection["handoff"], extra)

    def test_other_native_failures_and_missing_messages_never_recover(self):
        for result in (rejected(code="RUNTIME_ERROR"), limit_exceeded()):
            with self.subTest(status=result.get("status"), code=(result.get("error") or {}).get("code")):
                text, message = self.folded(result, fenced(handoff()))
                projection = fleet.project_provider_handoff("minimax", text, handoff_validator=validator,
                                                            final_message_text=message)
                self.assertIsNone(projection["handoff"])
                self.assertIn("candidate_text", projection)
        for message in (None, "", "   "):
            with self.subTest(message=message):
                self.assertIsNone(fleet.minimax_recovered_handoff(rejected(), message, handoff_validator=validator))
                projection = fleet.project_provider_handoff("minimax", json.dumps(rejected()),
                                                            handoff_validator=validator, final_message_text=message)
                self.assertIsNone(projection["handoff"])
                self.assertNotIn("candidate_text", projection)
        self.assertIsNone(fleet.minimax_recovered_handoff(succeeded(handoff()), fenced(handoff())))
        self.assertIsNone(fleet.minimax_recovered_handoff(None, fenced(handoff())))
        self.assertIsNone(fleet.minimax_recovered_handoff("failed", fenced(handoff())))


class ExecuteSingleTests(unittest.TestCase):
    def run_single(self, child, root):
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        values = (("resolve_executable", "synthetic-mcode.exe"),
                  ("probe_command", {"launchable": True, "exit_code": 0, "output": "ok"}),
                  ("profile_runtime_environment", {}), ("profile_credential_status", {"ready": True}),
                  ("merge_credential_readiness", {"ready": True, "reasons": []}),
                  ("fleet_runtime_attestation", {"status": "VERIFIED"}), ("read_usage", []),
                  ("build_command", (["synthetic-mcode"], "", None)))
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
            result = fleet.execute_single("Synthetic stream salvage.", profiles=profiles,
                agent_id="minimax", mode="review", kind="review", complexity="low",
                model_override="MiniMax-M3", workspace=root, timeout=5, allow_write=False,
                native_subagent_limit=0, enable_web=False, inherit_api_keys=False,
                dry_run=False, output_root=root / "evidence", root=root, knowledge_mode="none")
        self.assertEqual(provider.call_count, 1)
        return result

    def test_execute_single_re_receives_the_fenced_answer(self):
        # Retained fixture, normal inherited permissions; no tempfile cleanup.
        root = Path(os.environ["TEAM_TEST_ROOT"]) / ("minimax-salvage-" + uuid.uuid4().hex)
        root.mkdir()
        # run_provider_child is mocked, so the fold that run_bounded_child applies is applied here.
        child = fleet.minimax_fold_child_stdout({"exit_code": EXIT, "timed_out": False,
                 "stdout": stream(rejected(), fenced(handoff())), "stderr": "",
                 "model_tool_calls": None, "tool_budget_exceeded": False})
        result = self.run_single(child, root)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["handoff"], handoff())
        self.assertEqual(result["model_identity"]["status"], "verified")
        self.assertEqual(result["completion_reason"], "minimax-structured-output-recovered")
        self.assertEqual(result["transport_outcome"],
                         {"status": "complete", "reason": "minimax-structured-output-recovered"})
        self.assertIsNone(result["handoff_parse_error"])
        self.assertEqual(result["exit_code"], EXIT)
        attempt = result["provider_attempts"][0]
        self.assertTrue(attempt["handoff_projection_source"].startswith(
            "minimax.stream.agent_message.recovered-after-native-rejection"))
        self.assertEqual(attempt["minimax_stream"]["folded"], True)
        self.assertEqual(attempt["minimax_stream"]["final_message_chars"], len(fenced(handoff())))
        self.assertEqual(attempt["transport_outcome"], result["transport_outcome"])
        self.assertEqual(attempt["token_usage"]["output_tokens"], 5)
        run_dir = Path(attempt["stdout_path"]).parent
        self.assertEqual((run_dir / "attempt-1.stdout.stream.jsonl").read_text(encoding="utf-8"),
                         child["stdout_stream"])
        self.assertTrue(Path(attempt["stdout_path"]).read_text(encoding="utf-8").startswith("{"))
        record = json.loads(Path(result["usage_record"]).read_text(encoding="utf-8"))
        self.assertEqual(record["run_status"], "COMPLETED")
        self.assertEqual(record["completion_reason"], "minimax-structured-output-recovered")
        self.assertEqual(record["handoff_projection_source"], attempt["handoff_projection_source"])

    def test_execute_single_prose_answer_stays_failed_and_keeps_the_answer(self):
        root = Path(os.environ["TEAM_TEST_ROOT"]) / ("minimax-salvage-" + uuid.uuid4().hex)
        root.mkdir()
        child = fleet.minimax_fold_child_stdout({"exit_code": EXIT, "timed_out": False,
                 "stdout": stream(rejected(), "Review finished. No findings."), "stderr": "",
                 "model_tool_calls": None, "tool_budget_exceeded": False})
        result = self.run_single(child, root)
        self.assertEqual(result["status"], "FAILED")
        self.assertIsNone(result["handoff"])
        self.assertEqual(result["completion_reason"], "minimax-structured-output-rejected")
        self.assertEqual(result["transport_outcome"],
                         {"status": "failed", "reason": "minimax-structured-output-rejected"})
        self.assertIn("MINIMAX STRUCTURED OUTPUT REJECTED", result["handoff_parse_error"])
        self.assertNotIn("SCHEMA RETURNED INSTEAD OF DATA", result["handoff_parse_error"])
        attempt = result["provider_attempts"][0]
        self.assertEqual(attempt["minimax_stream"]["has_result"], True)
        self.assertIn("Review finished.", (Path(attempt["stdout_path"]).parent
                                           / "attempt-1.stdout.stream.jsonl").read_text(encoding="utf-8"))

    def test_execute_single_empty_final_message_does_not_resurrect_an_earlier_handoff(self):
        root = Path(os.environ["TEAM_TEST_ROOT"]) / ("minimax-salvage-" + uuid.uuid4().hex)
        root.mkdir()
        raw = stream(rejected(), fenced(handoff())).replace(
            event(8, "turn.failed", status="failed", durationMs=1),
            event(8, "item.completed", item={"id": "i2:message", "type": "agent_message",
                                             "content": ""}) + "\n"
            + event(8, "turn.failed", status="failed", durationMs=1))
        child = fleet.minimax_fold_child_stdout({"exit_code": EXIT, "timed_out": False, "stdout": raw,
                 "stderr": "", "model_tool_calls": None, "tool_budget_exceeded": False})
        self.assertIsNone(child["minimax_final_message"])
        result = self.run_single(child, root)
        self.assertEqual(result["status"], "FAILED")
        self.assertIsNone(result["handoff"])
        self.assertEqual(result["completion_reason"], "minimax-structured-output-rejected")
        self.assertIn("MINIMAX STRUCTURED OUTPUT REJECTED", result["handoff_parse_error"])


if __name__ == "__main__":
    unittest.main()
