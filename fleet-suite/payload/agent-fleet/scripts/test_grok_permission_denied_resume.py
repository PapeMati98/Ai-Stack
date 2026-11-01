"""Bounded one-shot resume after a Fleet permission refusal (RSI HD-20261013-bb5973).

The native envelope from grok_acp must report how many permission requests were
refused inside THIS prompt (cumulative alone is not enough to qualify the session
for a bounded same-session resume) so that fleet.grok_continuation_reason only
returns "permission-denied-retry" when a refusal actually caused the cancelled
turn, not for unrelated cancellations that happened to inherit the same
cumulative counter."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import fleet  # noqa: E402
import grok_acp  # noqa: E402


FAKE_SERVER = SCRIPT_DIR / "fake_grok_acp_server.py"
SCHEMA = {
    "type": "object",
    "required": [
        "status",
        "summary",
        "files_changed",
        "verification",
        "risks",
        "next_steps",
    ],
    "properties": {
        "status": {"type": "string"},
        "summary": {"type": "string"},
        "files_changed": {"type": "array"},
        "verification": {"type": "array"},
        "risks": {"type": "array"},
        "next_steps": {"type": "array"},
    },
}


def _make_request(*, options=("allow_once", "reject_once")):
    return {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "session/request_permission",
        "params": {
            "toolCall": {"kind": "execute", "title": "run_terminal_command", "rawInput": {"variant": "Bash", "command": "pwsh -NoLogo -NoProfile -Command Remove-Item -Recurse project"}},
            "options": [{"kind": k, "optionId": f"opt-{k}", "name": f"name-{k}"} for k in options],
        },
    }


def _envelope(*, denied_in_prompt=None, denied_total=1, triggers=None, include_fleet=True):
    fleet_block = None
    if include_fleet:
        fleet_block = {
            "permission_requests_denied": denied_total,
            "continuation_triggers": list(triggers) if triggers is not None else [],
        }
        if denied_in_prompt is not None:
            fleet_block["permission_requests_denied_in_prompt"] = denied_in_prompt
    body = {"stopReason": "cancelled", "sessionId": "11111111-1111-4111-8111-111111111111"}
    if fleet_block is not None:
        body["_fleet"] = fleet_block
    return json.dumps(body)


class AcpStateCounterTests(unittest.TestCase):
    """The per-prompt counter is born at zero, accumulates alongside the cumulative
    one only inside the same prompt, and is the only thing grok_continuation_reason
    is allowed to trust when deciding whether to resume."""

    def test_state_is_born_with_both_counters_at_zero(self) -> None:
        state = grok_acp._AcpState(max_tool_calls=4)
        self.assertEqual(state.permission_requests_denied, 0)
        self.assertEqual(state.prompt_permission_requests_denied, 0)

    def test_begin_prompt_resets_only_the_per_prompt_counter(self) -> None:
        state = grok_acp._AcpState(max_tool_calls=4)
        state.permission_requests_denied = 5
        state.prompt_permission_requests_denied = 3
        state.begin_prompt()
        self.assertEqual(state.permission_requests_denied, 5)
        self.assertEqual(state.prompt_permission_requests_denied, 0)

    def test_denial_via_reverse_request_increments_both_counters(self) -> None:
        sent: list[dict[str, object]] = []

        class Conn(grok_acp._AcpConnection):
            def __init__(self) -> None:  # bypass process start: only the reverse-request path is exercised
                self.deny_all_tools = False
                self.allow_writes = True
                self.workspace_root = Path(tempfile.mkdtemp()) / "project"
                self.workspace_root.mkdir()
                self.state = grok_acp._AcpState(max_tool_calls=4)

            def _send(self, message: dict[str, object]) -> None:
                sent.append(message)

        c = Conn()
        c.state.begin_prompt()
        c._handle_reverse_request(_make_request())
        self.assertEqual(c.state.permission_requests_denied, 1)
        self.assertEqual(c.state.prompt_permission_requests_denied, 1)
        self.assertEqual(sent[0]["result"]["outcome"]["outcome"], "cancelled")

    def test_normalize_envelope_exposes_per_prompt_counter(self) -> None:
        state = grok_acp._AcpState(max_tool_calls=4)
        state.permission_requests_denied = 7
        state.prompt_permission_requests_denied = 2
        envelope = grok_acp._normalize_envelope(
            result={"stopReason": "cancelled"},
            state=state,
            session_id="S",
            prompt_count=1,
            attempts=[],
            continuation_triggers=[],
            continuation_skip_reason=None,
        )
        self.assertEqual(envelope["_fleet"]["permission_requests_denied"], 7)
        self.assertEqual(envelope["_fleet"]["permission_requests_denied_in_prompt"], 2)


class GrokContinuationReasonTests(unittest.TestCase):
    """The new rule: cumulative count alone never qualifies; only this-prompt
    count > 0 and fewer than the resume budget does."""

    def test_cancelled_with_per_prompt_denial_returns_permission_denied_retry(self) -> None:
        text = _envelope(denied_in_prompt=1, denied_total=1, triggers=[])
        projection = fleet.project_provider_handoff("grok", text)
        self.assertEqual(fleet.grok_continuation_reason(text, projection), "permission-denied-retry")

    def test_retry_prompt_text_contains_do_not_retry_the_denied_request(self) -> None:
        prompt = fleet.grok_continuation_prompt(
            "permission-denied-retry", schema=SCHEMA, validation_error=None
        )
        self.assertIn("do not retry the denied request", prompt)

    def test_cancelled_with_only_cumulative_counter_returns_none(self) -> None:
        text = _envelope(denied_in_prompt=0, denied_total=2, triggers=[])
        projection = fleet.project_provider_handoff("grok", text)
        self.assertIsNone(fleet.grok_continuation_reason(text, projection))

    def test_cancelled_with_resume_budget_already_used_returns_none(self) -> None:
        text = _envelope(
            denied_in_prompt=1, denied_total=3, triggers=["permission-denied-retry", "permission-denied-retry"]
        )
        projection = fleet.project_provider_handoff("grok", text)
        self.assertIsNone(fleet.grok_continuation_reason(text, projection))

    def test_cancelled_with_per_prompt_bool_returns_none(self) -> None:
        text = _envelope(denied_in_prompt=True, denied_total=1, triggers=[])
        projection = fleet.project_provider_handoff("grok", text)
        self.assertIsNone(fleet.grok_continuation_reason(text, projection))

    def test_cancelled_without_fleet_metadata_returns_none(self) -> None:
        text = _envelope(include_fleet=False)
        projection = fleet.project_provider_handoff("grok", text)
        self.assertIsNone(fleet.grok_continuation_reason(text, projection))


class GrokProjectionTests(unittest.TestCase):
    """The diagnostic must name a refusal-caused cancellation instead of the
    pre-existing "native cancellation source is unknown" filler."""

    def test_per_prompt_counter_names_refusal_with_redacted_reason(self) -> None:
        text = json.dumps(
            {
                "stopReason": "cancelled",
                "sessionId": "11111111-1111-4111-8111-111111111111",
                "_fleet": {
                    "permission_requests_denied": 1,
                    "permission_requests_denied_in_prompt": 1,
                    "permission_denials": [{"reason": "shell_command_not_allowed", "variant": "Bash"}],
                },
            }
        )
        projection = fleet.project_provider_handoff("grok", text)
        detail = projection["no_candidate_detail"] or ""
        self.assertIn("refused 1 permission request", detail)
        self.assertIn("shell_command_not_allowed", detail)
        self.assertNotIn("source is unknown", detail)

    def test_missing_per_prompt_counter_keeps_unknown_source_text(self) -> None:
        text = json.dumps(
            {
                "stopReason": "cancelled",
                "sessionId": "11111111-1111-4111-8111-111111111111",
                "_fleet": {"permission_requests_denied": 2},
            }
        )
        projection = fleet.project_provider_handoff("grok", text)
        detail = projection["no_candidate_detail"] or ""
        self.assertIn("native cancellation source is unknown", detail)


class GrokEndToEndResumeTests(unittest.TestCase):
    """End-to-end: a refused permission in prompt 1 ends that turn cancelled,
    the bounded decider returns "permission-denied-retry", and prompt 2 finishes
    the task with a valid handoff. The control without max_additional_prompts
    confirms the budget guard still prevents infinite resume."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.run_id = "run-20260820T000000Z-abcdef1234"
        self.run_dir = self.root / self.run_id
        self.workspace = self.root / "workspace"
        self.proxy_base = self.root / "proxy"
        self.auth_path = self.root / "auth.json"
        self.run_dir.mkdir()
        self.workspace.mkdir()
        (self.workspace / "fixture.txt").write_text("bounded", encoding="utf-8")
        self.auth_path.write_text('{"token":"fixture-only"}', encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def command(self) -> list[str]:
        return grok_acp.build_command(
            sys.executable,
            run_dir=self.run_dir,
            run_id=self.run_id,
            mode="review",
            model="grok-4.7",
            reasoning_effort="medium",
            complexity="medium",
            handoff_schema=SCHEMA,
            max_model_tool_calls=8,
            proxy_base=self.proxy_base,
            executable_args=(str(FAKE_SERVER),),
        )

    def environment(self, scenario: str) -> dict[str, str]:
        base = dict(os.environ)
        base["GROK_API_KEY"] = "must-be-removed"
        base["XAI_API_KEY"] = "must-bealso-removed"
        base["FAKE_GROK_ACP_SCENARIO"] = scenario
        return grok_acp.build_environment(
            base, run_dir=self.run_dir, auth_path=self.auth_path
        )

    def decider(self) -> object:
        def decide(text: str):
            projection = fleet.project_provider_handoff("grok", text)
            if fleet.grok_continuation_reason(text, projection) == "permission-denied-retry":
                return "permission-denied-retry", fleet.GROK_PERMISSION_DENIED_RETRY_PROMPT
            return None

        return decide

    def run_with_budget(self, *, max_additional_prompts: int) -> dict[str, object]:
        return grok_acp.run_session(
            command=self.command(),
            run_dir=self.run_dir,
            run_id=self.run_id,
            workspace=self.workspace,
            mode="review",
            prompt="Inspect project/fixture.txt and return the handoff.",
            timeout=30,
            max_model_tool_calls=8,
            max_additional_prompts=max_additional_prompts,
            env=self.environment("permission-deny-then-complete"),
            continuation_decider=self.decider(),
            knowledge_snapshot=None,
            proxy_base=self.proxy_base,
        )

    def test_resume_after_refusal_completes_with_valid_handoff(self) -> None:
        result = self.run_with_budget(max_additional_prompts=1)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["prompt_count"], 2)
        self.assertEqual(result["continuation_triggers"], ["permission-denied-retry"])
        self.assertEqual(result["attempts"][0]["stop_reason"], "cancelled")
        self.assertEqual(result["attempts"][1]["stop_reason"], "end_turn")
        envelope = json.loads(str(result["stdout"]))
        self.assertEqual(envelope["stopReason"], "end_turn")
        self.assertEqual(envelope["_fleet"]["permission_requests_denied"], 1)
        self.assertEqual(envelope["_fleet"]["permission_requests_denied_in_prompt"], 0)
        self.assertIsNotNone(envelope["structuredOutput"])

    def test_control_without_additional_prompts_exhausts_budget(self) -> None:
        result = self.run_with_budget(max_additional_prompts=0)
        self.assertEqual(result["prompt_count"], 1)
        self.assertEqual(result["continuation_triggers"], ["permission-denied-retry"])
        self.assertEqual(result["continuation_skip_reason"], "same-session-prompt-budget-exhausted")
        envelope = json.loads(str(result["stdout"]))
        self.assertEqual(envelope["stopReason"], "cancelled")


if __name__ == "__main__":
    unittest.main()