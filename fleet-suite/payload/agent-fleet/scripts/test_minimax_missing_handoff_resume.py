"""MiniMax structured-output-missing-handoff same-session resume (HD-20261015-afe374).

Measured 2026-10-15 with mcode 0.2.4 in the stack's synthetic directory: when the
native structured-output validator refuses a final answer (prose, a schema echo
or an object the run validator rejects) MCode ends the run with exit
``MINIMAX_STRUCTURED_OUTPUT_REJECTED_EXIT_CODE`` (4) and the stream's final
``agent_message`` carries the same prose the validator refused. Fleet now
resumes the same MCode session exactly once with ``--session <sessionId>`` and
a lowered ``--max-steps`` budget, asking the model to return only one JSON
object matching the required schema; every other native outcome keeps its
existing verdict.
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


EXIT = fleet.MINIMAX_STRUCTURED_OUTPUT_REJECTED_EXIT_CODE


def handoff() -> dict[str, object]:
    return {
        "status": "completed",
        "summary": "Synthetic MiniMax missing-handoff resume.",
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
             code: str = "STRUCTURED_OUTPUT_INVALID",
             session_id: str = "mvs_synthetic") -> dict[str, object]:
    body = {"schemaVersion": 1, "type": "exec.result", "runId": "exec_turn_synthetic",
            "sessionId": session_id, "turnId": "turn_synthetic", "status": "failed",
            "error": {"category": "runtime", "code": code, "message": message, "retryable": True},
            "model": model(),
            "usage": {"inputTokens": 10, "outputTokens": 5, "reasoningTokens": 0,
                      "cacheReadTokens": 3, "cacheWriteTokens": 0, "totalTokens": 15},
            "durationMs": 1}
    return body


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


def office_schema() -> dict[str, object]:
    base = fleet.load_json(fleet.HANDOFF_SCHEMA_PATH, "handoff schema")
    return fleet.office_relay_handoff_schema(base, messages_injected=True)


def office_validator(value: dict[str, object]) -> str | None:
    return fleet.handoff_schema_validation_error(value, office_schema())


class SessionIdParserTests(unittest.TestCase):
    def test_session_id_is_returned_for_a_folded_rejected_envelope(self):
        stdout = fleet.minimax_fold_child_stdout(
            {"stdout": stream(rejected(), "Returning the handoff now."), "stderr": ""}
        )["stdout"]
        self.assertEqual(fleet.minimax_exec_session_id(stdout), "mvs_synthetic")

    def test_session_id_is_none_for_non_strings_and_prose(self):
        for case in (None, "  ", "{not json}", "Returning the handoff now.",
                     json.dumps({"not": "an envelope"})):
            with self.subTest(case=case if isinstance(case, str) else type(case).__name__):
                self.assertIsNone(fleet.minimax_exec_session_id(case))

    def test_session_id_is_none_when_envelope_lacks_a_valid_id(self):
        no_id = rejected(); del no_id["sessionId"]
        self.assertIsNone(fleet.minimax_exec_session_id(json.dumps(no_id)))
        wrong_type = rejected(); wrong_type["sessionId"] = 7
        self.assertIsNone(fleet.minimax_exec_session_id(json.dumps(wrong_type)))
        bad_id = rejected(); bad_id["sessionId"] = "bad id!"
        self.assertIsNone(fleet.minimax_exec_session_id(json.dumps(bad_id)))


class MissingHandoffDecisionTests(unittest.TestCase):
    def _folded(self, result, message):
        return fleet.minimax_fold_child_stdout(
            {"exit_code": EXIT, "timed_out": False,
             "stdout": stream(result, message), "stderr": "",
             "model_tool_calls": None, "tool_budget_exceeded": False})

    def test_prose_only_final_triggers_a_resume_with_session_id(self):
        first = self._folded(rejected(), "Returning the handoff now.")
        decision = fleet.minimax_missing_handoff_continuation_decision(
            first, remaining_seconds=600, handoff_validator=validator)
        self.assertEqual(decision["trigger"], fleet.MINIMAX_MISSING_HANDOFF_CONTINUATION_TRIGGER)
        self.assertEqual(decision["session_id"], "mvs_synthetic")
        self.assertIsNone(decision["skip_reason"])
        self.assertIsNone(decision["projection"]["handoff"])

    def test_short_remaining_window_skips_with_continuation_reason(self):
        first = self._folded(rejected(), "Returning the handoff now.")
        decision = fleet.minimax_missing_handoff_continuation_decision(
            first, remaining_seconds=30, handoff_validator=validator)
        self.assertEqual(decision["trigger"], fleet.MINIMAX_MISSING_HANDOFF_CONTINUATION_TRIGGER)
        self.assertEqual(decision["session_id"], "mvs_synthetic")
        self.assertEqual(decision["skip_reason"], "insufficient-provider-time-for-continuation")

    def test_missing_session_id_skips_with_id_reason(self):
        first = self._folded(rejected(session_id=""), "Returning the handoff now.")
        decision = fleet.minimax_missing_handoff_continuation_decision(
            first, remaining_seconds=600, handoff_validator=validator)
        self.assertEqual(decision["trigger"], fleet.MINIMAX_MISSING_HANDOFF_CONTINUATION_TRIGGER)
        self.assertEqual(decision["skip_reason"], "minimax-session-id-missing")

    def test_recoverable_fenced_answer_does_not_trigger(self):
        first = self._folded(rejected(), fenced(handoff()))
        decision = fleet.minimax_missing_handoff_continuation_decision(
            first, remaining_seconds=600, handoff_validator=validator)
        self.assertIsNone(decision["trigger"])
        self.assertIsNone(decision["skip_reason"])

    def test_timed_out_and_non_rejected_exits_do_not_trigger(self):
        for kwargs, label in (
            ({"timed_out": True}, "timed-out"),
            ({"exit_code": 0}, "exit-0"),
            ({"exit_code": True}, "exit-bool"),
        ):
            base = {"timed_out": False, "exit_code": EXIT,
                    "stdout": stream(rejected(), "Returning the handoff now."), "stderr": "",
                    "model_tool_calls": None, "tool_budget_exceeded": False}
            base.update(kwargs)
            child = fleet.minimax_fold_child_stdout(base)
            with self.subTest(case=label):
                decision = fleet.minimax_missing_handoff_continuation_decision(
                    child, remaining_seconds=600, handoff_validator=validator)
                self.assertIsNone(decision["trigger"])
                self.assertIsNone(decision["skip_reason"])

    def test_other_native_failures_do_not_trigger(self):
        for label, result in (
            ("runtime-error", rejected(code="RUNTIME_ERROR")),
            ("limit-exceeded", limit_exceeded()),
        ):
            child = fleet.minimax_fold_child_stdout(
                {"exit_code": 7 if result.get("status") == "limit_exceeded" else EXIT,
                 "timed_out": False,
                 "stdout": stream(result, fenced(handoff())), "stderr": "",
                 "model_tool_calls": None, "tool_budget_exceeded": False})
            with self.subTest(case=label):
                decision = fleet.minimax_missing_handoff_continuation_decision(
                    child, remaining_seconds=600, handoff_validator=validator)
                self.assertIsNone(decision["trigger"])
                self.assertIsNone(decision["skip_reason"])

    def test_contract_breaking_final_triggers_a_resume(self):
        valid_body = handoff()
        valid_body["office_replies"] = [{"reply_to": "a" * 32, "body": "b" * 2000}]
        self.assertIsNone(office_validator(valid_body))

        invalid = handoff()
        invalid["office_replies"] = [{"reply_to": "a" * 32, "body": "b" * 2001}]
        body_error = office_validator(invalid)
        self.assertIsNotNone(body_error)
        self.assertIn("body", body_error)
        self.assertIn("2000", body_error)
        child = fleet.minimax_fold_child_stdout(
            {"exit_code": EXIT, "timed_out": False,
             "stdout": stream(rejected(), fenced(invalid)), "stderr": "",
             "model_tool_calls": None, "tool_budget_exceeded": False})
        decision = fleet.minimax_missing_handoff_continuation_decision(
            child, remaining_seconds=600, handoff_validator=office_validator)
        self.assertEqual(decision["trigger"], fleet.MINIMAX_MISSING_HANDOFF_CONTINUATION_TRIGGER)
        self.assertEqual(decision["session_id"], "mvs_synthetic")
        self.assertIsNone(decision["skip_reason"])
        self.assertIsNone(decision["projection"]["handoff"])


class ContinuationCommandTests(unittest.TestCase):
    def _argv(self) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            command, _, _ = fleet.build_command(
                "minimax", "mcode.cmd", prompt="Synthetic bounded task.",
                workspace=Path.cwd(), mode="implement", model="minimax/MiniMax-M3",
                complexity="medium", timeout=300, run_dir=Path(tmp),
                native_subagent_limit=0, enable_web=False, materialize=True)
        return command

    def test_session_pair_lands_before_output_format_and_lowers_step_cap(self):
        argv = self._argv()
        snapshot = list(argv)
        resumed = fleet.minimax_continuation_command(
            argv, session_id="mvs_synthetic", timeout_seconds=77, max_steps=8)
        self.assertEqual(resumed[resumed.index("--session") + 1], "mvs_synthetic")
        self.assertLess(resumed.index("--session"), resumed.index("--output-format"))
        self.assertEqual(resumed[resumed.index("--timeout") + 1], "77s")
        self.assertEqual(resumed[resumed.index("--max-steps") + 1], "8")
        for flag in ("--config", "--cwd", "--model", "--permission", "--output-schema"):
            self.assertIn(flag, resumed)
            self.assertEqual(resumed[resumed.index(flag) + 1], snapshot[snapshot.index(flag) + 1])
        self.assertEqual(snapshot, argv, "the original argv must be left intact")

    def test_continue_flag_raises_fleet_error(self):
        argv = self._argv() + ["--continue"]
        with self.assertRaisesRegex(fleet.FleetError, "must not carry --continue"):
            fleet.minimax_continuation_command(
                argv, session_id="mvs_synthetic", timeout_seconds=60, max_steps=4)

    def test_minimal_argv_appends_the_three_flags(self):
        resumed = fleet.minimax_continuation_command(
            ["x"], session_id="mvs_synthetic", timeout_seconds=10, max_steps=3)
        self.assertEqual(resumed[resumed.index("--session") + 1], "mvs_synthetic")
        self.assertEqual(resumed[resumed.index("--timeout") + 1], "10s")
        self.assertEqual(resumed[resumed.index("--max-steps") + 1], "3")


class ContinuationPromptTests(unittest.TestCase):
    def test_base_prompt_asks_for_exactly_one_handoff_object(self):
        prompt = fleet.minimax_missing_handoff_continuation_prompt(validation_error=None)
        self.assertIn("exactly one JSON object", prompt)
        self.assertNotIn("Validation error", prompt)

    def test_validation_error_is_appended_whitespace_collapsed(self):
        prompt = fleet.minimax_missing_handoff_continuation_prompt(
            validation_error="office_replies[0].body: exceeds maxLength 2000")
        self.assertIn("exactly one JSON object", prompt)
        self.assertIn("Validation error: office_replies[0].body:", prompt)


class ExecuteSingleMissingHandoffResumeTests(unittest.TestCase):
    @staticmethod
    def _folded(result, message):
        return fleet.minimax_fold_child_stdout(
            {"exit_code": EXIT if result.get("status") == "failed" else 0,
             "timed_out": False, "stdout": stream(result, message), "stderr": "",
             "model_tool_calls": None, "tool_budget_exceeded": False})

    def _run_single(self, *, children, timeout=600):
        # Retained fixture, normal inherited permissions; no tempfile cleanup.
        root = Path(os.environ["TEAM_TEST_ROOT"]) / ("minimax-resume-" + uuid.uuid4().hex)
        root.mkdir()
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        calls: list[dict[str, object]] = []
        queue = list(children)

        def run_provider_child(command, **kwargs):
            calls.append({"command": list(command), **kwargs})
            if not queue:
                raise AssertionError("more provider calls than synthetic children")
            return {
                "child": dict(queue.pop(0)),
                "queue_wait_seconds": 0.0,
                "execution_serialized": False,
            }

        build_argv = (
            ["synthetic-mcode", "exec", "--config", "c.yaml", "--input", "-",
             "--input-format", "text", "--cwd", ".", "--model", "minimax/MiniMax-M3",
             "--permission", "smart", "--timeout", "600s", "--max-steps", "256",
             "--output-format", "stream-json", "--output-schema", "s.json"],
            "", None,
        )
        values = (("resolve_executable", "synthetic-mcode.cmd"),
                  ("probe_command", {"launchable": True, "exit_code": 0, "output": "ok"}),
                  ("profile_runtime_environment", {}),
                  ("profile_credential_status", {"ready": True}),
                  ("merge_credential_readiness", {"ready": True, "reasons": []}),
                  ("fleet_runtime_attestation", {"status": "VERIFIED"}),
                  ("read_usage", []),
                  ("build_command", build_argv))
        patches = [
            mock.patch.dict(os.environ, {"AGENT_FLEET_DEPTH": "0",
                "AGENT_FLEET_OFFICE": "0",
                "AGENT_FLEET_BILLING_MODE_PATH": str(root / "missing.json")}),
        ]
        with contextlib.ExitStack() as stack:
            for name, value in values:
                patches.append(mock.patch.object(fleet, name, return_value=value))
            for patcher in patches:
                stack.enter_context(patcher)
            stack.enter_context(mock.patch.object(fleet.routing_v2, "readiness_from_probes",
                                                  return_value={"ready": True, "reasons": []}))
            stack.enter_context(mock.patch.object(fleet.routing_v2, "runtime_model_visible",
                                                  return_value=(True, "visible")))
            stack.enter_context(mock.patch.object(fleet.adaptive, "latest_quotas",
                                                  return_value={}))
            stack.enter_context(mock.patch.object(fleet.quota_collectors,
                                                  "collect_profile_quotas",
                                                  return_value=({}, {})))
            stack.enter_context(mock.patch.object(fleet, "run_provider_child", run_provider_child))
            result = fleet.execute_single(
                "Synthetic minimax missing-handoff fixture.", profiles=profiles,
                agent_id="minimax", mode="review", kind="review",
                complexity="low", model_override="MiniMax-M3", workspace=root,
                timeout=timeout, allow_write=False, native_subagent_limit=0,
                enable_web=False, inherit_api_keys=False, dry_run=False,
                output_root=root / "evidence", root=root, knowledge_mode="none")
        return result, calls, root

    def test_one_same_session_resume_collects_the_handoff(self):
        first = self._folded(rejected(), "Returning the handoff now.")
        second = self._folded(succeeded(handoff()), None)
        result, calls, root = self._run_single(children=[first, second], timeout=600)

        self.assertEqual(len(calls), 2)
        first_call, second_call = calls
        self.assertNotIn("--session", first_call["command"])
        self.assertEqual(second_call["command"][second_call["command"].index("--session") + 1],
                         "mvs_synthetic")
        self.assertEqual(second_call["command"][second_call["command"].index("--max-steps") + 1],
                         "8")
        self.assertIn("exactly one JSON object", second_call["stdin_text"])
        self.assertNotIn("--continue", second_call["command"])

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["handoff"], handoff())
        self.assertEqual(result["completion_reason"], "completed-after-same-session-repair")
        self.assertEqual(result["continuation_count"], 1)
        self.assertEqual(result["continuation_trigger"],
                         fleet.MINIMAX_MISSING_HANDOFF_CONTINUATION_TRIGGER)
        self.assertIsNone(result["continuation_skip_reason"])
        self.assertEqual(result["provider_session_id"], "mvs_synthetic")
        attempts = result["provider_attempts"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["kind"], "initial")
        self.assertEqual(attempts[0]["continuation_reason"],
                         fleet.MINIMAX_MISSING_HANDOFF_CONTINUATION_TRIGGER)
        self.assertEqual(attempts[1]["kind"], "native-session-continuation")
        self.assertEqual(attempts[1]["handoff_status"], "completed")
        self.assertIsNone(attempts[1]["handoff_error"])
        record = json.loads(Path(result["usage_record"]).read_text(encoding="utf-8"))
        self.assertEqual(record["run_status"], "COMPLETED")
        self.assertEqual(record["continuation_count"], 1)

    def test_second_rejection_stays_failed_with_structured_output_reason(self):
        first = self._folded(rejected(), "Returning the handoff now.")
        second = self._folded(rejected(), "Still prose.")
        result, calls, _ = self._run_single(children=[first, second], timeout=600)

        self.assertEqual(len(calls), 2)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["completion_reason"], "minimax-structured-output-rejected")
        self.assertEqual(result["continuation_count"], 1)
        self.assertEqual(result["continuation_trigger"],
                         fleet.MINIMAX_MISSING_HANDOFF_CONTINUATION_TRIGGER)

    def test_short_budget_skips_the_resume_with_continuation_skip_reason(self):
        first = self._folded(rejected(), "Returning the handoff now.")
        result, calls, _ = self._run_single(children=[first], timeout=5)

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["continuation_trigger"],
                         fleet.MINIMAX_MISSING_HANDOFF_CONTINUATION_TRIGGER)
        self.assertEqual(result["continuation_skip_reason"],
                         "insufficient-provider-time-for-continuation")
        self.assertEqual(result["continuation_count"], 0)


if __name__ == "__main__":
    unittest.main()
