"""Synthetic regressions for Antigravity headless permission denials.

RSI Helpdesk HD-20261013-f2a859: agy in print mode cannot answer a permission
prompt, so a native file tool aimed outside the --add-dir workspace is
auto-denied and the turn ends with an empty response, exit 0 and status
SUCCESS. Fleet names the denied target in the diagnosis and resumes the same
native conversation exactly once to collect the handoff of the work already
done. No real agy process, model, credential or owner state is used here.
"""
import contextlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import fleet


MODEL = "gemini-3.8-flash-high"
OUTSIDE = "D:\\other-project\\brief.txt"
CONVERSATION = "11111111-2222-3333-4444-555555555555"


def sample_handoff(status: str = "completed") -> dict[str, object]:
    return {
        "status": status,
        "summary": "Synthetic Antigravity handoff after the resumed turn.",
        "files_changed": [],
        "verification": ["Synthetic resume parsed."],
        "risks": [] if status == "completed" else [OUTSIDE],
        "next_steps": [] if status == "completed" else [f"Parent supplies the content of {OUTSIDE} inside the workspace."],
    }


def init_event(conversation_id: str = CONVERSATION, model: str = MODEL) -> str:
    return json.dumps(
        {
            "event": "init",
            "conversation_id": conversation_id,
            "init": {"model": model, "cwd": "D:\\work\\ws", "permission_mode": "accept-edits"},
        }
    )


def denied_step(path: str = OUTSIDE, tool: str = "view_file", message: str | None = None) -> str:
    error_message = message or (
        f'permission check failed for read_file "{path}": '
        f"user denied permission for read_file({path})"
    )
    return json.dumps(
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": CONVERSATION,
                "step_index": 3,
                "state": "ERROR",
                "step_type": "tool",
                "tool_name": tool,
                "tool_info": {
                    "name": tool,
                    "parameters": {"AbsolutePath": path},
                    "error": {"type": "TOOL_ERROR", "message": error_message},
                },
            },
        }
    )


def result_event(**result: object) -> str:
    payload = {"conversation_id": CONVERSATION, "status": "SUCCESS", "json_schema": {"type": "object"}}
    payload.update(result)
    return json.dumps({"event": "result", "result": payload})


def denied_stream(
    conversation_id: str | None = CONVERSATION,
    *,
    with_step: bool = True,
    model: str = MODEL,
    response: object = "",
    usage: dict[str, int] | None = None,
) -> str:
    lines = []
    if conversation_id is not None:
        lines.append(init_event(conversation_id, model))
    else:
        lines.append(json.dumps({"event": "init", "init": {"model": model}}))
    if with_step:
        lines.append(denied_step())
    terminal: dict[str, object] = {
        "response": response,
        "denied_actions": [{"action": "read_file", "display_name": "ViewFile"}],
    }
    if usage is not None:
        terminal["usage"] = usage
    if conversation_id is None:
        terminal["conversation_id"] = None
    lines.append(result_event(**terminal))
    return "\n".join(lines)


def success_stream(
    handoff: dict[str, object] | None = None,
    *,
    model: str = MODEL,
    usage: dict[str, int] | None = None,
) -> str:
    handoff = handoff or sample_handoff()
    terminal: dict[str, object] = {"response": json.dumps(handoff), "structured_output": handoff}
    if usage is not None:
        terminal["usage"] = usage
    return "\n".join([init_event(model=model), result_event(**terminal)])


def empty_without_denial_stream() -> str:
    return "\n".join([init_event(), result_event(response="")])


def child(stdout: str, *, exit_code: int = 0, timed_out: bool = False) -> dict[str, object]:
    return {
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": "",
        "timed_out": timed_out,
        "model_tool_calls": None,
        "tool_budget_exceeded": False,
    }


class AntigravityDeniedReadProjectionTests(unittest.TestCase):
    def test_projection_names_the_denied_target(self) -> None:
        projected = fleet.project_provider_handoff("antigravity", denied_stream())

        self.assertIsNone(projected["handoff"])
        self.assertEqual(projected["reason"], "EMPTY_RESPONSE")
        self.assertEqual(projected["denied_actions"], ["read_file (ViewFile)"])
        self.assertEqual(projected["denied_paths"], [f"view_file({OUTSIDE})"])
        self.assertIn("EMPTY_RESPONSE", projected["error"])
        self.assertIn(f"view_file({OUTSIDE})", projected["error"])
        self.assertIn("auto-denied the permission prompt", projected["error"])
        self.assertIn("outside the --add-dir workspace", projected["error"])
        self.assertEqual(projected["validation_error"], projected["error"])

    def test_projection_without_step_detail_keeps_action_only_diagnosis(self) -> None:
        projected = fleet.project_provider_handoff(
            "antigravity", denied_stream(with_step=False)
        )
        self.assertEqual(projected["reason"], "EMPTY_RESPONSE")
        self.assertEqual(projected["denied_paths"], [])
        self.assertIn("read_file (ViewFile)", projected["error"])
        self.assertNotIn("auto-denied the permission prompt", projected["error"])

    def test_valid_handoff_is_untouched_by_denial_bookkeeping(self) -> None:
        handoff = sample_handoff()
        projected = fleet.project_provider_handoff("antigravity", success_stream(handoff))
        self.assertEqual(projected["handoff"], handoff)
        self.assertIsNone(projected["error"])
        self.assertEqual(projected["denied_paths"], [])

    def test_conversation_id_and_denied_paths_parsers(self) -> None:
        self.assertEqual(fleet.antigravity_conversation_id(denied_stream()), CONVERSATION)
        self.assertEqual(
            fleet.antigravity_conversation_id(
                "\n".join([json.dumps({"event": "init", "init": {"model": MODEL}}), result_event()])
            ),
            CONVERSATION,
        )
        self.assertIsNone(fleet.antigravity_conversation_id("not json\n{}"))
        self.assertIsNone(fleet.antigravity_conversation_id(denied_stream(None)))

        stream = "\n".join(
            [
                init_event(),
                denied_step(),
                denied_step(),
                denied_step("D:\\other-project\\second.txt", tool="list_dir"),
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "state": "ERROR",
                            "step_type": "tool",
                            "tool_name": "view_file",
                            "tool_info": {
                                "name": "view_file",
                                "parameters": {"AbsolutePath": "D:\\work\\ws\\missing.txt"},
                                "error": {"type": "TOOL_ERROR", "message": "file not found"},
                            },
                        },
                    }
                ),
                result_event(response=""),
            ]
        )
        self.assertEqual(
            fleet.antigravity_denied_tool_paths(stream),
            [f"view_file({OUTSIDE})", "list_dir(D:\\other-project\\second.txt)"],
        )
        many = "\n".join(
            denied_step(f"D:\\other-project\\{index}.txt") for index in range(9)
        )
        self.assertEqual(
            len(fleet.antigravity_denied_tool_paths(many)),
            fleet.ANTIGRAVITY_DENIED_PATH_LIMIT,
        )

    def test_decision_triggers_only_on_denied_empty_success(self) -> None:
        decision = fleet.antigravity_denied_read_continuation_decision(
            child(denied_stream()), remaining_seconds=600
        )
        self.assertEqual(decision["trigger"], fleet.ANTIGRAVITY_DENIED_READ_CONTINUATION_TRIGGER)
        self.assertIsNone(decision["skip_reason"])
        self.assertEqual(decision["conversation_id"], CONVERSATION)
        self.assertEqual(decision["denied_paths"], [f"view_file({OUTSIDE})"])

        short = fleet.antigravity_denied_read_continuation_decision(
            child(denied_stream()), remaining_seconds=fleet.ANTIGRAVITY_CONTINUATION_MIN_SECONDS - 1
        )
        self.assertEqual(short["trigger"], fleet.ANTIGRAVITY_DENIED_READ_CONTINUATION_TRIGGER)
        self.assertEqual(short["skip_reason"], "insufficient-provider-time-for-continuation")

        no_id = fleet.antigravity_denied_read_continuation_decision(
            child(denied_stream(None)), remaining_seconds=600
        )
        self.assertEqual(no_id["trigger"], fleet.ANTIGRAVITY_DENIED_READ_CONTINUATION_TRIGGER)
        self.assertEqual(no_id["skip_reason"], "antigravity-conversation-id-missing")

        for label, record in (
            ("exit-1", child(denied_stream(), exit_code=1)),
            ("timed-out", child(denied_stream(), timed_out=True)),
            ("valid-handoff", child(success_stream())),
            ("empty-without-denial", child(empty_without_denial_stream())),
            ("no-terminal", child("not json")),
        ):
            with self.subTest(case=label):
                decision = fleet.antigravity_denied_read_continuation_decision(
                    record, remaining_seconds=600
                )
                self.assertIsNone(decision["trigger"])
                self.assertIsNone(decision["skip_reason"])

    def test_continuation_command_resumes_without_widening_permissions(self) -> None:
        command, initial_stdin, _ = fleet.build_command(
            "antigravity",
            "agy.exe",
            prompt="Original task prompt.",
            workspace=Path("D:/work/ws"),
            mode="implement",
            model=MODEL,
            complexity="medium",
            timeout=3600,
            run_dir=Path("D:/work/run"),
            native_subagent_limit=0,
            enable_web=False,
            materialize=False,
        )
        resumed = fleet.antigravity_continuation_command(
            command, conversation_id=CONVERSATION, prompt="Resume prompt.", timeout_seconds=842
        )
        self.assertEqual(resumed[resumed.index("--conversation") + 1], CONVERSATION)
        self.assertLess(resumed.index("--conversation"), resumed.index("--print"))
        self.assertEqual(resumed[resumed.index("--print") + 1], "")
        self.assertEqual(resumed[resumed.index("--input-format") + 1], "stream-json")
        self.assertEqual(resumed[resumed.index("--print-timeout") + 1], "842s")
        self.assertEqual(resumed[resumed.index("--mode") + 1], "accept-edits")
        self.assertEqual(resumed[resumed.index("--add-dir") + 1], str(Path("D:/work/ws")))
        self.assertIn("--sandbox", resumed)
        self.assertIn("--json-schema", resumed)
        self.assertNotIn("--dangerously-skip-permissions", resumed)
        self.assertEqual(command[command.index("--print") + 1], "")
        self.assertEqual(json.loads(initial_stdin)["message"]["content"], "Original task prompt.")
        self.assertNotIn("--conversation", command)
        self.assertEqual(resumed.count("--conversation"), 1)
        twice = fleet.antigravity_continuation_command(
            resumed, conversation_id="other", prompt="again", timeout_seconds=0
        )
        self.assertEqual(twice.count("--conversation"), 1)
        self.assertEqual(twice[twice.index("--conversation") + 1], "other")
        self.assertEqual(twice[twice.index("--print-timeout") + 1], "1s")
        with self.assertRaisesRegex(fleet.FleetError, "dangerously-skip-permissions"):
            fleet.antigravity_continuation_command(
                command + ["--dangerously-skip-permissions"],
                conversation_id=CONVERSATION,
                prompt="x",
                timeout_seconds=10,
            )

    def test_continuation_prompt_names_targets_and_forbids_retry(self) -> None:
        prompt = fleet.antigravity_denied_read_continuation_prompt(
            workspace="D:\\work\\ws",
            denied_paths=[f"view_file({OUTSIDE})"],
            denied_actions=["read_file (ViewFile)"],
        )
        self.assertIn(f"view_file({OUTSIDE})", prompt)
        self.assertIn("D:\\work\\ws", prompt)
        self.assertIn("Do not retry", prompt)
        self.assertIn("status partial", prompt)
        fallback = fleet.antigravity_denied_read_continuation_prompt(
            workspace="D:\\work\\ws", denied_paths=[], denied_actions=["read_file (ViewFile)"]
        )
        self.assertIn("read_file (ViewFile)", fallback)

    def test_worker_prompt_warns_about_outside_workspace_reads(self) -> None:
        prompt = fleet.build_worker_prompt(
            "Review one file.",
            agent_id="antigravity",
            adapter_id="antigravity",
            mode="review",
            workspace=Path("C:/work/project"),
            kind="review",
            complexity="medium",
            native_subagent_limit=0,
            enable_web=False,
            knowledge_snapshot=None,
            schema={"type": "object"},
        )
        self.assertIn("auto-denied by agy", prompt)
        self.assertIn("Never target a path outside the workspace", prompt)


class AntigravityDeniedReadExecuteSingleTests(unittest.TestCase):
    def run_single(self, children: list[dict[str, object]], *, timeout: int = 900) -> tuple[dict, list[dict]]:
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

        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
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
                    (fleet, "resolve_executable", "synthetic-agy.exe"),
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
                )
            )
            with contextlib.ExitStack() as stack:
                for patcher in patches:
                    stack.enter_context(patcher)
                stack.enter_context(
                    mock.patch.object(fleet, "run_provider_child", run_provider_child)
                )
                result = fleet.execute_single(
                    "Synthetic Antigravity denied-read fixture.",
                    profiles=profiles,
                    agent_id="antigravity",
                    mode="review",
                    kind="review",
                    complexity="low",
                    model_override=None,
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
                )
        self.assertEqual(queue, [], "every synthetic child must be consumed")
        return result, calls

    def test_one_same_conversation_resume_collects_the_handoff(self) -> None:
        handoff = sample_handoff()
        result, calls = self.run_single([child(denied_stream()), child(success_stream(handoff))])

        self.assertEqual(len(calls), 2)
        first, second = calls
        self.assertNotIn("--conversation", first["command"])
        self.assertEqual(second["command"][second["command"].index("--conversation") + 1], CONVERSATION)
        self.assertEqual(second["command"][second["command"].index("--print") + 1], "")
        resume_prompt = json.loads(second["stdin_text"])["message"]["content"]
        self.assertNotEqual(first["stdin_text"], second["stdin_text"])
        self.assertIn(f"view_file({OUTSIDE})", resume_prompt)
        self.assertIn("Do not retry", resume_prompt)
        self.assertEqual(second["command"][second["command"].index("--mode") + 1], "plan")
        self.assertIn("--sandbox", second["command"])
        self.assertNotIn("--dangerously-skip-permissions", second["command"])
        self.assertEqual(second["checkpoint_path"].name, "attempt-2.process-checkpoint.json")
        self.assertLessEqual(second["timeout"], first["timeout"])
        self.assertGreaterEqual(second["timeout"], fleet.ANTIGRAVITY_CONTINUATION_MIN_SECONDS)

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["handoff"], handoff)
        self.assertEqual(result["model_identity"]["status"], "verified")
        self.assertEqual(result["continuation_trigger"], fleet.ANTIGRAVITY_DENIED_READ_CONTINUATION_TRIGGER)
        self.assertEqual(result["continuation_count"], 1)
        self.assertIsNone(result["continuation_skip_reason"])
        self.assertEqual(result["completion_reason"], "completed-after-same-session-repair")
        self.assertEqual(result["provider_session_id"], CONVERSATION)
        attempts = result["provider_attempts"]
        self.assertEqual([attempt["kind"] for attempt in attempts], ["initial", "native-session-continuation"])
        self.assertEqual(attempts[0]["continuation_reason"], fleet.ANTIGRAVITY_DENIED_READ_CONTINUATION_TRIGGER)
        self.assertIn(f"view_file({OUTSIDE})", attempts[0]["handoff_error"])
        self.assertEqual(attempts[1]["handoff_status"], "completed")
        self.assertIsNone(attempts[1]["handoff_error"])
        self.assertTrue(Path(attempts[0]["stdout_path"]).name.endswith("attempt-1.stdout.txt"))
        self.assertTrue(Path(attempts[1]["stdout_path"]).name.endswith("attempt-2.stdout.txt"))

    def test_partial_handoff_after_resume_stays_partial(self) -> None:
        handoff = sample_handoff("partial")
        result, calls = self.run_single([child(denied_stream()), child(success_stream(handoff))])
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["handoff"], handoff)
        self.assertEqual(result["completion_reason"], "partial")
        self.assertEqual(result["continuation_count"], 1)

    def test_second_denial_fails_with_the_named_target_and_no_third_call(self) -> None:
        result, calls = self.run_single([child(denied_stream()), child(denied_stream())])
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["status"], "FAILED")
        self.assertIsNone(result["handoff"])
        self.assertEqual(
            result["completion_reason"], "antigravity-permission-denied-after-same-session-repair"
        )
        self.assertIn(f"view_file({OUTSIDE})", result["handoff_parse_error"])
        self.assertIn("EMPTY_RESPONSE", result["handoff_parse_error"])
        self.assertEqual(result["transport_outcome"], {"status": "complete", "reason": "process-exit-0"})
        self.assertEqual(result["continuation_count"], 1)

    def test_empty_response_without_denial_is_not_resumed(self) -> None:
        result, calls = self.run_single([child(empty_without_denial_stream())])
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertIsNone(result["continuation_trigger"])
        self.assertEqual(result["continuation_count"], 0)
        self.assertEqual(result["completion_reason"], "process-exit-0")
        self.assertIn("EMPTY_RESPONSE", result["handoff_parse_error"])

    def test_short_budget_skips_the_resume_but_keeps_the_diagnosis(self) -> None:
        result, calls = self.run_single(
            [child(denied_stream())], timeout=fleet.ANTIGRAVITY_CONTINUATION_MIN_SECONDS - 10
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["continuation_trigger"], fleet.ANTIGRAVITY_DENIED_READ_CONTINUATION_TRIGGER)
        self.assertEqual(result["continuation_skip_reason"], "insufficient-provider-time-for-continuation")
        self.assertEqual(result["continuation_count"], 0)
        self.assertEqual(result["completion_reason"], "antigravity-permission-denied")
        self.assertEqual(result["provider_session_id"], CONVERSATION)
        self.assertIn(f"view_file({OUTSIDE})", result["handoff_parse_error"])

    def test_provider_exit_failure_is_not_resumed(self) -> None:
        result, calls = self.run_single([child(denied_stream(), exit_code=1)])
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertIsNone(result["continuation_trigger"])
        self.assertEqual(result["completion_reason"], "process-exit-1")

    def test_first_attempt_identity_mismatch_blocks_completed(self) -> None:
        result, calls = self.run_single(
            [child(denied_stream(model="gemini-other-model")), child(success_stream())]
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["model_identity"]["status"], "mismatch")
        self.assertEqual(
            result["model_identity"]["observed_models"], sorted([MODEL, "gemini-other-model"])
        )
        self.assertIn("exact-model gate failed", result["handoff_parse_error"])

    def test_second_attempt_identity_mismatch_blocks_completed(self) -> None:
        result, calls = self.run_single(
            [child(denied_stream()), child(success_stream(model="gemini-other-model"))]
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["model_identity"]["status"], "mismatch")

    def test_missing_init_in_either_attempt_blocks_completed(self) -> None:
        no_init_denied = "\n".join(
            [
                denied_step(),
                result_event(
                    response="",
                    denied_actions=[{"action": "read_file", "display_name": "ViewFile"}],
                ),
            ]
        )
        handoff = sample_handoff()
        no_init_success = result_event(response=json.dumps(handoff), structured_output=handoff)
        no_model_success = "\n".join(
            [
                json.dumps({"event": "init", "conversation_id": CONVERSATION, "init": {"cwd": "D:\\work\\ws"}}),
                result_event(response=json.dumps(handoff), structured_output=handoff),
            ]
        )
        cases = (
            ("first-without-init", [child(no_init_denied), child(success_stream(handoff))]),
            ("second-without-init", [child(denied_stream()), child(no_init_success)]),
            ("second-without-model", [child(denied_stream()), child(no_model_success)]),
        )
        for label, children in cases:
            with self.subTest(case=label):
                result, calls = self.run_single(children)
                self.assertEqual(len(calls), 2)
                self.assertEqual(result["status"], "FAILED")
                self.assertEqual(result["model_identity"]["status"], "unavailable")
                self.assertEqual(result["model_identity"]["observed_models"], [MODEL])
                self.assertIn("exact-model gate failed", result["handoff_parse_error"])
                self.assertIn("attempt(s)", result["model_identity"]["error"])
        identity = fleet.antigravity_attempts_model_identity(
            [denied_stream(model="gemini-other-model"), no_init_success], expected_model=MODEL
        )
        self.assertEqual(identity["status"], "mismatch")
        self.assertEqual(identity["attempt_statuses"], ["mismatch", "unavailable"])
        self.assertEqual(identity["observed_models"], ["gemini-other-model"])
        verified = fleet.antigravity_attempts_model_identity(
            [denied_stream(), success_stream()], expected_model=MODEL
        )
        self.assertEqual(verified["status"], "verified")
        self.assertIsNone(verified["error"])

    def test_run_token_usage_is_the_cumulative_resumed_result(self) -> None:
        first = {"input_tokens": 4571, "output_tokens": 1104, "cache_read_tokens": 24489, "total_tokens": 5675}
        second = {"input_tokens": 9675, "output_tokens": 2406, "cache_read_tokens": 81600, "total_tokens": 12081}
        result, calls = self.run_single(
            [child(denied_stream(usage=first)), child(success_stream(usage=second))]
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["token_usage"]["total_tokens"], 12081)
        self.assertEqual(result["token_usage"]["input_tokens"], 9675)
        self.assertEqual(result["token_usage"]["output_tokens"], 2406)
        attempts = result["provider_attempts"]
        self.assertEqual(attempts[0]["token_usage"]["total_tokens"], 5675)
        self.assertEqual(attempts[1]["token_usage"]["total_tokens"], 12081)

    def test_non_empty_list_response_is_not_resumed(self) -> None:
        result, calls = self.run_single([child(denied_stream(response=["not empty"]))])
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertIsNone(result["continuation_trigger"])
        self.assertEqual(result["continuation_count"], 0)
        self.assertIn("EMPTY_RESPONSE", result["handoff_parse_error"])

    def test_resume_process_failure_keeps_the_exit_code_reason(self) -> None:
        result, calls = self.run_single([child(denied_stream()), child(denied_stream(), exit_code=1)])
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["completion_reason"], "process-exit-1")
        self.assertEqual(result["continuation_count"], 1)

    def test_denied_label_is_bounded_and_emptiness_is_strict(self) -> None:
        long_path = "D:\\other-project\\" + ("x" * 400) + ".txt"
        labels = fleet.antigravity_denied_tool_paths(denied_step(long_path))
        self.assertEqual(len(labels), 1)
        self.assertLessEqual(len(labels[0]), 240)
        self.assertTrue(labels[0].startswith("view_file(D:\\other-project\\"))
        self.assertTrue(fleet.antigravity_terminal_response_is_empty({"response": ""}))
        self.assertTrue(fleet.antigravity_terminal_response_is_empty({"response": None}))
        self.assertTrue(fleet.antigravity_terminal_response_is_empty({"status": "SUCCESS"}))
        self.assertFalse(fleet.antigravity_terminal_response_is_empty({"response": ["x"]}))
        self.assertFalse(fleet.antigravity_terminal_response_is_empty({"response": "text"}))
        self.assertFalse(fleet.antigravity_terminal_response_is_empty(None))


if __name__ == "__main__":
    unittest.main()
