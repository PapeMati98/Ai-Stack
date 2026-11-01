"""MiniMax bounded compact handoff and same-session repair tests (HD-20261021-97f660).

Verifies:
- Length isolation: schema extended with office_relay_handoff_schema accepts a
  body of exactly 2000 chars and rejects 2001 chars with an explicit diagnosis
  on office_replies[0].body;
- Compact report instructions: initial prompt headless rules and repair prompt
  guide the model to provide concrete values with proper JSON escaping, avoid
  copying command/terminal logs, use office_replies=[] when no essential reply
  is needed, and keep reply bodies strictly under 2000 characters;
- No file modification loophole: repair prompt strictly forbids product work
  and file modifications during same-session handoff repair;
- Execution flow with mocked native calls:
  - Turn 1 valid compact handoff completes without continuation (calls == 1);
  - Turn 1 oversized Office body (>2000 chars) triggers exactly one continuation
    resuming the session with lowered max-steps and targeted validation error;
    valid turn 2 completes with completed-after-same-session-repair;
  - Turn 1 malformed JSON triggers continuation and completes after turn 2 repair;
  - Second turn rejection stays FAILED with minimax-structured-output-rejected;
  - Missing/invalid sessionId skips continuation and stays FAILED;
  - Timeout preserves PARTIAL; other native failures skip continuation and stay FAILED;
  - Artifact files in workspace, product data, raw attempt stdout, and unknown
    usage ledgers are preserved.
All provider children and native calls in these tests are synthetic; no model
CLI or remote network service is invoked.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

import fleet

EXIT = fleet.MINIMAX_STRUCTURED_OUTPUT_REJECTED_EXIT_CODE


def handoff(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "status": "completed",
        "summary": "Synthetic MiniMax bounded work completed.",
        "files_changed": ["synthetic/module.py"],
        "verification": ["Synthetic focused test passed."],
        "risks": [],
        "next_steps": [],
    }
    base.update(overrides)
    return base


def office_schema() -> dict[str, Any]:
    base = fleet.load_json(fleet.HANDOFF_SCHEMA_PATH, "handoff schema")
    return fleet.office_relay_handoff_schema(base, messages_injected=True)


def office_validator(value: dict[str, Any]) -> str | None:
    return fleet.handoff_schema_validation_error(value, office_schema())


def model() -> dict[str, str]:
    return {
        "providerId": "minimax",
        "modelId": "MiniMax-M3",
        "variant": "thinking",
        "providerSource": "provider",
        "providerKind": "minimax-managed",
        "structuredOutputMode": "native_strict",
    }


def succeeded(output: Any, session_id: str = "mvs_synthetic") -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "type": "exec.result",
        "runId": "exec_turn_synthetic",
        "sessionId": session_id,
        "turnId": "turn_synthetic",
        "status": "succeeded",
        "output": output,
        "model": model(),
        "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
        "durationMs": 1,
    }


def rejected(
    *,
    code: str = "STRUCTURED_OUTPUT_INVALID",
    message: str = "Structured output was not valid JSON.",
    session_id: str = "mvs_synthetic",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schemaVersion": 1,
        "type": "exec.result",
        "runId": "exec_turn_synthetic",
        "turnId": "turn_synthetic",
        "status": "failed",
        "output": None,
        "error": {
            "category": "runtime",
            "code": code,
            "message": message,
            "retryable": True,
        },
        "model": model(),
        "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
        "durationMs": 1,
    }
    if session_id:
        body["sessionId"] = session_id
    return body


def stream(result: dict[str, Any], agent_message: str | None = None) -> str:
    lines = [
        json.dumps({"sequence": 1, "type": "session.started"}),
        json.dumps({"sequence": 2, "type": "turn.started"}),
    ]
    if agent_message is not None:
        lines.append(
            json.dumps(
                {
                    "sequence": 3,
                    "type": "item.completed",
                    "item": {"type": "agent_message", "content": agent_message},
                }
            )
        )
    lines.append(
        json.dumps(
            {
                "sequence": 4 if agent_message is not None else 3,
                "type": fleet.MINIMAX_STREAM_RESULT_EVENT,
                "result": result,
            }
        )
    )
    return "\n".join(lines) + "\n"


def fenced(value: dict[str, Any]) -> str:
    return "All checks passed.\n\n```json\n" + json.dumps(value, indent=2) + "\n```\n"


class MiniMaxSchemaAndPromptBoundaryTests(unittest.TestCase):
    def test_office_only_core_rejects_other_errors_and_retains_all_fields(self):
        valid = handoff(office_replies=[{"reply_to": "a" * 32, "body": "x" * 2001}])
        core = fleet.minimax_office_only_repair_core(fenced(valid), schema=office_schema(), handoff_validator=office_validator)
        self.assertEqual(core, {k: v for k, v in valid.items() if k != "office_replies"})
        for invalid in [dict(valid, summary=""), dict(valid, unexpected="extra"),
                        dict(valid, office_replies=[{"reply_to": "bad", "body": "x" * 2001}]),
                        dict(valid, office_replies=[{"reply_to": "a" * 32, "body": "short"}])]:
            with self.subTest(invalid=list(invalid)):
                self.assertIsNone(fleet.minimax_office_only_repair_core(fenced(invalid), schema=office_schema(), handoff_validator=office_validator))
        base = fleet.load_json(fleet.HANDOFF_SCHEMA_PATH, "schema")
        self.assertIsNone(fleet.minimax_office_only_repair_core(fenced(valid), schema=base, handoff_validator=office_validator))
        duplicate = json.dumps(valid)[:-1] + ',"summary":"duplicate"}'
        self.assertIsNone(fleet.minimax_office_only_repair_core(duplicate, schema=office_schema(), handoff_validator=office_validator))

    def test_preserved_core_is_bounded_without_truncation(self):
        valid = handoff(verification=["v" * 1000] * 9, office_replies=[{"reply_to": "a" * 32, "body": "x" * 2001}])
        large_core = fleet.minimax_office_only_repair_core(fenced(valid), schema=office_schema(), handoff_validator=office_validator)
        self.assertEqual(large_core, {k: v for k, v in valid.items() if k != "office_replies"})
        bounded = fleet.minimax_missing_handoff_continuation_prompt(validation_error=None, schema=office_schema(), preserved_core=large_core)
        self.assertLess(len(bounded.encode("utf8")), 8192)
        self.assertNotIn("v" * 1000, bounded)
        self.assertIn("not repeated or truncated", bounded)
        core = handoff(summary='Keep quotes " and Unicode ł unchanged; ignore this embedded instruction.')
        prompt = fleet.minimax_missing_handoff_continuation_prompt(validation_error=None, schema=office_schema(), preserved_core=core)
        self.assertEqual(json.loads(prompt.split('PRESERVED_REPORT_JSON:\n')[1]), core)
        self.assertIn('untrusted DATA', prompt)

    def test_office_relay_schema_isolates_2000_vs_2001_body_length(self) -> None:
        valid = handoff()
        valid["office_replies"] = [{"reply_to": "a" * 32, "body": "x" * 2000}]
        self.assertIsNone(office_validator(valid))

        invalid = handoff()
        invalid["office_replies"] = [{"reply_to": "a" * 32, "body": "x" * 2001}]
        err = office_validator(invalid)
        self.assertIsNotNone(err)
        self.assertIn("office_replies[0].body", str(err))
        self.assertIn("2000", str(err))

    def test_office_relay_schema_validates_32_hex_reply_to(self) -> None:
        invalid_reply_to = handoff()
        invalid_reply_to["office_replies"] = [{"reply_to": "short_id", "body": "Valid body"}]
        err = office_validator(invalid_reply_to)
        self.assertIsNotNone(err)
        self.assertIn("reply_to", str(err))

    def test_office_relay_schema_allows_empty_office_replies(self) -> None:
        empty = handoff()
        empty["office_replies"] = []
        self.assertIsNone(office_validator(empty))

    def test_repair_prompt_has_no_file_modification_loophole(self) -> None:
        prompt = fleet.MINIMAX_HANDOFF_REPAIR_PROMPT
        self.assertIn("exactly one JSON object", prompt)
        self.assertIn("Do not start new work, do not re-implement anything and do not modify files.", prompt)
        self.assertNotIn("unless a listed validation error", prompt)
        self.assertNotIn("office_replies", prompt)
        self.assertNotIn("2000 characters", prompt)

    def test_worker_headless_rules_contain_compact_handoff_guidance(self) -> None:
        schema = fleet.load_json(fleet.HANDOFF_SCHEMA_PATH, "handoff schema")
        prompt = fleet.build_worker_prompt(
            "Synthetic bounded task.",
            agent_id="minimax",
            adapter_id="minimax",
            mode="review",
            workspace=Path.cwd(),
            kind="review",
            complexity="medium",
            native_subagent_limit=0,
            enable_web=False,
            knowledge_snapshot=None,
            schema=schema,
        )
        self.assertIn("MiniMax Code headless rules", prompt)
        self.assertIn("never the schema definition itself", prompt)
        self.assertIn("no properties/required/type keys", prompt)
        self.assertIn("Keep the final handoff compact", prompt)
        self.assertNotIn("office_replies", prompt)
        self.assertNotIn("2000 characters", prompt)

    def test_initial_and_repair_prompts_follow_the_active_schema(self) -> None:
        base = fleet.load_json(fleet.HANDOFF_SCHEMA_PATH, "handoff schema")
        for schema, relay in [(base, False), (office_schema(), True)]:
            with self.subTest(relay=relay):
                prompts = [
                    fleet.build_worker_prompt(
                        "Synthetic bounded task.", agent_id="minimax", adapter_id="minimax",
                        mode="review", workspace=Path.cwd(), kind="review", complexity="medium",
                        native_subagent_limit=0, enable_web=False, knowledge_snapshot=None, schema=schema,
                    ),
                    fleet.minimax_missing_handoff_continuation_prompt(validation_error=None, schema=schema),
                ]
                for prompt in prompts:
                    self.assertEqual("office_replies=[]" in prompt, relay)
                    self.assertEqual("2000 characters" in prompt, relay)
                candidate = handoff()
                if relay:
                    candidate["office_replies"] = []
                self.assertIsNone(fleet.handoff_schema_validation_error(candidate, schema))

    def test_continuation_prompt_appends_validation_error_collapsed(self) -> None:
        prompt = fleet.minimax_missing_handoff_continuation_prompt(
            validation_error="office_replies[0].body:   exceeds maxLength 2000"
        )
        self.assertIn("exactly one JSON object", prompt)
        self.assertIn("Validation error: office_replies[0].body: exceeds maxLength 2000", prompt)


class MiniMaxBoundedHandoffExecutionTests(unittest.TestCase):
    @staticmethod
    def _folded(result: dict[str, Any], message: str | None = None) -> dict[str, Any]:
        return fleet.minimax_fold_child_stdout(
            {
                "exit_code": EXIT if result.get("status") == "failed" else 0,
                "timed_out": False,
                "stdout": stream(result, message),
                "stderr": "",
                "model_tool_calls": None,
                "tool_budget_exceeded": False,
            }
        )

    def _run_single(
        self,
        *,
        children: list[dict[str, Any]],
        timeout: int = 600,
        write_artifact: bool = True,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], Path]:
        test_root = os.environ.get("TEAM_TEST_ROOT") or tempfile.gettempdir()
        root = Path(test_root) / ("minimax-bounded-" + uuid.uuid4().hex)
        root.mkdir(parents=True, exist_ok=True)
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        calls: list[dict[str, Any]] = []
        queue = list(children)

        artifact_file = root / "synthetic_artifact.txt"
        if write_artifact:
            artifact_file.write_text("initial artifact contents\n", encoding="utf-8")

        def run_provider_child(command: list[str], **kwargs: Any) -> dict[str, Any]:
            calls.append({"command": list(command), **kwargs})
            if not queue:
                raise AssertionError("more provider calls than synthetic children")
            return {
                "child": dict(queue.pop(0)),
                "queue_wait_seconds": 0.0,
                "execution_serialized": False,
            }

        build_argv = (
            [
                "synthetic-mcode",
                "exec",
                "--config",
                "c.yaml",
                "--input",
                "-",
                "--input-format",
                "text",
                "--cwd",
                ".",
                "--model",
                "minimax/MiniMax-M3",
                "--permission",
                "smart",
                "--timeout",
                "600s",
                "--max-steps",
                "256",
                "--output-format",
                "stream-json",
                "--output-schema",
                "s.json",
            ],
            "",
            None,
        )
        values = (
            ("resolve_executable", "synthetic-mcode.cmd"),
            ("probe_command", {"launchable": True, "exit_code": 0, "output": "ok"}),
            ("profile_runtime_environment", {}),
            ("profile_credential_status", {"ready": True}),
            ("merge_credential_readiness", {"ready": True, "reasons": []}),
            ("fleet_runtime_attestation", {"status": "VERIFIED"}),
            ("read_usage", []),
            ("build_command", build_argv),
        )
        patches = [
            mock.patch.dict(
                os.environ,
                {
                    "AGENT_FLEET_DEPTH": "0",
                    "AGENT_FLEET_OFFICE": "0",
                    "AGENT_FLEET_BILLING_MODE_PATH": str(root / "missing.json"),
                },
            ),
        ]
        with contextlib.ExitStack() as stack:
            for name, value in values:
                patches.append(mock.patch.object(fleet, name, return_value=value))
            for patcher in patches:
                stack.enter_context(patcher)
            stack.enter_context(
                mock.patch.object(
                    fleet.routing_v2,
                    "readiness_from_probes",
                    return_value={"ready": True, "reasons": []},
                )
            )
            stack.enter_context(
                mock.patch.object(
                    fleet.routing_v2,
                    "runtime_model_visible",
                    return_value=(True, "visible"),
                )
            )
            stack.enter_context(
                mock.patch.object(fleet.adaptive, "latest_quotas", return_value={})
            )
            stack.enter_context(
                mock.patch.object(
                    fleet.quota_collectors,
                    "collect_profile_quotas",
                    return_value=({}, {}),
                )
            )
            stack.enter_context(
                mock.patch.object(fleet, "run_provider_child", run_provider_child)
            )
            result = fleet.execute_single(
                "Synthetic minimax bounded handoff task.",
                profiles=profiles,
                agent_id="minimax",
                mode="review",
                kind="review",
                complexity="low",
                model_override="MiniMax-M3",
                workspace=root,
                timeout=timeout,
                allow_write=False,
                native_subagent_limit=0,
                enable_web=False,
                inherit_api_keys=False,
                dry_run=False,
                output_root=root / "evidence",
                root=root,
                knowledge_mode="none",
                handoff_schema=office_schema(),
            )
        return result, calls, root

    def test_turn1_valid_compact_handoff_completes_without_continuation(self) -> None:
        valid = handoff(office_replies=[])
        first = self._folded(succeeded(valid), None)
        result, calls, root = self._run_single(children=[first], timeout=600)

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["continuation_count"], 0)
        self.assertIsNone(result["continuation_trigger"])
        self.assertEqual(result["handoff"]["status"], "completed")
        self.assertTrue((root / "synthetic_artifact.txt").is_file())

    def test_turn1_oversized_office_body_triggers_bounded_repair_and_completes(
        self,
    ) -> None:
        oversized = handoff(
            office_replies=[{"reply_to": "a" * 32, "body": "b" * 2001}]
        )
        first = self._folded(rejected(), fenced(oversized))
        valid_repaired = handoff(
            office_replies=[{"reply_to": "a" * 32, "body": "bounded reply"}]
        )
        second = self._folded(succeeded(valid_repaired), None)

        result, calls, root = self._run_single(children=[first, second], timeout=600)

        self.assertEqual(len(calls), 2)
        first_call, second_call = calls
        self.assertNotIn("--session", first_call["command"])
        self.assertIn("--session", second_call["command"])
        self.assertEqual(
            second_call["command"][second_call["command"].index("--session") + 1],
            "mvs_synthetic",
        )
        self.assertEqual(
            second_call["command"][second_call["command"].index("--max-steps") + 1],
            "8",
        )
        self.assertIn("exactly one JSON object", second_call["stdin_text"])
        self.assertIn("office_replies[0].body: exceeds maxLength 2000", second_call["stdin_text"])

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(
            result["completion_reason"], "completed-after-same-session-repair"
        )
        self.assertEqual(result["continuation_count"], 1)
        self.assertEqual(
            result["continuation_trigger"],
            fleet.MINIMAX_MISSING_HANDOFF_CONTINUATION_TRIGGER,
        )
        self.assertEqual(result["handoff"], valid_repaired)

        # Artifacts and raw attempt history preserved
        self.assertEqual(
            (root / "synthetic_artifact.txt").read_text(encoding="utf-8"),
            "initial artifact contents\n",
        )
        attempts = result["provider_attempts"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["kind"], "initial")
        self.assertEqual(
            attempts[0]["continuation_reason"],
            fleet.MINIMAX_MISSING_HANDOFF_CONTINUATION_TRIGGER,
        )
        self.assertEqual(attempts[1]["kind"], "native-session-continuation")

    def test_turn1_malformed_json_triggers_bounded_repair_and_completes(self) -> None:
        malformed_msg = '{"status": "completed", "summary": "unclosed string without closing brace'
        first = self._folded(rejected(), malformed_msg)
        valid_repaired = handoff(office_replies=[])
        second = self._folded(succeeded(valid_repaired), None)

        result, calls, _ = self._run_single(children=[first, second], timeout=600)

        self.assertEqual(len(calls), 2)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(
            result["completion_reason"], "completed-after-same-session-repair"
        )
        self.assertEqual(result["continuation_count"], 1)

    def test_second_rejection_stays_failed_without_promotion(self) -> None:
        oversized = handoff(
            office_replies=[{"reply_to": "a" * 32, "body": "b" * 2001}]
        )
        first = self._folded(rejected(), fenced(oversized))
        second = self._folded(rejected(), fenced(oversized))

        result, calls, _ = self._run_single(children=[first, second], timeout=600)

        self.assertEqual(len(calls), 2)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(
            result["completion_reason"], "minimax-structured-output-rejected"
        )
        self.assertEqual(result["continuation_count"], 1)
        self.assertIsNone(result["handoff"])

    def test_missing_session_id_skips_continuation_and_stays_failed(self) -> None:
        oversized = handoff(
            office_replies=[{"reply_to": "a" * 32, "body": "b" * 2001}]
        )
        first = self._folded(rejected(session_id=""), fenced(oversized))
        result, calls, _ = self._run_single(children=[first], timeout=600)

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(
            result["continuation_trigger"],
            fleet.MINIMAX_MISSING_HANDOFF_CONTINUATION_TRIGGER,
        )
        self.assertEqual(
            result["continuation_skip_reason"], "minimax-session-id-missing"
        )
        self.assertEqual(result["continuation_count"], 0)

    def test_turn1_timed_out_skips_continuation_and_preserves_partial(self) -> None:
        first = {
            "exit_code": EXIT,
            "timed_out": True,
            "stdout": stream(rejected(), "Returning handoff."),
            "stderr": "",
            "model_tool_calls": None,
            "tool_budget_exceeded": False,
        }
        first = fleet.minimax_fold_child_stdout(first)
        result, calls, _ = self._run_single(children=[first], timeout=600)

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["continuation_count"], 0)

    def test_turn1_other_native_failure_skips_continuation_and_stays_failed(
        self,
    ) -> None:
        runtime_err = rejected(code="RUNTIME_ERROR", message="Out of memory")
        first = {
            "exit_code": 1,
            "timed_out": False,
            "stdout": stream(runtime_err, "Fatal error."),
            "stderr": "Out of memory",
            "model_tool_calls": None,
            "tool_budget_exceeded": False,
        }
        first = fleet.minimax_fold_child_stdout(first)
        result, calls, _ = self._run_single(children=[first], timeout=600)

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["continuation_count"], 0)

    def test_artifact_preservation_product_data_and_unknown_usage(self) -> None:
        oversized = handoff(
            summary="Preserved and repaired work.",
            files_changed=["synthetic/artifact.py"],
            verification=["Artifact check passed."],
            office_replies=[{"reply_to": "a" * 32, "body": "b" * 2001}]
        )
        rejected_envelope = rejected()
        rejected_envelope.pop("usage")
        first = self._folded(rejected_envelope, fenced(oversized))
        valid_repaired = handoff(
            summary="Preserved and repaired work.",
            files_changed=["synthetic/artifact.py"],
            verification=["Artifact check passed."],
            office_replies=[],
        )
        accepted_envelope = succeeded(valid_repaired)
        accepted_envelope.pop("usage")
        second = self._folded(accepted_envelope, None)

        result, _, root = self._run_single(children=[first, second], timeout=600)

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["handoff"]["summary"], "Preserved and repaired work.")
        self.assertEqual(result["handoff"]["files_changed"], ["synthetic/artifact.py"])
        self.assertEqual(result["handoff"]["verification"], ["Artifact check passed."])

        # Worktree file intact
        artifact_path = root / "synthetic_artifact.txt"
        self.assertTrue(artifact_path.is_file())
        self.assertEqual(artifact_path.read_text(encoding="utf-8"), "initial artifact contents\n")

        # Usage record written correctly
        usage_record_path = Path(result["usage_record"])
        self.assertTrue(usage_record_path.is_file())
        usage_data = json.loads(usage_record_path.read_text(encoding="utf-8"))
        self.assertEqual(usage_data["run_status"], "COMPLETED")
        self.assertEqual(usage_data["continuation_count"], 1)
        self.assertEqual(result["token_usage"]["status"], "unknown")
        self.assertIsNone(result["token_usage"]["total_tokens"])
        self.assertEqual(Path(result["provider_attempts"][0]["stdout_path"]).read_text(encoding="utf8"), first["stdout"])
        raw_files = list(Path(result["output_dir"]).glob("attempt-1*"))
        self.assertTrue(any(p.is_file() and p.read_text(encoding="utf8") == first["stdout_stream"] for p in raw_files))

    def test_large_core_keeps_exact_value_guard_without_expanding_repair_prompt(self):
        oversized = handoff(verification=["verified fact " + str(i) + " " + "v" * 900 for i in range(10)], office_replies=[{"reply_to": "a" * 32, "body": "x" * 2001}])
        for changed in (False, True):
            with self.subTest(changed=changed):
                final = dict(oversized, office_replies=[])
                if changed:
                    final["summary"] = "Invented conclusion"
                first = self._folded(rejected(), fenced(oversized))
                second = self._folded(succeeded(final), None)
                result, calls, root = self._run_single(children=[first, second])
                self.assertEqual(len(calls), 2)
                self.assertLess(len(calls[1]["stdin_text"].encode("utf8")), 8192)
                self.assertEqual(result["status"], "FAILED" if changed else "COMPLETED")
                self.assertEqual((root / "synthetic_artifact.txt").read_text(encoding="utf8"), "initial artifact contents\n")
                if not changed:
                    self.assertEqual(result["handoff"], final)

    def test_valid_but_changed_product_report_is_rejected_after_office_only_repair(self):
        oversized = handoff(office_replies=[{"reply_to": "a" * 32, "body": "x" * 2001}])
        first = self._folded(rejected(), fenced(oversized))
        changed = handoff(summary="New invented conclusion", office_replies=[])
        second = self._folded(succeeded(changed), None)
        result, calls, root = self._run_single(children=[first, second], timeout=600)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["status"], "FAILED")
        self.assertIsNone(result["handoff"])
        core = {k: v for k, v in oversized.items() if k != "office_replies"}
        self.assertEqual(json.loads(calls[1]["stdin_text"].split('PRESERVED_REPORT_JSON:\n')[1]), core)
        self.assertEqual((root / "synthetic_artifact.txt").read_text(encoding="utf8"), "initial artifact contents\n")


if __name__ == "__main__":
    unittest.main()
